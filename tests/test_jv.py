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
                                metrics, run_jv)
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


def test_dark_means_the_led_output_is_off_not_merely_low():
    """A sub-threshold level is right for the transient, where the generator has
    to keep producing a waveform. For a dark J-V there is nothing to keep."""
    sim, rig = build()
    seen = []
    for ev in run_jv(rig, JVConfig(dark=True, led_levels_v=()), sleep=NO_SLEEP):
        if isinstance(ev, JVCurveDone):
            seen.append((sim.bench.led_mode, sim.led.output_enabled))
    assert seen == [("OFF", False)]


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


def test_walking_away_leaves_the_sourcemeter_and_led_off():
    sim, rig = build()
    gen = run_jv(rig, JVConfig(dark=True, led_levels_v=(1.020, 1.060)),
                 sleep=NO_SLEEP)
    next(gen); next(gen)
    gen.close()
    assert sim.smu.output_enabled is False
    assert sim.led.output_enabled is False


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
