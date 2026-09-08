"""The per-shot alignment and completeness diagnostics (2026-09-05).

Six of twenty-three delay-scan shots on the first hardware day had a
photocurrent ten times too large and a charge of the wrong sign, and every
one passed the rail check: the trigger had jittered for the length of the
shot, and the light and dark displacement spikes sat 0.3-1.2 ns apart where
a good shot aligns to 0.01 ns. `core.diagnostics` says so from the traces;
the service's verdict warns on it; the recorder files it; the engine fetches
the sync trace and the average count beside every acquisition.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pytest

from bace.core.axis import Axis, ScanSpec
from bace.core.diagnostics import edge_10_90_ns, spike_lag_ns, sync_edge_ns
from bace.drivers.protocols import Trace
from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig, run_transient_scan
from bace.service import wire as W
from bace.storage.recorder import RunRecorder, record

NO_SLEEP = lambda s: None  # noqa: E731
DT = 5e-10
N = 4000
T0 = -1.995e-7


def spike(shift_ns: float = 0.0, smear_ns: float = 0.0, photo: float = 0.0) -> np.ndarray:
    """A 50 mA displacement spike at 320 ns (the rig's), 6 ns edge, with an
    optional extra `smear_ns` of Gaussian jitter and a small photocurrent."""
    t = np.arange(N) * DT * 1e9 - shift_ns
    sigma = 6.0 / 1.68 + smear_ns            # a Gaussian's 10-90 % time is 1.68 sigma
    y = -0.05 * np.exp(-0.5 * ((t - 320.0) / sigma) ** 2)
    y -= photo * np.exp(-(t - 330.0).clip(0) / 70.0) * (t > 330.0)
    # the digitiser's noise, so a flat tail is not a rail of identical codes
    rng = np.random.default_rng(int(abs(shift_ns) * 1000 + smear_ns * 10 + photo * 1e6) + 1)
    return y + rng.normal(0, 2e-6, N)


def extraction(amp: float = 1.4e-2, tau: float = 150.0, t0: float = 322.0,
               rise: float = 3.0) -> np.ndarray:
    """The charge coming out, as the rig's traces carry it: a 14 mA transient
    that rises just after the 50 mA displacement spike's peak and decays over
    150 ns, so it loads the spike's *decay* and leaves its leading flank
    alone. That is the shape that moves the correlation peak while the spike
    itself stands still -- lag -0.56 ns with both edges still 6.0 ns, which is
    what the rig showed from 220 to 295 K."""
    t = np.arange(N) * DT * 1e9
    return -amp * (1 - np.exp(-(t - t0).clip(0) / rise)) * np.exp(-(t - t0).clip(0) / tau) * (t > t0)


def sync(shift_ns: float = 0.0) -> np.ndarray:
    """The trigger edge as this rig gives it: a 5.5 ns pulse at the trigger,
    1.19 V. It carries no photocurrent and no device, so it is offset only
    when the two acquisitions really are."""
    t = np.arange(N) * DT * 1e9 + T0 * 1e9 - shift_ns
    return 1.19 * np.exp(-(t / 2.8) ** 2)


def test_a_dark_spike_half_a_nanosecond_late_is_measured_as_such():
    lag = spike_lag_ns(spike(0.0, photo=3e-4), spike(0.5), DT)
    assert lag is not None and lag == pytest.approx(0.5, abs=0.06)
    assert spike_lag_ns(spike(0.0), spike(0.0), DT) == pytest.approx(0.0, abs=0.02)
    assert spike_lag_ns(spike(0.0), spike(-1.15), DT) == pytest.approx(-1.15, abs=0.06)


def test_traces_without_a_common_spike_give_no_lag_rather_than_a_number():
    noise = np.random.default_rng(2).normal(0, 1e-5, N)
    assert spike_lag_ns(spike(), noise, DT) is None, "a bare-noise dark has no spike to align"
    assert spike_lag_ns(np.zeros(N), np.zeros(N), DT) is None
    assert spike_lag_ns(spike(), spike(), 0.0) is None
    assert spike_lag_ns(spike()[:10], spike()[:10], DT) is None


def test_jitter_smears_the_edge_and_the_edge_is_read_in_ns():
    sharp = edge_10_90_ns(spike(), DT)
    smeared = edge_10_90_ns(spike(smear_ns=1.5), DT)
    assert sharp is not None and 5.0 <= sharp <= 7.5
    assert smeared is not None and smeared > sharp + 1.5
    assert edge_10_90_ns(np.zeros(N), DT) is None


def test_the_sync_edge_is_read_at_the_trigger_whichever_way_it_goes():
    t = np.arange(N) * DT + T0
    rising = 1.2 / (1 + np.exp(-t / (2e-9 / 4.4)))
    assert sync_edge_ns(rising, DT, T0) == pytest.approx(2.0, abs=0.6)
    assert sync_edge_ns(1.2 - rising, DT, T0) == pytest.approx(2.0, abs=0.6)
    assert sync_edge_ns(np.full(N, 0.01), DT, T0) is None, "a flat sync has no edge"


def test_a_lag_with_sharp_edges_is_the_extracted_charge_and_not_a_warning():
    """The correction of 2026-09-07, and the reason the rule needs two symptoms.

    The light trace is the displacement spike *plus* the charge coming out,
    and adding that charge moves the correlation peak while the spike itself
    stands still. On the rig a no-light acquisition with its spike untouched,
    plus the measured photocurrent, reproduced the observed lag to 0.01 ns at
    every temperature from 220 to 290 K, and shifting the dark trace by the
    lag cancelled only 4 % of the light-dark difference. The old rule called
    177 of 492 good shots void on this alone.

    Here the spikes are half a nanosecond apart with 6 ns edges and equal
    heights: nothing was smeared, so nothing jittered.
    """
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(0.0), DT, T0, count=200)   # not shifted by anything
    aligned = Trace(sync(0.0), DT, T0)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark, sync_light=aligned, sync_dark=aligned)
    assert abs(v["spike_lag_ns"]) > W.SPIKE_LAG_NS, "the lag is there"
    assert v["edge_light_ns"] == pytest.approx(6.0, abs=0.6)
    assert abs(v["sync_lag_ns"]) < W.SYNC_LAG_NS, "the trigger did not move"
    assert v["level"] == "ok", "a lag the sync says is not timing, with a sharp edge"
    assert "charge, not jitter" in v["text"], "and the line says so, with the number"
    assert "ns apart" in v["text"]


def test_a_lag_with_no_sync_to_check_it_against_is_not_called_charge():
    """Without sync traces nothing can say whether the acquisitions were
    offset, so the line reports the lag and says what it could not check --
    it must not claim the lag was explained."""
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(0.0), DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark)
    assert v["sync_lag_ns"] is None and v["level"] == "ok"
    assert "no sync to check the timing" in v["text"]
    assert "charge, not jitter" not in v["text"]


def test_spikes_of_different_height_are_not_by_themselves_jitter():
    """The incident lowered the spikes by 4-14 % as well as smearing them, but
    a height difference is not the acquisition's alone to explain: under the
    default `dark_reference = "translated"` the two traces repeat one swing
    over different absolute voltage ranges, so their capacitive terms agree
    only where C(V) is flat. On the rig the two spikes drift from 0.4 % apart
    at 220 K to 1.1 % at 295 K and are still climbing -- and they do it in the
    shots that also carry the charge-induced lag, which is exactly the pair
    this rule must not call void."""
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(0.0) * 0.90, DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark)
    assert abs(v["spike_lag_ns"]) > W.SPIKE_LAG_NS and v["edge_light_ns"] < W.SPIKE_EDGE_NS
    assert v["level"] == "ok", "10 % apart in height, but nothing was smeared and nothing slid"
    assert v["peak_light_a"] != v["peak_dark_a"], "the heights are still on the verdict"


def test_a_real_offset_between_the_two_acquisitions_is_a_warning_however_sharp():
    """The case a lag-and-edges rule would wave through: the light and dark
    acquisitions genuinely offset in time -- a fixed timing difference, or too
    few averages to smooth one out -- shifts a sharp spike without smearing
    it. The engine subtracts sample for sample, so that leaves the 50 mA spike
    in the photocurrent and Q is not a charge.

    Sliding the dark trace back is what tells the two apart: it cancels 97 %
    of the difference here and 0.1 % when the lag is the extracted charge."""
    light = Trace(spike(0.0), DT, T0, count=200)
    dark = Trace(spike(0.5), DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark,
                       sync_light=Trace(sync(0.0), DT, T0),
                       sync_dark=Trace(sync(0.5), DT, T0))
    assert abs(v["spike_lag_ns"]) > W.SPIKE_LAG_NS
    assert v["edge_light_ns"] == pytest.approx(6.0, abs=0.6), "nothing is smeared"
    assert v["edge_dark_ns"] == pytest.approx(6.0, abs=0.6)
    assert abs(v["sync_lag_ns"]) == pytest.approx(0.5, abs=0.1)
    assert v["level"] == "warn", "sharp edges, but the traces really are offset"
    assert "offset in time" in v["text"] and "not a charge" in v["text"]


def test_an_offset_the_charge_hides_from_the_spike_lag_is_still_caught():
    """Worse than masking the residual: the charge's lag and a real offset move
    the spike correlation in *opposite* directions, so half a nanosecond of
    offset reads as a 0.08 ns spike lag -- under the threshold, with the two
    acquisitions genuinely a sample apart. The sync test cannot be gated
    behind the spike lag or this walks through."""
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(0.5), DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark,
                       sync_light=Trace(sync(0.0), DT, T0),
                       sync_dark=Trace(sync(0.5), DT, T0))
    assert abs(v["spike_lag_ns"]) < W.SPIKE_LAG_NS, "the spike lag hides it"
    assert abs(v["sync_lag_ns"]) == pytest.approx(0.5, abs=0.1), "the sync does not"
    assert v["level"] == "warn" and "offset in time" in v["text"]


def test_a_sync_of_noise_or_glitches_gives_no_lag_at_all():
    """A trace of digitiser noise has a peak-to-peak above zero and correlates
    against another one to an arbitrary number -- -1.54 ns for these two --
    and two single-sample glitches gave 1.5 ns. Either would be read as a
    verdict: over the threshold it voids a good shot, under it the `ok` line
    calls a spike lag "charge, not jitter" on a number that means nothing."""
    from bace.core.diagnostics import sync_lag_ns

    rng = np.random.default_rng(7)
    noise_a, noise_b = rng.normal(0, 2e-3, N), rng.normal(0, 2e-3, N)
    assert sync_lag_ns(noise_a, noise_b, DT, T0) is None
    glitch_a, glitch_b = np.full(N, 0.01), np.full(N, 0.01)
    glitch_a[int(round(-T0 / DT))] = 1.2
    glitch_b[int(round(-T0 / DT)) + 3] = 1.2
    assert sync_lag_ns(glitch_a, glitch_b, DT, T0) is None
    # and a real pair still reads
    assert sync_lag_ns(sync(0.0), sync(1.0), DT, T0) == pytest.approx(1.0, abs=0.15)

    # the verdict must then say the timing was not checked, not that it was
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(0.0), DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark,
                       sync_light=Trace(noise_a, DT, T0), sync_dark=Trace(noise_b, DT, T0))
    assert v["sync_lag_ns"] is None
    assert "no sync to check the timing" in v["text"]
    assert "charge, not jitter" not in v["text"]


def test_a_photocurrent_cannot_hide_a_real_offset_from_the_sync():
    """The case that defeats every measure taken from the light and dark
    traces alone: a 14 mA extraction current on top of a genuine 1 ns offset.
    Its own contribution to the difference swamps the shift's, so residual
    ratios and derivative fits both read "no shift" -- and the charge even
    drags the spike lag back under half its true value. The sync is untouched
    by any of that and reports the nanosecond."""
    light = Trace(spike(0.0) + extraction(), DT, T0, count=200)
    dark = Trace(spike(1.0), DT, T0, count=200)
    v = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT,
                       light=light, dark=dark,
                       sync_light=Trace(sync(0.0), DT, T0),
                       sync_dark=Trace(sync(1.0), DT, T0))
    assert v["edge_light_ns"] == pytest.approx(6.0, abs=0.6), "nothing is smeared"
    assert abs(v["sync_lag_ns"]) == pytest.approx(1.0, abs=0.15)
    assert v["level"] == "warn"
    assert "sync traces are" in v["text"] and "not a charge" in v["text"]


def test_the_sync_edge_is_read_off_a_narrow_pulse_too():
    """This rig's sync is a 5.5 ns pulse, not a step. Read with percentiles
    over the +-100 ns window it looked flat -- 11 samples of 400 -- so every
    shot reported no sync edge and the verdict said "No sync trace was
    fetched" with the trace sitting in the file (2026-09-07)."""
    t = np.arange(N) * DT + T0
    pulse = 1.19 * np.exp(-(t / 2.8e-9) ** 2)
    assert sync_edge_ns(pulse, DT, T0) == pytest.approx(3.5, abs=1.0)
    assert sync_edge_ns(-pulse, DT, T0) == pytest.approx(3.5, abs=1.0)
    assert sync_edge_ns(np.full(N, 0.01), DT, T0) is None, "still no edge on a flat sync"

    # A single stray sample satisfies everything a pulse does -- it supplies
    # both the excursion and most of the trace's range -- and the 10/90
    # crossings then land on it together, so the edge came back 0.0 ns: a sync
    # sharper than any real one, reported as if it had been measured. It has to
    # last more than one sample, and the two crossings have to be distinct.
    glitch = np.full(N, 0.01)
    glitch[int(round(-T0 / DT))] = 1.2
    assert sync_edge_ns(glitch, DT, T0) is None, "one stray sample is not an edge"
    square = np.full(N, 0.01)
    square[int(round(-T0 / DT)):int(round(-T0 / DT)) + 2] = 1.2
    assert sync_edge_ns(square, DT, T0) is None, "an edge the sampler cannot resolve is not one"


def test_a_sync_that_was_fetched_but_could_not_be_read_says_which():
    """Two different states, and the operator acts on them differently: no
    trace at all is a wiring or driver question, an unreadable one is a
    diagnostics question."""
    light = Trace(spike(0.0), DT, T0, count=200)
    dark = Trace(spike(0.5, smear_ns=2.0), DT, T0, count=200)
    flat = Trace(np.full(N, 0.01), DT, T0)
    none = W.shot_verdict(light.y, dark.y, {}, dt=DT, light=light, dark=dark)
    assert "No sync trace was fetched" in none["text"]
    unread = W.shot_verdict(light.y, dark.y, {}, dt=DT, light=light, dark=dark,
                            sync_light=flat, sync_dark=flat)
    assert "fetched but no edge could be read" in unread["text"]


def test_the_acceptance_run_of_2026_09_02_is_a_good_shot():
    """The service run that matched LabVIEW: spikes aligned to a hundredth
    of a nanosecond, 6-7 ns edges. The numbers the rule is calibrated on."""
    from bace.storage.hdf5 import read_run
    files = glob.glob(os.path.join(os.path.dirname(__file__), "..", "acceptance",
                                   "20260902_service-vs-labview", "service_153722", "run*.h5"))
    if not files:
        pytest.skip("the acceptance run is not in this checkout")
    d = read_run(files[0])
    dt = float(d["time_s"][1] - d["time_s"][0])
    lag = spike_lag_ns(d["light"][0], d["dark"][0], dt)
    assert lag is not None and abs(lag) < 0.1
    assert 5.5 <= edge_10_90_ns(d["light"][0], dt) <= 7.0
    assert 5.5 <= edge_10_90_ns(d["dark"][0], dt) <= 7.0


# -- the verdict ------------------------------------------------------------
def test_misaligned_spikes_are_a_warning_and_aligned_ones_are_said_so():
    # `smear_ns=2` puts the dark spike's edge at 9 ns, inside the 8-13 ns the
    # 2026-09-05 incident showed. A lag with a *sharp* edge is a different
    # thing and is not a warning any more -- the test below it says why.
    light = Trace(spike(0.0, photo=3e-4), DT, T0, count=200)
    dark = Trace(spike(0.5, smear_ns=2.0), DT, T0, count=200)
    t = np.arange(N) * DT + T0
    sync = Trace(1.2 / (1 + np.exp(-t / (2e-9 / 4.4))), DT, T0)
    bad = W.shot_verdict(light.y, dark.y, {"autorange_passes": 1}, dt=DT, light=light, dark=dark,
                         sync_light=sync, sync_dark=sync)
    assert bad["level"] == "warn"
    assert "ns apart" in bad["text"] and "not a charge" in bad["text"]
    assert "spike edge" in bad["text"], "the text names the symptom that corroborated the lag"
    assert bad["spike_lag_ns"] == pytest.approx(0.5, abs=0.06)
    assert bad["averages_light"] == 200 and bad["sync_edge_light_ns"] == pytest.approx(2.0, abs=0.6)
    assert "sync edges are" in bad["text"], "the text says which side to look at"

    good = W.shot_verdict(light.y, spike(0.0).copy(), {"autorange_passes": 1}, dt=DT,
                          light=light, dark=Trace(spike(0.0), DT, T0, count=200),
                          sync_light=sync, sync_dark=sync)
    assert good["level"] == "ok"
    assert good["text"].startswith("autorange pass 1 · ")
    assert "spikes 0.0" in good["text"] and "200 avg" in good["text"] and "sync edge 2." in good["text"]
    assert set(W.VERDICT_KEYS) <= set(good)

    # the rail rules come first: a railed trace is the rail, whatever the lag
    railed = np.maximum(spike(0.0), -0.02)
    v = W.shot_verdict(railed, np.maximum(spike(0.5), -0.02), dt=DT)
    assert v["level"] == "warn" and "consecutive samples" in v["text"]


# -- the engine -------------------------------------------------------------
def _rig(seed: int = 1):
    sim = make_bench(seed=seed)
    sim.led.set_pulse(1.020, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig(),
              power=sim.power, smu=sim.smu, router=sim.router)
    return sim, rig


CFG = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0, t0_int_s=2.71e-7,
                invert_polarity=True)


def test_every_shot_carries_its_sync_traces_and_the_count_the_digitiser_folded():
    _, rig = _rig()
    evs = list(run_transient_scan(rig, ScanSpec(axis=Axis("vpre", 1.0, 1.0), vcoll=-2.0, n_loops=2),
                                  CFG, sleep=NO_SLEEP))
    shots = [e for e in evs if isinstance(e, E.StepDone)]
    assert len(shots) == 2
    for s in shots:
        assert s.light.count == 64 and s.dark.count == 64
        assert isinstance(s.sync_light, Trace) and isinstance(s.sync_dark, Trace)
        assert s.sync_light.y.size == s.light.y.size and s.sync_light.dt == s.light.dt
        assert np.ptp(s.sync_light.y) > 0.5, "the sync is a volt-scale edge, not the current"
    assert not any(isinstance(e, E.Notice) and "acquisitions, not" in e.text for e in evs)
    # the simulated shot is a good shot by the alignment rule
    v = W.shot_verdict(shots[0].light.y, shots[0].dark.y, dt=shots[0].light.dt,
                       light=shots[0].light, dark=shots[0].dark,
                       sync_light=shots[0].sync_light, sync_dark=shots[0].sync_dark)
    assert v["level"] == "ok", v["text"]
    assert v["sync_edge_light_ns"] is not None


def test_a_digitiser_that_folded_fewer_averages_than_asked_is_said_so_once_per_trace():
    _, rig = _rig()
    real = rig.scope.acquire

    def short(n_averages, **kw):
        tr = real(n_averages, **kw)
        return Trace(tr.y, tr.dt, tr.t0, count=n_averages - 3)

    rig.scope.acquire = short
    evs = list(run_transient_scan(rig, ScanSpec(axis=Axis("vpre", 1.0, 1.0), vcoll=-2.0, n_loops=1),
                                  CFG, sleep=NO_SLEEP))
    notes = [e for e in evs if isinstance(e, E.Notice) and "acquisitions, not the 64" in e.text]
    assert len(notes) == 2 and "light" in notes[0].text and "dark" in notes[1].text
    shot = [e for e in evs if isinstance(e, E.StepDone)][0]
    assert shot.light.count == 61


def test_a_scope_that_cannot_hand_over_the_sync_costs_one_line_not_the_run():
    _, rig = _rig()

    def refuse(source):
        raise RuntimeError("channel not displayed")

    rig.scope.fetch_volts = refuse
    evs = list(run_transient_scan(rig, ScanSpec(axis=Axis("vpre", 1.0, 1.0), vcoll=-2.0, n_loops=2),
                                  CFG, sleep=NO_SLEEP))
    shots = [e for e in evs if isinstance(e, E.StepDone)]
    assert len(shots) == 2 and all(s.sync_light is None and s.sync_dark is None for s in shots)
    notes = [e for e in evs if isinstance(e, E.Notice) and "sync trace" in e.text]
    assert len(notes) == 1, "said once, not per acquisition"


# -- the file ---------------------------------------------------------------
def test_the_file_keeps_the_sync_traces_and_the_per_shot_diagnostics(tmp_path):
    from bace.storage.hdf5 import read_run
    from bace.storage.naming import RunMetadata

    _, rig = _rig()
    rec = RunRecorder(str(tmp_path), RunMetadata(sample="sim", temperature_k=290.0,
                                                  temperature_how="typed"))
    list(record(run_transient_scan(rig, ScanSpec(axis=Axis("vpre", 0.9, 1.0, 0.1), vcoll=-2.0,
                                                 n_loops=2), CFG, sleep=NO_SLEEP), rec))
    h5 = [p for p in rec.written if p.endswith(".h5")][0]
    d = read_run(h5)
    assert d["sync_light"].shape == d["light"].shape and d["sync_dark"].shape == d["dark"].shape
    assert np.ptp(d["sync_light"]) > 0.5
    diag = d["diagnostics"]
    assert set(diag) >= {"averages_light", "averages_dark", "spike_lag_ns", "edge_light_ns",
                         "edge_dark_ns", "sync_edge_light_ns", "sync_edge_dark_ns"}
    assert diag["averages_light"].shape == (2, 2)
    assert (diag["averages_light"] == 64).all() and (diag["averages_dark"] == 64).all()
    assert np.isfinite(diag["spike_lag_ns"]).all() and np.abs(diag["spike_lag_ns"]).max() < 0.25
    assert np.isfinite(diag["edge_light_ns"]).all()
