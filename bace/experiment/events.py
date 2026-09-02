"""The event stream a run emits.

One stream, many listeners. HDF5 storage, the legacy `.dat` writer, the live
UI and the log are all just consumers of this sequence; adding another touches
no existing code. That is the whole reason the run is a generator of typed
events rather than a function that returns a result and writes files on the
side.

Events are frozen dataclasses with no methods and no instrument references, so
a consumer can hold on to one, queue it, or serialise it without worrying about
what the hardware is doing afterwards. Arrays are the exception: they are
handed over by reference and must not be mutated by a consumer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from ..core.axis import Axis, Setpoint
from ..drivers.protocols import DCPoint, Trace


@dataclass(frozen=True)
class Event:
    """Base class, for typing and isinstance dispatch only."""


@dataclass(frozen=True)
class RunStarted(Event):
    description: str
    n_shots: int
    n_steps: int
    n_loops: int
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DCMeasured(Event):
    """SourceMeter characterisation at the illumination in force."""

    led_drive_v: float
    dc: DCPoint


@dataclass(frozen=True)
class AxisResolved(Event):
    """The axis after V_oc has been folded in — the moment the run becomes concrete."""

    axis: Axis
    values: np.ndarray
    voc: float | None


@dataclass(frozen=True)
class InstrumentState(Event):
    """What the instruments actually report once setup has run.

    The config a run is *given* is not the same thing as the state it *ran in*.
    `output_polarity = "leave"` is the clearest case: the recipe deliberately
    says "do not touch it", so the recipe alone cannot tell a later reader which
    convention the data was taken under -- only the instrument can. Anything
    resolved at setup rather than declared in the recipe belongs here, and the
    recorder writes it beside the config so every file still carries its own
    conventions.

    Values are strings because this is a readback, not a setting: `?` for an
    instrument that would not answer is more honest than a plausible float.
    """

    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StepStarted(Event):
    index: int
    loop: int
    step: int
    setpoint: Setpoint
    axis_value: float


@dataclass(frozen=True)
class StepDone(Event):
    """One shot, fully processed.

    `light` and `dark` are currents in amps as the digitizer returned them;
    `photo` is `light - dark` after baseline correction; `q` is the charge for
    this single shot. `q_mean`/`q_std` are the running statistics over the
    loops completed so far at this axis position, so a consumer can plot error
    bars that tighten as the run proceeds.
    """

    index: int
    loop: int
    step: int
    setpoint: Setpoint
    axis_value: float
    light: Trace
    dark: Trace
    photo: np.ndarray
    photo_averaged: np.ndarray
    q: float
    q_mean: float
    q_std: float
    intensity_w: float | None = None
    clipped: bool = False


@dataclass(frozen=True)
class LoopDone(Event):
    loop: int
    q_mean: np.ndarray
    q_std: np.ndarray


@dataclass(frozen=True)
class Progress(Event):
    done: int
    total: int
    elapsed_s: float
    eta_s: float | None


@dataclass(frozen=True)
class RunFinished(Event):
    axis: Axis
    values: np.ndarray
    q_mean: np.ndarray
    q_std: np.ndarray
    q_all: np.ndarray            # (n_loops, n_steps)
    photo_averaged: np.ndarray   # (n_steps, n_samples)
    dt: float
    elapsed_s: float


@dataclass(frozen=True)
class RunAborted(Event):
    reason: str
    done: int
    total: int


@dataclass(frozen=True)
class RunFailed(Event):
    error: str
    where: str


@dataclass(frozen=True)
class Notice(Event):
    """Something a human should see but that does not stop the run."""

    level: Literal["info", "warning"]
    text: str
