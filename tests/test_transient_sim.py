"""The standalone measurement, end to end on simulated instruments.

The simulated bench is wired so that the scope returns the transient implied by
what the generator and shutter were actually told to do. That makes these tests
sequencing tests, not just plumbing tests: if the dark levels were applied for
the light trace, or the shutter were left open for the dark one, the recovered
charge would be wrong and these assertions would fail.
"""
from __future__ import annotations

import numpy as np
import pytest

from bace.core.axis import Axis, ScanSpec, bace_at_voc, bace_sweep, field_dependence
from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig, resolve_t0_int, run_transient_scan

NO_SLEEP = lambda s: None


def build(seed: int = 1, drive: float = 1.020):
    sim = make_bench(seed=seed)
    sim.led.set_pulse(drive, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), power=sim.power, smu=sim.smu, router=sim.router)
    return sim, rig


def run(rig, spec, cfg=None, **kw):
    # `t0_int_s` is pinned here rather than taken from the dataclass default.
    # The default (3.18e-7) is the archive's number, and the archive is real
    # hardware: the field arrives ~38 ns after `:PULS:DEL1` elapses, through the
    # generator and the cable. The simulator models no such latency -- its field
    # arrives the instant the delay expires -- so the same window would start
    # 38 ns late and clip the front off the transient. Until 2026-09-02 that was
    # hidden, because `trigger_offset_s = 47e-9` pushed the simulated pulse late
    # by almost exactly the missing latency. Measuring the offset to be zero
    # took the compensation away and exposed it.
    cfg = cfg or RunConfig(n_averages=200, settle_s=0.0, dark_settle_s=0.0,
                           t0_int_s=2.71e-7)
    return list(run_transient_scan(rig, spec, cfg, sleep=NO_SLEEP, **kw))


def finished(evs):
    return next(e for e in evs if isinstance(e, E.RunFinished))


# -- physics recovered through the whole chain ---------------------------
def test_recovers_the_injected_photocharge():
    """Dark subtraction has to remove a capacitive charge ~13x larger than the
    signal. Getting within a few percent means the whole chain is wired up.

    The device model returns the *physical* photocharge. The digitizer applies
    `current_sign` (-1 on this bench, `RigConfig.current_sign`) on the way out,
    exactly where the real one does, so what the run recovers is
    `current_sign * photocharge` -- what a reader of a real file sees. The
    comparison carries the sign rather than the simulator dropping it."""
    sim, rig = build()
    evs = run(rig, bace_sweep(0.80, 1.00, 0.02, n_loops=2))
    f = finished(evs)
    physical = np.array([sim.bench.device.photocharge(v, 1.020) for v in f.values])
    assert sim.scope.current_sign == -1.0, "the simulator must match the rig"
    np.testing.assert_allclose(f.q_mean, sim.scope.current_sign * physical,
                               rtol=0.05)


def test_the_simulator_reports_in_the_rigs_sign_convention():
    """`current_sign` is a bench constant, so the simulated bench defaults to
    the one `RigConfig` carries -- otherwise a simulated file and a real one
    would disagree in sign for the same physics. It is applied once, in the
    fetch: flipping it flips every number and changes nothing else, which the
    same seed on both benches shows to the last bit."""
    assert make_bench().scope.current_sign == RigConfig().current_sign == -1.0

    def q_and_peak(sign):
        sim = make_bench(seed=3, current_sign=sign)
        sim.led.set_pulse(1.020, 0.4)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(current_sign=sign))
        evs = run(rig, bace_at_voc(1), voc=0.906)
        step = next(e for e in evs if isinstance(e, E.StepDone))
        return (finished(evs).q_mean[0],
                step.light.y[np.argmax(np.abs(step.light.y))])

    q_minus, peak_minus = q_and_peak(-1.0)
    q_plus, peak_plus = q_and_peak(+1.0)
    assert q_minus < 0 < q_plus, "extraction reads negative on this bench"
    assert q_minus == pytest.approx(-q_plus, rel=1e-12)
    assert peak_minus == pytest.approx(-peak_plus, rel=1e-12)


def test_charge_is_insensitive_to_the_collection_field():
    """The dark reference shares the swing, so the capacitive term cancels and
    Q should depend on V_pre, not on V_coll. This is the property the whole
    dark-run design exists to buy."""
    sim, rig = build()
    f = finished(run(rig, field_dependence(-0.5, -3.5, 0.5, vpre=0.906)))
    # abs(): every Q is negative in the rig's sign convention, and a negative
    # denominator would make this pass for any spread at all.
    spread = np.ptp(f.q_mean) / abs(f.q_mean.mean())
    assert spread < 0.10, f"Q varied by {spread:.1%} across V_coll; the dark " \
                          "subtraction is not cancelling the capacitive charge"


def test_the_recorded_geometry_matches_the_archive():
    """The archive was taken at 200 ns/div, so this pins 200 rather than
    inheriting the default. `RunConfig.timebase_ns_per_div` is now 500 — the
    panel's own value, where one 5 us pulse fills the record — and a test that
    asserts the *archive's* 2 us / 0.5 ns geometry has to say so itself."""
    sim, rig = build()
    f = finished(run(rig, bace_at_voc(1), voc=0.906,
                     cfg=RunConfig(n_averages=200, settle_s=0.0, dark_settle_s=0.0,
                                   timebase_ns_per_div=200.0)))
    assert f.photo_averaged.shape[1] == 4000        # 5000 requested, 4000 returned
    assert f.dt == pytest.approx(0.5e-9)


# -- the event stream -----------------------------------------------------
def test_event_order_and_counts():
    sim, rig = build()
    evs = run(rig, bace_sweep(0.88, 0.92, 0.02, n_loops=2))
    kinds = [type(e).__name__ for e in evs]

    assert kinds[0] == "RunStarted"
    assert kinds[1] == "AxisResolved"
    assert kinds[-1] == "RunFinished"
    assert kinds.count("StepDone") == 6
    assert kinds.count("StepStarted") == 6
    assert kinds.count("LoopDone") == 2
    # every StepStarted is followed by exactly one StepDone before the next
    seq = [k for k in kinds if k in ("StepStarted", "StepDone")]
    assert seq == ["StepStarted", "StepDone"] * 6


def test_running_statistics_tighten_over_loops():
    sim, rig = build()
    evs = run(rig, bace_at_voc(n_loops=4), voc=0.906)
    steps = [e for e in evs if isinstance(e, E.StepDone)]
    assert steps[0].q_std == 0.0                    # one sample: no spread yet
    assert all(s.q_mean == pytest.approx(np.mean([x.q for x in steps[:i + 1]]))
               for i, s in enumerate(steps))


def test_progress_reaches_the_end():
    sim, rig = build()
    evs = run(rig, bace_sweep(0.9, 0.94, 0.02, n_loops=2))
    prog = [e for e in evs if isinstance(e, E.Progress)]
    assert prog[-1].done == prog[-1].total == 6


def test_intensity_is_read_when_a_power_meter_is_present():
    sim, rig = build()
    steps = [e for e in run(rig, bace_at_voc(2), voc=0.906)
             if isinstance(e, E.StepDone)]
    assert all(s.intensity_w is not None and s.intensity_w > 0 for s in steps)


# -- safety and unwinding -------------------------------------------------
def test_abort_stops_the_run_and_parks_the_bias():
    sim, rig = build()
    seen = {"n": 0}

    def abort():
        seen["n"] += 1
        return seen["n"] > 3

    evs = run(rig, bace_sweep(0.8, 1.0, 0.02, n_loops=2), abort=abort)
    assert isinstance(evs[-1], E.RunAborted)
    assert evs[-1].done == 3
    assert sim.bench.bias_output is False
    assert sim.bench.shutter_open is False


def test_walking_away_mid_run_still_parks_the_bias():
    """A consumer that stops iterating must not leave the sample driven."""
    sim, rig = build()
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0)
    gen = run_transient_scan(rig, bace_sweep(0.8, 1.0, 0.02, n_loops=5), cfg,
                             sleep=NO_SLEEP)
    for _ in range(6):
        next(gen)
    assert sim.bench.bias_output is True            # mid-run: output is live
    gen.close()                                      # GeneratorExit at the yield
    assert sim.bench.bias_output is False
    assert sim.bench.shutter_open is False


def test_an_instrument_failure_still_parks_the_bias():
    sim, rig = build()
    calls = {"n": 0}
    real = sim.scope.acquire

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("scope fell over")
        return real(*a, **kw)

    sim.scope.acquire = flaky
    with pytest.raises(RuntimeError, match="fell over"):
        run(rig, bace_sweep(0.8, 1.0, 0.02, n_loops=2))
    assert sim.bench.bias_output is False
    assert sim.bench.shutter_open is False


# -- the simulator earns its place ---------------------------------------
def test_a_shutter_left_open_for_the_dark_trace_biases_the_charge():
    """Not a test of the code under test — a test that the simulator would
    catch the mistake, and a reminder of why the dark run must actually be dark.

    A dark reference taken in the light still swings 0 -> V_coll-V_pre, so the
    capacitive term still cancels. What it *also* subtracts is the charge stored
    at zero prebias under illumination, which is not zero. The result stays
    plausible and comes out low — the worst kind of error, and the reason the
    shutter is sequenced with the levels rather than assumed.
    """
    sim, rig = build()
    good = finished(run(rig, bace_at_voc(3), voc=0.906)).q_mean[0]

    sim2, rig2 = build()
    sim2.shutter.shut = lambda: None                 # sabotage: never closes
    bad = finished(run(rig2, bace_at_voc(3), voc=0.906)).q_mean[0]

    # Magnitudes. In the rig's sign convention both charges are negative, and
    # "comes out low" means smaller in size, not further down the number line.
    good, bad = abs(good), abs(bad)
    assert bad < good
    assert 0.05 < (good - bad) / good < 0.30, (
        "the simulator should show a clear but non-obvious bias here; if this "
        "ever passes trivially the bench has stopped modelling the shutter"
    )


def test_a_missing_vertical_range_is_an_error_not_a_silent_zero():
    sim, rig = build()
    with pytest.raises(RuntimeError, match="no vertical range"):
        sim.scope.acquire(64, autorange_first=False)


def test_a_range_that_is_too_small_reports_clipping():
    sim, rig = build()
    sim.bias.set_levels(0.906 / 4, -1.0 / 4, delay_s=1.4e-7, width_s=5e-6)
    sim.shutter.unblock()
    sim.scope.configure_channel(2, vertical_range=1e-3, offset=0.0)
    sim.scope.acquire(64, autorange_first=False)
    assert sim.scope.clipped is True


# -- where the integration window starts ---------------------------------
def test_the_two_t0_int_references_name_the_same_window_at_the_archive_geometry():
    """318 ns of record time and 118.5 ns after the trigger are one window at
    200 ns/div, where `:WAV:XOR?` reads -199.5 ns. That identity is what makes
    `t0_int_reference` a change of spelling rather than of value, and it is the
    reason the 2026-08-07 regression is untouched by the option existing."""
    rec = RunConfig(t0_int_s=3.18e-7, t0_int_reference="record")
    trg = RunConfig(t0_int_s=1.185e-7, t0_int_reference="trigger")
    assert resolve_t0_int(rec, -1.995e-7) == pytest.approx(3.18e-7)
    assert resolve_t0_int(trg, -1.995e-7) == pytest.approx(3.18e-7)


def test_a_record_time_window_ignores_the_timebase_and_a_trigger_one_follows_it():
    """Why the option exists. `:TIM:POS` is `timebase * 4` ns and is the screen
    centre, so the trigger sits `range/2 - TIM:POS` into the record: 200 ns at
    200 ns/div, 500 ns at 500 ns/div. Held in record time the window keeps its
    number and changes its meaning — at 500 ns/div, 318 ns lands 182 ns *before*
    the trigger. Held against the trigger it does the opposite, which is what a
    measurement wants."""
    rec = RunConfig(t0_int_s=3.18e-7, t0_int_reference="record")
    trg = RunConfig(t0_int_s=1.185e-7, t0_int_reference="trigger")

    assert resolve_t0_int(rec, -2.0e-7) == resolve_t0_int(rec, -5.0e-7)
    assert resolve_t0_int(trg, -5.0e-7) == pytest.approx(6.185e-7)
    assert resolve_t0_int(trg, -5.0e-7) > resolve_t0_int(trg, -2.0e-7)


def test_an_unknown_t0_int_reference_is_refused_before_an_instrument_is_touched():
    with pytest.raises(ValueError, match="'record', 'trigger' or 'pulse'"):
        RunConfig(t0_int_reference="start")
    with pytest.raises(ValueError, match="'auto', 'NORM', 'INV' or 'leave'"):
        RunConfig(output_polarity="maybe")


def test_a_trigger_referenced_run_says_where_the_window_landed():
    """The conversion depends on the instrument's own report of its record, so
    it is not knowable from the config alone. A run that uses it says the number
    out loud rather than leaving it to be recomputed from a docstring."""
    sim, rig = build()
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0,
                    timebase_ns_per_div=200.0,
                    t0_int_s=1.185e-7, t0_int_reference="trigger")
    evs = run(rig, bace_at_voc(1), cfg=cfg, voc=0.906)
    notes = [e.text for e in evs if isinstance(e, E.Notice)]
    assert any("of record time" in t for t in notes), notes
    assert sum("of record time" in t for t in notes) == 1, "said once, not per shot"


# -- clipping actually travels -------------------------------------------
def test_step_done_reports_clipping_from_the_digitizer():
    """`StepDone.clipped` was `getattr(scope, "clipped", False)` until
    2026-09-01. The simulator had `clipped`; `Infiniium` called the same state
    `last_autorange_clipped`; the protocol required neither. So the field was
    False for every shot ever taken on the rig, and this test passed anyway
    because it ran on the simulator. The name is in `Digitizer` now — this
    asserts the value travels, and `test_architecture` asserts it exists."""
    sim, rig = build()

    class AlwaysClipped:
        """Wraps the simulated scope and reports a clipped window."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        @property
        def clipped(self) -> bool:
            return True

    rig.scope = AlwaysClipped(sim.scope)
    steps = [e for e in run(rig, bace_at_voc(1), voc=0.906)
             if isinstance(e, E.StepDone)]
    assert steps and all(s.clipped for s in steps)


# -- a window that travels with the pulse, and a polarity nobody guesses --
def test_a_pulse_referenced_window_travels_with_the_delay():
    """The scope triggers on the 81150A *Sync*, which `:PULS:DEL1` does not
    delay — only the output. So along a delay axis the transient slides through
    the record while a trigger-referenced window stands still, and every point
    gets a different slice of its own transient. Q(delay) would then be half
    physics and half window."""
    trg = RunConfig(t0_int_s=1.185e-7, t0_int_reference="trigger")
    pul = RunConfig(t0_int_s=1.185e-7, t0_int_reference="pulse")

    near = resolve_t0_int(pul, -2.0e-7, pulse_delay_s=7e-9)
    far = resolve_t0_int(pul, -2.0e-7, pulse_delay_s=2.47e-7)
    assert far - near == pytest.approx(2.4e-7), "it has to move with the pulse"
    assert resolve_t0_int(trg, -2.0e-7, pulse_delay_s=7e-9) == \
        resolve_t0_int(trg, -2.0e-7, pulse_delay_s=2.47e-7), "and this must not"


def test_leave_means_the_run_does_not_write_the_output_polarity():
    """Which level the device rests at between pulses depends on how the sample
    is wired, and that is not always known. A wrong guess puts the extraction on
    the other edge of the pulse — microseconds away, outside the record — while
    still producing a transient that integrates to a plausible charge. So a run
    may decline to set it; what was there is read back and announced."""
    sim, rig = build()
    sim.bias.inverted = True                       # what the front panel holds
    cfg = RunConfig(n_averages=16, settle_s=0.0, dark_settle_s=0.0,
                    output_polarity="leave")
    evs = run(rig, bace_at_voc(1), cfg=cfg, voc=0.906)

    assert sim.bias.inverted is True, "the run must not have written it"
    notes = [e.text for e in evs if isinstance(e, E.Notice)]
    assert any("left as found" in t and "INV" in t for t in notes), notes


def test_the_run_arms_the_generator_and_records_what_it_holds():
    """The simulated 81150A starts free-running (`IMM`, what `*RST` leaves),
    which is exactly the state a run that forgot to arm would inherit. The
    run has to write the arming and then read it back into the
    `InstrumentState` that reaches the file, beside the polarity."""
    sim, rig = build()
    assert sim.bias.trigger_state()["arm_source"] == "IMM"

    evs = run(rig, bace_at_voc(1), voc=0.906,
              cfg=RunConfig(n_averages=16, settle_s=0.0, dark_settle_s=0.0))
    state = next(e for e in evs if isinstance(e, E.InstrumentState)).values
    assert state["bias_arm_source"] == "EXT"
    assert state["bias_arm_slope"] == "POS"
    assert sim.bias.trigger_state() == {"arm_source": "EXT", "arm_slope": "POS"}

    sim2, rig2 = build()
    evs = run(rig2, bace_at_voc(1), voc=0.906,
              cfg=RunConfig(n_averages=16, settle_s=0.0, dark_settle_s=0.0,
                            external_trigger=False))
    state = next(e for e in evs if isinstance(e, E.InstrumentState)).values
    assert state["bias_arm_source"] == "IMM"


def test_auto_still_writes_what_inverted_output_says():
    sim, rig = build()
    sim.bias.inverted = True
    cfg = RunConfig(n_averages=16, settle_s=0.0, dark_settle_s=0.0,
                    inverted_output=False)         # output_polarity defaults to auto
    run(rig, bace_at_voc(1), cfg=cfg, voc=0.906)
    assert sim.bias.inverted is False


# -- the tail that was not a baseline ------------------------------------
def test_a_decayed_transient_reads_as_a_flat_baseline():
    from bace.core.process import baseline_is_flat
    t = np.arange(2000) * 1.6e-9
    light = 2e-3 * np.exp(-(t - 6e-7).clip(0) / 2e-7) * (t > 6e-7)
    dark = np.zeros_like(light)
    flat, head, tail, peak = baseline_is_flat(light, dark)
    assert flat
    assert abs(tail) < 0.01 * peak
    assert head == pytest.approx(0.0, abs=1e-9)


def test_a_tail_that_is_still_signal_is_not_a_baseline():
    """The 2026-09-01 17:59 run: the device rested at V_oc (light - dark = 0)
    and stepped into a *steady* 1.9 mA extraction that never decayed, because
    the LED never turned off inside the record. `offset_correct` subtracted
    that 1.9 mA, so `photo` came out reading -1.9 mA before the step and 0
    after -- the exact inverse of the truth, with a believable Q."""
    from bace.core.process import baseline_is_flat, photocurrent
    n, dt = 3125, 1.6e-9
    step = 450
    light = np.full(n, -0.12e-3)
    light[step:] = -1.1e-3
    dark = np.full(n, -0.16e-3)
    dark[step:] = -3.0e-3

    flat, head, tail, peak = baseline_is_flat(light, dark)
    assert not flat
    assert head == pytest.approx(0.04e-3, abs=1e-6)     # V_oc: no net current
    assert tail == pytest.approx(1.9e-3, abs=1e-4)      # steady extraction

    # and this is what the correction then does to it
    photo = photocurrent(light, dark, dt, offset_correct=True)
    assert photo[:100].mean() == pytest.approx(-1.86e-3, abs=1e-4)   # sign flipped
    assert abs(photo[-100:].mean()) < 1e-6                           # signal erased


def test_the_run_warns_when_the_tail_is_not_a_baseline(monkeypatch):
    """The warning has to reach the event stream, not just be computable."""
    from bace.core import process
    from bace.experiment.transient import run_transient_scan

    monkeypatch.setattr(process, "baseline_is_flat",
                        lambda light, dark, **kw: (False, 4e-5, 1.9e-3, 3.2e-3))
    import bace.experiment.transient as T
    monkeypatch.setattr(T, "baseline_is_flat", process.baseline_is_flat)

    sim = make_bench(seed=1)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig())
    cfg = RunConfig(n_averages=4, settle_s=0.0, dark_settle_s=0.0, record_length=400,
                    offset_correct=True)
    evs = list(run_transient_scan(rig, bace_sweep(0.88, 0.90, 0.02, n_loops=1),
                                  cfg, sleep=NO_SLEEP))
    warns = [e for e in evs if isinstance(e, E.Notice) and e.level == "warning"
             and "not a baseline" in e.text]
    assert len(warns) == 1, "warned once, not once per step"
    assert "1.9 mA" in warns[0].text


# -- the shutter has to settle before the trace is taken -------------------
def _sequence(cfg):
    """The order of shutter moves, sleeps and acquisitions for one step."""
    sim = make_bench(seed=5)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig())
    log: list = []

    class Watched:
        def __init__(self, inner): self._i = inner
        def unblock(self): log.append(("shutter", "open")); return self._i.unblock()
        def shut(self): log.append(("shutter", "shut")); return self._i.shut()
        def __getattr__(self, n): return getattr(self._i, n)

    real_acquire = rig.scope.acquire
    def acquire(*a, **k):
        log.append(("acquire", k.get("autorange_first", False)))
        return real_acquire(*a, **k)

    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=Watched(sim.shutter),
              config=RigConfig())
    rig.scope.acquire = acquire                      # type: ignore[method-assign]
    list(run_transient_scan(rig, bace_sweep(0.90, 0.90, 0.02, n_loops=1), cfg,
                            sleep=lambda s: log.append(("sleep", round(s, 4)))))
    return log


def test_both_traces_wait_after_the_shutter_moves():
    """Until 2026-09-01 the dark trace was acquired the instant `shut()` returned:
    `dark_settle_s` was slept *before* the shutter closed, so the mechanical
    shutter and the device got no settling at all, while the light trace got
    `settle_s` after `unblock()`. A difference of two traces means nothing when
    one of them is measured during a transient the other was allowed to finish."""
    cfg = RunConfig(n_averages=4, record_length=400,
                    settle_s=0.2, dark_settle_s=0.3, shutter_settle_s=0.5)
    log = _sequence(cfg)
    step = log[log.index(("shutter", "open")):]

    assert step[0] == ("shutter", "open")
    assert step[1] == ("sleep", 0.7)          # settle_s + shutter_settle_s
    assert step[2] == ("acquire", True)       # light, with autorange
    assert step[3] == ("sleep", 0.3)          # dark_settle_s, levels only
    assert step[4] == ("shutter", "shut")
    assert step[5] == ("sleep", 0.5)          # shutter_settle_s -- the fix
    assert step[6] == ("acquire", False)      # dark, range inherited


def test_the_default_keeps_the_archive_timing():
    """`shutter_settle_s` defaults to 0 so the 2026-08 regression is untouched;
    a real measurement is expected to set it."""
    assert RunConfig().shutter_settle_s == 0.0
    log = _sequence(RunConfig(n_averages=4, record_length=400))
    step = log[log.index(("shutter", "open")):]
    assert step[1] == ("sleep", 0.2)          # settle_s alone, as before
    assert step[5] == ("sleep", 0.0)


# -- what the dark trace is a reference for -------------------------------
def test_the_dark_trace_can_be_shutter_only():
    """`dark_reference = "same"` leaves the generator alone between the two
    traces, so the shutter is the only thing that moves and the voltage step is
    bit-identical. The default translates the dark swing to start at 0 V, which
    is a different absolute range and only cancels the capacitive term where
    C(V) is flat."""
    from bace.core.pulses import pulse_levels

    lv = pulse_levels(1.01354, -1.0, 4.0, 90.0, 5000.0, invert=True,
                      trigger_offset_s=0.0)
    # the two conventions, at the device
    assert (lv.high_light * 4, lv.low_light * 4) == pytest.approx((1.0, -1.01354))
    assert (lv.high_dark * 4, lv.low_dark * 4) == pytest.approx((2.01354, 0.0))

    for ref, expect_writes in (("translated", 2), ("same", 1)):
        sim = make_bench(seed=2)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig())
        seen: list = []
        real = rig.bias.set_levels
        def watch(hi, lo, **kw):
            seen.append((round(hi, 6), round(lo, 6)))
            return real(hi, lo, **kw)
        rig.bias.set_levels = watch          # type: ignore[method-assign]
        cfg = RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                        record_length=400, t0_int_s=2.71e-7, dark_reference=ref)
        list(run_transient_scan(rig, bace_sweep(0.90, 0.90, 0.02, n_loops=1),
                                cfg, sleep=NO_SLEEP))
        assert len(seen) == expect_writes, f"{ref}: {seen}"
        if ref == "same":
            # the light pair, written once, and never rewritten for the dark
            assert len(set(seen)) == 1


def test_a_misspelt_dark_reference_is_refused():
    with pytest.raises(ValueError, match="dark_reference must be"):
        RunConfig(dark_reference="shutter-only")


def test_every_shot_announces_its_phases_in_order():
    """`StepPhase` says where inside a shot the run is, between the same
    instrument calls in the same order; filtering it out leaves the stream
    exactly as it was. With `dark_reference = "same"` nothing is rewritten
    for the dark trace, so that segment is absent and `of` says so."""
    sim, rig = build()
    evs = run(rig, bace_sweep(0.88, 0.92, 0.02, n_loops=1))
    phases = [e for e in evs if isinstance(e, E.StepPhase)]
    names = ["levels", "light settle", "acquire light", "dark levels", "dark settle",
             "acquire dark", "process"]
    assert [(p.phase, p.k, p.of) for p in phases] == \
        [(n, k, 7) for _ in range(3) for k, n in enumerate(names, 1)]
    assert [p.index for p in phases] == [i for i in range(3) for _ in names]
    kinds = [type(e).__name__ for e in evs if not isinstance(e, E.StepPhase)]
    seq = [k for k in kinds if k in ("StepStarted", "StepDone")]
    assert seq == ["StepStarted", "StepDone"] * 3
    # between StepStarted and StepDone, and nowhere else
    order = [type(e).__name__ for e in evs]
    for i, e in enumerate(evs):
        if isinstance(e, E.StepPhase):
            before = [k for k in order[:i] if k in ("StepStarted", "StepDone")]
            assert before and before[-1] == "StepStarted"

    sim, rig = build()
    evs = run(rig, bace_sweep(0.88, 0.92, 0.02, n_loops=1),
              cfg=RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                            dark_reference="same"))
    phases = [e for e in evs if isinstance(e, E.StepPhase)]
    assert [(p.phase, p.of) for p in phases][:6] == [
        ("levels", 6), ("light settle", 6), ("acquire light", 6), ("dark settle", 6),
        ("acquire dark", 6), ("process", 6)]


# -- trigger calibration ----------------------------------------------------
def test_the_trigger_threshold_is_calibrated_in_volts_whatever_the_sign_convention():
    """The first service run on the rig (2026-09-02, session 103857) set a
    0.25 mV trigger threshold and averaged twenty untriggered records into a
    flat trace and a charge of 1e-12 C. `acquire` returns amps in the rig's
    convention -- divided by R and, since that day, multiplied by
    `current_sign = -1` -- and the calibration took max() of that, so the
    positive sync read as a negative pulse whose maximum is the baseline. The
    threshold has to come from the scope-input volts, whichever sign the
    charges carry."""
    from bace.experiment.transient import MIN_SYNC_SWING_V

    thresholds = {}
    for sign in (-1.0, 1.0):
        sim = make_bench(seed=1, current_sign=sign)
        sim.led.set_pulse(1.020, 0.4)
        rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
                  config=RigConfig(current_sign=sign), power=sim.power)
        cfg = RunConfig(n_averages=50, settle_s=0.0, dark_settle_s=0.0,
                        t0_int_s=2.71e-7, calibrate_trigger=True)
        evs = run(rig, bace_sweep(0.90, 0.90, 0.0, n_loops=1), cfg=cfg)
        note = next(e for e in evs if isinstance(e, E.Notice)
                    and e.text.startswith("trigger threshold set to"))
        thresholds[sign] = float(note.text.split()[4])
        assert " V " in note.text, "the threshold is a voltage and should say so"
    assert thresholds[-1.0] == pytest.approx(thresholds[1.0], rel=0.2), (
        "the same sync must give the same threshold under either sign")
    assert thresholds[-1.0] > MIN_SYNC_SWING_V / 2


class _NoSync:
    """A digitizer whose trigger channel carries nothing but noise."""

    def __init__(self, inner, source: str = "CHAN3"):
        self._inner, self._source = inner, source

    def acquire(self, n_averages, *, source="CHAN2", autorange_first=False, timeout_s=30.0):
        tr = self._inner.acquire(n_averages, source=source,
                                 autorange_first=autorange_first, timeout_s=timeout_s)
        if source == self._source:
            rng = np.random.default_rng(0)
            return type(tr)(y=rng.normal(0.0, 1e-4, tr.y.size) / 5.192, dt=tr.dt, t0=tr.t0)
        return tr

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_a_trigger_channel_with_no_sync_refuses_to_measure_noise_under_auto():
    """With `trigger_sweep = AUTO` a missing sync is not an error the scope
    reports: it sweeps anyway, the dark subtraction cancels the noise, and a
    plausible charge comes out of a disconnected cable. The run stops before
    the first shot and says why, instead."""
    from bace.experiment.transient import SyncError

    sim, rig = build()
    rig = Rig(bias=sim.bias, scope=_NoSync(sim.scope), shutter=sim.shutter,
              config=RigConfig(), power=sim.power)
    cfg = RunConfig(n_averages=50, settle_s=0.0, dark_settle_s=0.0,
                    t0_int_s=2.71e-7, calibrate_trigger=True, trigger_sweep="AUTO")
    seen = []
    with pytest.raises(SyncError, match="no sync on CHAN3"):
        for ev in run_transient_scan(rig, bace_sweep(0.9, 0.9, 0.0), cfg, sleep=NO_SLEEP):
            seen.append(ev)
    assert any(isinstance(e, E.Notice) and e.level == "warning"
               and "no sync on CHAN3" in e.text for e in seen)
    assert not any(isinstance(e, E.StepDone) for e in seen), "nothing was acquired"
    assert sim.bias.output_enabled is False, "the finally still parks the generator"


def test_a_trigger_channel_with_no_sync_only_warns_under_trig():
    """TRIG waits for a real edge and times out on its own; the warning is
    still worth having on the stream, the refusal is not."""
    sim, rig = build()
    rig = Rig(bias=sim.bias, scope=_NoSync(sim.scope), shutter=sim.shutter,
              config=RigConfig(), power=sim.power)
    cfg = RunConfig(n_averages=50, settle_s=0.0, dark_settle_s=0.0,
                    t0_int_s=2.71e-7, calibrate_trigger=True, trigger_sweep="TRIG")
    evs = run(rig, bace_sweep(0.9, 0.9, 0.0), cfg=cfg)
    assert any(isinstance(e, E.Notice) and e.level == "warning"
               and "no sync on CHAN3" in e.text for e in evs)
    assert any(isinstance(e, E.RunFinished) for e in evs)
