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
class StepPhase(Event):
    """Where inside one shot the run is: `phase` is the segment that is
    starting (`levels`, `light settle`, `acquire light`, `dark levels`,
    `dark settle`, `acquire dark`, `process`), `k` its 1-based place among
    the `of` segments this shot has. `dark levels` is absent when the dark
    reference is `same` (nothing is rewritten, nothing settles), so `of` is
    6 then and 7 otherwise.

    Between `StepStarted` and `StepDone` a shot is up to a second of
    instrument calls, and a card that shows only "shot 12 in flight" cannot
    tell a slow settle from a hung acquisition. This says which. Per-shot
    noise for a log: the service sends it live and journals none of it.
    """

    index: int
    phase: str
    k: int
    of: int


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
    sync_light: Trace | None = None
    sync_dark: Trace | None = None
    """The trigger channel's trace out of the same record as `light` and
    `dark`, in volts (2026-09-05). What the edge the scope fired on looked
    like: smeared, the jitter is between the sync and the scope's trigger;
    sharp beside a smeared displacement spike, it is between the sync and the
    81150A's pulse. None on a rig whose digitiser cannot fetch it."""


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
    node_path: str = ""
    """Which node this progress describes. `""` is the run itself.

    A pipeline executor emits its own `Progress(node_path="T=250K")` for a
    loop node -- `done`/`total` counting that loop's children -- while the
    `Envelope` around every event carries the node_path of the *leaf* being
    executed. So the console's three counters on three time scales (shot,
    point, loop) come from three Progress events with three node_paths, not
    from one event that tries to say everything, and a consumer that only
    wants the run's own progress filters on `node_path == ""`.
    """


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


# -- the service vocabulary ---------------------------------------------------
# Defined here, beside the run events, and not assembled in the service: the
# hierarchy labels and the envelope are part of what an event *is*, and the
# recorder, the journal and the console all read the same fields. A service
# that invented its own wrapper would be a second vocabulary to keep in step.

@dataclass(frozen=True)
class Envelope:
    """What every event is wrapped in on the wire.

    `seq` is monotonic per service session -- a client that sees a gap knows
    it missed something and re-syncs rather than guessing. `ts` is unix
    seconds. `node_path` is the position in the pipeline tree, such as
    "T=250K/led=1.020V/bace"; a manual run is a one-node pipeline, so its
    events carry the module name ("bace"), and `""` is reserved for
    session-level events -- a bench action, a verdict before Start -- whose
    `run_id` is None. `run_id` is otherwise the run the event belongs to.

    Not an `Event` itself: it wraps one, and `wire.envelope_to_wire` is what
    turns the pair into JSON.
    """

    seq: int
    ts: float
    run_id: str | None
    node_path: str
    event: Event


@dataclass(frozen=True)
class NeedsOperator(Event):
    """The executor has stopped and is waiting for a person: `what` names the
    action ("set temperature"), `detail` carries the target. Emitted at a
    temperature node while the 331 is not wired in, so a tree with manual
    temperatures still runs end to end; the API does not change when the node
    is automated, the event just stops appearing."""

    what: str
    node_path: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorResumed(Event):
    """A person answered a `NeedsOperator`: `note` is what they typed,
    `detail` what they reported (the temperature they reached, say). The pair
    brackets the pause in the journal."""

    node_path: str
    note: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NodeStarted(Event):
    """A pipeline node begins. `kind` is the node type ("temperature",
    "illumination", "repeat", "bace", "jv", ...), `label` its human form
    ("T=250K")."""

    node_path: str
    kind: str
    label: str


@dataclass(frozen=True)
class NodeDone(Event):
    """A pipeline node ended: `outcome` is "ok", "aborted", "failed" or
    "skipped", and `detail` carries what the node produced that the tree
    still needs (the V_oc a sibling `bace` centres on)."""

    node_path: str
    outcome: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunQueued(Event):
    """A run was accepted onto the worker's queue: `kind` is "manual" (one
    module node) or "pipeline"; `module` the module name for a manual run,
    None for a pipeline; `tree` what was posted; `params` what the operator
    chose for the module -- the edited layer and last-used values carried
    forward, never the V_oc -- which is what the journal hands back as
    last-used next session; `resolved` every value the module runs with;
    `folder` the pipeline's parent folder when there is one; `sample` the
    `[sample]` block the run was queued under -- `sample`, `material`,
    `pixel`, `operator`, `comment` -- which is the run's identity and
    travels with it rather than only in the session header, because a
    journal file is resumed by a second process started in the same second
    and a header is a property of the file, not of the runs in it
    (`docs/naming-plan.md` rule 1). A fact about the queue, not something a
    run emits, so the recorders never see it; it lives here so the wire has
    one vocabulary and the service assembles none of it."""

    kind: str
    module: str | None
    tree: dict | None
    params: dict | None
    resolved: dict | None = None
    name: str = ""
    folder: str | None = None
    sample: dict | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("manual", "pipeline"):
            raise ValueError(f"RunQueued kind must be one of ('manual', 'pipeline'), "
                             f"not {self.kind!r}")
        if self.kind == "manual" and not self.module:
            raise ValueError("a manual RunQueued names its module; without it "
                             "last_used_params can never find this run")


@dataclass(frozen=True)
class RunStateChanged(Event):
    """The worker's state machine moved: `state` is "queued", "running",
    "stopping", "paused", "done", "aborted" or "failed"; `reason` says why
    ("operator", "after_shot", an exception's text)."""

    state: str
    reason: str


@dataclass(frozen=True)
class BenchAction(Event):
    """An explicit, single-click change to the bench outside any run --
    `POST /bench/actions/{name}`: set the 33220A polarity, park, move the
    relay. `args` is what was asked, `result` the readback afterwards, `by`
    who asked ("operator", "pipeline"). Every bench change that is not part
    of a run is one of these, which is what lets the journal show it as a
    "by hand" entry."""

    name: str
    args: dict
    result: dict
    by: str


@dataclass(frozen=True)
class Verdict(Event):
    """One line of a pre-flight or monitoring verdict, in three tiers.

    `crit` is **hardware safety only** -- two sources into the device node, a
    compliance above the bench ceiling -- and is the only level that blocks
    Start. `warn` **states evidence** ("the 33220A is NORM, so the Sync edge
    means light on") and never blocks: the operator decides what it means.
    `ok`/`info` are readbacks. `invalid` is a tree that cannot be executed at
    all (a `bace` with no V_oc source), refused at validate -- there is
    nothing to start, which is different from a safety block. **Nothing is
    auto-corrected** by any tier: a fix is an explicit `BenchAction` the
    operator clicks, so the journal shows who changed what and when.

    `code` is stable, for the UI to key on; `text` is for people; `data` is
    the evidence (the readbacks the verdict was made from).
    """

    level: Literal["ok", "info", "warn", "crit", "invalid"]
    code: str
    text: str
    node_path: str = ""
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PowerReading(Event):
    """One reading from the 1918-C, taken by the power monitor beside a run
    (the meter is on a beam splitter). `trustworthy` is the meter's own
    saturated / overrange / wrong-units verdict, carried rather than
    filtered; `source` is the console URL, or "simulated"."""

    watts: float
    trustworthy: bool
    wavelength_nm: float | None
    source: str
    averaged: bool | None = None
    """True when the meter was averaging (DC-continuous, 5 Hz analog filter),
    so the number is the time average of a pulsed LED -- half the DC level
    at 50 % duty -- and not one instant of it. None when the driver cannot
    say (a console that has no filter route)."""


@dataclass(frozen=True)
class TemperatureRead(Event):
    """One temperature, from the 331 console or typed by an operator
    (`source = "operator"`). `in_band` is None when there is no setpoint to
    be in band of."""

    kelvin: float
    setpoint_k: float | None
    in_band: bool | None
    source: str


@dataclass(frozen=True)
class SampleNamed(Event):
    """The session's `[sample]` block was set from outside the file it was
    opened with -- `PUT /session/sample`, which is the console's identity
    field. A session-level event: `run_id` is None and `node_path` is "".

    `before` and `after` are the whole block either side, `changed` the keys
    that actually moved.

    **It applies to runs queued after it and to no others.** Every run takes
    its own copy of the block when it is queued (`RunQueued.sample`, and the
    `RunMetadata` its files are written from), so a run already going keeps
    the identity it started under rather than being relabelled halfway. That
    is the reason this is an event at all: a journal that has to be read
    years later must be able to say which runs in a file were measured under
    which name, and a block that simply changed would leave no trace of ever
    having been anything else.
    """

    before: dict
    after: dict
    changed: list
