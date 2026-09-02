"""The wrapper — the only layer where a software mistake reaches the sample.

So most of these are safety tests, and they are written as assertions about
what the instruments were actually told, not about what the code intended.
"""
from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pytest

from bace.core.axis import Axis, ScanSpec, bace_at_voc, bace_legacy_three_point
from bace.core.illumination import IlluminationError
from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment import intensity_series as S
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig
from bace.storage.naming import RunMetadata
from bace.storage.series import SeriesRecorder, record

NO_SLEEP = lambda s: None
RUN = RunConfig(n_averages=32, settle_s=0.0, dark_settle_s=0.0, record_length=400)


class WatchfulRouter:
    """Wraps the simulated router and records the state of both sources at the
    instant the relay is asked to move. The interlock is the thing under test,
    so it is not enough that nothing raised."""

    def __init__(self, inner, bias, smu):
        self.inner, self.bias, self.smu = inner, bias, smu
        self.moves: list[tuple[str, bool, bool]] = []

    def _snapshot(self, where):
        self.moves.append((where, self.bias.output_enabled, self.smu.output_enabled))

    def dc(self):
        self._snapshot("dc")
        return self.inner.dc()

    def transient(self):
        self._snapshot("transient")
        return self.inner.transient()

    def park(self):
        return self.inner.park()

    @property
    def position(self):
        return self.inner.position


def build(watch=False):
    sim = make_bench(seed=6)
    router = sim.router
    if watch:
        router = WatchfulRouter(sim.router, sim.bias, sim.smu)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led, power=sim.power,
              router=router)
    return sim, rig, router


def run(rig, series, spec, **kw):
    return list(S.run_intensity_series(rig, series, spec, RUN, sleep=NO_SLEEP, **kw))


ONE_LEVEL = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.020, led_settle_s=0.0)
TWO_LEVELS = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.060,
                            led_step_v=0.040, led_settle_s=0.0)


# -- preflight ------------------------------------------------------------
def test_a_missing_router_is_refused_before_anything_is_touched():
    sim = make_bench()
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led)
    with pytest.raises(S.SeriesError, match="share the device node"):
        run(rig, ONE_LEVEL, bace_at_voc(1))
    assert sim.bench.shots == 0


def test_an_axis_centred_on_voc_without_a_dc_measurement_is_refused():
    """It would centre on nothing — and it would look like a normal run."""
    _, rig, _ = build()
    series = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.020,
                            led_settle_s=0.0, measure_dc=False)
    with pytest.raises(S.SeriesError, match="centred on nothing"):
        run(rig, series, bace_at_voc(1))


def test_an_axis_not_centred_on_voc_may_skip_the_dc_measurement():
    _, rig, _ = build()
    series = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.020,
                            led_settle_s=0.0, measure_dc=False)
    spec = ScanSpec(axis=Axis("vpre", 0.9, 0.9), n_loops=1)
    evs = run(rig, series, spec)
    assert isinstance(evs[-1], S.SeriesFinished)
    assert not [e for e in evs if isinstance(e, E.DCMeasured)]


def test_a_low_level_above_the_led_threshold_is_refused():
    _, rig, _ = build()
    series = S.SeriesConfig(led_start_v=1.020, led_stop_v=1.020,
                            led_low_v=1.2, led_settle_s=0.0)
    with pytest.raises(S.SeriesError, match="not below the LED threshold"):
        run(rig, series, bace_at_voc(1))


# -- the relay ------------------------------------------------------------
def test_the_relay_never_moves_while_a_source_is_driving():
    """The one software mistake on this rig that costs hardware."""
    _, rig, router = build(watch=True)
    run(rig, TWO_LEVELS, bace_at_voc(2))
    assert router.moves, "the relay was never asked to move"
    for where, bias_live, smu_live in router.moves:
        assert not bias_live and not smu_live, (
            f"relay asked to move to {where} with bias={bias_live} smu={smu_live}")


def test_the_transient_runs_on_the_amplifier_and_the_dc_on_the_sourcemeter():
    sim, rig, _ = build()
    seen = []
    for ev in S.run_intensity_series(rig, ONE_LEVEL, bace_at_voc(1), RUN,
                                     sleep=NO_SLEEP):
        if isinstance(ev, E.DCMeasured):
            seen.append(("dc", sim.router.position))
        elif isinstance(ev, E.StepDone):
            seen.append(("shot", sim.router.position))
    assert ("dc", "sourcemeter") in seen
    assert ("shot", "amplifier") in seen


# -- the coupling invariant ----------------------------------------------
def test_voc_is_measured_at_the_level_the_transient_then_uses():
    """A V_oc from a previous intensity would centre the axis on the wrong
    number and bias every charge, with no symptom in the data."""
    sim, rig, _ = build()
    levels, vocs, axis_centres = [], [], []
    for ev in S.run_intensity_series(rig, TWO_LEVELS, bace_at_voc(1), RUN,
                                     sleep=NO_SLEEP):
        if isinstance(ev, E.DCMeasured):
            levels.append(ev.led_drive_v)
            vocs.append(ev.dc.voc)
        elif isinstance(ev, E.AxisResolved):
            axis_centres.append(float(ev.values[0]))
    assert levels == [1.020, 1.060]
    assert axis_centres == pytest.approx(vocs)
    # and the two V_oc genuinely differ, so a stale one would have been caught
    assert abs(vocs[1] - vocs[0]) > 0.02


def test_the_axis_moves_with_illumination():
    sim, rig, _ = build()
    evs = run(rig, TWO_LEVELS, bace_at_voc(1))
    pts = [e for e in evs if isinstance(e, S.SeriesPointDone)]
    assert pts[1].voc > pts[0].voc
    assert pts[1].axis_values[0] > pts[0].axis_values[0]


def test_more_light_extracts_more_charge():
    """Not a physics claim about a real device — a check that the illumination
    actually reached the shot, rather than the series changing only a label."""
    _, rig, _ = build()
    pts = [e for e in run(rig, TWO_LEVELS, bace_at_voc(2))
           if isinstance(e, S.SeriesPointDone)]
    # abs(): Q carries the rig's sign convention (negative), so "more charge"
    # is larger in magnitude, not larger on the number line.
    assert abs(pts[1].q_mean[0]) > abs(pts[0].q_mean[0])


# -- illumination sequencing ---------------------------------------------
def test_dc_then_pulse_at_each_level():
    _, rig, _ = build()
    modes = [(e.level_v, e.mode) for e in run(rig, TWO_LEVELS, bace_at_voc(1))
             if isinstance(e, S.IlluminationSet)]
    assert modes == [(1.020, "dc"), (1.020, "pulse"),
                     (1.060, "dc"), (1.060, "pulse")]


def test_an_untrustworthy_intensity_reading_is_surfaced():
    class Saturated:
        class last:
            trustworthy = False
        def read_power(self):
            return 8.4e-4
        def read_statistics(self, n, **kw):
            return 8.4e-4, 0.0
        def set_wavelength(self, nm):
            pass

    _, rig, _ = build()
    rig.power = Saturated()
    notices = [e.text for e in run(rig, ONE_LEVEL, bace_at_voc(1))
               if isinstance(e, E.Notice)]
    assert any("not trustworthy" in t for t in notices)


# -- unwinding ------------------------------------------------------------
def test_everything_is_parked_when_the_series_finishes():
    """Parked means the sources off and the shutter shut. The LED is left
    pulsing at the last level: since 2026-09-02 the shutter is the light
    switch, and a generator that is cycled has to be waited for again."""
    sim, rig, _ = build()
    run(rig, TWO_LEVELS, bace_at_voc(1))
    assert sim.bench.bias_output is False
    assert sim.bench.smu_output is False
    assert sim.bench.shutter_open is False
    assert sim.led.output_enabled is True and sim.bench.led_mode == "PULSE"


def test_walking_away_mid_series_parks_everything():
    sim, rig, _ = build()
    gen = S.run_intensity_series(rig, TWO_LEVELS, bace_at_voc(3), RUN,
                                 sleep=NO_SLEEP)
    for _ in range(8):
        next(gen)
    gen.close()
    assert sim.bench.bias_output is False
    assert sim.bench.smu_output is False
    assert sim.bench.shutter_open is False
    assert sim.led.output_enabled is True, "left as set; the shutter made the dark"


def test_an_instrument_failure_mid_series_parks_everything():
    sim, rig, _ = build()
    real = sim.scope.acquire
    n = {"i": 0}

    def flaky(*a, **kw):
        n["i"] += 1
        if n["i"] == 3:
            raise RuntimeError("scope fell over")
        return real(*a, **kw)

    sim.scope.acquire = flaky
    with pytest.raises(RuntimeError, match="fell over"):
        run(rig, TWO_LEVELS, bace_at_voc(2))
    assert sim.bench.bias_output is False
    assert sim.bench.smu_output is False
    assert sim.bench.shutter_open is False
    assert sim.led.output_enabled is True, "left as set; the shutter made the dark"


def test_abort_stops_between_levels():
    _, rig, _ = build()
    n = {"i": 0}

    def abort():
        n["i"] += 1
        return n["i"] > 4

    evs = run(rig, TWO_LEVELS, bace_at_voc(2), abort=abort)
    assert not [e for e in evs if isinstance(e, S.SeriesFinished)]
    assert any(isinstance(e, (E.RunAborted, E.Notice)) for e in evs)


# -- storage --------------------------------------------------------------
def _meta():
    return RunMetadata(sample="s9", material="SIM", pixel="pxa", temperature_k=290,
                       started=datetime(2026, 8, 31, 21, 0, 0))


def test_each_illumination_point_gets_a_complete_run_folder(tmp_path):
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta(), pixel_area_cm2=0.04)
    list(record(S.run_intensity_series(rig, TWO_LEVELS, bace_at_voc(2), RUN,
                                       sleep=NO_SLEEP), rec))
    subfolders = sorted(d for d in os.listdir(rec.folder)
                        if os.path.isdir(os.path.join(rec.folder, d)))
    assert len(subfolders) == 2
    for d in subfolders:
        names = os.listdir(os.path.join(rec.folder, d))
        assert any(n.startswith("1_averagesQ") for n in names)
        assert any(n.startswith("4_averagesLightCurrent") for n in names)
        assert any(n.endswith(".h5") for n in names)
    # each folder is named with the V_oc measured at its own level
    assert "1020mVLED" in subfolders[0] and "1060mVLED" in subfolders[1]


def test_the_long_summary_has_one_row_per_level_and_axis_point(tmp_path):
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta())
    list(record(S.run_intensity_series(rig, TWO_LEVELS,
                                       bace_legacy_three_point(dv=0.001), RUN,
                                       sleep=NO_SLEEP), rec))
    path = [p for p in rec.written if os.path.basename(p).startswith("series_")
            and p.endswith(".dat")][0]
    lines = [l for l in open(path, "rb").read().decode().split("\r\n") if l]
    assert len(lines) == 1 + 2 * 3


def test_the_legacy_summary_is_written_only_for_a_three_point_axis(tmp_path):
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta(), pixel_area_cm2=0.04)
    list(record(S.run_intensity_series(rig, ONE_LEVEL,
                                       bace_legacy_three_point(dv=0.001), RUN,
                                       sleep=NO_SLEEP), rec))
    assert any("LED_Voc_Jsc_BACE Parameters" in p for p in rec.written)

    rec2 = SeriesRecorder(str(tmp_path / "b"), _meta())
    list(record(S.run_intensity_series(rig, ONE_LEVEL, bace_at_voc(2), RUN,
                                       sleep=NO_SLEEP), rec2))
    assert not any("LED_Voc_Jsc" in p for p in rec2.written)


def test_uncalibrated_columns_are_nan_not_a_guess(tmp_path):
    """J_sc in mA/cm² needs the pixel area, and the intensity column needs a
    factor nobody recorded. A number that looks calibrated and is not is worse
    than a gap."""
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta())          # no area, no factor
    list(record(S.run_intensity_series(rig, ONE_LEVEL,
                                       bace_legacy_three_point(dv=0.001), RUN,
                                       sleep=NO_SLEEP), rec))
    text = open([p for p in rec.written if "LED_Voc_Jsc" in p][0],
                "rb").read().decode()
    row = text.split("\r\n")[2].split("\t")
    assert row[1].strip() == "NaN"          # Jsc [mA/cm2]
    assert row[-1].strip() == "NaN"         # LED Intensity [mW/cm^2]


def test_an_interrupted_series_still_writes_the_points_that_completed(tmp_path):
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta())
    gen = record(S.run_intensity_series(rig, TWO_LEVELS, bace_at_voc(2), RUN,
                                        sleep=NO_SLEEP), rec)
    for ev in gen:
        if isinstance(ev, S.SeriesPointDone):
            gen.close()
            break
    assert len(rec.points) == 1
    assert any(os.path.basename(p).startswith("series_") for p in rec.written)


def test_the_series_hdf5_holds_every_level(tmp_path):
    import h5py
    _, rig, _ = build()
    rec = SeriesRecorder(str(tmp_path), _meta())
    list(record(S.run_intensity_series(rig, TWO_LEVELS, bace_at_voc(2), RUN,
                                       sleep=NO_SLEEP), rec))
    with h5py.File([p for p in rec.written if p.endswith("h5")
                    and "series_" in os.path.basename(p)][0], "r") as f:
        assert f.attrs["n_levels"] == 2
        np.testing.assert_allclose(f["levels/led_v"][()], [1.020, 1.060])
        assert f["charge/mean"].shape == (2, 1)
