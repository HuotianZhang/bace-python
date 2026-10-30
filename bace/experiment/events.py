"""Run events carry measurements and progress to independent consumers.

HDF5 storage, the LabVIEW-compatible `.dat` writer, the console and the log
consume the same typed stream. A generator permits those consumers to handle
results without making the measurement write files or return one final result.
The `.dat` output matches LabVIEW files byte for byte, as
`tests/test_storage.py` checks.

Events are frozen dataclasses without instrument references. Consumers can
retain, queue or serialise them independently of hardware state. Arrays pass
by reference and must not be mutated by consumers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

from ..core.axis import Axis, Setpoint
from ..drivers.protocols import DCPoint, Trace


@dataclass(frozen=True)
class Event:
    """The event base class supports typing and `isinstance` dispatch."""


@dataclass(frozen=True)
class RunStarted(Event):
    description: str
    n_shots: int
    n_steps: int
    n_loops: int
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DCMeasured(Event):
    """The DC event contains source-meter results at `led_drive_v`, in V."""

    led_drive_v: float
    dc: DCPoint


@dataclass(frozen=True)
class AxisResolved(Event):
    """The resolved axis contains point values after V_oc centring."""

    axis: Axis
    values: np.ndarray
    voc: float | None


@dataclass(frozen=True)
class InstrumentState(Event):
    """The instrument state contains setup read-backs beside the requested settings.

    A run's settings do not fully describe its instrument state. With
    `output_polarity = "leave"`, only the instrument can supply the polarity.
    The recorder stores these resolved conventions beside the configuration so
    each file identifies its own measurement conditions.

    Values are strings because an unread instrument returns `?`; a numeric
    placeholder would look like a measurement.
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
    """The shot phase identifies the segment starting within one shot.

    `phase` names the segment: `levels`, `light settle`, `acquire light`,
    `dark levels`, `dark settle`, `acquire dark` or `process`. `k` is its
    one-based position among `of` segments. A dark trace taken first also writes
    dark levels; its following light segment is `light levels`. With
    `dark_reference = "same"` and the light trace first, `dark levels` is absent:
    there are six segments instead of seven.

    Instrument calls within a shot can take a second or more. Phase events let
    the console distinguish settling from a stalled acquisition. The service
    streams them live and omits them from the journal to avoid repetitive entries.
    """

    index: int
    phase: str
    k: int
    of: int


@dataclass(frozen=True)
class StepDone(Event):
    """The completed shot contains traces, Q and running statistics at one point.

    `light` and `dark` contain currents in A as returned by the digitiser.
    `photo` is light minus dark after baseline correction. `photo_averaged` is
    its running mean over loops at this point. `q` is this shot's Q in C;
    `q_mean` and `q_std` are its running mean and sample standard deviation over
    acquired loops at this point. These support error bars during acquisition.
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
    t0_int_record_s: float | None = None
    t1_int_record_s: float | None = None
    """The integration bounds locate Q in seconds from the first record sample.

    They follow field arrival and therefore vary along a delay axis. Storing
    them with each shot makes the charge's integration interval explicit.
    """
    dark_first: bool = False
    """The order flag marks a dark trace acquired before its light trace.

    `RunConfig.alternate_order` requests this on even loops.
    """
    """The Sync traces record the trigger channel in V beside each current trace.

    They come from the same records as `light` and `dark`. A smeared Sync edge
    indicates jitter between Sync and the digitiser trigger; a sharp Sync edge
    beside a smeared displacement spike places it between Sync and the 81150A
    pulse. The traces are `None` when the digitiser cannot fetch them.
    """


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
    """The progress path identifies the node whose counter is reported.

    An empty `node_path` denotes the run itself. A pipeline executor also emits
    `Progress` for a loop node, with `done` and `total` counting its children.
    The surrounding `Envelope.node_path` identifies the executing leaf instead.
    The console's shot, point and loop counters therefore use separate events
    and paths. Consumers of run-only progress filter on `node_path == ""`.
    """


@dataclass(frozen=True)
class RunFinished(Event):
    axis: Axis
    values: np.ndarray
    q_mean: np.ndarray
    q_std: np.ndarray
    q_all: np.ndarray            # `(n_loops, n_steps)`
    photo_averaged: np.ndarray   # `(n_steps, n_samples)`
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
    """A notice carries information or a warning without stopping the run."""

    level: Literal["info", "warning"]
    text: str


# -- Service event vocabulary -------------------------------------------------
# Hierarchy labels and envelopes belong with the events because recorders,
# the journal and the console share those fields. A separate service wrapper
# would require keeping two event vocabularies consistent.

@dataclass(frozen=True)
class Envelope:
    """The envelope identifies an event on the service stream.

    `seq` increases within each service session. A gap tells a client to
    resynchronise. `ts` is Unix time in s. `node_path` locates a pipeline node,
    such as `T=250K/led=1.020V/bace`. A manual run uses its module name, such as
    `bace`. An empty path is reserved for session events outside a run, such as
    a bench action or a verdict before Start; their `run_id` is `None`.
    Otherwise `run_id` identifies the containing run.

    An envelope wraps an `Event` rather than inheriting it.
    `bace.experiment.wire.envelope_to_wire` converts the pair to a JSON frame.
    """

    seq: int
    ts: float
    run_id: str | None
    node_path: str
    event: Event


@dataclass(frozen=True)
class NeedsOperator(Event):
    """The operator request pauses the executor for a manual action.

    `what` names the action, such as setting temperature, and `detail` gives
    the target. Temperature nodes emit it when automatic control is unavailable,
    so manual temperature pipelines can run end to end through the same API.
    """

    what: str
    node_path: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorResumed(Event):
    """The operator response records the answer to `NeedsOperator`.

    `note` contains entered text and `detail` contains reported values, such as
    the achieved temperature. The two events bracket the pause in the journal.
    """

    node_path: str
    note: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class NodeStarted(Event):
    """The node-start event identifies a pipeline node.

    `kind` is its type, such as `temperature`, `illumination`, `repeat`, `bace`
    or `jv`. `label` is the console label, such as `T=250K`.
    """

    node_path: str
    kind: str
    label: str


@dataclass(frozen=True)
class NodeDone(Event):
    """The node outcome reports `ok`, `aborted`, `failed` or `skipped`.

    `detail` carries results needed elsewhere in the tree, such as V_oc for a
    sibling `bace` axis.
    """

    node_path: str
    outcome: str
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunQueued(Event):
    """The queue event records an accepted manual run or pipeline.

    `kind` is `manual` for one module node or `pipeline` for a tree. `module`
    names the manual module and is `None` for a pipeline. `tree` contains the
    submitted tree. `params` holds operator choices from the edited layer and
    the last-used layer, excluding V_oc; the journal supplies these to the next
    session's last-used layer. `resolved` contains every effective module value.
    `folder` is the pipeline parent folder when present.

    `sample` copies the queued run's `[sample]` fields: `sample`, `material`,
    `pixel`, `operator` and `comment`. Identity travels with each run because a
    journal can be resumed by another process started in the same second. A
    session header identifies the file and cannot identify all runs within it.

    The service journal records this queue event; measurement recorders do not
    receive it. Keeping it here preserves one event vocabulary.
    """

    kind: str
    module: str | None
    tree: dict | None
    params: dict | None
    resolved: dict | None = None
    name: str = ""
    folder: str | None = None
    sample: dict | None = None
    plan: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("manual", "pipeline"):
            raise ValueError(f"RunQueued kind must be one of ('manual', 'pipeline'), "
                             f"not {self.kind!r}")
        if self.kind == "manual" and not self.module:
            raise ValueError("a manual RunQueued names its module; without it "
                             "last_used_params can never find this run")


@dataclass(frozen=True)
class RunStateChanged(Event):
    """The worker transition reports a state and its reason.

    `state` is `queued`, `running`, `stopping`, `paused`, `done`, `aborted` or
    `failed`. `reason` names the trigger, such as `operator`, `after_shot` or
    an exception message.
    """

    state: str
    reason: str


@dataclass(frozen=True)
class BenchAction(Event):
    """The bench action records an explicit change outside a run.

    `POST /bench/actions/{name}` requests actions such as setting 33220A polarity,
    parking or moving the relay. `args` holds the request, `result` the read-back
    and `by` the actor, such as `operator` or `pipeline`. The journal uses this
    event to distinguish manual bench changes from run operations.
    """

    name: str
    args: dict
    result: dict
    by: str


@dataclass(frozen=True)
class Verdict(Event):
    """The verdict reports one validation or monitoring result with evidence.

    `crit` is reserved for hardware safety, such as two sources on the device
    node or compliance above the bench ceiling. It is the only safety level that
    blocks Start. `warn` states evidence and leaves the decision to the operator.
    `ok` and `info` report read-backs. `invalid` rejects a tree that cannot execute,
    such as a `bace` node without a V_oc source; there is no runnable tree.

    No level changes settings automatically. A correction requires an explicit
    `BenchAction`, so the journal records the actor and the change. `code` is a
    stable console key, `text` is the explanation and `data` holds the read-backs
    that support it.
    """

    level: Literal["ok", "info", "warn", "crit", "invalid"]
    code: str
    text: str
    node_path: str = ""
    data: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PowerReading(Event):
    """The power reading reports 1918-C optical power from the beam splitter.

    `watts` is in W. `trustworthy` carries the meter's saturation, overrange and
    wrong-unit checks without filtering out readings. `source` is the meter
    console URL or `simulated`.
    """

    watts: float
    trustworthy: bool
    wavelength_nm: float | None
    source: str
    averaged: bool | None = None
    """The averaging flag identifies time-averaged power from the meter.

    `True` denotes DC-continuous mode with the 5 Hz analog filter. At 50 % duty
    the reading is half the LED DC power (manual 1.2). `None` means the driver
    cannot report the filter state, such as a console without a filter route.
    """


@dataclass(frozen=True)
class TemperatureRead(Event):
    """The temperature event reports a 331 or operator reading in K.

    `source = "operator"` identifies a typed reading. `in_band` is `None` without
    a setpoint.
    """

    kelvin: float
    setpoint_k: float | None
    in_band: bool | None
    source: str


@dataclass(frozen=True)
class SampleNamed(Event):
    """The sample event records a session identity change from `PUT /session/sample`.

    It is a session event: `run_id` is `None` and `node_path` is empty. `before`
    and `after` contain the whole block; `changed` lists the modified keys.

    The change applies only to runs queued after the event. Each run copies
    `RunQueued.sample` at queue time and uses it for `RunMetadata` in its files.
    A running or queued measurement therefore keeps its identity. Recording the
    change lets the journal associate each run with its sample even when one
    session file contains several identities.
    """

    before: dict
    after: dict
    changed: list


@dataclass(frozen=True)
class ParamsEdited(Event):
    """The edited layer of a module changed through `PUT /modules/{name}/params` or its reset.

    It is a session event: `run_id` is `None` and `node_path` is empty. `before`
    and `after` contain the module's whole edited layer; `changed` lists the
    modified keys.

    The layer is shared by every console, and a run started from one console
    uses it. A console therefore refetches its catalogue on this event, so that
    what it shows is what a run will use.
    """

    module: str
    before: dict
    after: dict
    changed: list


@dataclass(frozen=True)
class PlanProposed(Event):
    """A plan was proposed: its id, title, goal, envelope and fingerprint.

    A session event (`run_id` is `None`). The plan cannot be run until a
    person approves it (`PlanDecided`); see `service.plans`.
    """

    plan: dict


@dataclass(frozen=True)
class PlanDecided(Event):
    """A plan was approved, rejected or closed.

    `fingerprint` is the one the decision applied to, so the journal shows
    which plan, exactly, a person approved. A session event.
    """

    plan_id: str
    decision: str
    fingerprint: str
    reason: str = ""
