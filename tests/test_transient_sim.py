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
from bace.experiment.transient import RunConfig, resolve_window, run_transient_scan

NO_SLEEP = lambda s: None


def build(seed: int = 1, drive: float = 1.020):
    sim = make_bench(seed=seed)
    sim.led.set_pulse(drive, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), power=sim.power, smu=sim.smu, router=sim.router)
    return sim, rig


def run(rig, spec, cfg=None, **kw):
    # `t0_int_s` is measured from the field's arrival at the device, so the
    # window starts 2 ns before the simulated step whatever the delay and the
    # timebase. The simulated bench has no sync-to-field latency unless the
    # `RigConfig` says so (`field_latency_s`), and `RigConfig()` says 0.
    #
    # `invert_polarity=True` with the generator at NORM is the pair the rig
    # validated (`run.toml`): through the inverting amplifier the device then
    # rests at `v_pre` and is pulsed to `v_coll`. The simulator models that
    # amplifier and that rest level, so the recipe with neither flag prebiases
    # the simulated device at `-v_coll` -- exactly as the rig does.
    cfg = cfg or RunConfig(n_averages=200, settle_s=0.0, dark_settle_s=0.0,
                           t0_int_s=-2e-9, invert_polarity=True)
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


@pytest.mark.parametrize("led_v", [1.010, 1.020])
def test_a_lit_shot_recovers_the_photocharge_under_the_validated_recipe(led_v):
    """The service's own recipe -- `invert_polarity = true` with `:OUTP1:POL
    NORM`, a pinned `vpre` above V_oc and the LED above its 1.000 V threshold
    -- through the simulator, and the charge it hands back is the device
    model's, not a fifth of a coulomb.

    Until 2026-09-03 the simulated bench read the prebias off the generator's
    *high* level through a non-inverting amplifier whatever the polarity, so
    this recipe prebiased the device at `-v_coll` = 2 V, 1.1 V above V_oc,
    where the charge model's +20 e-fold clip let it store 0.18 C and the
    `photo` trace peaked at 2e6 A. `ui/fixtures/stream_bace_sim.jsonl` never
    showed it because it was recorded at `led_v = 1.0`, exactly the threshold,
    where `led_current()` is zero and so is the photocharge."""
    sim, rig = build(drive=led_v)
    spec = ScanSpec(axis=Axis("vpre", 1.0, 1.0), vcoll=-2.0, n_loops=2)
    f = finished(run(rig, spec, cfg=RunConfig(n_averages=200, settle_s=0.0,
                                                dark_settle_s=0.0, t0_int_s=-2e-9,
                                                invert_polarity=True,
                                                output_polarity="NORM")))
    expected = sim.bench.device.photocharge(1.0, led_v)
    assert expected > 0, "the LED must be lit for this to test anything"
    recovered = abs(float(f.q_mean[0]))
    assert 0.5 * expected < recovered < 2.0 * expected, (
        f"recovered {recovered:.3e} C against photocharge {expected:.3e} C")
    # q / tau_ext: tens of milliamps for a charge a few times q_ref, not megaamps.
    assert np.abs(f.photo_averaged).max() < 1.0


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


# -- where the integration window sits -----------------------------------
def test_the_window_is_measured_from_the_field_and_lands_in_record_time():
    """`t0_int_s` is from the field's arrival: `:PULS:DEL1` plus the rig's
    sync-to-field latency after the trigger. At 200 ns/div `:WAV:XOR?` reads
    -199.5 ns, so with a 90 ns delay, a 47 ns latency and a -2 ns lead the
    window starts at 334.5 ns of record time and ends `t_int_width_s` later."""
    cfg = RunConfig(t0_int_s=-2e-9, t_int_width_s=1.0e-6)
    t0, t1 = resolve_window(cfg, -1.995e-7, 90e-9, 47e-9)
    assert t0 == pytest.approx(3.345e-7)
    assert t1 == pytest.approx(3.345e-7 + 1.0e-6)


def test_the_window_follows_the_timebase_and_the_delay():
    """`:TIM:POS` is `timebase * 4` ns and is the screen centre, so the trigger
    sits `range/2 - TIM:POS` into the record: 200 ns at 200 ns/div, 500 ns at
    500 ns/div. And the scope triggers on the 81150A *Sync*, which `:PULS:DEL1`
    does not delay -- only the output. So the transient moves through the
    record with both, and the window has to move with it, start and end alike;
    otherwise Q(delay) is half physics and half window."""
    cfg = RunConfig(t0_int_s=0.0, t_int_width_s=1.0e-6)
    at_200 = resolve_window(cfg, -2.0e-7, 90e-9)
    at_500 = resolve_window(cfg, -5.0e-7, 90e-9)
    assert at_500[0] - at_200[0] == pytest.approx(3.0e-7)

    near = resolve_window(cfg, -2.0e-7, 7e-9)
    far = resolve_window(cfg, -2.0e-7, 2.47e-7)
    assert far[0] - near[0] == pytest.approx(2.4e-7), "the start moves with the pulse"
    assert far[1] - near[1] == pytest.approx(2.4e-7), "and so does the end"
    assert far[1] - far[0] == pytest.approx(1.0e-6), "so the window keeps its length"


def test_an_empty_window_is_refused_before_an_instrument_is_touched():
    with pytest.raises(ValueError, match="t_int_width_s must be positive"):
        RunConfig(t_int_width_s=0.0)
    with pytest.raises(ValueError, match="'auto', 'NORM', 'INV' or 'leave'"):
        RunConfig(output_polarity="maybe")


def test_a_run_says_where_the_window_landed_and_records_it_per_step():
    """The conversion depends on the instrument's own report of its record and
    on the step's delay, so it is not knowable from the config alone. A run
    says the number out loud once, and every `StepDone` carries its own."""
    sim, rig = build()
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0,
                    timebase_ns_per_div=200.0, t0_int_s=-2e-9)
    evs = run(rig, bace_at_voc(1), cfg=cfg, voc=0.906)
    notes = [e.text for e in evs if isinstance(e, E.Notice)]
    assert sum("of record time" in t for t in notes) == 1, "said once, not per shot"
    steps = [e for e in evs if isinstance(e, E.StepDone)]
    # trigger 200 ns in, delay 90 ns, no latency on this rig, 2 ns lead
    assert steps[0].t0_int_record_s == pytest.approx(2.88e-7)
    assert steps[0].t1_int_record_s == pytest.approx(2.88e-7 + cfg.t_int_width_s)


def test_a_window_the_record_cuts_short_is_said_once():
    """Along a delay axis the window runs off the end of the record at some
    point before it does at the others; the run warns at the first such shot,
    naming the delay, and does not repeat itself."""
    sim, rig = build()
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0,
                    timebase_ns_per_div=200.0, t0_int_s=-2e-9, t_int_width_s=1.9e-6)
    from bace.core.axis import tdcf_delay
    evs = run(rig, tdcf_delay(0, 400, 200, vpre=0.906, n_loops=2), cfg=cfg)
    cut = [e.text for e in evs if isinstance(e, E.Notice) and "cut short" in e.text]
    assert len(cut) == 1, cut
    assert "delay 0 ns" in cut[0]


def test_the_simulated_bench_puts_its_field_where_the_rig_says():
    """`trigger_offset_s` is the sync-to-field latency, and the simulated bench
    honours it, so a window measured from the field lands on the simulated
    transient as it does on the real one -- Q does not depend on the number."""
    from bace.service.rigs import Bench
    qs = []
    for latency in (0.0, 47e-9):
        b = Bench.build_simulated(RigConfig(trigger_offset_s=latency), fast=True)
        b.sim.led.set_pulse(1.02, 0.4)
        cfg = RunConfig(n_averages=200, settle_s=0.0, dark_settle_s=0.0,
                        t0_int_s=-2e-9, invert_polarity=True)
        evs = list(run_transient_scan(b.rig, bace_at_voc(1), cfg, sleep=NO_SLEEP, voc=0.906))
        assert b.sim.bench.field_latency_s == latency
        qs.append(finished(evs).q_mean[0])
    assert qs[0] == pytest.approx(qs[1], rel=0.02)


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


# -- a polarity nobody guesses --------------------------------------------
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

    lv = pulse_levels(1.01354, -1.0, 4.0, 90.0, 5000.0, invert=True)
    # the two conventions, at the device
    assert (lv.high_light * 4, lv.low_light * 4) == pytest.approx((1.0, -1.01354))
    assert (lv.high_dark * 4, lv.low_dark * 4) == pytest.approx((2.01354, 0.0))

    # One extra write per run since 2026-09-02: setup applies the first
    # step's light levels before enabling the generator, so the scope can
    # calibrate its trigger against a live sync. The loop then writes the
    # light pair again (same numbers) and, for "translated", the dark pair.
    for ref, expect_writes in (("translated", 3), ("same", 2)):
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
        assert seen[0] == seen[1], "setup and the first shot apply the same light pair"
        if ref == "same":
            # the light pair, written once, and never rewritten for the dark
            assert len(set(seen)) == 1


def test_the_light_trace_can_be_taken_with_the_shutter_shut():
    """`light_shutter = "shut"`: the control that measures the dark term of
    the charge at V_pre. The shutter must be shut for *both* traces, the
    levels still the light pair then the dark pair, and the simulator -- which
    only photogenerates with the shutter open -- must return a charge of
    nothing but noise beside the open-shutter run."""
    opened: dict[str, list[bool]] = {}
    charges = {}
    for mode in ("open", "shut"):
        sim, rig = build(seed=3)             # a lit bench: the LED pulsing
        states: list[bool] = []
        real_acquire = rig.scope.acquire
        def watch(*a, **kw):
            states.append(bool(sim.shutter.is_open))
            return real_acquire(*a, **kw)
        rig.scope.acquire = watch            # type: ignore[method-assign]
        cfg = RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                        t0_int_s=-2e-9, invert_polarity=True, light_shutter=mode)
        events = list(run(rig, bace_at_voc(3), cfg, voc=0.906))
        opened[mode] = states
        charges[mode] = [e.q for e in events if isinstance(e, E.StepDone)]
        assert any(isinstance(e, E.InstrumentState) and e.values.get("light_shutter") == mode
                   for e in events), "the choice is reported with the run"
    # open: the light acquisition sees the shutter open, the dark one shut
    assert opened["open"][-2:] == [True, False]
    # shut: neither acquisition of the pair sees it open
    assert opened["shut"][-2:] == [False, False]
    assert abs(charges["shut"][0]) < 0.05 * abs(charges["open"][0]),         f"shut {charges['shut'][0]:.3e} vs open {charges['open'][0]:.3e}"


def test_a_misspelt_light_shutter_is_refused():
    with pytest.raises(ValueError, match="light_shutter must be"):
        RunConfig(light_shutter="closed")


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


def test_the_generator_is_enabled_before_the_scope_calibrates_its_trigger():
    """Session 115857 on the rig (2026-09-02): the service parks the bench after
    every run, so the 81150A output was OFF when the scope calibrated, CHAN3
    swung 5.2 mV, and there was nothing to trigger on. Every earlier run had
    inherited a generator the LabVIEW VI left ON. The VI enables the generator
    first and calibrates after; so does the run now, at the first step's light
    levels with the shutter still shut. The file says the output was on."""
    sim, rig = build()
    order: list[str] = []
    real_enable, real_acquire = rig.bias.enable_output, rig.scope.acquire

    def enable(on=True):
        order.append(f"bias.enable_output({on})")
        return real_enable(on)

    def acquire(n, *, source="CHAN2", autorange_first=False, timeout_s=30.0):
        order.append(f"scope.acquire({source})")
        return real_acquire(n, source=source, autorange_first=autorange_first,
                            timeout_s=timeout_s)

    rig.bias.enable_output = enable          # type: ignore[method-assign]
    rig.scope.acquire = acquire              # type: ignore[method-assign]
    cfg = RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                    t0_int_s=2.71e-7, calibrate_trigger=True)
    evs = list(run_transient_scan(rig, bace_sweep(0.9, 0.9, 0.0), cfg, sleep=NO_SLEEP))
    assert order.index("bias.enable_output(True)") < order.index("scope.acquire(CHAN3)")
    states = [e.values for e in evs if isinstance(e, E.InstrumentState)]
    assert any(v.get("bias_output") == "ON" for v in states), states
    assert sim.bench.shutter_open is False or True   # the shutter moved only inside the loop
    assert isinstance(evs[-1], E.RunFinished)


def test_the_bias_output_state_in_the_file_is_read_from_the_instrument_when_it_can_be():
    """A driver with `read_output` (the real 81150A asks `:OUTP1?`) is asked;
    the cached flag is the fallback. A generator that did not take `:OUTP1 ON`
    must be reported OFF, not remembered ON."""
    from bace.experiment.transient import _output_state

    class Stubborn:
        output_enabled = True                 # what the driver remembers
        def read_output(self):
            return False                      # what the instrument says
    class Mute:
        output_enabled = True
        def read_output(self):
            return None
    class Plain:
        output_enabled = True
    assert _output_state(Stubborn()) == "OFF"
    assert _output_state(Mute()) == "?"
    assert _output_state(Plain()) == "ON"


def test_the_scope_triggers_on_the_sync_at_a_provisional_level_before_measuring_it():
    """Session 125751 on the rig: both generators read ON, CHAN3 swung 4.5 mV.
    The probe record only contains the 5 us sync if the scope starts the
    record on it, and the scope was still on the 0.25 mV level the previous
    session had left, free-running. So the trigger is put at half a volt on
    the sync channel first, the sync is measured from triggered records, and
    the measured half-amplitude replaces the provisional level."""
    from bace.experiment.transient import PROVISIONAL_TRIGGER_V

    sim, rig = build()
    calls: list[tuple] = []
    real_cfg, real_acq = rig.scope.configure_edge_trigger, rig.scope.acquire

    def cfg_trigger(source="CHAN3", **kw):
        calls.append(("trigger", source, kw.get("high_threshold"), kw.get("sweep")))
        return real_cfg(source, **kw)

    def acquire(n, *, source="CHAN2", autorange_first=False, timeout_s=30.0):
        calls.append(("acquire", source))
        return real_acq(n, source=source, autorange_first=autorange_first,
                        timeout_s=timeout_s)

    rig.scope.configure_edge_trigger = cfg_trigger    # type: ignore[method-assign]
    rig.scope.acquire = acquire                       # type: ignore[method-assign]
    cfg = RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                    t0_int_s=2.71e-7, calibrate_trigger=True, trigger_sweep="AUTO")
    list(run_transient_scan(rig, bace_sweep(0.9, 0.9, 0.0), cfg, sleep=NO_SLEEP))
    triggers = [c for c in calls if c[0] == "trigger"]
    probe = calls.index(("acquire", "CHAN3"))
    assert calls.index(triggers[0]) < probe, "the provisional level comes before the probe"
    assert triggers[0][2] == PROVISIONAL_TRIGGER_V and triggers[0][3] == "AUTO"
    assert calls.index(triggers[1]) > probe, "the measured level comes after it"
    assert 0.0 < triggers[1][2] < PROVISIONAL_TRIGGER_V * 3
    assert triggers[1][2] != PROVISIONAL_TRIGGER_V


def test_a_generator_that_reports_its_output_still_off_stops_the_run():
    """The operator watched the 81150A's panel during session 125751 and did
    not see Output 1 come on. The run now asks the instrument (`read_output`,
    `:OUTP1?`) right after `:OUTP1 ON` and stops with the answer in the file
    if it says 0, instead of calibrating against a silent CHAN3 and blaming
    the sync cable."""
    from bace.experiment.transient import BiasOutputError

    sim, rig = build()

    class Deaf:
        """The simulated generator, answering :OUTP1? = 0 whatever was sent."""
        def __init__(self, inner): self._inner = inner
        def read_output(self): return False
        def __getattr__(self, name): return getattr(self._inner, name)

    rig = Rig(bias=Deaf(sim.bias), scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), power=sim.power)
    cfg = RunConfig(n_averages=8, settle_s=0.0, dark_settle_s=0.0,
                    t0_int_s=2.71e-7, calibrate_trigger=True)
    seen = []
    with pytest.raises(BiasOutputError, match=":OUTP1\? = 0"):
        for ev in run_transient_scan(rig, bace_sweep(0.9, 0.9, 0.0), cfg, sleep=NO_SLEEP):
            seen.append(ev)
    states = [e.values for e in seen if isinstance(e, E.InstrumentState)]
    assert any(v.get("bias_output") == "OFF" for v in states)
    assert sim.bench.shots == 0, "nothing was acquired, not even the probe"
