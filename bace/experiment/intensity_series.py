"""The intensity series couples DC characterisation to transient scans.

At each LED level it measures V_oc, J_sc and J_sat under steady illumination,
then pulses the LED at the same level and scans about that V_oc. V_oc depends
on illumination, so the axis requires the DC result before it can be built.

The source meter and bias amplifier share the device node. `Router` refuses
to move while either source reports an enabled output; this module does not
write relay lines directly. `bace.core.illumination.assert_axis_centre` checks
that the DC drive level equals the pulse high level. An axis centred on V_oc
with DC measurement disabled is refused before instrument access.

Nested transient events pass through unchanged as one event stream. Cleanup
disables the sources before moving the relay and shuts the shutter. The LED
keeps running: cycling its generator loses thermal steady state and requires
another settle. A rig without a shutter cannot run this series.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterator

import numpy as np

from ..core.axis import ScanSpec
from ..core.illumination import IlluminationError, LedDrive, assert_axis_centre
from ..drivers.protocols import DCPoint
from .events import DCMeasured, Event, Notice, Progress
from .rig import Rig
from .transient import RunConfig, run_transient_scan


class SeriesError(RuntimeError):
    pass


@dataclass(frozen=True)
class SeriesConfig:
    """The series configuration defines LED levels and DC characterisation."""

    led_start_v: float = 1.020
    led_stop_v: float = 1.020
    led_step_v: float = 0.020
    led_low_v: float = 0.4
    """The 33220A pulse low level driving the LED, in V.

    It must stay below the LED turn-on threshold so the dark phase contains no
    light. `LedDrive` refuses a level at or above that threshold.
    """

    led_settle_s: float = 2.0
    """How long the run waits after changing the 33220A LED drive mode before DC or transient measurements, in s.

    It applies before both DC and transient measurements. The device must reach
    its illuminated steady state before V_oc is read; a short wait centres the
    axis on a transient voltage.
    """

    measure_dc: bool = True
    v_sat: float = -1.0
    dc_settle_s: float | None = None
    pixel_area_cm2: float = 0.0

    intensity_factor_mw_cm2_per_w: float = 0.0
    """Converts the power meter's W to irradiance at the sample using the beam splitter ratio and illuminated area, in mW/cm² per W.

    It depends on the beam splitter ratio and illuminated area. Zero denotes
    an unknown calibration and writes `NaN` in the intensity column. A nonzero
    uncalibrated value would label an unsupported irradiance as measured.
    """

    read_intensity: bool = True
    intensity_samples: int = 0
    """Zero or one reads a single power value; larger counts request the 1918-C's mean and sample standard deviation over that many meter-clock samples.

    Zero or one reads a single value. A count above one requests the mean and
    sample standard deviation over that many meter samples.
    """

    def levels(self) -> np.ndarray:
        if self.led_start_v == self.led_stop_v:
            return np.array([self.led_start_v], dtype=float)
        if self.led_step_v <= 0:
            raise SeriesError("led_step_v must be positive for a range of levels")
        n = int(round(abs(self.led_stop_v - self.led_start_v) / self.led_step_v)) + 1
        return np.linspace(self.led_start_v, self.led_stop_v, n)

    def as_dict(self) -> dict:
        return asdict(self)


# -- events ---------------------------------------------------------------
@dataclass(frozen=True)
class SeriesStarted(Event):
    levels: np.ndarray
    n_levels: int
    config: dict


@dataclass(frozen=True)
class IlluminationSet(Event):
    index: int
    level_v: float
    mode: str                       # `dc` or `pulse`


@dataclass(frozen=True)
class IntensityMeasured(Event):
    level_v: float
    watts: float
    watts_std: float | None
    trustworthy: bool


@dataclass(frozen=True)
class SeriesPointDone(Event):
    """The series point contains the results at one LED level.

    `level_v`, `voc` and `vcoll` are in V, `delay_ns` in ns, Q arrays in C and
    `intensity_w` in W. `dc` and `intensity_w` are absent when not measured.
    """

    index: int
    level_v: float
    dc: DCPoint | None
    voc: float | None
    axis_values: np.ndarray
    q_mean: np.ndarray
    q_std: np.ndarray
    intensity_w: float | None
    vcoll: float
    delay_ns: float


@dataclass(frozen=True)
class SeriesFinished(Event):
    points: tuple[SeriesPointDone, ...]
    elapsed_s: float


AbortCheck = Callable[[], bool]
Sleep = Callable[[float], None]


# -- preflight ------------------------------------------------------------
def preflight(rig: Rig, series: SeriesConfig, spec: ScanSpec) -> list[str]:
    """Preflight returns configuration problems without instrument access.

    `run_intensity_series` calls it before starting: a missing source, unsafe
    routing or an undefined V_oc axis must fail before a long run begins.
    """
    problems: list[str] = []
    if rig.smu is None:
        problems.append("no SourceMeter: V_oc cannot be measured, so the "
                        "prebias axis cannot be centred")
    if rig.led is None:
        problems.append("no LED source: the illumination series has nothing to sweep")
    if rig.router is None:
        problems.append(
            "no Router. The SourceMeter and the amplifier share the device node; "
            "without the relay both would be connected at once")
    if spec.axis.centre_on_voc and not series.measure_dc:
        problems.append(
            "the axis is centred on V_oc but measure_dc is off — the axis would "
            "be centred on nothing")
    try:
        LedDrive(level=float(series.levels()[0]), low_level=series.led_low_v,
                 threshold_v=rig.config.led_threshold_v)
    except IlluminationError as exc:
        problems.append(str(exc))
    except SeriesError as exc:
        problems.append(str(exc))
    return problems


# -- the run --------------------------------------------------------------
def run_intensity_series(rig: Rig, series: SeriesConfig, spec: ScanSpec,
                         run: RunConfig = RunConfig(), *,
                         abort: AbortCheck | None = None,
                         sleep: Sleep = time.sleep) -> Iterator[Event]:
    """The series run yields DC results and a transient scan at each LED level.

    V_oc is measured under the same drive level as the shots. Invalid
    configuration raises `SeriesError` before instrument access. Cleanup attempts
    to disable both sources, shut the shutter and park the router on every exit.
    """
    problems = preflight(rig, series, spec)
    if problems:
        raise SeriesError("cannot start the series:\n  - " + "\n  - ".join(problems))

    levels = series.levels()
    started = time.monotonic()
    yield SeriesStarted(levels=levels, n_levels=int(levels.size),
                        config={"series": series.as_dict(),
                                "run": run.as_dict(),
                                "rig": rig.config.as_dict()})

    points: list[SeriesPointDone] = []
    try:
        for i, level in enumerate(levels):
            if abort is not None and abort():
                yield Notice("warning", f"series aborted before {level:g} V")
                return
            level = float(level)

            drive = LedDrive(level=level, low_level=series.led_low_v,
                             frequency_hz=run.pulse_frequency_hz,
                             duty_percent=run.duty_percent,
                             threshold_v=rig.config.led_threshold_v)

            # -- steady illumination, DC characterisation ------------------
            # The DC measurement needs light at the sample, not just an enabled
            # LED. A shut shutter gives a dark voltage under an illuminated label.
            # The shutter closes before the transient scan, which opens it for
            # each light trace.
            rig.led.set_dc(drive.dc_settings()["offset"])
            rig.led.enable_output(True)
            rig.shutter.unblock()
            yield IlluminationSet(index=i, level_v=level, mode="dc")
            sleep(series.led_settle_s)

            intensity = None
            if series.read_intensity and rig.power is not None:
                intensity = yield from _read_intensity(rig, series, level)

            dc: DCPoint | None = None
            try:
                if series.measure_dc:
                    with rig.router.dc():
                        dc = rig.smu.measure_dc(v_sat=series.v_sat,
                                                settle_s=series.dc_settle_s)
            finally:
                rig.shutter.shut()
            if dc is not None:
                yield DCMeasured(led_drive_v=level, dc=dc)

            voc = dc.voc if dc is not None else None
            if spec.axis.centre_on_voc:
                # V_oc must correspond to the same LED drive as the shots.
                assert_axis_centre(drive, voc_measured_at=level)

            # -- pulsed illumination, transient scan -----------------------
            p = drive.pulse_settings()
            rig.led.set_pulse(p["high"], p["low"], frequency_hz=p["frequency"],
                              duty_percent=p["duty"])
            rig.led.enable_output(True)
            yield IlluminationSet(index=i, level_v=level, mode="pulse")
            sleep(series.led_settle_s)

            q_mean = q_std = values = np.empty(0)
            with rig.router.transient():
                for ev in run_transient_scan(rig, spec, run, voc=voc,
                                             abort=abort, sleep=sleep):
                    yield ev
                    if type(ev).__name__ == "RunFinished":
                        values, q_mean, q_std = ev.values, ev.q_mean, ev.q_std
                    elif type(ev).__name__ == "RunAborted":
                        yield Notice("warning",
                                     f"transient run aborted at {level:g} V; "
                                     "stopping the series")
                        return

            point = SeriesPointDone(index=i, level_v=level, dc=dc, voc=voc,
                                    axis_values=values, q_mean=q_mean, q_std=q_std,
                                    intensity_w=intensity,
                                    vcoll=spec.vcoll, delay_ns=spec.delay_ns)
            points.append(point)
            yield point
            yield Progress(done=i + 1, total=int(levels.size),
                           elapsed_s=time.monotonic() - started,
                           eta_s=((time.monotonic() - started) / (i + 1)
                                  * (levels.size - i - 1)))

        yield SeriesFinished(points=tuple(points),
                             elapsed_s=time.monotonic() - started)
    finally:
        # Cleanup covers completion, abort, exception and generator close.
        # Sources must be disabled before relay motion; the interlock refuses
        # to move while either source is enabled.
        for dev in (rig.bias, rig.smu):
            if dev is not None:
                try:
                    dev.disable_output()
                except Exception:
                    pass
        try:
            rig.shutter.shut()
        except Exception:
            pass
        if rig.led is not None and rig.shutter is None:
            # Without a shutter, disabling the LED is the only way to remove
            # light. With a shutter, the LED keeps its mode and thermal steady
            # state for the next module.
            try:
                rig.led.off()
            except Exception:
                pass
        if rig.router is not None:
            try:
                rig.router.park()
            except Exception:
                pass


def _read_intensity(rig: Rig, series: SeriesConfig, level: float):
    """The intensity read yields meter power in W with its trust flag.

    A failed read emits a warning and returns `None`. Saturated, overrange or
    wrong-unit readings remain in the event stream with a warning because using
    them for normalisation biases the result.
    """
    watts = std = None
    try:
        if series.intensity_samples > 1:
            watts, std = rig.power.read_statistics(series.intensity_samples)
        else:
            watts = rig.power.read_power()
    except Exception as exc:
        yield Notice("warning", f"intensity not read at {level:g} V: {exc}")
        return None
    last = getattr(rig.power, "last", None)
    ok = True if last is None else bool(getattr(last, "trustworthy", True))
    if not ok:
        yield Notice("warning",
                     f"intensity reading at {level:g} V is not trustworthy "
                     "(saturated, overrange, or the meter is not in watts) — "
                     "every normalisation using it will be biased")
    yield IntensityMeasured(level_v=level, watts=watts, watts_std=std,
                            trustworthy=ok)
    return watts
