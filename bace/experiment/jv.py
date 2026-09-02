"""Current–voltage characterisation, standalone.

A J–V scan is a measurement in its own right, not a preamble to a transient, so
this module runs with or without BACE. It needs only the SourceMeter; the LED
and the relay are used when they are present, and their absence just means "the
illumination is whatever it is" and "nothing else is contending for the device".

What it does that the original did not:

* **Dark and light in one run.** The original swept the Keithley under whatever
  the 33220A happened to be doing. Here `dark` and the LED levels are explicit,
  the LED output is actually switched off for a dark scan rather than set low,
  the shutter is opened for a light curve and shut for a dark one, and both
  land in the same file set so the pair is kept together — a dark curve is
  only useful next to the light curve it belongs to.
* **Both sweep directions, optionally.** Forward and reverse curves that differ
  is hysteresis, which for many device chemistries is the most interesting thing
  in the measurement and is invisible if you only ever sweep one way. Off by
  default so it stays a deliberate choice, because it doubles the time and
  changes what "the" curve means.
* **Derived numbers are clearly derived.** V_oc, J_sc, FF and the maximum power
  point are computed and reported, because they are what anyone looks at first
  and V_oc is needed by BACE anyway. They are interpolations of the measured
  curve, not the measurement — the curve is the deliverable.

The routing rule is the same as everywhere: the SourceMeter and the amplifier
share the device node, so the sweep happens inside `router.dc()` when a router
is present, and the interlock refuses to move the relay while either source is
live.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterator, Literal

import numpy as np

from .events import Event, InstrumentState, Notice, Progress
from .rig import Rig

Direction = Literal["forward", "reverse"]


@dataclass(frozen=True)
class JVConfig:
    """One J–V measurement."""

    start_v: float = -0.2
    stop_v: float = 1.2
    step_v: float = 0.02
    settle_s: float = 0.05
    """Delay at each point before the reading. Too short and the curve is a
    picture of the RC of the cell, not of its steady state."""

    both_directions: bool = False
    """Sweep start->stop and back. Hysteresis is real and worth seeing; it also
    doubles the run and makes 'the' curve ambiguous, so it is opt-in."""

    dark: bool = True
    """Include a dark scan with the LED output off."""

    led_levels_v: tuple[float, ...] = ()
    """Drive levels for the light scans, at the 33220A output. Empty means no
    light scan — a dark-only run."""

    pixel_area_cm2: float = 0.0
    """0 means report current in amps and leave density out. Better than
    defaulting to 1 cm², which silently mislabels A as A/cm²."""

    led_settle_s: float = 2.0
    """After changing the LED level. The original called this 'LED stab. time'
    and gave it its own control, which is a hint that it matters."""

    def points(self) -> np.ndarray:
        if self.step_v <= 0:
            raise ValueError("step_v must be positive")
        n = int(round(abs(self.stop_v - self.start_v) / self.step_v)) + 1
        return np.linspace(self.start_v, self.stop_v, n)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["led_levels_v"] = list(self.led_levels_v)
        return d


# -- derived quantities ---------------------------------------------------
@dataclass(frozen=True)
class JVMetrics:
    """Interpolated from the measured curve. Not measurements themselves.

    Sign convention: `current` is what the instrument reported, so an
    illuminated cell gives negative current in the power-producing quadrant.
    `p_max` is reported as a positive extracted power.
    """

    voc: float | None
    jsc: float | None            # current at 0 V, in A (or A/cm² if area given)
    p_max: float | None
    v_mpp: float | None
    j_mpp: float | None
    fill_factor: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _interp_zero_crossing(x: np.ndarray, y: np.ndarray) -> float | None:
    """First x where y crosses zero, linearly interpolated.

    The original used LabVIEW's `Threshold 1D Array` + `Interpolate 1D Array`,
    which is this. Returns None when the curve never crosses — a dark scan, or
    a light scan that did not reach V_oc — rather than extrapolating a number
    that would look like a measurement.
    """
    y = np.asarray(y, dtype=float)
    sign = np.sign(y)
    idx = np.nonzero((sign[:-1] * sign[1:]) < 0)[0]
    if idx.size == 0:
        return None
    i = int(idx[0])
    y0, y1 = y[i], y[i + 1]
    return float(x[i] + (x[i + 1] - x[i]) * (-y0) / (y1 - y0))


def metrics(voltage: np.ndarray, current: np.ndarray, *,
            dark: bool = False) -> JVMetrics:
    """Photovoltaic figures from a measured curve.

    `dark=True` reports only the current at 0 V. A dark curve still crosses zero
    somewhere — on noise, or on the shunt — and calling that crossing a V_oc
    produces a plausible number from nothing. The caller knows whether the light
    was on, so it says so rather than having this function guess.
    """
    v = np.asarray(voltage, dtype=float)
    i = np.asarray(current, dtype=float)
    order = np.argsort(v)
    v, i = v[order], i[order]

    jsc = float(np.interp(0.0, v, i)) if v.min() <= 0.0 <= v.max() else None
    if dark:
        return JVMetrics(None, jsc, None, None, None, None)
    voc = _interp_zero_crossing(v, i)

    power = -v * i                      # positive where the cell delivers power
    k = int(np.argmax(power))
    if power[k] <= 0 or voc is None or jsc is None:
        return JVMetrics(voc, jsc, None, None, None, None)
    p_max = float(power[k])
    ff = p_max / abs(voc * jsc) if voc and jsc else None
    return JVMetrics(voc=voc, jsc=jsc, p_max=p_max, v_mpp=float(v[k]),
                     j_mpp=float(i[k]), fill_factor=ff)


# -- events ---------------------------------------------------------------
@dataclass(frozen=True)
class JVStarted(Event):
    n_curves: int
    n_points: int
    config: dict


@dataclass(frozen=True)
class JVCurveDone(Event):
    """One sweep in one direction at one illumination."""

    index: int
    label: str                   # "dark" or "1.020 V"
    dark: bool
    led_level_v: float | None
    direction: Direction
    voltage: np.ndarray
    current: np.ndarray          # A, as the instrument reported it
    density: np.ndarray | None   # A/cm², only if a pixel area was given
    metrics: JVMetrics
    intensity_w: float | None = None


@dataclass(frozen=True)
class JVFinished(Event):
    curves: tuple[JVCurveDone, ...]
    elapsed_s: float


AbortCheck = Callable[[], bool]
Sleep = Callable[[float], None]


# -- the run --------------------------------------------------------------
def run_jv(rig: Rig, config: JVConfig = JVConfig(), *,
           abort: AbortCheck | None = None,
           sleep: Sleep = time.sleep) -> Iterator[Event]:
    """Sweep the SourceMeter, dark and/or at each LED level.

    Yields `JVStarted`, then per illumination an `InstrumentState` naming the
    shutter position (when the rig has a shutter) and a `JVCurveDone` per
    sweep, then `JVFinished`.
    Unwinds the same way the transient run does: the `finally` disables the
    SourceMeter, the LED and the shutter whether the run finished, was
    aborted, raised, or the consumer simply stopped iterating.
    """
    if rig.smu is None:
        raise RuntimeError(
            "a J-V scan needs a SourceMeter. Give the Rig one — it is the only "
            "instrument this experiment requires."
        )

    plan: list[tuple[bool, float | None]] = []
    if config.dark:
        plan.append((True, None))
    plan += [(False, lvl) for lvl in config.led_levels_v]
    if not plan:
        raise ValueError("nothing to measure: dark is off and no LED levels given")

    directions: tuple[Direction, ...] = (
        ("forward", "reverse") if config.both_directions else ("forward",))
    v_forward = config.points()
    total = len(plan) * len(directions)

    yield JVStarted(n_curves=total, n_points=int(v_forward.size),
                    config={"jv": config.as_dict(), "rig": rig.config.as_dict()})

    curves: list[JVCurveDone] = []
    started = time.monotonic()
    router_ctx = rig.router.dc() if rig.router is not None else _null()

    try:
        with router_ctx:
            index = 0
            for dark, level in plan:
                if abort is not None and abort():
                    yield Notice("warning", "J-V run aborted before "
                                            f"{'dark' if dark else f'{level} V'}")
                    return
                _set_illumination(rig, dark, level)
                shutter = _set_shutter(rig, dark)
                if shutter is not None:
                    # On the event stream, for a console or recorder to pick
                    # up: a light curve is only a light curve if light reached
                    # the sample. `JVRecorder` folds it into /config/resolved
                    # and onto each curve group (schema bace-jv/2), the way
                    # the transient recorder does, so the *file* can say.
                    yield InstrumentState({"shutter": shutter})
                if rig.led is not None or shutter is not None:
                    # Only when something moved. A bare SourceMeter rig has
                    # nothing to settle after, and before 2026-09-02 the
                    # settle lived inside `_set_illumination`, behind its
                    # `rig.led is None` return, so such a rig never waited;
                    # a silent 2 s per curve would be a regression nobody
                    # reports.
                    sleep(config.led_settle_s)
                label = "dark" if dark else f"{level:g} V"

                intensity = None
                if not dark and rig.power is not None:
                    try:
                        intensity = rig.power.read_power()
                    except Exception as exc:            # a meter is not the point
                        yield Notice("warning", f"intensity not read: {exc}")

                for direction in directions:
                    v = v_forward if direction == "forward" else v_forward[::-1]
                    vm, im = rig.smu.sweep(float(v[0]), float(v[-1]), int(v.size),
                                           settle_s=config.settle_s)
                    density = (im / config.pixel_area_cm2
                               if config.pixel_area_cm2 > 0 else None)
                    ev = JVCurveDone(index=index, label=label, dark=dark,
                                     led_level_v=level, direction=direction,
                                     voltage=vm, current=im, density=density,
                                     metrics=metrics(vm, im, dark=dark),
                                     intensity_w=intensity)
                    curves.append(ev)
                    yield ev
                    index += 1
                    yield Progress(done=index, total=total,
                                   elapsed_s=time.monotonic() - started,
                                   eta_s=None)

        yield JVFinished(curves=tuple(curves),
                         elapsed_s=time.monotonic() - started)
    finally:
        try:
            rig.smu.disable_output()
        except Exception:
            pass
        _led_off(rig)
        if rig.shutter is not None:
            try:
                rig.shutter.shut()
            except Exception:
                pass


def _set_illumination(rig: Rig, dark: bool, level: float | None) -> None:
    """Dark means the LED output *off*, not merely a low level.

    A sub-threshold level is right for the transient, where the generator has to
    keep producing a waveform. For a dark J-V there is nothing to keep, and an
    output that is off cannot leak.

    Loud, not swallowed: an `off()` that fails mid-run leaves the LED on under
    a curve labelled dark, which is worse than a stopped run.
    """
    if rig.led is None:
        return
    if dark:
        rig.led.off()
    else:
        rig.led.set_dc(float(level))
        rig.led.enable_output(True)


def _set_shutter(rig: Rig, dark: bool) -> str | None:
    """Open for a light curve, shut for a dark one. Returns the state for the
    `InstrumentState` the run yields; None when the rig has no shutter.

    Until 2026-09-02 `run_jv` never touched the shutter (recorded gap,
    ui-brief 01-modules section 4). An LED that is on is not the same as light
    reaching the sample, and only the shutter knows the difference: a light
    J-V taken with the shutter shut is a dark J-V wearing a light label --
    V_oc of nothing, J_sc of nothing, and a file that says "1.02 V". Moved
    before the illumination settle, so the settle is spent under the light
    the curve will be taken in.
    """
    if rig.shutter is None:
        return None
    if dark:
        rig.shutter.shut()
        return "shut"
    rig.shutter.unblock()
    return "open"


def _led_off(rig: Rig) -> None:
    """The unwind: never raises, so a dead LED cannot mask the exception that
    is already propagating. `off()` is in the `LedSource` contract, so there
    is no fallback spelling to try."""
    if rig.led is None:
        return
    try:
        rig.led.off()
    except Exception:
        pass


class _null:
    def __enter__(self): return None
    def __exit__(self, *exc): return False
