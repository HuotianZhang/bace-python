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
}


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
                # The module's own unwind: outputs off, shutter shut
                # (`_build_bace`/`run_jv` finally blocks); the relay stays,
                # and so does the LED -- since 2026-09-02 no module switches
                # it off, the shutter is the light switch.
                self._parked()
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
            for key, value in ev.values.items():
                if key in INSTRUMENT_STATE_KEYS:
                    instrument, field = INSTRUMENT_STATE_KEYS[key]
                    self._set(instrument, **{field: str(value)})
                elif key == "shutter":
                    self._set("shutter", open=(str(value) == "open"))
                    self._jv_light(str(value) == "open")
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

    def _parked(self) -> None:
        self._set("bias", output=False)
        self._set("smu", output=False)
        self._set("shutter", open=False)

    def _jv_light(self, lit: bool) -> None:
        """`run_jv` sets the LED to DC before it opens the shutter for a
        light curve and leaves it alone for a dark one; the shutter's state
        event is the only one it yields, so it stands for the LED too. A
        dark curve therefore infers nothing about the LED: it is whatever
        the last module left, which the overlay already holds."""
        if self.step is None or self.step.module not in ("jv_bace", "jv_dark"):
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
                                invert=cfg.invert_polarity,
                                trigger_offset_s=self.rig_config.trigger_offset_s)
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
