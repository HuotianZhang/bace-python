"""Current–voltage characterisation, standalone.

A J–V scan is a measurement in its own right, not a preamble to a transient, so
this module runs with or without BACE. It needs only the SourceMeter; the LED
and the relay are used when they are present, and their absence just means "the
illumination is whatever it is" and "nothing else is contending for the device".

What it does that the original did not:

* **Dark and light in one run.** The original swept the Keithley under whatever
  the 33220A happened to be doing. With `light_control="manage"` the `dark`
  flag and the LED levels are explicit, the shutter is opened for a light curve
  and shut for a dark one, and both land in the same file set so the pair is
  kept together — a dark curve is only useful next to the light curve it
  belongs to. The shutter is the light switch: the LED generator is left
  exactly as it is for a dark curve and on the way out (operator instruction,
  2026-09-02 -- a generator that is cycled loses its thermal steady state and
  the next module waits for it again). Only a rig with no shutter still
  switches the LED off, because that is the only way it can be dark.
* **Or the light left strictly alone.** `light_control="leave"` sweeps under
  whatever illumination it finds and touches neither shutter nor LED, going in
  or coming out. That is the original's behaviour made deliberate rather than
  accidental, and it is what lets the light be set by something else — a
  `light` step before this one, or the operator's own hand on the bench. The
  difference from the original is that this one *reads the light back* and
  records what it found (`illumination_state`), so a curve is never labelled by
  an assumption. When the bench cannot say, the curve is `unknown` and a
  warning says so: an unknown curve is worth more than a light one wearing a
  dark label.
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
LightControl = Literal["manage", "leave"]


@dataclass(frozen=True)
class JVConfig:
    """One J–V measurement."""

    start_v: float = -0.2
    """First bias the SourceMeter sources, at the device."""

    stop_v: float = 1.2
    """Last bias. Past V_oc for a light curve, or the interesting half of the
    sweep is missing; a J-V that stops short of the crossing reports no V_oc
    at all, because `metrics` interpolates and will not extrapolate."""

    step_v: float = 0.02
    """Spacing between points. The point count is rounded from the span, and
    it multiplies the run: every point costs `settle_s` plus the reading."""

    settle_s: float = 0.05
    """Delay at each point before the reading. Too short and the curve is a
    picture of the RC of the cell, not of its steady state."""

    both_directions: bool = False
    """Sweep start->stop and back. Hysteresis is real and worth seeing; it also
    doubles the run and makes 'the' curve ambiguous, so it is opt-in."""

    light_control: LightControl = "manage"
    """`manage` sets the illumination for each curve in the plan — the shutter
    shut for the dark one, the LED at each level for the light ones — and shuts
    the shutter on the way out. `leave` touches neither, sweeps once under
    whatever is there, and reads back what that was.

    There is no third value and no half-way: a run either owns the light or it
    does not, and the file says which. `dark` and `led_levels_v` belong to
    `manage` alone; asking for either under `leave` is a contradiction the
    constructor refuses rather than resolves."""

    dark: bool = True
    """Include a dark scan: the shutter shut (the LED output off only on a rig
    with no shutter). `manage` only."""

    led_levels_v: tuple[float, ...] = ()
    """Drive levels for the light scans, at the 33220A output. Empty means no
    light scan — a dark-only run."""

    pixel_area_cm2: float = 0.0
    """0 means report current in amps and leave density out. Better than
    defaulting to 1 cm², which silently mislabels A as A/cm²."""

    led_settle_s: float = 2.0
    """After changing the LED level. The original called this 'LED stab. time'
    and gave it its own control, which is a hint that it matters. `manage`
    only: `leave` changed nothing, so there is nothing to settle after."""

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
    label: str                   # "dark", "1.020 V", "as found dark", "as found unknown"
    dark: bool | None
    """True dark, False lit, **None unknown**. None is only ever produced by
    `light_control="leave"` on a bench that cannot say what the light was
    doing, and it is not the same as False: False is a read that came back
    lit, None is no read at all. Everything that consumes this — the metrics,
    the file, the console — has to keep the two apart, because a light label
    on a dark curve is the failure `ui-rules` §9 is about."""

    led_level_v: float | None
    direction: Direction
    voltage: np.ndarray
    current: np.ndarray          # A, as the instrument reported it
    density: np.ndarray | None   # A/cm², only if a pixel area was given
    metrics: JVMetrics
    intensity_w: float | None = None
    illumination: dict | None = None
    """What the bench said the light was doing, for a `leave` curve:
    `illumination_state`'s answer, verbatim. None for a `manage` curve, where
    the run set the light and `dark`/`led_level_v` already say what it set."""


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

    **Unwind what you turned on.** The `finally` disables the SourceMeter on
    every exit path -- finished, aborted, raised, or the consumer simply
    stopped iterating -- because the SourceMeter is a source this run switched
    on into the device. The light is unwound only under `manage`, which set it:
    the shutter is shut and the LED left as it was set, except on a rig with no
    shutter where switching the LED off is the only way to leave the bench
    dark. Under `leave` the light is exactly as it was found, on the way out as
    on the way in; a run that did not touch the shutter has no business shutting
    it, and the operator or the step that set the light still owns it.
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
                        # On the event stream, for a console or recorder to
                        # pick up: a light curve is only a light curve if
                        # light reached the sample. `JVRecorder` folds it into
                        # /config/resolved and onto each curve group (schema
                        # bace-jv/3), the way the transient recorder does, so
                        # the *file* can say.
                        yield InstrumentState({"shutter": shutter})
                    if rig.led is not None or shutter is not None:
                        # Only when something moved. A bare SourceMeter rig
                        # has nothing to settle after, and before 2026-09-02
                        # the settle lived inside `_set_illumination`, behind
                        # its `rig.led is None` return, so such a rig never
                        # waited; a silent 2 s per curve would be a regression
                        # nobody reports.
                        sleep(config.led_settle_s)
                    label = "dark" if dark else f"{level:g} V"
                    if not dark and rig.power is not None:
                        try:
                            intensity = rig.power.read_power()
                        except Exception as exc:        # a meter is not the point
                            yield Notice("warning", f"intensity not read: {exc}")
                else:
                    # Nothing is set. The light is read instead, and the read
                    # is what the curve is labelled and filed by.
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
                        # The output flag too, or `LiveState` overlays mode and
                        # level onto the *Start* snapshot's stale one and the rail
                        # shows the LED off through an illuminated sweep.
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
                    vm, im = rig.smu.sweep(float(v[0]), float(v[-1]), int(v.size),
                                           settle_s=config.settle_s)
                    density = (im / config.pixel_area_cm2
                               if config.pixel_area_cm2 > 0 else None)
                    ev = JVCurveDone(index=index, label=label, dark=dark,
                                     led_level_v=level, direction=direction,
                                     voltage=vm, current=im, density=density,
                                     # An unknown illumination gets the full
                                     # set: V_oc and FF are interpolations of
                                     # the curve either way (`ui-rules` §6),
                                     # and suppressing them would hide the one
                                     # evidence the operator has that the light
                                     # was on -- a V_oc where a dark curve has
                                     # none. Only a *known* dark curve has them
                                     # withheld, because there the caller knows.
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
    """What the next curve was going to be, for the abort notice."""
    if not manage:
        return "the curve"
    return "dark" if dark else f"{level} V"


def illumination_state(rig: Rig) -> dict:
    """What the light is doing *now*, as far as this bench can tell.

    The question is not "what is the 33220A set to" -- that is the bench card's
    question and `service.rigs._led_state` answers it for the chain checks --
    but the narrower one a curve's label depends on: **is light reaching the
    sample?** Three things have to be true for yes, and each of them is on the
    driver protocols, so this works on any rig and in the simulator:
    the shutter open, the LED output enabled, and the LED not in `OFF` mode.

    `lit` is None when any of the three cannot be read, and None is the whole
    point of this function. A rig with no shutter driver cannot know whether
    light reaches the sample however confident the generator is; a `False`
    there would be a dark label on a curve taken in the light. The level is
    read when the driver offers `read_state()` (the 33220A does) and left None
    when it does not -- a missing number is not a wrong one.

    **A driver that was asked and would not answer is unread, not cached.**
    `Agilent33220A.read_state()` returns `output: None` when `:OUTP?` goes
    unanswered and `mode: "?"` when `FUNC:SHAP?` does, and the instance still
    holds the flags it set last. Falling back to those turns "the generator
    did not reply" into a measurement -- and the cache can be stale exactly
    when it matters, after somebody used the front panel. The cached flags are
    used only where there is no `read_state` to ask, which is the simulator and
    any driver that never claimed to read back.
    """
    shutter, led = rig.shutter, rig.led
    out: dict = {"lit": None, "shutter": None, "led_output": None,
                 "led_mode": None, "led_level_v": None, "intensity_w": None,
                 "unread": []}

    if shutter is not None:
        # `read_line()` asks the DIO module; `is_open` is `self._state == OPEN`,
        # and `_state` "only knows what *this* object has set, and is None on a
        # fresh open" (`drivers/shutter.py`). So after a service restart, or
        # any change made outside this process, `is_open` reads False on a
        # physically open shutter -- and this function would turn that into the
        # definite answer "shut", which becomes a *dark label on a lit curve*.
        # Same order `service.rigs._shutter_state` uses for the bench snapshot.
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
            # It has a read-back and the read-back would not answer. The cached
            # flag is not a substitute for it.
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
            # The driver was asked. `None` and `"?"` are its way of saying it
            # got no answer, and neither is a reading.
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
            # No read-back to ask for: the driver's own flags are the only
            # account there is, and they are not a claim about an instrument
            # that might have been touched by hand.
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
        # **In DC the level is the offset.** `set_dc` writes `:VOLT:OFFS`, and
        # `read_state` returns that separately from `high_v` -- which in DC
        # holds whatever amplitude the last pulse left. Taking `high_v` would
        # label a DC J-V with a stale number and register its V_oc at that
        # level, which is exactly what the coupling check compares.
        #
        # And there is **no fallback between the two**. A first version of this
        # fell back to `high_v` when `:VOLT:OFFS?` went unanswered, which is
        # the same "unread is not cached" mistake in a new place: the answer
        # would have been the stale pulse amplitude, presented as the DC drive.
        # An unread level is simply absent -- the curve is labelled
        # `as found lit` rather than with a number nobody read. It does not
        # make the *illumination* unknown, because whether light reaches the
        # sample is the other three readings' business, not this one's.
        mode_now = str(out["led_mode"] or "").upper()
        level = state.get("offset_v") if mode_now == "DC" else state.get("high_v")
        if level is None and not asked:
            # `last_levels` is the spelling the drivers keep -- the simulated
            # ones and the 33220A both -- and the one `service.rigs._levels`
            # reads for the bench card. One vocabulary for one fact.
            levels = getattr(led, "last_levels", None)
            level = levels[0] if levels else None
        try:
            out["led_level_v"] = None if level is None else float(level)
        except (TypeError, ValueError):
            out["led_level_v"] = None
    else:
        out["unread"].append("LED (none on this bench)")

    # **Dark needs one proof; light needs all three.** A shut shutter means no
    # light reaches the sample whatever the generator is doing -- the shutter
    # *is* the light switch -- and an LED that is off or in OFF mode means
    # there is none to reach it whatever the shutter is doing. Any one of
    # those, definitively read, settles the question.
    #
    # The all-or-nothing gate this replaces made a supported arrangement
    # useless: `light(shutter="shut")` on a bench with no LED is explicitly
    # allowed, and the `jv` after it was then recorded `unknown` -- with the
    # full non-dark metric set -- although the closed shutter proved it dark.
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
            pass                # a meter is not the point, here or in the run
    return out


def _found_label(state: dict) -> str:
    """The curve label for a `leave` curve, from the read-back. Kept free of
    anything but letters, digits, dots and spaces: `storage.jv` turns it into
    an HDF5 group name by replacing spaces with underscores."""
    if state.get("lit") is None:
        return "as found unknown"
    if not state["lit"]:
        return "as found dark"
    level = state.get("led_level_v")
    return "as found lit" if level is None else f"as found {level:g} V"


def _set_illumination(rig: Rig, dark: bool, level: float | None) -> None:
    """A light curve sets the LED to DC at the level; a dark curve leaves the
    LED exactly as it is and lets the shutter make the dark.

    Operator instruction, 2026-09-02, after watching a real run: do not switch
    the LED generator's output off any more; when no light is wanted, use the
    shutter. A generator that is cycled loses its thermal steady state, and
    the next module -- a bace after this J-V, or the light curve after this
    dark one -- waits for it all over again, which is what the operator saw
    the fixed settle fail to cover. The shutter (LED -> shutter -> fibre ->
    device) is the light switch, and it is `_set_shutter` that shuts it for a
    dark curve.

    The one exception is a rig with no shutter: there the LED output off is
    the only way to be dark, so it is still switched off -- loud, not
    swallowed, because an `off()` that fails on such a rig leaves the LED on
    under a curve labelled dark, which is worse than a stopped run.
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
    """Open for a light curve, shut for a dark one. Returns the state for the
    `InstrumentState` the run yields; None when the rig has no shutter.

    Until 2026-09-02 `run_jv` never touched the shutter (recorded gap,
    the design pack 01-modules section 4, docs/ui-rules.md). An LED that is on is not the same as light
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
    """The unwind on a rig with no shutter: never raises, so a dead LED cannot
    mask the exception that is already propagating. `off()` is in the
    `LedSource` contract, so there is no fallback spelling to try. A rig with
    a shutter never comes here -- its unwind shuts the shutter and leaves the
    LED as set."""
    if rig.led is None:
        return
    try:
        rig.led.off()
    except Exception:
        pass


class _null:
    def __enter__(self): return None
    def __exit__(self, *exc): return False
