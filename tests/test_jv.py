"""The J–V module, standalone.

A J–V scan is a measurement in its own right here, so these tests run it with a
Rig that has only a SourceMeter and an LED — no scope, no bias generator, no
shutter. If it needed them, that would be the bug.
"""
from __future__ import annotations

import numpy as np
import pytest

from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment.jv import (JVConfig, JVCurveDone, JVFinished, JVStarted,
                                illumination_state, metrics, run_jv)
from bace.experiment.rig import Rig, RigConfig
from bace.storage.jv import JVRecorder, record

NO_SLEEP = lambda s: None


def build(with_router=True):
    sim = make_bench(seed=4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led,
              router=sim.router if with_router else None)
    return sim, rig


def run(rig, cfg):
    return list(run_jv(rig, cfg, sleep=NO_SLEEP))


# -- it stands alone ------------------------------------------------------
def test_a_jv_scan_needs_only_a_sourcemeter():
    """No scope, no pulse generator, no shutter — this is a separate
    experiment, not a preamble to a transient."""
    sim = make_bench(seed=1)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led)
    evs = run(rig, JVConfig(dark=True, led_levels_v=()))
    assert isinstance(evs[-1], JVFinished)
    assert sim.bench.shots == 0            # the scope was never touched


def test_without_a_sourcemeter_it_says_so():
    sim = make_bench()
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig())
    with pytest.raises(RuntimeError, match="needs a SourceMeter"):
        list(run_jv(rig, JVConfig(), sleep=NO_SLEEP))


def test_nothing_to_measure_is_an_error_not_an_empty_run():
    _, rig = build()
    with pytest.raises(ValueError, match="nothing to measure"):
        list(run_jv(rig, JVConfig(dark=False, led_levels_v=()), sleep=NO_SLEEP))


# -- dark and light -------------------------------------------------------
def test_dark_and_light_land_in_one_run():
    _, rig = build()
    evs = run(rig, JVConfig(dark=True, led_levels_v=(1.020,)))
    curves = [e for e in evs if isinstance(e, JVCurveDone)]
    assert [c.dark for c in curves] == [True, False]
    assert curves[0].label == "dark" and curves[1].label == "1.02 V"


def test_dark_means_the_shutter_shut_and_the_led_left_exactly_as_it_was():
    """Operator instruction, 2026-09-02: do not switch the LED generator off;
    use the shutter when no light is wanted. A generator that is cycled loses
    its thermal steady state and the next module waits for it again. So a
    dark curve on a rig with a shutter leaves the LED as the last module set
    it (here: lit, at DC) and the curve is dark because the shutter is shut
    -- which the simulated SourceMeter now sees: its J_sc is nothing."""
    sim, rig = build()
    sim.led.set_dc(1.020)
    sim.led.enable_output(True)          # as a light run before this left it
    seen = []
    for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=()), sleep=NO_SLEEP):
        if isinstance(ev, JVCurveDone):
            seen.append((sim.bench.led_mode, sim.led.output_enabled, sim.bench.shutter_open))
            assert abs(ev.metrics.jsc) < 1e-5, "dark through the shutter, not through the LED"
    assert seen == [("DC", True, False)]
    assert sim.led.output_enabled and sim.bench.led_mode == "DC", "left as it was"
    assert sim.bench.shutter_open is False


def test_without_a_shutter_dark_still_means_the_led_output_off():
    """The exception to the rule above: a rig with no shutter has no other
    way to be dark, so the LED output is switched off -- off, not merely a
    low level, because an output that is off cannot leak."""
    sim = make_bench(seed=4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=None, config=RigConfig(),
              smu=sim.smu, led=sim.led)
    sim.led.set_dc(1.020)
    sim.led.enable_output(True)
    seen = []
    for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=()), sleep=NO_SLEEP):
        if isinstance(ev, JVCurveDone):
            seen.append((sim.bench.led_mode, sim.led.output_enabled))
    assert seen == [("OFF", False)]
    assert sim.led.output_enabled is False, "and off on the way out"


def test_a_dark_curve_reports_no_voc():
    """A dark curve still crosses zero — on noise, or on the shunt. Calling that
    a V_oc invents a plausible number from nothing."""
    _, rig = build()
    evs = run(rig, JVConfig(dark=True, led_levels_v=(1.020,)))
    dark, light = [e for e in evs if isinstance(e, JVCurveDone)]
    assert dark.metrics.voc is None
    assert dark.metrics.fill_factor is None
    assert light.metrics.voc is not None


def test_the_light_curve_agrees_with_the_voc_the_transient_axis_would_use():
    """The simulator derives its diode saturation current from the same V_oc the
    transient run centres on, so the two cannot drift apart. If this ever fails,
    a J-V V_oc and a BACE V_oc are describing different devices."""
    sim, rig = build()
    evs = run(rig, JVConfig(dark=False, led_levels_v=(1.020,), step_v=0.005))
    light = [e for e in evs if isinstance(e, JVCurveDone)][0]
    assert light.metrics.voc == pytest.approx(sim.bench.device.voc(1.020), abs=0.005)


def test_metrics_are_plausible_for_a_working_cell():
    _, rig = build()
    c = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,), step_v=0.005))
         if isinstance(e, JVCurveDone)][0]
    m = c.metrics
    assert m.jsc < 0                              # photocurrent, as measured
    assert 0.3 < m.fill_factor < 0.95
    assert 0.0 < m.v_mpp < m.voc
    assert m.p_max == pytest.approx(abs(m.v_mpp * m.j_mpp), rel=1e-9)


# -- hysteresis -----------------------------------------------------------
def test_both_directions_is_opt_in_and_doubles_the_curves():
    _, rig = build()
    one = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,)))
           if isinstance(e, JVCurveDone)]
    both = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,),
                                         both_directions=True))
            if isinstance(e, JVCurveDone)]
    assert [c.direction for c in one] == ["forward"]
    assert [c.direction for c in both] == ["forward", "reverse"]
    assert both[1].voltage[0] > both[1].voltage[-1]


# -- current density ------------------------------------------------------
def test_density_is_omitted_rather_than_defaulted_to_one_square_centimetre():
    """Defaulting the area silently mislabels A as A/cm²."""
    _, rig = build()
    c = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,)))
         if isinstance(e, JVCurveDone)][0]
    assert c.density is None

    c2 = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,),
                                       pixel_area_cm2=0.04))
          if isinstance(e, JVCurveDone)][0]
    np.testing.assert_allclose(c2.density, c2.current / 0.04)


# -- compliance -----------------------------------------------------------
def test_a_compliance_set_too_low_clips_the_curve_visibly():
    """Which is what the instrument does. Better a flat top than a quietly
    wrong fill factor."""
    sim, rig = build()
    sim.smu.compliance_a = 1e-5
    c = [e for e in run(rig, JVConfig(dark=False, led_levels_v=(1.020,)))
         if isinstance(e, JVCurveDone)][0]
    assert sim.smu.clipped is True
    assert np.abs(c.current).max() == pytest.approx(1e-5, rel=1e-6)


# -- safety and unwinding -------------------------------------------------
def test_the_sweep_happens_on_the_sourcemeter_side_of_the_relay():
    sim, rig = build(with_router=True)
    positions = []
    for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=()), sleep=NO_SLEEP):
        if isinstance(ev, JVCurveDone):
            positions.append(sim.router.position)
    assert positions == ["sourcemeter"]


def test_walking_away_leaves_the_sourcemeter_off_the_shutter_shut_and_the_led_as_set():
    """The unwind parks what is the run's to park: the SourceMeter off and
    the shutter shut. The LED is left as it was set (2026-09-02: the shutter
    is the light switch), so whatever runs next finds a generator that has
    kept its steady state."""
    sim, rig = build()
    gen = run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020, 1.060)),
                 sleep=NO_SLEEP)
    for ev in gen:
        if isinstance(ev, JVCurveDone) and not ev.dark:
            break
    assert sim.bench.shutter_open is True            # mid light curve
    gen.close()
    assert sim.smu.output_enabled is False
    assert sim.bench.shutter_open is False
    assert sim.led.output_enabled is True and sim.bench.led_mode == "DC"
    assert sim.bench.led_drive_v == 1.020


def test_abort_stops_before_the_next_illumination():
    sim, rig = build()
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 2

    evs = list(run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020, 1.060)),
                      abort=abort, sleep=NO_SLEEP))
    assert isinstance(evs[-1], E.Notice)
    assert len([e for e in evs if isinstance(e, JVCurveDone)]) == 2


# -- the shutter ----------------------------------------------------------
def test_a_light_curve_opens_the_shutter_and_a_dark_curve_shuts_it():
    """Recorded gap (the design pack 01-modules section 4, docs/ui-rules.md): `run_jv` never touched the
    shutter, so a light J-V taken with it shut was a dark J-V wearing a light
    label. This pins the sequence: the shutter position at the moment each
    sweep is taken, the `InstrumentState` the run yields for a console or
    recorder to pick up (`JVRecorder` writes it, see the storage tests), and
    the shutter shut afterwards. (The simulated SourceMeter sees the shutter
    since 2026-09-02, so the dark curve's J_sc says the same thing.)"""
    sim, rig = build()
    seen, states = [], []
    for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020, 1.060)),
                     sleep=NO_SLEEP):
        if isinstance(ev, E.InstrumentState):
            states.append(ev.values["shutter"])
        elif isinstance(ev, JVCurveDone):
            seen.append((ev.label, sim.bench.shutter_open))
    assert seen == [("dark", False), ("1.02 V", True), ("1.06 V", True)]
    assert states == ["shut", "open", "open"]
    assert sim.bench.shutter_open is False, "shut on the way out"


def test_the_shutter_moves_before_the_illumination_settles():
    """The settle exists so the device reaches its steady state under the
    light it is about to be measured in. A shutter that opens after it makes
    the settle count for nothing."""
    sim, rig = build()
    log = []

    class Watched:
        def __init__(self, inner): self._i = inner
        def unblock(self): log.append("open"); return self._i.unblock()
        def shut(self): log.append("shut"); return self._i.shut()
        def __getattr__(self, n): return getattr(self._i, n)

    rig.shutter = Watched(sim.shutter)
    list(run_jv(rig, JVConfig(dark=False, led_levels_v=(1.020,), led_settle_s=0.5),
                sleep=lambda s: log.append(("sleep", s))))
    assert log[:2] == ["open", ("sleep", 0.5)]
    assert log[-1] == "shut"


def test_walking_away_mid_light_curve_shuts_the_shutter():
    sim, rig = build()
    gen = run_jv(rig, JVConfig(dark=False, led_levels_v=(1.020, 1.060)),
                 sleep=NO_SLEEP)
    for ev in gen:
        if isinstance(ev, JVCurveDone):
            break
    assert sim.bench.shutter_open is True            # mid-run, lit
    gen.close()
    assert sim.bench.shutter_open is False


def test_a_bare_sourcemeter_rig_does_not_wait_for_an_led_it_has_not_got():
    """`led_settle_s` is the time the device takes to reach steady state after
    the illumination changed. With no LED and no shutter nothing changed, and
    the run used to skip the wait (it lived inside `_set_illumination`, behind
    the `rig.led is None` return). Moving the shutter into the run must not
    turn that into a silent 2 s per curve."""
    sim = make_bench(seed=4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=None, config=RigConfig(),
              smu=sim.smu, led=None)
    slept = []
    evs = list(run_jv(rig, JVConfig(dark=True, led_levels_v=(), led_settle_s=2.0),
                      sleep=lambda s: slept.append(s)))
    assert isinstance(evs[-1], JVFinished)
    assert slept == []
    assert not any(isinstance(e, E.InstrumentState) for e in evs)


def test_a_shutter_alone_still_earns_the_settle():
    """A rig with a shutter and no LED still changes the light on the device
    when the shutter moves, so the settle is spent."""
    sim = make_bench(seed=4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=None)
    slept = []
    list(run_jv(rig, JVConfig(dark=True, led_levels_v=(), led_settle_s=0.5),
                sleep=lambda s: slept.append(s)))
    assert slept == [0.5]


def test_an_led_that_will_not_switch_off_stops_a_dark_curve_on_a_shutterless_rig():
    """On a rig with no shutter the LED output off is the only dark there
    is. Before 2026-09-02 a failing `off()` on the dark branch was swallowed
    and the sweep went ahead under whatever the LED was doing -- a dark curve
    taken lit, labelled dark. Now it stops the run before any curve is taken,
    and the unwind still parks everything it can: the SourceMeter off, and
    the LED off on the retry. (A rig with a shutter never calls `off()`:
    the shutter is its light switch.)"""
    sim = make_bench(seed=4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=None, config=RigConfig(),
              smu=sim.smu, led=sim.led)
    sim.led.set_dc(1.020)
    sim.led.enable_output(True)          # lit going in, as after a light run
    calls = {"off": 0}

    class Flaky:
        def __init__(self, inner): self._i = inner
        def off(self):
            calls["off"] += 1
            if calls["off"] == 1:
                raise OSError("VISA timeout on :OUTP 0")
            return self._i.off()
        def __getattr__(self, n): return getattr(self._i, n)

    rig.led = Flaky(sim.led)
    seen = []
    with pytest.raises(OSError, match="OUTP 0"):
        for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020,)),
                         sleep=NO_SLEEP):
            seen.append(ev)
    assert not any(isinstance(e, JVCurveDone) for e in seen)
    assert calls["off"] == 2             # the unwind tried again, and succeeded
    assert sim.led.output_enabled is False
    assert sim.smu.output_enabled is False


def test_a_rig_with_a_shutter_never_calls_off_on_the_led():
    """The whole of a dark-and-light run, and its unwind, without one `off()`:
    the operator's instruction of 2026-09-02, checked on the transcript."""
    sim, rig = build()
    log = []

    class Watched:
        def __init__(self, inner): self._i = inner
        def off(self): log.append("off"); return self._i.off()
        def set_dc(self, v): log.append(("dc", v)); return self._i.set_dc(v)
        def enable_output(self, on=True): log.append(("enable", on)); return self._i.enable_output(on)
        def __getattr__(self, n): return getattr(self._i, n)

    rig.led = Watched(sim.led)
    evs = run(rig, JVConfig(dark=True, led_levels_v=(1.020,)))
    assert isinstance(evs[-1], JVFinished)
    assert log == [("dc", 1.020), ("enable", True)]
    assert sim.bench.shutter_open is False and sim.led.output_enabled


# -- storage --------------------------------------------------------------
def test_a_recorded_jv_run_writes_both_flat_files_and_an_hdf5(tmp_path):
    import os
    _, rig = build()
    rec = JVRecorder(str(tmp_path), "20260831_120000", metadata={"sample": "sim"},
                     rig_config=rig.config.as_dict())
    list(record(run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020,),
                                     both_directions=True, pixel_area_cm2=0.04),
                       sleep=NO_SLEEP), rec))
    names = sorted(os.path.basename(p) for p in rec.written)
    assert any(n.startswith("BACE_JV_Data_") for n in names)
    assert any(n.startswith("BACE_JV_Parameters_") for n in names)
    assert any(n.endswith(".h5") for n in names)

    data = open([p for p in rec.written if "JV_Data" in p][0], "rb").read().decode()
    head = data.split("\r\n")[0]
    assert "dark fwd" in head and "1.02 V rev" in head
    assert "J/A cm-2" in head


def test_an_interrupted_jv_series_still_writes_what_completed(tmp_path):
    _, rig = build()
    rec = JVRecorder(str(tmp_path), "20260831_130000", metadata={},
                     rig_config=rig.config.as_dict())
    gen = record(run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020, 1.060)),
                        sleep=NO_SLEEP), rec)
    for ev in gen:
        if isinstance(ev, JVCurveDone) and not ev.dark:
            gen.close()
            break
    assert rec.written
    assert len(rec.curves) == 2


def test_the_file_says_which_curves_were_taken_with_the_shutter_open(tmp_path):
    """A light J-V taken with the shutter shut is a dark J-V wearing a light
    label, and until 2026-09-02 nothing in the file could tell the two
    apart. Schema bace-jv/2 folds the shutter `InstrumentState` into
    /config/resolved and onto every curve group -- per curve, because one
    file holds a dark and a light curve and the last value alone would
    mislabel one of them."""
    import h5py
    _, rig = build()
    rec = JVRecorder(str(tmp_path), "20260902_150000", metadata={},
                     rig_config=rig.config.as_dict(),
                     resolved={"led_output_polarity": "INV"})
    list(record(run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020,)),
                       sleep=NO_SLEEP), rec))
    with h5py.File([p for p in rec.written if p.endswith(".h5")][0], "r") as f:
        assert f.attrs["schema"] == "bace-jv/3"
        assert f["config/resolved"].attrs["led_output_polarity"] == "INV"
        assert f["config/resolved"].attrs["shutter"] == "open"   # the last one
        by_name = {n: f["curves"][n].attrs["shutter"] for n in f["curves"]}
        assert [by_name[n] for n in sorted(by_name)] == ["shut", "open"]


def test_hdf5_keeps_dark_and_light_together(tmp_path):
    import h5py
    _, rig = build()
    rec = JVRecorder(str(tmp_path), "20260831_140000", metadata={},
                     rig_config=rig.config.as_dict())
    list(record(run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020,)),
                       sleep=NO_SLEEP), rec))
    with h5py.File([p for p in rec.written if p.endswith(".h5")][0], "r") as f:
        names = sorted(f["curves"])
        assert len(names) == 2
        assert any("dark" in n for n in names)
        assert f["curves"][names[0]].attrs["dark"]


# -- light_control="leave": the light is not this run's -------------------
def _leave(**kw):
    # The default sweep, so the lit curve actually reaches V_oc: the point of
    # several of these is that an unknown curve gets the same metrics a lit
    # one does, and a range that crosses nothing would prove it by accident.
    return JVConfig(light_control="leave", **kw)


def test_leave_touches_neither_shutter_nor_led_going_in_or_coming_out():
    """The whole of the `jv` module is this test. A run that sets no light has
    no business unwinding one, so the shutter is where it was found on both
    sides of the sweep -- including the `finally`, which under `manage` shuts
    it whatever happened."""
    sim, rig = build()
    rig.shutter.unblock()
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    before = (rig.shutter.is_open, rig.led.mode, rig.led.output_enabled)

    events = run(rig, _leave())
    assert (rig.shutter.is_open, rig.led.mode, rig.led.output_enabled) == before
    assert rig.shutter.is_open, "the shutter was open before the run and stays open"

    # And the same on the abort path, which is the one that goes through
    # `finally` with the run unfinished.
    gen = run_jv(rig, _leave(), sleep=NO_SLEEP)
    next(gen)
    gen.close()
    assert rig.shutter.is_open

    curves = [e for e in events if isinstance(e, JVCurveDone)]
    assert len(curves) == 1, "one curve: there is no dark-plus-levels plan"
    assert curves[0].dark is False and curves[0].led_level_v == 1.02
    assert curves[0].label == "as found 1.02 V"
    assert curves[0].metrics.voc is not None, "a lit curve reports its V_oc"


def test_leave_reads_the_dark_it_finds_rather_than_making_one():
    sim, rig = build()
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    rig.shutter.shut()
    curves = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)]
    assert curves[0].dark is True and curves[0].label == "as found dark"
    assert curves[0].illumination["shutter"] == "shut"
    assert curves[0].metrics.voc is None, "a curve read as dark gets the dark metrics"


def test_a_bench_that_cannot_say_gets_unknown_and_a_warning_never_dark():
    """`None` is not `False`. A rig with no shutter cannot know whether light
    reaches the sample however confident the generator is, and a curve
    labelled dark there would be the failure `ui-rules` §9 is about.

    The generator has to be *on* for this to be the unknown case: an LED that
    is off is proof of dark on its own, shutter or no shutter, and answering
    `unknown` there would be its own kind of wrong."""
    sim = make_bench(seed=5)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=None,
              config=RigConfig(), smu=sim.smu, led=sim.led)
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    events = run(rig, _leave())
    curve = [e for e in events if isinstance(e, JVCurveDone)][0]
    assert curve.dark is None and curve.label == "as found unknown"
    assert curve.illumination["lit"] is None
    warnings = [e for e in events if isinstance(e, E.Notice) and e.level == "warning"]
    assert any("unknown" in w.text and "not as dark" in w.text for w in warnings)
    # Unknown gets the full metric set: V_oc and FF are interpolations of the
    # curve either way, and withholding them would hide the operator's own
    # evidence that the light was on.
    assert curve.metrics.voc is not None


def test_leave_refuses_led_levels_because_a_level_is_a_request_to_set_the_light():
    with pytest.raises(ValueError, match="cannot take led_levels_v"):
        JVConfig(light_control="leave", led_levels_v=(1.02,))
    with pytest.raises(ValueError, match="must be 'manage' or 'leave'"):
        JVConfig(light_control="off")


def test_illumination_state_needs_all_three_to_say_lit():
    sim, rig = build()
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    rig.shutter.unblock()
    assert illumination_state(rig)["lit"] is True
    rig.shutter.shut()
    assert illumination_state(rig)["lit"] is False, "shutter shut is dark, LED or not"
    rig.shutter.unblock()
    rig.led.off()
    assert illumination_state(rig)["lit"] is False, "LED off is dark, shutter or not"


def test_an_unknown_curve_has_no_dark_attribute_in_the_file(tmp_path):
    """`bace-jv/3`: absent, not False. A reader that asks for `dark` on an
    unknown curve gets a KeyError, which is loud; a False would have been a
    dark label on a curve nobody read."""
    import h5py
    sim = make_bench(seed=6)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=None,
              config=RigConfig(), smu=sim.smu, led=sim.led)
    rig.led.set_dc(1.02)                # on: an LED that is off proves dark
    rig.led.enable_output(True)
    rec = JVRecorder(str(tmp_path), "20260903_120000", metadata={},
                     rig_config=rig.config.as_dict())
    list(record(run_jv(rig, _leave(), sleep=NO_SLEEP), rec))
    with h5py.File([p for p in rec.written if p.endswith(".h5")][0], "r") as f:
        assert f.attrs["schema"] == "bace-jv/3"
        group = f["curves"][sorted(f["curves"])[0]]
        assert group.attrs["illumination"] == "unknown"
        assert "dark" not in group.attrs


def test_a_driver_that_was_asked_and_would_not_answer_is_unread_not_cached():
    """`Agilent33220A.read_state()` answers `output: None` when `:OUTP?` goes
    unanswered and `mode: "?"` when `FUNC:SHAP?` does, while the instance still
    holds the flags it last set. Reading the cache there turns "the generator
    did not reply" into a measurement -- and the cache is most likely to be
    stale exactly when it matters, after somebody used the front panel.

    The `"?"` half is the sharper one: `"?" != "OFF"` is true, so an unread
    mode used to count *towards* lit."""
    sim, rig = build()
    rig.shutter.unblock()

    class Deaf:
        """Answers read_state, and read_state answers nothing."""

        mode = "DC"                     # the stale cache, saying the LED is on
        output_enabled = True
        last_levels = (1.02, None)

        def __init__(self, **state):
            self._state = {"output": None, "mode": "?", "high_v": 1.02, **state}

        def read_state(self):
            return dict(self._state)

    rig.led = Deaf()
    state = illumination_state(rig)
    assert state["lit"] is None, "no output read-back is not a light reading"
    assert state["led_output"] is None and state["led_mode"] is None
    assert any("OUTP?" in u for u in state["unread"])
    assert any("FUNC:SHAP?" in u for u in state["unread"])

    # Half an answer is still not an answer.
    rig.led = Deaf(output=True)
    assert illumination_state(rig)["lit"] is None, "the mode is still unread"
    rig.led = Deaf(mode="DC")
    assert illumination_state(rig)["lit"] is None, "the output is still unread"

    # Both read: now it is a reading.
    rig.led = Deaf(output=True, mode="DC")
    got = illumination_state(rig)
    assert got["lit"] is True and got["unread"] == []

    # And the curve follows: unknown, never dark.
    rig.led = Deaf()
    curve = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)][0]
    assert curve.dark is None and curve.label == "as found unknown"


def test_a_driver_with_no_read_state_still_uses_its_own_flags():
    """The simulator, and any driver that never claimed to read back: its
    flags are the only account there is, and using them is not a claim about
    an instrument someone may have touched."""
    sim, rig = build()
    rig.shutter.unblock()
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    assert not hasattr(rig.led, "read_state"), "the simulated LED has no read-back"
    assert illumination_state(rig)["lit"] is True


def test_the_shutter_line_is_read_not_the_cache_that_starts_at_none():
    """`is_open` is `self._state == OPEN`, and `_state` is None on a fresh
    open -- it only knows what *this* object has set. So after a service
    restart, or any change made at the bench, `is_open` reads False on a
    physically open shutter, and turning that into "shut" is a dark label on a
    lit curve. `read_line()` asks the module; the bench snapshot has always
    preferred it and this must too."""
    sim, rig = build()
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)

    class Restarted:
        """A DIO shutter this process has not touched: the cache says shut
        (it starts as None), the line says the shutter is open."""

        is_open = False                 # `_state == OPEN` with `_state = None`
        line: int | None = 1

        def read_line(self):
            return self.line

    rig.shutter = Restarted()
    assert illumination_state(rig)["shutter"] == "open", "the line, not the cache"
    curve = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)][0]
    assert curve.dark is False, "a lit curve, and it would have been labelled dark"

    # And a read-back that will not answer is unread, not the cache either.
    rig.shutter.line = None
    state = illumination_state(rig)
    assert state["shutter"] is None and state["lit"] is None
    assert any("would not read back" in u for u in state["unread"])


def test_a_dc_level_is_the_offset_not_a_stale_pulse_amplitude():
    """`set_dc` writes `:VOLT:OFFS`, and `read_state` reports that separately
    from `high_v`, which in DC still holds whatever the last pulse left.
    Labelling a DC J-V with `high_v` puts the wrong number in the file *and*
    registers the V_oc at that level -- and the level is exactly what the
    coupling check compares."""
    sim, rig = build()
    rig.shutter.unblock()

    class Generator:
        output_enabled, mode = True, "DC"

        def __init__(self, **state):
            self._state = state

        def read_state(self):
            return dict(self._state)

    # DC at 1.02, with 1.30 left in the amplitude registers by an earlier pulse.
    rig.led = Generator(output=True, mode="DC", offset_v=1.02, high_v=1.30)
    got = illumination_state(rig)
    assert got["led_level_v"] == 1.02, "the DC offset is the level"
    curve = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)][0]
    assert curve.label == "as found 1.02 V" and curve.led_level_v == 1.02

    # In PULSE the high level is the level, as before.
    rig.led = Generator(output=True, mode="PULSE", offset_v=0.71, high_v=1.02)
    assert illumination_state(rig)["led_level_v"] == 1.02

    # And there is no falling back between the two. `:VOLT:OFFS?` unanswered
    # while `:VOLT:HIGH?` answers would have recorded the stale pulse
    # amplitude *as the DC drive* -- the same "unread is not cached" mistake
    # in a new place, and the first version of this fix made it.
    rig.led = Generator(output=True, mode="DC", high_v=1.30)
    got = illumination_state(rig)
    assert got["led_level_v"] is None, "an unread level is absent, not the other register"
    assert got["lit"] is True, "but the light is still known to be reaching the sample"
    curve = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)][0]
    assert curve.label == "as found lit" and curve.dark is False
    assert curve.led_level_v is None


def test_one_proof_of_dark_is_enough_and_light_needs_all_three():
    """The shutter *is* the light switch: shut means no light reaches the
    sample whatever the generator does, and an LED that is off or in OFF mode
    means there is none to reach it whatever the shutter does. Any one of
    those, definitively read, settles the question — where claiming *light*
    still needs all three.

    The all-or-nothing gate this replaced made a supported arrangement
    useless: `light(shutter="shut")` on a bench with no LED is explicitly
    allowed, and the `jv` after it came out `unknown`, with the full non-dark
    metric set, though the closed shutter proved it dark."""
    sim, rig = build()

    # A shut shutter, and no LED at all to ask about.
    rig.led = None
    rig.shutter.shut()
    got = illumination_state(rig)
    assert got["lit"] is False, "shut is dark, LED or no LED"
    curve = [e for e in run(rig, _leave()) if isinstance(e, JVCurveDone)][0]
    assert curve.dark is True and curve.label == "as found dark"
    assert curve.metrics.voc is None, "and it gets the dark metrics"

    # An LED that is off, with no shutter to ask about.
    sim, rig = build()
    rig.shutter = None
    rig.led.off()
    assert illumination_state(rig)["lit"] is False, "off is dark, shutter or no shutter"

    # But claiming *light* still needs all three: LED on, shutter unreadable.
    sim, rig = build()
    rig.shutter = None
    rig.led.set_dc(1.02)
    rig.led.enable_output(True)
    assert illumination_state(rig)["lit"] is None
