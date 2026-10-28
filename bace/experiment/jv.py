"""The J–V measurement sweeps the source meter and reports curves and metrics.

It runs independently of BACE and requires only a source meter. Optional LED,
shutter and relay control define illumination and route the device safely.
Without them, illumination requires external control and the device must
have no competing source connected.

`light_control = "manage"` sets light levels and opens or shuts the shutter
for light or dark curves. The curves share a file set so their relationship
is explicit. The shutter controls light while the LED keeps running; cycling
the generator loses thermal steady state and requires another settle. Without
a shutter, a dark curve requires disabling the LED output.

`light_control = "leave"` changes neither LED nor shutter on entry or exit.
It supports illumination set by a preceding module or the operator.
`illumination_state` reads the bench to label the curve. An unread state
remains `unknown` and emits a warning instead of assuming light or dark.

Optional forward and reverse sweeps expose hysteresis but double acquisition
time. Derived V_oc, J_sc, fill factor and maximum power describe the measured
curve; interpolated quantities do not replace it.

The source meter and bias amplifier share the device node. A router encloses
the sweep in `Router.dc` and refuses relay motion while either source is
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
LightControl = Literal["manage", "leave"]


@dataclass(frozen=True)
class JVConfig:
    """The J–V configuration defines sweep points, illumination and timing."""

    start_v: float = -0.2
    """First bias the source meter sources at the device, in V."""

    stop_v: float = 1.2
    """Last bias the source meter sources at the device, in V.

    A light curve must extend beyond V_oc to include its crossing.
    `metrics` interpolates without extrapolation, so a sweep stopping short
    returns no V_oc.
    """

    step_v: float = 0.02
    """Spacing between the source meter's sweep points, in V.

    The point count is rounded from the span. Each point costs `settle_s` plus
    its measurement. Coarse spacing reduces curve resolution.
    """

    settle_s: float = 0.05
    """How long the source meter holds each bias before it takes the reading, in s.

    It costs time at every point. Too short a wait measures device RC settling
    instead of the steady-state curve.
    """

    both_directions: bool = False
    """Sweep the source meter from start to stop, then back from stop to start as a second, separate sweep with an open-circuit gap.

    Each direction is a separate `rig.smu.sweep_points` call. The driver resets
    before each leg and disables output after it, leaving the device at open
    circuit during reconfiguration. Differences therefore include hysteresis
    and relaxation during that gap; they do not represent continuous-sweep
    hysteresis alone. The option doubles acquisition time.
    """

    light_control: LightControl = "manage"
    """`manage` controls LED levels and the shutter for each curve; `leave` sweeps once under existing illumination without changing either.

    `manage` shuts the shutter for dark curves, sets LED levels for light curves
    and shuts the shutter on exit. `leave` touches neither, sweeps under the
    bench illumination and reads it back. There is no partial ownership mode.

    `dark` and `led_levels_v` describe managed curves only. Nonempty
    `led_levels_v` under `leave` is refused by the constructor; `dark` does not
    add a dark curve in that mode.
    """

    dark: bool = True
    """Include a managed dark scan with the shutter shut, or LED output off on a rig without a shutter.

    Without a shutter, the LED output is disabled instead. The flag applies
    only under `light_control = "manage"`.
    """

    led_levels_v: tuple[float, ...] = ()
    """Drive levels for managed light scans at the 33220A output, in V; empty means no light scan.

    An empty tuple adds no light curves and permits a dark-only run.
    """

    pixel_area_cm2: float = 0.0
    """Illuminated pixel area, in cm², by which the source meter's current is divided to obtain current density in mA/cm²; zero omits density.

    `current_density` divides by this area in cm². Zero omits density and reports
    current in A. Assuming 1 cm² would silently mislabel current with an
    unsupported area and a factor of one thousand.
    """

    led_settle_s: float = 2.0
    """How long the run waits after arranging each curve's illumination through the 33220A level, shutter or both before the source meter sweeps, in s.

    It applies after LED level or shutter changes, including closure for a dark
    curve. `_set_illumination` keeps the LED running for dark curves and
    `_set_shutter` removes the light. A rig with neither LED nor shutter skips
    the wait because nothing changes. `leave` also skips it. Too short a wait
    records an unsettled device.
    """

    def __post_init__(self) -> None:
        if self.light_control not in ("manage", "leave"):
            raise ValueError(
                f"light_control must be 'manage' or 'leave', not {self.light_control!r}")
        if self.light_control == "leave" and self.led_levels_v:
            raise ValueError(
                "light_control='leave' cannot take led_levels_v: asking for a level "
                "is asking to set the light. Use 'manage', or set the level with a "
                "light step before this one.")

    def points(self) -> np.ndarray:
        if self.step_v <= 0:
            raise ValueError("step_v must be positive")
        n = int(round(abs(self.stop_v - self.start_v) / self.step_v)) + 1
        return np.linspace(self.start_v, self.stop_v, n)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["led_levels_v"] = list(self.led_levels_v)
        return d


# -- current density ------------------------------------------------------
MA_PER_A = 1e3
"""The current conversion factor is mA per A."""


def current_density(current: np.ndarray | float,
                    area_cm2: float) -> np.ndarray | None:
    """The density conversion returns current in mA/cm², or `None` without an area.

    The file summary and console use mA/cm². Converting once beside the area
    keeps every consumer's numbers in the same unit, with no per-curve prefix
    choice or repeated factor of one thousand.

    `area_cm2 <= 0` means no area. Returning `None` lets the caller report A;
    assuming 1 cm² would give a plausible density wrong by the device area and
    the unit conversion.
    """
    if area_cm2 <= 0:
        return None
    return np.asarray(current, dtype=float) * MA_PER_A / area_cm2


# -- derived quantities ---------------------------------------------------
@dataclass(frozen=True)
class JVMetrics:
    """The J–V metrics contain quantities derived from the measured curve.

    V_oc and `v_mpp` are in V, `jsc` and `j_mpp` in A, `p_max` in W and
    `fill_factor` is dimensionless. V_oc and `jsc` are interpolated; maximum
    power uses a measured point. Current retains the instrument sign: an
    illuminated cell supplies negative current in the power-producing quadrant.
    `p_max` is positive for delivered power.
    """

    voc: float | None
    jsc: float | None            # Current at 0 V, in A; never a density.
                                 # `metrics` receives the current array
                                 # regardless of pixel area; density is
                                 # a separate result.
    p_max: float | None
    v_mpp: float | None
    j_mpp: float | None
    fill_factor: float | None

    def as_dict(self) -> dict:
        return asdict(self)


def _interp_zero_crossing(x: np.ndarray, y: np.ndarray) -> float | None:
    """The zero crossing returns the first sign-changing interval's interpolated x.

    Linear interpolation matches LabVIEW `Threshold 1D Array` followed by
    `Interpolate 1D Array`. It returns `None` if no adjacent values change sign,
    such as a dark curve or a light curve short of V_oc. It does not extrapolate
    an unsupported crossing.
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
    """The metrics function derives photovoltaic quantities from V and A arrays.

    `dark=True` reports only current at 0 V. A dark curve can cross zero because
    of noise or shunt current; labelling that crossing V_oc gives a plausible
    voltage without illuminated open circuit. The caller supplies the known
    illumination state rather than inferring it from the curve.
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
    """The completed curve contains one sweep direction at one illumination.

    `voltage` is in V, `current` in A, `density` in mA/cm² when an area is given,
    and `intensity_w` in W when measured.
    """

    index: int
    label: str                   # Curve label, including read-back state.
    dark: bool | None
    """The dark flag is `True` for dark, `False` for light and `None` for unknown.

    Only `light_control = "leave"` can produce `None` when bench read-back cannot
    establish illumination. Consumers must retain that distinction: unknown is
    not a measured light state or a measured dark state.
    """

    led_level_v: float | None
    direction: Direction
    voltage: np.ndarray
    current: np.ndarray          # A, as the instrument reported it
    density: np.ndarray | None   # mA/cm², with a supplied pixel area
    metrics: JVMetrics
    intensity_w: float | None = None
    illumination: dict | None = None
    """The illumination dictionary preserves `illumination_state` for a `leave` curve.

    It is `None` for a managed curve, where `dark` and `led_level_v` describe
    the requested illumination.
    """


@dataclass(frozen=True)
class JVPoint(Event):
    """The point event reports one J–V reading during acquisition.

    `k` counts from one through `of` on curve `index`. Repeating curve identity
    lets consumers associate a point after missing the start. Voltage is in V,
    current in A and density in mA/cm² when an area is given. The service streams
    points live without journalling them. `JVCurveDone` carries the whole curve,
    so reconnecting loses only the live view of its growth.
    """

    index: int
    k: int
    of: int
    label: str
    dark: bool | None
    led_level_v: float | None
    direction: Direction
    voltage: float
    current: float               # A, as the instrument reported it
    density: float | None        # mA/cm², with a supplied pixel area


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
    """The J–V run yields managed curves or one curve under external illumination.

    It emits `JVStarted`, an `InstrumentState` for illumination, `JVPoint` for
    each reading, `JVCurveDone` for each sweep and `JVFinished` on completion.

    The `finally` block attempts to disable the source meter on completion,
    abort, exception or generator close because the run enables its output.
    Under `manage`, cleanup shuts the shutter and keeps the LED running, or
    disables the LED if no shutter exists. Under `leave`, neither LED nor shutter
    changes; the module or operator that sets illumination retains control.
    """
    if rig.smu is None:
        raise RuntimeError(
            "a J-V scan needs a SourceMeter. Give the Rig one — it is the only "
            "instrument this experiment requires."
        )

    manage = config.light_control == "manage"
    # (dark, level) under `manage`; the single (None, None) of `leave` means
    # "whatever is there", and is resolved by reading rather than by setting.
    plan: list[tuple[bool | None, float | None]] = []
    if not manage:
        plan.append((None, None))
    else:
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
                                            f"{_planned(manage, dark, level)}")
                    return

                found: dict | None = None
                intensity = None
                if manage:
                    _set_illumination(rig, dark, level)
                    shutter = _set_shutter(rig, dark)
                    if shutter is not None:
                        # The recorder needs the shutter state to identify the curve's
                        # illumination. `JVRecorder` stores it in `/config/resolved` and
                        # on each curve group so the file carries the read-back.
                        yield InstrumentState({"shutter": shutter})
                    if rig.led is not None or shutter is not None:
                        # A rig with neither LED nor shutter changes no illumination.
                        # Waiting there would add time without a settling process.
                        sleep(config.led_settle_s)
                    label = "dark" if dark else f"{level:g} V"
                    if not dark and rig.power is not None:
                        try:
                            intensity = rig.power.read_power()
                        except Exception as exc:        # Power is optional for J–V.
                            yield Notice("warning", f"intensity not read: {exc}")
                else:
                    # A `leave` curve takes its label and recorded illumination
                    # from read-back because the run does not set the light.
                    found = illumination_state(rig)
                    dark = None if found["lit"] is None else not found["lit"]
                    level = found["led_level_v"]
                    intensity = found["intensity_w"]
                    label = _found_label(found)
                    yield InstrumentState({
                        "shutter": found["shutter"] or "?",
                        "illumination": ("unknown" if found["lit"] is None
                                         else "light" if found["lit"] else "dark"),
                        "led_mode": found["led_mode"] or "?",
                        "led_level_v": found["led_level_v"],
                        # `led_output` must accompany mode and level. Otherwise the
                        # console can retain the Start snapshot's stale output flag
                        # and show the LED off during an illuminated sweep.
                        "led_output": found["led_output"],
                    })
                    if found["lit"] is None:
                        yield Notice(
                            "warning",
                            "illumination unknown: this bench cannot say whether light "
                            "reached the sample (" + ", ".join(found["unread"]) + "). "
                            "The curve is recorded as unknown, not as dark.")

                for direction in directions:
                    v = v_forward if direction == "forward" else v_forward[::-1]
                    # Streaming each `sweep_points` reading preserves partial data,
                    # updates the console and permits abort between points.
                    vs: list[float] = []
                    cs: list[float] = []
                    for k, (vk, ik) in enumerate(
                            rig.smu.sweep_points(float(v[0]), float(v[-1]), int(v.size),
                                                 settle_s=config.settle_s), start=1):
                        vs.append(float(vk))
                        cs.append(float(ik))
                        dk = current_density(float(ik), config.pixel_area_cm2)
                        yield JVPoint(index=index, k=k, of=int(v.size), label=label,
                                      dark=dark, led_level_v=level, direction=direction,
                                      voltage=float(vk), current=float(ik),
                                      density=None if dk is None else float(dk))
                        if abort is not None and abort():
                            yield Notice("warning", f"J-V run aborted at point {k} of "
                                                    f"{int(v.size)} of {label}")
                            return
                    vm, im = np.asarray(vs, dtype=float), np.asarray(cs, dtype=float)
                    density = current_density(im, config.pixel_area_cm2)
                    ev = JVCurveDone(index=index, label=label, dark=dark,
                                     led_level_v=level, direction=direction,
                                     voltage=vm, current=im, density=density,
                                     # Unknown illumination retains derived metrics
                                     # as evidence about the curve. Only a known dark
                                     # curve suppresses V_oc and fill factor; hiding
                                     # them otherwise removes evidence of light.
                                     metrics=metrics(vm, im, dark=bool(dark)),
                                     intensity_w=intensity, illumination=found)
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
        if manage:
            if rig.shutter is not None:
                try:
                    rig.shutter.shut()
                except Exception:
                    pass
            else:
                _led_off(rig)


def _planned(manage: bool, dark: bool | None, level: float | None) -> str:
    """The planned label describes the next curve in an abort notice."""
    if not manage:
        return "the curve"
    return "dark" if dark else f"{level} V"


def illumination_state(rig: Rig) -> dict:
    """The illumination state reports whether light reaches the sample.

    Light requires an open shutter, an enabled LED output and an LED mode other
    than `OFF`. Any one definite blocking state proves dark. If no blocking
    state is established and a required reading is unavailable, `lit` is `None`:
    an enabled generator alone cannot establish illumination.

    The LED level comes from `read_state` when available and remains `None` if
    unread. In DC mode it is the offset voltage; in pulse mode it is the high
    level. An absent level does not make the three illumination checks unknown.

    A failed read-back does not fall back to cached flags, which can disagree
    with front-panel changes. `Agilent33220A.read_state` marks unread output as
    `None` and unread mode as `?`. Cached flags are used only when no read-back
    method exists, as in the simulator.
    """
    shutter, led = rig.shutter, rig.led
    out: dict = {"lit": None, "shutter": None, "led_output": None,
                 "led_mode": None, "led_level_v": None, "intensity_w": None,
                 "unread": []}

    if shutter is not None:
        # `read_line` queries the DIO module. Cached `is_open` can report
        # false after a process restart or an external change even when the
        # shutter is physically open. Using that cache would label a light
        # curve dark, so read-back takes precedence.
        line = None
        reader = getattr(shutter, "read_line", None)
        if callable(reader):
            try:
                line = reader()
            except Exception:                               # noqa: BLE001
                line = None
        if line is not None:
            out["shutter"] = "open" if bool(line) else "shut"
        elif callable(reader):
            # A failed read-back leaves the shutter unread. A cached flag
            # cannot substitute for the physical state.
            out["unread"].append("shutter (the line would not read back)")
        else:
            try:
                out["shutter"] = "open" if bool(shutter.is_open) else "shut"
            except Exception as exc:                        # noqa: BLE001
                out["unread"].append(f"shutter ({type(exc).__name__})")
    else:
        out["unread"].append("shutter (none on this bench)")

    if led is not None:
        state, asked = {}, False
        reader = getattr(led, "read_state", None)
        if callable(reader):
            asked = True
            try:
                state = dict(reader() or {})
            except Exception as exc:                        # noqa: BLE001
                out["unread"].append(f"LED ({type(exc).__name__})")
                state = {}

        if asked:
            # `None` and `?` indicate unanswered queries, not measured states.
            output, mode = state.get("output"), state.get("mode")
            if output is None:
                out["unread"].append("LED output (:OUTP? unanswered)")
            else:
                out["led_output"] = bool(output)
            if not mode or mode == "?":
                out["unread"].append("LED mode (FUNC:SHAP? unanswered)")
            else:
                out["led_mode"] = str(mode)
        else:
            # Without a read-back method, cached driver flags are the only
            # available state; they cannot verify external instrument changes.
            try:
                out["led_output"] = bool(led.output_enabled)
            except Exception as exc:                        # noqa: BLE001
                out["unread"].append(f"LED output ({type(exc).__name__})")
            try:
                mode = str(led.mode)
                if not mode or mode == "?":
                    out["unread"].append("LED mode (the driver will not say)")
                else:
                    out["led_mode"] = mode
            except Exception as exc:                        # noqa: BLE001
                out["unread"].append(f"LED mode ({type(exc).__name__})")
        # In DC mode, `set_dc` writes `:VOLT:OFFS`; `high_v` can retain a
        # pulse amplitude. A fallback from unread `offset_v` to `high_v`
        # would mislabel the curve and associate V_oc with a stale LED level.
        # An unread level remains absent and yields `as found lit` without a
        # number. Illumination still depends on shutter, output and mode,
        # so a missing level alone does not make `lit` unknown.
        mode_now = str(out["led_mode"] or "").upper()
        level = state.get("offset_v") if mode_now == "DC" else state.get("high_v")
        if level is None and not asked:
            # `last_levels` supplies the level only for drivers without
            # read-back, including the simulator.
            levels = getattr(led, "last_levels", None)
            level = levels[0] if levels else None
        try:
            out["led_level_v"] = None if level is None else float(level)
        except (TypeError, ValueError):
            out["led_level_v"] = None
    else:
        out["unread"].append("LED (none on this bench)")

    # Any definite blocking state proves dark: shutter shut, output off
    # or mode `OFF`. This permits a shutter-only rig to establish dark
    # without an LED driver. Light requires all three positive readings;
    # otherwise the curve must retain unknown illumination.
    if out["shutter"] == "shut" or out["led_output"] is False \
            or str(out["led_mode"] or "").upper() == "OFF":
        out["lit"] = False
    elif not out["unread"] and out["shutter"] and out["led_output"] is not None \
            and out["led_mode"]:
        out["lit"] = bool(out["shutter"] == "open" and out["led_output"]
                          and str(out["led_mode"]).upper() != "OFF")

    if rig.power is not None:
        try:
            out["intensity_w"] = float(rig.power.read_power())
        except Exception:                                   # noqa: BLE001
            pass                # Power is optional for J–V.
    return out


def _found_label(state: dict) -> str:
    """The read-back label identifies a curve measured under `leave`.

    It contains only letters, digits, dots and spaces because `bace.storage.jv`
    replaces spaces with underscores to form an HDF5 group name.
    """
    if state.get("lit") is None:
        return "as found unknown"
    if not state["lit"]:
        return "as found dark"
    level = state.get("led_level_v")
    return "as found lit" if level is None else f"as found {level:g} V"


def _set_illumination(rig: Rig, dark: bool, level: float | None) -> None:
    """The illumination setter applies DC light levels or preserves the running LED.

    A light curve enables the LED at `level` in V. A dark curve uses
    `_set_shutter` instead: cycling the generator loses thermal steady state and
    a constant settle time can be too short for the next module. The shutter
    removes light without that thermal cost.

    Without a shutter, disabling the LED is the only way to make a dark curve.
    An `off` failure propagates in that case; suppressing it could record a light
    curve labelled dark.
    """
    if rig.led is None:
        return
    if dark:
        if rig.shutter is None:
            rig.led.off()
    else:
        rig.led.set_dc(float(level))
        rig.led.enable_output(True)


def _set_shutter(rig: Rig, dark: bool) -> str | None:
    """The shutter setter opens for light, shuts for dark and returns the state.

    The state enters `InstrumentState`; no shutter returns `None`. An enabled
    LED does not establish light at the sample: a shut shutter gives a dark
    curve with invalid illuminated V_oc and J_sc labels. Shutter motion precedes
    settling so the device settles under the illumination used for the curve.
    """
    if rig.shutter is None:
        return None
    if dark:
        rig.shutter.shut()
        return "shut"
    rig.shutter.unblock()
    return "open"


def _led_off(rig: Rig) -> None:
    """The LED cleanup attempts to disable light when no shutter exists.

    It suppresses failures so they cannot mask a propagating exception.
    `LedSource.off` defines the operation without a fallback method. Rigs with
    a shutter shut it instead and keep the LED running.
    """
    if rig.led is None:
        return
    try:
        rig.led.off()
    except Exception:
        pass


class _null:
    def __enter__(self): return None
    def __exit__(self, *exc): return False
