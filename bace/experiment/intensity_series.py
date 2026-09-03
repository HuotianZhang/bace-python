"""The intensity series — the wrapper, and the only place a mistake reaches the sample.

For each LED drive level: measure V_oc, J_sc and J_sat on the SourceMeter under
steady illumination, switch the LED to its pulsed mode at the *same* level, and
run a transient scan whose prebias axis is centred on the V_oc just measured.
That last dependency is the whole reason this layer exists — V_oc moves with
illumination, so the axis cannot be built until the light is on and the
SourceMeter has read it.

Three things here can damage hardware or silently ruin data, and each is guarded
rather than documented:

1. **The relay.** The SourceMeter and the x4 amplifier share the device node.
   Every move goes through `Router`, which refuses while either source reports
   its output enabled, and this module never touches a relay line directly.
2. **The coupling invariant.** The DC level used for V_oc and the pulse high
   level used for the transient must be the same number. `core.illumination`
   holds the check; this module feeds it the level it actually measured at, not
   the level it intended to.
3. **A stale V_oc.** An axis centred on V_oc with the DC measurement switched
   off would centre on nothing. That is refused before the first instrument is
   touched, not discovered at step 40.

Events from the nested transient runs are forwarded unchanged, so one consumer
sees the whole series as a single stream.

The LED generator is never switched off here: the unwind shuts the shutter,
which is the light switch on this rig, and leaves the 33220A as it was set
(operator instruction, 2026-09-02 -- a generator that is cycled loses its
thermal steady state and the next run waits for it again). Only a rig with no
shutter, which this series cannot run on anyway, would still switch it off.
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
    """The illumination sweep that wraps the transient measurement."""

    led_start_v: float = 1.020
    led_stop_v: float = 1.020
    led_step_v: float = 0.020
    led_low_v: float = 0.4
    """The 33220A pulse low level. Must be below the LED's turn-on threshold, so
    the dark half of the cycle really is dark; `LedDrive` refuses otherwise."""

    led_settle_s: float = 2.0
    """The original's 'LED stab. time', which had its own panel control — a hint
    that it matters. The device must reach its light steady state before V_oc is
    read, or the axis is centred on a transient."""

    measure_dc: bool = True
    v_sat: float = -1.0
    dc_settle_s: float | None = None
    pixel_area_cm2: float = 0.0

    intensity_factor_mw_cm2_per_w: float = 0.0
    """Converts the meter's watts into the irradiance at the sample, which
    depends on the beam splitter ratio and the illuminated area. The original
    had a 'Factor' control for this and its value is not recoverable from the
    binaries, so 0 means 'unknown' and the intensity column is written as NaN
    rather than as a number that looks calibrated and is not."""

    read_intensity: bool = True
    intensity_samples: int = 0
    """0 reads a single value; >1 asks the meter for mean and sample std over
    that many samples, taken on its own clock."""

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
    mode: str                       # "dc" or "pulse"


@dataclass(frozen=True)
class IntensityMeasured(Event):
    level_v: float
    watts: float
    watts_std: float | None
    trustworthy: bool


@dataclass(frozen=True)
class SeriesPointDone(Event):
    """One illumination level, complete."""

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
    """Everything that can be checked before an instrument is touched.

    Returns the list of problems. Called by `run_intensity_series` before it
    does anything, because a series is an hours-long run and finding out at
    step 40 that the axis was never centred is the expensive way to learn it.
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
    """Sweep illumination, characterise, and run a transient scan at each level."""
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
            # The shutter is opened for it: an LED that is on is not light
            # at the sample, and a V_oc read with the shutter shut (as the
            # transient's park leaves it) is the dark V_oc wearing the
            # level's name. Shut again before the transient, which opens
            # it per shot for the light trace only.
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
                # The guard that catches a V_oc measured at a different drive.
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
        # Runs on completion, abort, exception and walk-away. Order matters:
        # the sources come down before anything else, because the relay
        # interlock will refuse to act while one is live.
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
            # Only where there is no shutter to make the dark. With one, the
            # LED keeps its mode and its thermal steady state for whatever
            # runs next; the shutter just shut is what makes the bench dark.
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
    """Read the meter, and report a bad reading rather than hiding it."""
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
