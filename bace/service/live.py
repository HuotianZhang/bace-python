"""Infers the running step's instrument state from its events.

`GET /bench` starts from the cached read-back. A read-back is a worker job,
so during a run that snapshot predates the module's instrument writes.
It can show the relay on the source-meter with bias and LED off while
`bace` uses the amplifier with the LED pulsing.

Run events describe instrument commands in order. Module builders define
what happens between them: `RunStarted` implies a prepared transient
chain, and `StepPhase` identifies the shutter state within a shot.
`InstrumentState` supplies explicit state. This module folds those events
into an overlay on `instruments`, marked `how: "inferred"` to distinguish
it from read-back (docs/service-contract.md section 4).

`NodeDone` clears the module's inferred active outputs. Run completion
clears the overlay, so an inference cannot outlive the step that supports
it. This module performs no instrument I/O and writes no journal entries.
The next read-back replaces the inferred state.
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
"""Identifies `StepPhase` values with the shutter open.
The shutter stays open through `dark levels` and shuts at `dark settle`.
"""

INSTRUMENT_STATE_KEYS: dict[str, tuple[str, str]] = {
    "bias_output_polarity": ("bias", "polarity"),
    "bias_arm_source": ("bias", "arm_source"),
    "bias_arm_slope": ("bias", "arm_slope"),
    "led_mode": ("led", "mode"),
}

LIGHT_UNWOUND: frozenset[str] = frozenset({"bace", "jv_bace", "park"})
"""Identifies modules that leave the shutter shut.
`bace` and `jv_bace` shut it in `finally`; `park` calls `Rig.park`.
`jv` uses `light_control="leave"`, and `light` retains the requested
illumination. Their `NodeDone` events must not imply a shut shutter:
that would display darkness while the sample is lit. Conversely, park
must clear an open-shutter inference from a preceding `light` module,
including during a long following module.
"""


class LiveState:
    """Folds ordered run events into one run record's instrument overlay.

    `apply` consumes events; `overlay` combines inference with cached state.
    """

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
                # Unwind turns outputs off and shuts the shutter only for modules
                # that manage it (`_build_bace` and `run_jv` with `manage`).
                # Relay and LED state persist; the shutter controls illumination.
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
            # The mode determines which field carries the level. Read it first
            # because dictionary order does not define that relationship.
            led_mode = str(ev.values.get("led_mode") or "").upper()
            for key, value in ev.values.items():
                if key in INSTRUMENT_STATE_KEYS:
                    # `?` means no answer. An inferred unknown must not overwrite a
                    # usable read-back value.
                    if value is None or str(value) == "?":
                        continue
                    instrument, field = INSTRUMENT_STATE_KEYS[key]
                    self._set(instrument, **{field: str(value)})
                elif key == "shutter":
                    # An explicit failed shutter read supersedes the Start snapshot.
                    # Keeping a stale `open` beside a valid LED level would imply light
                    # for a curve whose illumination is unknown. `None` displays that
                    # absence on the rail and unknown state on the card.
                    self._set("shutter", open=None if str(value) == "?"
                              else str(value) == "open")
                    self._jv_light(str(value) == "open")
                elif key == "led_level_v" and value is not None:
                    # `set_dc` writes `:VOLT:OFFS`; `set_pulse` writes `:VOLT:HIGH`.
                    # The card selects the matching level through `fields.driveLevel`.
                    # Writing a DC level to `high_v` would leave a stale `offset_v` on
                    # screen while the file records the actual level. An unknown mode
                    # identifies neither field, so its level cannot be assigned safely.
                    if led_mode == "DC":
                        self._set("led", offset_v=float(value))
                    elif led_mode == "PULSE":
                        self._set("led", high_v=float(value))
                elif key == "led_output" and value is not None:
                    # Explicit read-back replaces the inferred output flag. The Start
                    # snapshot cannot describe subsequent `light` output changes.
                    self._set("led", output=bool(value))
                elif key == "illumination" and str(value) == "unknown":
                    # An explicit failed LED read supersedes the Start snapshot.
                    # Retaining stale levels beside the current shutter state could
                    # display light while the file records unknown illumination.
                    # This differs from skipping an unread inferred field above.
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
        """Returns instrument state with inferred fields and `how: "inferred"`.

        Other instruments retain their cached read-back fields.
        """
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
        """Infers managed LED state from a light curve's shutter event.

        `run_jv` with `manage` sets the LED to DC before opening the shutter
        for a light curve. It leaves the LED unchanged for a dark curve, so
        that curve adds no LED inference and retains the overlay's state.

        `jv` sets no illumination. Its shutter event is read-back, with LED
        readings in `led_mode` and `led_level_v`; those values are handled
        directly rather than inferred from the shutter.
        """
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
