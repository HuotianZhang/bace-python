"""What the running step implies about the bench, folded from its events.

`GET /bench` is rendered from the last read-back, and a read-back is a job
on the worker -- which, while a run is on it, is running the run. So for the
length of a scan the cached snapshot is the one `Start` took before the
module enabled anything: relay on the SourceMeter, bias off, LED off,
shutter shut, while bace drives the device through the amplifier with the
LED pulsing. The pinned rail the design puts in every view ("bias LIVE",
"relay amplifier", 03-states section A) would be wrong for hours.

The run's own events say what the instruments were told, in order, and a
module's builder is deterministic about what it does between them: a
`NodeStarted` for a `bace` step means the router was entered on the
amplifier side; a `RunStarted` from `run_transient_scan` comes after the LED
was set to pulse at the step's level and the 81150A output enabled; each
`StepPhase` says whether the shutter is open; an `InstrumentState` carries
the arming and polarity the run read back. This folds those into an
overlay on the snapshot's `instruments`, every field of it marked
`how: "inferred"` so a client can tell "what the run implies" from "what a
read-back saw" -- and it is cleared the moment the module's `NodeDone` says
the outputs are off again, and again at the run's end, so nothing inferred
outlives the step that implied it.

Inferred, not read: nothing here touches an instrument, and nothing here is
written to the journal. The next read-back replaces it.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Iterable

from ..core.pulses import pulse_levels
from ..experiment import events as E
from ..experiment.jv import JVFinished, JVStarted
from ..experiment.rig import RigConfig
from ..experiment.transient import RunConfig
from .pipeline import Schedule, Step

RELAY_SIDE = {"dc": "sourcemeter", "transient": "amplifier"}

SHUTTER_OPEN_PHASES: frozenset[str] = frozenset({"light settle", "acquire light", "dark levels"})
SHUTTER_SHUT_PHASES: frozenset[str] = frozenset({"dark settle", "acquire dark", "process"})
"""Which `StepPhase`s find the shutter open. `dark levels` is slept under
the dark levels with the shutter still open; it shuts at `dark settle`."""

INSTRUMENT_STATE_KEYS: dict[str, tuple[str, str]] = {
    "bias_output_polarity": ("bias", "polarity"),
    "bias_arm_source": ("bias", "arm_source"),
    "bias_arm_slope": ("bias", "arm_slope"),
    "led_mode": ("led", "mode"),
}

LIGHT_UNWOUND: frozenset[str] = frozenset({"bace", "jv_bace", "park"})
"""Which modules leave the shutter shut, and therefore leave the bench dark
whatever they did in between: `bace` and `jv_bace` shut it in their own
`finally`, and `park` shuts it outright (`Rig.park()`).

`jv` and `light` are not here and must not be: `jv` never touches the light
(`light_control="leave"`), and `light` exists to leave it where it put it.
Inferring "shutter shut" at their `NodeDone` would tell the operator the lamp
is off while it is on -- an inference the rail marks as inferred and the
operator still reads. The mistake runs both ways, which is why `park` is here:
a `light(shutter=open)` followed by `park` and then something long leaves the
overlay saying open for the whole of it."""


class LiveState:
    """The overlay, one instance per run record. `apply` every event of the
    run in order; `overlay` the cached instruments with what is inferred."""

    def __init__(self, rig_config: RigConfig):
        self.rig_config = rig_config
        self.run_config: RunConfig | None = None
        self.step: Step | None = None
        self.state: dict[str, dict[str, Any]] = {}
        self._levels = None

    # -- folding ----------------------------------------------------------
    def apply(self, ev: E.Event, schedule: Schedule | None) -> None:
        if isinstance(ev, E.NodeStarted):
            step = _module_step(schedule, ev.node_path)
            if step is None:
                return
            self.step = step
            if step.relay in RELAY_SIDE:
                self._set("relay", position=RELAY_SIDE[step.relay])
        elif isinstance(ev, E.NodeDone):
            if self.step is not None and ev.node_path == self.step.node_path:
                # The module's own unwind: outputs off, and the shutter shut
                # for the modules that shut it (`_build_bace`/`run_jv`'s
                # `finally`, under `light_control="manage"`); the relay stays,
                # and so does the LED -- since 2026-09-02 no module switches
                # it off, the shutter is the light switch.
                self._parked(light=self.step.module in LIGHT_UNWOUND)
                self.step = None
                self.run_config = None
        elif isinstance(ev, E.RunStarted):
            self.run_config = _run_config(ev.config)
            values = self.step.values() if self.step is not None else {}
            frequency = _float(values.get("pulse_frequency_hz"))
            self._set("bias", output=True, frequency_hz=frequency)
            self._set("led", output=True, mode="PULSE", high_v=_float(values.get("led_v")),
                      low_v=_float(values.get("led_low_v")), frequency_hz=frequency)
        elif isinstance(ev, E.InstrumentState):
            # Read once, up front: which field a level belongs to is the
            # mode's business, and dict order is not a contract.
            led_mode = str(ev.values.get("led_mode") or "").upper()
            for key, value in ev.values.items():
                if key in INSTRUMENT_STATE_KEYS:
                    # `?` is a driver saying it got no answer. Overlaying it
                    # would replace a real read-back with "unknown" *and* mark
                    # it inferred -- worse on both counts than leaving the
                    # snapshot's own value where it is.
                    if value is None or str(value) == "?":
                        continue
                    instrument, field = INSTRUMENT_STATE_KEYS[key]
                    self._set(instrument, **{field: str(value)})
                elif key == "shutter":
                    # `?` is the run saying it asked and got no answer, and
                    # that is newer than the snapshot Start took. Left in
                    # place, a stale `open` would be combined with a good LED
                    # reading into a "lit" the file is recording as unknown.
                    # `None` renders as an absence on the rail and as unknown
                    # on the card, which is what it is.
                    self._set("shutter", open=None if str(value) == "?"
                              else str(value) == "open")
                    self._jv_light(str(value) == "open")
                elif key == "led_level_v" and value is not None:
                    # `set_dc` writes `:VOLT:OFFS`, `set_pulse` writes
                    # `:VOLT:HIGH`, and the card reads whichever field the
                    # mode names (`fields.driveLevel`). Folding a DC level
                    # into `high_v` left `offset_v` at the Start snapshot's
                    # value, so a `light` node that moved the lamp to a new
                    # DC level showed the *old* one for the whole of the `jv`
                    # that followed -- while the file recorded the new one.
                    # An unread mode names neither field, and a good number
                    # in the wrong field is worse than none at all.
                    if led_mode == "DC":
                        self._set("led", offset_v=float(value))
                    elif led_mode == "PULSE":
                        self._set("led", high_v=float(value))
                elif key == "led_output" and value is not None:
                    # A read-back, so it replaces the overlay's flag rather
                    # than being inferred: a `light` node that switched the
                    # generator on from parked, or off, is the one thing the
                    # snapshot Start took cannot know.
                    self._set("led", output=bool(value))
                elif key == "illumination" and str(value) == "unknown":
                    # The run asked and the bench could not say. Skipping the
                    # unread `?` above protects a *good* read-back from being
                    # replaced by "unknown" -- but here the run's own failure
                    # to read is the newer fact, and leaving Start's stale
                    # values in place lets the card combine them with the
                    # current shutter and show "lit" for a curve the file is
                    # recording as `as found unknown`. The screen disagreeing
                    # with the file is the one thing the read-back exists to
                    # prevent, so the unknown is written.
                    self._set("led", mode="?", output=None)
        elif isinstance(ev, E.StepStarted):
            self._levels = self._pulse_levels(ev.setpoint)
            if self._levels is not None:
                self._set("bias", high_v=self._levels.high_light, low_v=self._levels.low_light)
        elif isinstance(ev, E.StepPhase):
            if ev.phase in SHUTTER_OPEN_PHASES:
                self._set("shutter", open=True)
            elif ev.phase in SHUTTER_SHUT_PHASES:
                self._set("shutter", open=False)
            if ev.phase == "levels":
                self._set("shutter", open=False)
            if ev.phase in ("dark levels", "dark settle") and self._levels is not None:
                cfg = self.run_config
                if cfg is not None and cfg.dark_reference != "same":
                    self._set("bias", high_v=self._levels.high_dark,
                              low_v=self._levels.low_dark)
        elif isinstance(ev, JVStarted):
            self._set("smu", output=True)
        elif isinstance(ev, JVFinished):
            self._set("smu", output=False)
        elif isinstance(ev, (E.RunFinished, E.RunAborted)):
            self._set("bias", output=False)
        elif isinstance(ev, E.RunStateChanged):
            if ev.state in ("done", "stopped", "aborted", "failed", "blocked", "cancelled",
                            "parked"):
                self.clear()

    def clear(self) -> None:
        self.state.clear()
        self.step = None
        self.run_config = None
        self._levels = None

    # -- the overlay ------------------------------------------------------
    @property
    def inferred(self) -> list[str]:
        return [k for k, v in self.state.items() if v]

    def overlay(self, instruments: dict) -> dict:
        """A copy of `instruments` with every inferred instrument's fields
        replaced and `how: "inferred"` on it. The others are untouched."""
        out = dict(instruments)
        for key, fields in self.state.items():
            if not fields:
                continue
            base = dict(out.get(key) or {})
            base.update(fields)
            base["how"] = "inferred"
            out[key] = base
        return out

    # -- pieces -------------------------------------------------------------
    def _set(self, instrument: str, **fields: Any) -> None:
        self.state.setdefault(instrument, {}).update(fields)

    def _parked(self, *, light: bool = True) -> None:
        self._set("bias", output=False)
        self._set("smu", output=False)
        if light:
            self._set("shutter", open=False)

    def _jv_light(self, lit: bool) -> None:
        """`run_jv` under `manage` sets the LED to DC before it opens the
        shutter for a light curve and leaves it alone for a dark one; the
        shutter's state event is the only one it yields, so it stands for the
        LED too. A dark curve therefore infers nothing about the LED: it is
        whatever the last module left, which the overlay already holds.

        `jv` is excluded because it sets nothing: its shutter event is a
        *read-back*, and the LED reading that comes with it arrives on the
        same event under `led_mode`/`led_level_v` rather than being inferred
        from the shutter."""
        if self.step is None or self.step.module != "jv_bace":
            return
        if lit:
            values = self.step.values()
            level = _float(values.get("led_v"))
            self._set("led", output=True, mode="DC",
                      **({"high_v": level} if level is not None else {}))

    def _pulse_levels(self, setpoint):
        cfg = self.run_config
        if cfg is None:
            return None
        try:
            return pulse_levels(setpoint.vpre, setpoint.vcoll, self.rig_config.pulse_amp,
                                setpoint.delay_ns, cfg.pulse_width_ns,
                                invert=cfg.invert_polarity)
        except (ValueError, TypeError, AttributeError):
            return None


def _module_step(schedule: Schedule | None, node_path: str) -> Step | None:
    if schedule is None:
        return None
    for step in schedule.modules:
        if step.node_path == node_path:
            return step
    return None


def _run_config(config: dict) -> RunConfig | None:
    run = (config or {}).get("run")
    if not isinstance(run, dict):
        return None
    names = {f.name for f in dataclasses.fields(RunConfig)}
    try:
        return RunConfig(**{k: v for k, v in run.items() if k in names})
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


__all__: Iterable[str] = ["LiveState", "RELAY_SIDE", "SHUTTER_OPEN_PHASES", "SHUTTER_SHUT_PHASES"]
