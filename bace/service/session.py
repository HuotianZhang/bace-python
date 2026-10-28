"""Owns one service session and serialises its event processing.

`Session` backs the routes in `app.py` and supports direct use in tests.
It owns a `Bench`, `Catalogue`, `Journal`, `RunWorker` and run registry.
Every job event, worker state, monitor reading and session event passes
through `Session._ingest`. That path stamps `seq`, `ts`, `run_id` and
`node_path`, attaches shot verdicts, appends journal lines, updates the
registry and replay ring, and fans out WebSocket frames. One lock and one
sequence counter make gaps meaningful to reconnecting clients. The ring
retains 5000 frames but full `StepDone` traces only for the last
`RING_TRACES_KEPT` shots, limiting memory use
(docs/service-contract.md section 3).

Worker callbacks run between instrument calls. With an attached asyncio
loop, `loop.call_soon_threadsafe` transfers processing to the loop so the
worker cannot wait on a socket. Without a loop, `Session._ingest` uses the
session lock on the caller's thread. Registry state changes follow
`RunStateChanged` on that event path, after preceding `NodeDone` data.
Otherwise `GET /runs/{id}` could report a finished run before its folder
and counts are visible (docs/service-contract.md section 2).

Two operations remain on the worker before handover. Digitiser diagnostics
are captured immediately after their `StepDone`, before another shot can
replace them. Open-node and shot counts are tracked on that thread so an
abort, which closes the generator before it can report, still produces a
synthesized `NodeDone` with folder and counts for each open node.

`Session.submit` validates against cached read-back and refuses `invalid`
or `crit` checks. A valid tree creates a `RunRecord` and queued `Job`.
At execution, the worker owns the bench, takes fresh read-back, attaches it
to the run, rechecks safety and builds `executor.run_pipeline`. A manual
run uses the same path as a one-node pipeline, preserving journal and
query semantics. Queued runs wait in FIFO order and can be cancelled.

`Session.bench_read` and `Session.bench_action` are worker jobs. Active
runs make them `Busy`, except park: it aborts the active run and cancels
queued runs before parking. Safety must not wait for the queued runs.
Every action is journaled as `BenchAction(by="hand")`, including refusal
reasons, and followed by read-back of its resulting state. This module
imports no FastAPI and touches instruments only through jobs.
"""
from __future__ import annotations

import asyncio
import collections
import dataclasses
import importlib.metadata
import logging
import os
import platform
import re
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping

from ..drivers.keithley2400 import SourceMeterConfig
from ..experiment import events as E
from ..experiment.jv import JVCurveDone, JVFinished, JVStarted
from ..experiment.rig import RigConfig
from ..experiment.transient import RunConfig
from ..experiment.wire import to_wire
from ..params import Source
from ..storage.naming import RunMetadata
from . import pipeline
from .executor import run_pipeline
from .journal import (Journal, add_curve, axis_result, count_shot, finished_result, jv_result,
                      loop_result, node_record)
from .live import LiveState
from .modules import Catalogue, RunContext, VocSource, coerce_sample, jsonable
from .monitors import (MAX_INTERVAL_S, MIN_INTERVAL_S, TEMPERATURE_MONITOR_S,
                       PowerMonitor, TemperatureMonitor)
from .pipeline import VOC_PARAM, Module, Schedule, Step, Validation
from .plans import PlanBook
from .rigs import ACTIONS, Bench, BenchActionRefused, Unavailable, action_before
from .wire import STEPDONE_ARRAYS, ephemeral_frame, is_ephemeral, make_envelope, payloads
from .worker import TERMINAL, Blocked, Job, RunWorker, StopMode

log = logging.getLogger("bace.service")

BENCH_STATES: tuple[str, ...] = ("idle", "preflight", "running", "paused", "stopping")
"""Defines cached bench states for `GET /bench`.
A queued run awaiting preflight reads as `preflight`. A terminal run still
being parked reads as `stopping`, reflecting ongoing safety work
(docs/service-contract.md section 4).
"""

RING_SIZE = 5000
"""Limits replay envelopes retained for `GET /events?since=`.
A typical 100-loop scan has about 300 frames, leaving room for several runs.
"""

RING_TRACES_KEPT = 200
"""Limits replay `StepDone` frames retaining trace arrays.
Four 1000-sample lists occupy about 130 KB per frame as measured on this
bench. A full ring can reach 600 MB in a 100-by-51 scan. Older frames keep
scalars and verdicts for Q-versus-loop replay, omit traces and declare that
in `decimated` (docs/service-contract.md section 3).
"""

SUBSCRIBER_BACKLOG = 1000
"""Limits a subscriber's unsent numbered frames before disconnection.
A slow client must not retain unbounded session memory. It receives a
`Notice` and can reconnect from `since`. Fast simulation can exceed socket
throughput. On this bench a shot takes about 0.8 s and three numbered frames,
so the limit holds about four minutes of stream.

Only numbered frames need replay. `StepPhase` never enters the ring and
costs much less memory: measured wire sizes are 77 KB for a decimated
`StepDone` and 193 bytes for `StepPhase`. Counting both would make the replay
limit depend on the module's live progress-event rate.

With a non-draining sink, a 21-by-60 scan queues 1001 numbered and 24
live-only frames at disconnection: 323 shots and about 25 MB, measured on
this bench. `EPHEMERAL_BACKLOG` already limits live-only frames, so numbered
counting adds about 2 % headroom without reducing memory. The two limits
separately express replay backlog and live-event staleness.

`Backlog` counts numbered frames in an `asyncio.Queue`. Caller-supplied
sinks are counted whole, which can over-count but cannot under-count.
The disconnect and replay policy follows docs/service-contract.md section 6.
"""

EPHEMERAL_BACKLOG = 50
"""Limits total unsent frames before suppressing live-only events.
`StepPhase` describes the current shot phase and is stale fifty frames
later. Every queued frame delays delivery, so this limit counts numbered
and unnumbered frames together.

Measured on this bench, suppression at fifty frames leaves about two dozen
live-only frames in flight while numbered frames accumulate toward the
thousand-frame disconnect limit. Thus a stalled queue remains almost
entirely replayable numbered frames.
"""

DATA_RUNS_KEPT = 20
"""Limits runs whose full arrays remain available from `GET /runs/{id}/data`.
Evicted runs retain folder paths for HDF5 access.
"""

WORKER_JOIN_S = 60.0

POWER_HISTORY_MAX = 200_000
"""Limits retained power readings for history and CSV to 200 000 points.
This spans 55 h at 1 Hz or 11 h at 0.2 s intervals. Session ownership keeps
the trace across monitor stops and interval changes. The shared replay ring
holds only 5000 envelopes; the journal retains every reading on disk
(docs/service-contract.md section 8).
"""
"""Extends the worker shutdown wait in seconds before bench release.
VISA resources use 20 s timeouts and acquisitions can wait up to 30 s.
A blocking call must return before another thread parks or closes those
same sessions (docs/service-contract.md section 9).
"""

LAST_USED_SOURCES: frozenset[Source] = frozenset({Source.EDITED, Source.LAST_USED})
"""Selects operator-choice layers for `RunQueued.params` and last-used values.
Defaults and `run.toml` values resolve from their own layers each run.
Journaling them as choices would mask recipe edits behind last-used values
(docs/service-contract.md section 3).
"""

_STEM = re.compile(r"[^A-Za-z0-9_.-]+")


# -- errors the routes map to status codes ------------------------------------
class SubmitRefused(ValueError):
    """Carries validation checks for an `invalid` or `crit` run request.

    The routes return every check with HTTP 422.
    """

    def __init__(self, validation: Validation):
        blocking = [c for c in validation.checks if c.level in ("invalid", "crit")]
        super().__init__("; ".join(f"{c.level} {c.code}: {c.text}" for c in blocking)
                         or "the tree was refused")
        self.validation = validation


class Conflict(RuntimeError):
    """Signals that session state refuses a well-formed request with HTTP 409."""


class Busy(Conflict):
    """Signals that a run prevents an independent bench read or action."""


class NotPaused(Conflict):
    """Signals a resume request without an open operator pause."""


class UnknownRun(KeyError):
    """Signals an unknown run with HTTP 404."""


class DataUnavailable(LookupError):
    """Signals that requested run arrays are unavailable in memory (404).

    Arrays can be evicted, absent or not yet produced. `folders` locates HDF5.
    """

    def __init__(self, run_id: str, reason: str, folders: list[str]):
        super().__init__(f"{run_id}: {reason}")
        self.run_id, self.reason, self.folders = run_id, reason, list(folders)


class NodeRequired(ValueError):
    """Signals a pipeline data request missing its required `node` (400)."""

    def __init__(self, run_id: str, nodes: list[str]):
        super().__init__(f"{run_id} has {len(nodes)} nodes; say which with node=<path>: "
                         + ", ".join(nodes))
        self.nodes = list(nodes)


class BlockedAtStart(Blocked):
    """Blocks Start when fresh read-back fails a `crit` safety check.

    The run makes no measurement writes and ends `blocked`, retaining the
    verdict text as its error (docs/service-contract.md section 2).
    """


# -- the registry -----------------------------------------------------------
@dataclass
class RunRecord:
    """Stores the in-memory record served by `GET /runs/{id}`.

    The journal retains the same facts as lines. This object avoids
    re-reading that file for active-session queries. Node paths key outcomes
    from `NodeStarted` and `NodeDone`. `params_as_executed` retains schedule
    parameters and their layers per node (docs/service-contract.md section 6).
    """

    run_id: str
    kind: str
    name: str
    tree: dict
    schedule: Schedule
    module: str | None
    out_folder: str
    folder: str | None
    counters: dict = field(default_factory=dict)
    cost: dict = field(default_factory=dict)
    state: str = "queued"
    parked: bool = False
    params_as_executed: dict[str, dict] = field(default_factory=dict)
    node_outcomes: dict[str, dict] = field(default_factory=dict)
    verdicts: list[dict] = field(default_factory=list)
    chain_at_start: dict | None = None
    folders: list[str] = field(default_factory=list)
    error: str | None = None
    sample: dict = field(default_factory=dict)
    """Retains the run's queued `[sample]` identity independently of the session.
    Each run carries its own identity (docs/service-contract.md section 6).
    """
    plan: str | None = None
    """The approved plan the run was submitted under, or None for a run a
    person started (`service.plans`)."""
    node_results: dict[str, dict] = field(default_factory=dict)
    """Reduces module results from journal payloads with the journal's reducers.
    `RunFinished`, `JVFinished` and `StepDone` feed the same `journal.node_record`
    shape for in-memory and file-backed runs, byte for byte
    (docs/service-contract.md section 6).
    """
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    kept: int | None = None
    requested: int | None = None
    progress: dict | None = None
    eta: dict | None = None
    """Retains the latest loop `Progress` with a measured ETA.
    Fields are `eta_s`, `finish_at`, `at`, `node_path`, `done` and `total`.
    Manual runs have no loop entry and use the module's `progress.eta_s`.
    """
    pending: dict | None = None
    position: int = 0
    evicted: bool = False
    live: LiveState | None = None
    """Carries `service.live` inferences for `/bench` during this run."""
    stopping: bool = False
    """Records that the session accepts a stop and reports `stopping`.
    The worker's subsequent report must not duplicate that journal entry.
    """
    session_voc: VocSource | None = None
    """Retains the session V_oc against which this tree resolves and validates.
    A queued `jv_bace` cannot replace this manual `bace` run's axis centre
    with a later measurement.
    """
    ended: threading.Event = field(default_factory=threading.Event)
    contexts: dict[str, RunContext] = field(default_factory=dict)
    """Indexes each module's `RunContext` by node path.
    Its `folders` exposes the recorder's active folder even if abort prevents
    `NodeDone`.
    """
    open_nodes: list[str] = field(default_factory=list)
    """Tracks open node paths on the worker thread."""
    tally: dict[str, dict] = field(default_factory=dict)
    """Tracks `module`, `kept` and `requested` per node on the worker thread.
    These supply `NodeDone` when abort prevents the generator from yielding it.
    """

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def all_folders(self) -> list[str]:
        """Returns folders from completed `NodeDone` events and active recorders."""
        out = list(self.folders)
        for ctx in list(self.contexts.values()):
            for folder in list(ctx.folders):
                if folder not in out:
                    out.append(folder)
        return out

    def nodes_wire(self) -> dict[str, dict]:
        """Returns completed modules in the journal's `nodes` shape.

        The results view receives the same representation from the registry
        and journal (docs/service-contract.md section 6).
        """
        out: dict[str, dict] = {}
        for path, entry in self.node_outcomes.items():
            detail = entry.get("detail") or {}
            if not detail.get("module") or entry.get("outcome") is None:
                continue
            out[path] = node_record(path, entry.get("outcome"), detail,
                                    self.node_results.get(path),
                                    started_at=entry.get("started_at"),
                                    finished_at=entry.get("finished_at"))
        return out

    def as_wire(self) -> dict:
        return {
            "run_id": self.run_id, "kind": self.kind, "name": self.name,
            "module": self.module, "state": self.state, "parked": self.parked,
            "tree": self.tree, "schedule": self.schedule.as_wire(),
            "counters": dict(self.counters), "cost": jsonable(self.cost),
            "params_as_executed": self.params_as_executed,
            "node_outcomes": jsonable(self.node_outcomes),
            "verdicts": list(self.verdicts), "chain_at_start": self.chain_at_start,
            "sample": dict(self.sample), "plan": self.plan, "nodes": self.nodes_wire(),
            "folder": self.folder, "folders": self.all_folders(), "error": self.error,
            "queued_at": self.queued_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "kept": self.kept,
            "requested": self.requested, "progress": self.progress, "eta": self.eta,
            "pending": self.pending, "position": self.position,
            "data_in_memory": not self.evicted, "from": "session",
        }


def _version() -> str:
    try:
        return importlib.metadata.version("bace")
    except Exception:                                       # noqa: BLE001
        pass
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, "pyproject.toml"), "rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
    except Exception:                                       # noqa: BLE001
        return "unknown"


def _voc_wire(voc: VocSource | None) -> dict:
    if voc is None:
        return {"value": None}
    return {"value": voc.value, "led_v": voc.led_v,
            "from": {"run_id": voc.run_id, "node_path": voc.node_path, "ts": voc.ts,
                     "how": voc.how}}


def _module_of(node_path: str) -> str:
    return node_path.rsplit("/", 1)[-1].split("#", 1)[0]


def _bench_state(run_state: str) -> str:
    """Maps a run state to the `BENCH_STATES` vocabulary for `GET /bench`."""
    if run_state in BENCH_STATES:
        return run_state
    if run_state == "queued":
        return "preflight"
    return "stopping"                     # terminal, the worker still parking


def _slim_step_done(frame: dict) -> dict:
    """Copies replay `StepDone` scalars and verdict without trace arrays.

    `decimated` declares the four omitted traces.
    """
    data = dict(frame["data"])
    info = dict(frame.get("decimated") or {})
    for name in STEPDONE_ARRAYS:
        if name in data:
            data[name] = None
            info[name] = {"omitted": True, "replay": True}
    return {**frame, "data": data, "decimated": info}


def _stem(name: str) -> str:
    return _STEM.sub("_", name.strip()).strip("_") or "pipeline"


class Backlog(asyncio.Queue):
    """Counts numbered frames in a subscriber's `asyncio.Queue`.

    `SUBSCRIBER_BACKLOG` bounds replay work and queue memory. Counting
    incrementally avoids walking the queue for every frame and subscriber
    on the event-loop thread, where repeated scans would delay other tasks.

    `_put` and `_get` are the `asyncio.Queue` extension hooks also used by
    `LifoQueue` and `PriorityQueue`. They cover all insertion and removal
    paths, including `get_nowait`. The terminal `None` and drop `Notice`
    have no sequence number and do not count toward replay backlog.
    """

    def _init(self, maxsize: int) -> None:
        super()._init(maxsize)
        self.numbered = 0

    def _put(self, item: Any) -> None:
        if isinstance(item, dict) and item.get("seq") is not None:
            self.numbered += 1
        super()._put(item)

    def _get(self) -> Any:
        item = super()._get()
        if isinstance(item, dict) and item.get("seq") is not None:
            self.numbered -= 1
        return item


def backlog_of(sink: Any) -> int:
    """Returns subscriber backlog in replayable numbered frames.

    Caller-supplied sinks without `Backlog` counting, including test lists
    and plain queues, are counted whole. This can over-count but cannot
    under-count.
    """
    numbered = getattr(sink, "numbered", None)
    return sink.qsize() if numbered is None else numbered


# -- the session --------------------------------------------------------------
class Session:
    """Owns one service process lifetime, as described by the module."""

    def __init__(self, rig_config: RigConfig, run_toml: Mapping[str, Any] | None = None, *,
                 out: str, mode: str = "sim", fast: bool = False,
                 smu_config: SourceMeterConfig | None = None,
                 run_config_defaults: RunConfig | None = None,
                 sample: Mapping[str, Any] | None = None,
                 rig_path: str | None = None, run_path: str | None = None,
                 session_id: str | None = None, seed: int = 0,
                 power_monitor_s: float | None = None,
                 temperature_monitor_s: float | None = TEMPERATURE_MONITOR_S):
        if mode not in ("sim", "rig"):
            raise ValueError(f"mode must be 'sim' or 'rig', not {mode!r}")
        self.rig_config = rig_config
        self.power_monitor_s = None if power_monitor_s is None else float(power_monitor_s)
        """Starts power monitoring at this interval in seconds, or leaves it off.
        `start` uses the `--power-monitor` setting to watch from service startup.
        With `None`, `POST /monitors/power` controls startup instead.
        """
        self.temperature_monitor_s = (None if temperature_monitor_s is None
                                      else float(temperature_monitor_s))
        """Starts 331 monitoring at this interval in seconds, or leaves it off.
        A cryostat can drift between runs. Default monitoring starts with the
        service, supplies readings of known age and journals temperature while
        no console is connected (docs/service-contract.md section 8).
        `None` implements `--no-temperature-monitor`; the monitor then starts
        only through `POST /monitors/temperature`.

        Power monitoring defaults to off and is enabled by `power_monitor_s`:
        a meter behind a dark box need not produce a continuous trace. Bus
        ownership keeps default temperature monitoring safe. On GPIB,
        `start_temperature_monitor` passes the worker lock; a busy tick is
        skipped instead of overlapping an acquisition.

        At 5 s intervals an idle day adds about 17 000 journal lines and a few
        megabytes, as measured on this bench. The 5000-envelope ring then
        spans about seven idle hours instead of several runs. This is the
        storage cost of retaining overnight temperature; the command-line
        flag can disable it.
        """
        self.run_toml: dict = dict(run_toml or {})
        self.out = str(out)
        self.mode = mode
        self.fast = bool(fast)
        self.rig_path = rig_path
        self.run_path = run_path
        self.started_at = time.time()
        self.session_id = session_id or datetime.fromtimestamp(self.started_at).strftime(
            "%Y%m%d_%H%M%S")
        os.makedirs(self.out, exist_ok=True)

        if mode == "sim":
            self.bench = Bench.build_simulated(rig_config, seed=seed, fast=self.fast,
                                               run_config=run_config_defaults,
                                               rig_path=rig_path)
        else:
            self.bench = Bench.build_real(rig_config, smu_config or SourceMeterConfig(),
                                          run_config=run_config_defaults or RunConfig(),
                                          rig_path=rig_path)
        sample_table = sample if sample is not None else self.run_toml.get("sample", {})
        self.journal = Journal(self.out, self.session_id, header={
            "mode": mode, "rig_toml": rig_path, "run_toml": run_path, "out": self.out,
            "fingerprint": self.bench.fingerprint, "python": platform.python_version(),
            "version": _version(), "fast": self.fast,
            # Assembly writes precede all jobs. They belong in the session
            # header because neither a run nor a by-hand action contains them.
            "startup_writes": list(self.bench.startup_writes),
            # The header identifies a session even without runs. Each
            # `RunQueued.sample` repeats identity because resumed files retain
            # the header from the first process.
            "sample": dict(sample_table or {})})
        self.catalogue = Catalogue(rig_config=rig_config, run_toml=self.run_toml,
                                   history=self.journal, sample=sample_table)
        self.plans = PlanBook(self.session_id)
        self._sample_file: dict = dict(self.catalogue.sample)
        """Retains the startup `[sample]` layer for `set_sample` resets.
        A null restores that layer, like a parameter reset, without requiring
        `ParamSet` for this block.
        """
        self.worker = RunWorker(on_event=self._on_event, on_state=self._on_state)

        self._lock = threading.RLock()
        self._seq = self.journal.seq
        self._ring: collections.deque[dict] = collections.deque(maxlen=RING_SIZE)
        self._traces: collections.deque[dict] = collections.deque(maxlen=RING_TRACES_KEPT)
        """Retains full `StepDone` frames behind reduced replay copies."""
        self._subscribers: list[Any] = []
        self._records: dict[str, RunRecord] = {}
        self._jobs: dict[str, Job] = {}
        self._job_done: dict[str, threading.Event] = {}
        self._job_count = 0
        self._run_count = len(self.journal.run_index("this")) if self.journal.resumed else 0
        self._data: dict[tuple[str, str], dict] = {}
        self._data_order: list[str] = []
        self._snapshot: dict | None = None
        self._temperature: dict | None = None
        """Retains the latest `TemperatureRead` from the shared event path.
        Monitor readings, pause polls and typed values can supersede read-back
        on the temperature card.
        """
        self.session_voc: VocSource | None = None
        self._monitor: PowerMonitor | None = None
        self._temperature_monitor: TemperatureMonitor | None = None
        self._power_history: collections.deque[tuple] = collections.deque(maxlen=POWER_HISTORY_MAX)
        """Stores power readings with frame timestamps, newest last.
        Rows contain `ts`, `watts`, `trustworthy`, `wavelength_nm`, `source` and
        `averaged`. Shared timestamps permit stream merging without duplicates.
        """
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self.last_validated: dict | None = None
        self.errors: list[str] = []
        """Collects event-path and shutdown errors without raising them into jobs.
        Payload or journal failure must not stop measurement. Each entry is
        logged and counted by `session_info`, so missing journal data remains
        visible in the console and service log.
        """
        self._closed = False

    def _error(self, text: str) -> None:
        self.errors.append(text)
        log.warning("%s", text)

    # -- lifecycle ------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> "Session":
        if loop is not None:
            self.attach_loop(loop)
        self.worker.start()
        if self.power_monitor_s is not None and not self.monitor_running:
            # A requested power monitor without a meter reports a startup error
            # without preventing the service from starting.
            try:
                self.start_power_monitor(self.power_monitor_s)
            except (ValueError, Conflict) as exc:
                self._error(f"--power-monitor: {exc}")
        if self.temperature_monitor_s is not None and not self.temperature_monitor_running:
            try:
                self.start_temperature_monitor(self.temperature_monitor_s)
            except (ValueError, Conflict) as exc:
                # No 331 is a supported bench configuration. Default monitoring
                # must not turn its absence into a session error. The read-back's
                # `unavailable["temperature"]` already explains the missing reading.
                log.info("the temperature monitor was not started: %s", exc)
        return self

    def attach_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Attaches the event path to asyncio from the loop's own thread.

        The recorded thread may call `_ingest` directly. Other threads must
        post callbacks to the loop (docs/service-contract.md section 2).
        """
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._loop_thread = threading.current_thread()

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stops monitors, drains the worker, releases the bench and closes the journal.

        Repeated calls are safe. Abort takes effect after a blocking VISA call
        returns. The worker must end before another thread parks or closes
        its resources, or instrument I/O and generator `finally` blocks could
        use closed sessions. If the worker remains alive after `timeout_s`
        plus `WORKER_JOIN_S`, the bench stays open and the error is recorded.
        The daemon thread ends with the process
        (docs/service-contract.md section 9).
        """
        if self._closed:
            return
        self._closed = True
        self.stop_power_monitor()
        self.stop_temperature_monitor()
        ended = self.worker.shutdown(timeout_s)
        if not ended:
            self._error(f"shutdown: the worker is still inside a job after {timeout_s:g} s; "
                        f"waiting up to {WORKER_JOIN_S:g} s more before the bench is released")
            ended = self.worker.join(WORKER_JOIN_S)
        if ended:
            self.bench.close()
        else:
            self._error("shutdown: the worker never ended; the bench was not parked or "
                        "released from this thread because a job is still on it")
        self.journal.close()

    def session_info(self) -> dict:
        return {"id": self.session_id, "mode": self.mode, "fast": self.fast,
                "started": datetime.fromtimestamp(self.started_at).isoformat(timespec="seconds"),
                "started_at": self.started_at, "sample": dict(self.catalogue.sample),
                # The startup layer lets the console offer a reset to `run.toml`.
                # This sample block has two layers but no `ParamSet` renderer.
                "sample_file": dict(self._sample_file),
                # Naming's `slug` and `NAME_MAX` determine the folder representation:
                # `PTQ10:IT-4F` becomes `PTQ10IT-4F`. The console displays that result
                # without independently reducing the value in the browser.
                "sample_in_name": dict(zip(("sample", "material", "pixel"),
                                           self._metadata_for_name().identity_in_name())),
                "out": self.out, "rig_toml": self.rig_path, "run_toml": self.run_path,
                "fingerprint": self.bench.fingerprint, "journal": self.journal.path,
                "errors": len(self.errors),
                "last_error": self.errors[-1] if self.errors else None}

    def _metadata_for_name(self) -> RunMetadata:
        """Returns identity metadata for the `sample_in_name` preview.

        `storage.naming` supplies the same reduction as `folder_name`.
        """
        s = self.catalogue.sample
        return RunMetadata(sample=str(s.get("sample", "")), material=str(s.get("material", "")),
                           pixel=str(s.get("pixel", "")))

    def set_sample(self, values: Mapping[str, Any]) -> dict:
        """Updates device identity through `PUT /session/sample`.

        Missing keys retain their values. Explicit `None` restores the startup
        `run.toml` layer. Merge semantics let the console edit four fields
        without dropping a recipe's `temperature_k`.

        `folder_name` applies `slug` to identity fields, handling colons, path
        separators and `..` at name construction. Metadata retains the verbatim
        value, so a material such as `PTQ10:IT-4F` is recorded accurately.
        `sample_in_name` previews the folder form. Identity must be correct
        before recording because renaming folders compromises the record.

        Changes bind only subsequently queued runs. Submission copies identity
        into `RunRecord.sample` and `RunQueued.sample`; active and queued runs
        retain their own copy. Updates are therefore allowed during a run,
        letting later runs use the correct identity without abandoning the
        active one. They cannot repair the active run's identity
        (docs/service-contract.md section 3a).

        Returns `session`, `changed` and `run_active`. Unknown keys or an
        unparsable `temperature_k` raise `ValueError` for HTTP 422.
        """
        patch = dict(values or {})
        # Validate the whole block before applying any key, preventing a
        # partial update on error. `coerce_sample` drops `None`, but here
        # `None` means restore the file layer, so its result is discarded.
        coerce_sample(patch)
        with self._lock:
            before = dict(self.catalogue.sample)
            merged = dict(before)
            for key, value in patch.items():
                if value is None:
                    # A field absent in the file is removed on reset. An empty
                    # string would incorrectly imply a named field with no content.
                    if key in self._sample_file:
                        merged[key] = self._sample_file[key]
                    else:
                        merged.pop(key, None)
                else:
                    merged[key] = value
            after = coerce_sample(merged)
            changed = sorted(k for k in set(before) | set(after)
                             if before.get(k) != after.get(k))
            if changed:
                # Replace rather than mutate: `base_metadata` and submit read this
                # dictionary from other threads. Readers need one complete identity,
                # even if they retain the preceding object.
                self.catalogue.sample = after
            active = self.run_active()
        if changed:
            self._dispatch(None, E.SampleNamed(before=before, after=dict(after),
                                               changed=list(changed)), node_path="")
        return {"session": self.session_info(), "changed": list(changed),
                "run_active": active}

    def hello(self) -> dict:
        """Returns `data` for the first WebSocket frame."""
        return {"session": self.session_info(), "seq": self.last_seq,
                "bench": self.bench_snapshot()}

    # -- runs -------------------------------------------------------------------
    def validate_tree(self, tree_obj: Mapping[str, Any], *, name: str = "",
                      remember: bool = True) -> Validation:
        """Validates a Dry run against cached read-back without instrument I/O.

        `remember=False` preserves `last_validated`, so assistant queries do
        not replace the tree reopened by the pipeline tab
        (docs/service-contract.md sections 7 and 8a).
        """
        v, _voc = self._validate(tree_obj)
        if remember and v.schedule is not None:
            self.last_validated = {"tree": v.schedule.tree.as_wire(), "name": name,
                                   "validated_at": time.time(), "valid": v.valid}
        return v

    def _validate(self, tree_obj: Mapping[str, Any]) -> tuple[Validation, VocSource | None]:
        with self._lock:
            snapshot, voc = self._snapshot, self.session_voc
        v = pipeline.validate(tree_obj, self.catalogue, bench=snapshot, history=self.journal,
                              session_voc=voc, rig_config=self.rig_config)
        return v, voc

    def pipeline_folder(self, tree_obj: Mapping[str, Any] | None, name: str = "") -> str:
        """Returns `<out>/<stem>` for a named pipeline's output folder.

        Submission adds its timestamp; `pipeline_folder_pattern` shows that form.
        """
        root_name = tree_obj.get("name", "") if isinstance(tree_obj, Mapping) else ""
        return os.path.join(self.out, _stem(str(name or root_name or "pipeline")))

    def pipeline_folder_pattern(self, tree_obj: Mapping[str, Any] | None, name: str = "") -> str:
        """Returns the output pattern `<out>/<stem>_YYYYMMDD_HHMMSS`.

        Only `submit` assigns the timestamp. A Dry run cannot supply an actual
        folder name without inventing a path that execution will not use.
        """
        return self.pipeline_folder(tree_obj, name) + "_YYYYMMDD_HHMMSS"

    def submit(self, tree_obj: Mapping[str, Any], name: str = "",
               kind: str | None = None, plan: str | None = None) -> tuple[str, Validation]:
        """Validates and queues a tree, returning its run ID and validation.

        `kind` is `manual` for one module or `pipeline`; `None` infers it from
        the root. Manual runs record directly under `out`, as `tools/scan.py`
        does. Pipeline modules record under `<out>/<stem>_<stamp>/`
        (docs/service-contract.md section 6).

        `plan` names an approved plan the tree must fit. A tree outside the
        plan's envelope, or a plan that is not open, refuses the run with the
        check `plan.envelope` (docs/service-contract.md section 8b).
        """
        if self._closed:
            raise Conflict("the session is closed")
        name = str(name or "")
        v, voc_at_submit = self._validate(tree_obj)
        cost_s = float(v.cost.get("total_s") or 0.0)
        if plan is not None and v.schedule is not None:
            refusals = self.plans.check(plan, v.schedule.tree, cost_s)
            if refusals:
                v.checks.append(pipeline.plan_verdict(plan, refusals))
                v.valid = False
        if v.schedule is not None:
            self.last_validated = {"tree": v.schedule.tree.as_wire(), "name": name,
                                   "validated_at": time.time(), "valid": v.valid}
        if not v.valid or v.schedule is None:
            # Journal refusal reasons as well as returning HTTP 422, so the
            # evidence survives the response.
            try:
                self.journal.append({
                    "seq": None, "ts": time.time(), "run_id": None,
                    "node_path": "", "type": "RunRefused",
                    "data": {"name": name,
                             "checks": [to_wire(c)[0] for c in v.checks
                                        if c.level in ("invalid", "crit")]},
                    "decimated": {}})
            except Exception:                           # noqa: BLE001
                pass
            raise SubmitRefused(v)
        tree = v.schedule.tree
        inferred = "manual" if isinstance(tree, Module) else "pipeline"
        kind = kind or inferred
        if kind not in ("manual", "pipeline"):
            raise ValueError(f"kind must be 'manual' or 'pipeline', not {kind!r}")
        if kind == "manual" and not isinstance(tree, Module):
            raise ValueError("a manual run is a single module node; this tree is a loop")

        with self._lock:
            self._run_count += 1
            run_id = f"{self.session_id}-{self._run_count:03d}"
            queued_at = time.time()
            if kind == "manual":
                folder, out_folder, module = None, self.out, tree.module
            else:
                stamp = datetime.fromtimestamp(queued_at).strftime("%Y%m%d_%H%M%S")
                folder = f"{self.pipeline_folder(tree_obj, name)}_{stamp}"
                out_folder, module = folder, None
            rec = RunRecord(run_id=run_id, kind=kind, name=name, tree=tree.as_wire(),
                            schedule=v.schedule, module=module, out_folder=out_folder,
                            folder=folder, counters=dict(v.counters), cost=dict(v.cost),
                            queued_at=queued_at, session_voc=voc_at_submit,
                            sample=dict(self.catalogue.sample), plan=plan,
                            live=LiveState(self.rig_config))
            rec.params_as_executed = {
                s.node_path: {n: pv.as_dict() for n, pv in s.params.items()}
                for s in v.schedule.modules}
            rec.verdicts = [to_wire(c)[0] for c in v.checks if c.level != "ok"]
            # `last_used_params` carries choices from `LAST_USED_SOURCES`, never
            # V_oc. A measured V_oc must retain its measured provenance, and even
            # a typed V_oc belongs to one illumination and bench condition.
            # Reusing it at another LED level is unsafe. `resolved` retains the
            # complete executed record.
            params = resolved = None
            if kind == "manual":
                step = v.schedule.modules[0]
                params = {n: pv.value for n, pv in step.params.items()
                          if pv.source in LAST_USED_SOURCES and n != VOC_PARAM}
                resolved = {n: pv.value for n, pv in step.params.items()}
            job = Job(run_id, "run", self._make_run(rec), park=self.bench.rig.park, name=name)
            # Queue before journaling: shutdown can refuse submission. A
            # `RunQueued` entry without an actual queued job would remain
            # permanently queued in the index.
            try:
                rec.position = self.worker.submit(job)
            except (RuntimeError, ValueError) as exc:
                self._run_count -= 1
                raise Conflict(f"cannot queue {run_id}: {exc}") from None
            self._records[run_id] = rec
            self._jobs[run_id] = job
            if plan is not None:
                self.plans.charge(plan, run_id, cost_s)
            # One event path preserves shared sequence, journal and fan-out.
            self._ingest(run_id, "", E.RunQueued(
                kind=kind, module=module, tree=rec.tree, params=jsonable(params),
                resolved=jsonable(resolved), name=name, folder=folder,
                sample=dict(rec.sample), plan=plan), queued_at, None)
            self._ingest(run_id, "", E.RunStateChanged("queued", "submitted"), time.time(), None)
            # Journal submit-time flags such as `trigger.auto`, `intensity.factor`
            # and `temperature.not-wired` for file-backed results too. `_apply`
            # replaces by code and node path, avoiding duplicates in `rec.verdicts`.
            for check in v.checks:
                if check.level != "ok":
                    self._ingest(run_id, check.node_path, check, time.time(), None)
        return run_id, v

    # -- plans ------------------------------------------------------------------
    def propose_plan(self, data: Mapping[str, Any]) -> dict:
        """Records a proposed plan and journals it; nothing runs until a person
        approves it. Raises `plans.PlanError` for a plan that cannot be accepted."""
        with self._lock:
            plan = self.plans.propose(data, catalogue=self.catalogue)
        wire = plan.as_wire()
        self._dispatch(None, E.PlanProposed(plan=wire), node_path="")
        return wire

    def decide_plan(self, plan_id: str, decision: str, *, fingerprint: str = "",
                    reason: str = "") -> dict:
        """Approves, rejects or closes a plan and journals the decision with
        the fingerprint it applied to. Unknown plan: `KeyError`."""
        with self._lock:
            plan = self.plans.decide(plan_id, decision, fingerprint_seen=fingerprint,
                                     reason=reason)
        self._dispatch(None, E.PlanDecided(plan_id=plan.plan_id, decision=decision,
                                           fingerprint=plan.fingerprint,
                                           reason=plan.reason), node_path="")
        return plan.as_wire()

    def plans_wire(self) -> list[dict]:
        with self._lock:
            return [p.as_wire() for p in self.plans.all()]

    def _make_run(self, rec: RunRecord) -> Callable[[Job], Any]:
        def make(job: Job):
            # Exclusive bench ownership permits fresh read-back and safety
            # checks at execution, independent of the cached submission snapshot.
            run_config = self._run_config_of(rec.schedule)
            if run_config is not None:
                self.bench.run_config = run_config
            with self._lock:
                voc = self.session_voc
            snapshot = self.bench.read_back(run_config=run_config, voc=voc,
                                            monitor=self.monitor_running)
            with self._lock:
                self._snapshot = snapshot
                rec.chain_at_start = snapshot["chain"]
            # Keep the V_oc used for validation even if a queued `jv_bace`
            # changes the session value. Fresh `crit` blocks safety hazards;
            # `invalid` also blocks, including instruments absent after Dry run,
            # rather than starting a run its builder must refuse.
            check = pipeline.validate(rec.tree, self.catalogue, bench=snapshot,
                                      history=self.journal, session_voc=rec.session_voc,
                                      rig_config=self.rig_config)
            # The tree's chain verdict can differ from the bench card's: `bace`
            # writes 33220A polarity, making NORM `info`. A bench `warn` would
            # replace the tree-specific entry because records key by code.
            chain = [c for c in check.checks if c.code.startswith("chain.")]
            tree_codes = {c.code for c in chain}
            for verdict in self.bench.chain_verdicts(snapshot):
                if verdict.code not in tree_codes:
                    self._dispatch(job, verdict, node_path="")
            for c in chain:
                if c.level != "ok":
                    self._dispatch(job, c, node_path="")
            blocking = [c for c in check.checks if c.level in ("invalid", "crit")]
            for c in blocking:
                self._dispatch(job, c, node_path="")
            if blocking:
                raise BlockedAtStart("blocked at start: " + "; ".join(
                    f"{c.code}: {c.text}" for c in blocking))
            if rec.kind == "pipeline":
                # Creating the parent at Start ensures the reported path exists
                # even if a pipeline stops before recording its first module.
                os.makedirs(rec.out_folder, exist_ok=True)
            return run_pipeline(self.bench.rig, rec.schedule, catalogue=self.catalogue,
                                ctx_factory=self._ctx_factory(rec, job), job=job,
                                on_voc=self._set_voc, session_voc=rec.session_voc,
                                emit=lambda ev: self._dispatch(job, ev))
        return make

    def _ctx_factory(self, rec: RunRecord, job: Job) -> Callable[[Step], RunContext]:
        # Only abort cuts sleep short. `after_shot` retains the entire shot,
        # including settle (see `Bench.sleeper`).
        sleep = self.bench.sleeper(lambda: job.stop_mode == StopMode.ABORT)

        def factory(step: Step) -> RunContext:
            ctx = RunContext(run_id=rec.run_id, node_path=step.node_path,
                             out_folder=rec.out_folder,
                             # The queued sample copy labels every folder.
                             # `set_sample` can change session identity during
                             # a run, but cannot split its folders between
                             # names or contradict `RunQueued.sample`.
                             metadata=self.catalogue.base_metadata(rec.sample),
                             sleep=sleep, abort=job.abort_check,
                             resolved=self._resolved(step),
                             on_data=self._data_sink(rec.run_id),
                             # `on_poll` reads the 331 during the blocked operator wait.
                             wait_for_operator=lambda detail, on_poll=None:
                                 job.wait_for_operator(on_poll=on_poll))
            if step.kind == "module":
                rec.contexts[step.node_path] = ctx
            return ctx
        return factory

    def _resolved(self, step: Step) -> dict[str, str]:
        """Seeds recorder `resolved` data with chain read-back and transient settings.

        The run does not read 33220A polarity itself. Transient entries also
        retain the LED levels and frequency the module will set.
        """
        out: dict[str, str] = {}
        with self._lock:
            snapshot = self._snapshot
        if snapshot is not None:
            inst = snapshot.get("instruments") or {}
            led, bias = inst.get("led") or {}, inst.get("bias") or {}
            for key, value in (("led_output_polarity", led.get("polarity")),
                               ("bias_arm_source", bias.get("arm_source")),
                               ("bias_arm_slope", bias.get("arm_slope"))):
                if value not in (None, "?"):
                    out[key] = str(value)
        if step.kind == "module" and step.relay == "transient":
            v = step.values()
            if v.get("led_v") is not None:
                low = v.get("led_low_v")
                out["led_levels_v"] = (f"{float(v['led_v']):g}"
                                       + (f"/{float(low):g}" if low is not None else ""))
            if v.get("pulse_frequency_hz") is not None:
                out["led_frequency_hz"] = f"{float(v['pulse_frequency_hz']):g}"
        return out

    @staticmethod
    def _run_config_of(schedule: Schedule) -> RunConfig | None:
        names = {f.name for f in dataclasses.fields(RunConfig)}
        for step in schedule.modules:
            if step.relay == "transient":
                v = step.values()
                try:
                    return RunConfig(**{k: v[k] for k in names if k in v})
                except (ValueError, TypeError):
                    return None
        return None

    def _data_sink(self, run_id: str) -> Callable[[str, dict], None]:
        def sink(node_path: str, data: dict) -> None:
            with self._lock:
                self._data[(run_id, node_path)] = data
                if run_id not in self._data_order:
                    self._data_order.append(run_id)
                while len(self._data_order) > DATA_RUNS_KEPT:
                    old = self._data_order.pop(0)
                    for key in [k for k in self._data if k[0] == old]:
                        del self._data[key]
                    if old in self._records:
                        self._records[old].evicted = True
        return sink

    def _set_voc(self, source: VocSource) -> None:
        with self._lock:
            self.session_voc = source

    def stop(self, run_id: str, mode: str = StopMode.AFTER_SHOT) -> dict:
        """Requests `after_shot` or `abort`, or cancels a queued run.

        Unknown IDs return 404; terminal runs return 409
        (docs/service-contract.md section 6).
        """
        if mode not in StopMode.ALL:
            raise ValueError(f"stop mode must be one of {StopMode.ALL}, not {mode!r}")
        job, rec = self._job(run_id)
        if job.state == "queued" and self.worker.cancel(run_id):
            return {"run_id": run_id, "state": "cancelled", "mode": mode}
        if job.terminal or rec.terminal:
            raise Conflict(f"{run_id} is already {rec.state}")
        # Set `rec.stopping` first so the worker's next state callback cannot
        # duplicate the session's `stopping` entry. Report acceptance at once:
        # a long shot can delay the worker's next event, while the record,
        # `/bench` and stream must already show the pending stop.
        with self._lock:
            first = not rec.stopping
            rec.stopping = True
        in_force = job.request_stop(mode)
        if first:
            self._dispatch(job, E.RunStateChanged("stopping", f"{in_force} requested"),
                           node_path="")
        return {"run_id": run_id, "state": "stopping", "mode": in_force}

    def resume(self, run_id: str, detail: Mapping[str, Any] | None = None) -> dict:
        """Answers `NeedsOperator`, refusing an unpaused run with HTTP 409."""
        job, rec = self._job(run_id)
        if rec.terminal:
            raise NotPaused(f"{run_id} is already {rec.state}; nothing to resume")
        if not job.resume(dict(detail or {})):
            raise NotPaused(f"{run_id} is {rec.state}, not paused; nothing to resume")
        return {"run_id": run_id, "state": rec.state}

    def cancel(self, run_id: str) -> bool:
        self._job(run_id)
        return self.worker.cancel(run_id)

    def wait_run(self, run_id: str, timeout_s: float | None = None) -> bool:
        """Waits for park or cancellation for scripts and tests.

        The app observes the event stream instead.
        """
        return self._job(run_id)[1].ended.wait(timeout_s)

    def _job(self, run_id: str) -> tuple[Job, RunRecord]:
        with self._lock:
            job, rec = self._jobs.get(run_id), self._records.get(run_id)
        if job is None or rec is None:
            raise UnknownRun(run_id)
        return job, rec

    def run_record(self, run_id: str) -> dict:
        """Returns a registry run record or its journal reconstruction.

        `GET /runs/{id}` serves the summary, tree and nodes across sessions.
        Journal records carry `from="journal"`. Unknown IDs return 404
        (docs/service-contract.md section 6).
        """
        with self._lock:
            rec = self._records.get(run_id)
            if rec is not None:
                return rec.as_wire()
        hit = self.journal.run_record(run_id)
        if hit is None:
            raise UnknownRun(run_id)
        return {**hit, "from": "journal", "data_in_memory": False}

    @property
    def records(self) -> list[RunRecord]:
        with self._lock:
            return list(self._records.values())

    def runs_index(self, session: str = "this") -> list[dict]:
        """Returns journal run summaries newest first across selected sessions."""
        return self.journal.run_index(session)

    def run_data(self, run_id: str, node_path: str | None = None) -> dict:
        """Returns one node's full-precision NumPy arrays.

        The route applies `modules.jsonable`. Unknown or unavailable data
        returns 404; a pipeline request without a node returns 400
        (docs/service-contract.md section 6).
        """
        with self._lock:
            rec = self._records.get(run_id)
            if rec is None:
                raise UnknownRun(run_id)
            have = [k[1] for k in self._data if k[0] == run_id]
            if not have:
                reason = ("no longer in memory (older than the last "
                          f"{DATA_RUNS_KEPT} runs); read the HDF5" if rec.evicted
                          else "no data yet")
                raise DataUnavailable(run_id, reason, rec.all_folders())
            nodes = list(rec.schedule.node_paths)
            if node_path is None:
                # Node paths are part of every pipeline data address, even with one
                # module. Special-casing small trees would break clients on larger
                # trees (docs/service-contract.md section 6).
                if rec.kind == "pipeline" or len(nodes) > 1:
                    raise NodeRequired(run_id, nodes)
                node_path = nodes[0] if nodes else have[0]
            data = self._data.get((run_id, node_path))
            if data is None:
                raise DataUnavailable(run_id, f"no data for node {node_path!r}; have: "
                                      + ", ".join(have), rec.all_folders())
            return data

    # -- the bench ----------------------------------------------------------------
    def run_active(self) -> str | None:
        """Returns the ID of a run holding or waiting for the bench, or `None`."""
        current = self.worker.current
        if current is not None and current.kind == "run" and not current.terminal:
            return current.id
        for job in self.worker.queued:
            if job.kind == "run":
                return job.id
        return None

    def bench_read(self, *, wait: bool = True, timeout_s: float = 30.0) -> dict:
        """Queues bench read-back and returns its snapshot or immediate job ID.

        `wait=False` returns `{"job": id}`. An active run raises `Busy`
        (docs/service-contract.md section 4).
        """
        def make(job: Job):
            def gen():
                snapshot = self._read_back()
                for verdict in self.bench.chain_verdicts(snapshot):
                    yield verdict
            return gen()

        # `submit` uses the same lock, preventing a run from entering the
        # queue between the busy check and this job's submission.
        with self._lock:
            active = self.run_active()
            if active is not None:
                raise Busy(f"run {active} is active; the chain is re-read at its Start")
            job_id, done = self._new_job_id("readback")
            job = Job(job_id, "readback", make)
            self.worker.submit(job)
        if not wait:
            return {"job": job_id}
        self._await(job, done, timeout_s)
        # Both waiting and immediate responses identify the executed job.
        return {"job": job_id, **self.bench_snapshot()}

    def bench_action(self, name: str, args: Mapping[str, Any] | None = None, *,
                     wait: bool = True, timeout_s: float = 30.0) -> dict:
        """Queues an explicit bench action and journals it as `by="hand"`.

        Unknown names raise `KeyError` (404), invalid bodies `ValueError`
        (422), and refusal `BenchActionRefused` (409 with verdict). Active
        runs raise `Busy`, except park, which aborts the run and cancels
        queued runs first (docs/service-contract.md section 4).
        `BenchAction.result.before` retains the changed fields' read-back
        values so the journal can show the transition.

        A park queued longer than `timeout_s` remains queued: blocking VISA
        may outlast the caller's wait. Cancelling it would omit the requested
        safety action and its by-hand journal record. The response reports
        `pending=True`; the job runs when the worker becomes available.
        """
        if name not in ACTIONS:
            raise KeyError(name)
        args = dict(args or {})
        outcome: dict[str, Any] = {}
        # Snapshot `bace` parameters on the caller's thread. The loop can edit
        # catalogue layers through `PUT /modules/bace/params`; worker-thread
        # iteration during an edit could encounter a changing dictionary.
        led_params = self.catalogue.param_set("bace").values()

        def make(job: Job):
            def gen():
                with self._lock:
                    before = action_before(name, self._snapshot)
                try:
                    result = self.bench.action(name, args, led_params=led_params)
                except BenchActionRefused as exc:
                    outcome["error"] = exc
                    result = {"refused": True, "level": exc.level, "text": exc.text}
                    yield E.Verdict(level=exc.level, code=f"action.{name}",
                                    text=f"{name} refused: {exc.text}",
                                    data={"action": name, "args": args})
                except ValueError as exc:
                    outcome["error"] = exc
                    result = {"refused": True, "level": "invalid", "text": str(exc)}
                if before:
                    result = {**result, "before": before}
                outcome["result"] = result
                yield E.BenchAction(name=name, args=args, result=jsonable(result), by="hand")
                snapshot = self._read_back()
                for verdict in self.bench.chain_verdicts(snapshot):
                    yield verdict
            return gen()

        with self._lock:
            active = self.run_active()
            if active is not None:
                if name != "park":
                    raise Busy(f"run {active} is active; only park is allowed now")
                # The worker reads current and queued runs under one lock, so a
                # run cannot escape cancellation and complete ahead of park.
                self.worker.stop_runs("park requested")
            job_id, done = self._new_job_id("action")
            job = Job(job_id, "action", make)
            self.worker.submit(job)
        if not wait:
            return {"job": job_id, "name": name}
        if not self._await(job, done, timeout_s, cancel_on_timeout=(name != "park")):
            return {"job": job_id, "name": name, "pending": True, "result": None,
                    "read_at": (self._snapshot or {}).get("read_at")}
        if "error" in outcome:
            raise outcome["error"]
        return {"job": job_id, "name": name, "result": outcome.get("result"),
                "read_at": (self._snapshot or {}).get("read_at")}

    def _read_back(self) -> dict:
        """Reads and caches every instrument on the worker thread."""
        with self._lock:
            voc = self.session_voc
        snapshot = self.bench.read_back(voc=voc, monitor=self.monitor_running)
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _new_job_id(self, kind: str) -> tuple[str, threading.Event]:
        with self._lock:
            self._job_count += 1
            job_id = f"{kind}-{self._job_count}"
            done = self._job_done[job_id] = threading.Event()
        return job_id, done

    def _await(self, job: Job, done: threading.Event, timeout_s: float, *,
               cancel_on_timeout: bool = True) -> bool:
        """Waits for job completion and returns true when it runs.

        On timeout, an unstarted job is cancelled to prevent an unattended
        late action, and `TimeoutError` reports failure to the caller.
        With `cancel_on_timeout=False`, park remains queued and returns false
        (docs/service-contract.md section 4).
        """
        finished = done.wait(timeout_s)
        with self._lock:
            self._job_done.pop(job.id, None)
        if not finished:
            if not cancel_on_timeout:
                return False
            if self.worker.cancel(job.id):
                raise TimeoutError(f"{job.id} did not start within {timeout_s:g} s "
                                   "and was cancelled")
            raise TimeoutError(f"{job.id} did not finish within {timeout_s:g} s; it is "
                               "still running on the bench")
        if job.state == "failed":
            raise RuntimeError(f"{job.id} failed: {job.error}")
        return True

    def bench_snapshot(self) -> dict:
        """Returns cached bench state, session V_oc, active run and queue.

        No instrument I/O occurs. During a run, `RunRecord.live` overlays the
        Start read-back with `how="inferred"` and lists inferred instruments.
        This reflects active bias and relay state while `read_at` retains the
        read-back timestamp (docs/service-contract.md section 4).

        The temperature block follows a more recent `TemperatureRead` from a
        monitor, pause poll or typed operator value on the shared event path.
        """
        with self._lock:
            snapshot = self._snapshot
            voc = self.session_voc
            current = self.worker.current
            queue = [j.id for j in self.worker.queued if j.kind == "run"]
            run = None
            state = "idle"
            inferred: list[str] = []
            instruments = dict((snapshot or {}).get("instruments")
                               or self.bench.snapshot_stub())
            if current is not None and current.kind == "run" and current.id in self._records:
                rec = self._records[current.id]
                state = _bench_state(rec.state)
                node = current.node_path
                run = {"run_id": rec.run_id, "state": rec.state, "name": rec.name,
                       "kind": rec.kind, "node_path": node,
                       "module": _module_of(node) if node and node in rec.schedule.node_paths
                       else rec.module,
                       "progress": rec.progress, "eta": rec.eta, "pending": rec.pending,
                       "finish_at": (rec.cost or {}).get("finish_at")}
                if rec.live is not None and rec.live.inferred:
                    instruments = rec.live.overlay(instruments)
                    inferred = list(rec.live.inferred)
            instruments["voc"] = _voc_wire(voc)
            instruments["power"] = {**(instruments.get("power") or {}),
                                    "monitor": self.monitor_running}
            instruments["temperature"] = self._temperature_block(
                instruments.get("temperature") or {},
                None if snapshot is None else snapshot.get("read_at"))
            return {
                "session": self.session_info(),
                "state": state, "run": run, "queue": queue,
                "read_at": None if snapshot is None else snapshot.get("read_at"),
                "instruments": instruments, "inferred": inferred,
                "chain": None if snapshot is None else snapshot.get("chain"),
                "rig": self.bench.rig_info(),
                "verdicts": list((snapshot or {}).get("verdicts") or []),
                "unavailable": dict(self.bench.unavailable),
                "monitors": self.monitors(),
            }

    def _temperature_block(self, base: Mapping[str, Any], read_at: float | None) -> dict:
        """Overlays cached temperature with the latest reading and monitor state.

        `wired` remains the rig's value. A typed temperature cannot establish
        an instrument connection.
        """
        out = dict(base)
        last = self._temperature
        if last is not None and (read_at is None or float(last["read_at"]) >= float(read_at)):
            out.update({"kelvin": last["kelvin"], "in_band": last["in_band"],
                        "source": last["source"], "read_at": last["read_at"]})
            if last.get("setpoint_k") is not None:
                out["setpoint_k"] = last["setpoint_k"]
        monitor = self._temperature_monitor
        running = monitor is not None and monitor.running
        out["monitor"] = running
        out["reads"] = monitor.readings if running else 0
        return out

    @property
    def snapshot(self) -> dict | None:
        """Returns raw cached read-back for validation, or `None` before a read."""
        with self._lock:
            return self._snapshot

    # -- modules --------------------------------------------------------------------
    def modules_wire(self) -> dict:
        """Returns the catalogue for `GET /modules`."""
        return {"modules": [self.module_wire(name) for name in self.catalogue.names()]}

    def module_wire(self, name: str) -> dict:
        """Returns one module with registry `last` and bench-dependent `needs`.

        Needs use session V_oc and cached read-back. Unknown modules raise `KeyError`.
        """
        with self._lock:
            snapshot, voc = self._snapshot, self.session_voc
        entry = self.catalogue.as_wire(name, bench=snapshot, session_voc=voc)
        entry["last"] = self._last_for(name)
        return entry

    def _last_for(self, name: str) -> dict | None:
        with self._lock:
            for rec in reversed(list(self._records.values())):
                for node_path in reversed(rec.schedule.node_paths):
                    if _module_of(node_path) != name:
                        continue
                    entry = rec.node_outcomes.get(node_path)
                    if entry is None:
                        if rec.terminal:
                            continue                # never reached this node
                        return {"run_id": rec.run_id, "state": rec.state,
                                "node_path": node_path, "ts": rec.queued_at, "summary": None}
                    detail = entry.get("detail") or {}
                    return {"run_id": rec.run_id,
                            "state": rec.state if entry.get("outcome") is None
                            else {"ok": "done"}.get(entry["outcome"], entry["outcome"]),
                            "node_path": node_path,
                            "ts": entry.get("finished_at") or entry.get("started_at"),
                            "summary": detail.get("summary") or None,
                            "folder": detail.get("folder")}
        return None

    # -- events -------------------------------------------------------------------
    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def events_since(self, seq: int = 0) -> list[dict]:
        """Returns replay frames with sequence numbers above `seq`.

        The ring holds 5000 envelopes. Only the last `RING_TRACES_KEPT` shots
        retain traces; earlier shots retain scalars and verdicts.
        """
        with self._lock:
            full = {f["seq"]: f for f in self._traces}
            return [full.get(f["seq"], f) for f in self._ring if f["seq"] > int(seq)]

    def subscribe(self, queue: Any = None) -> Any:
        """Registers and returns a sink supporting `put_nowait` and `qsize`.

        The default is `Backlog`. Beyond `SUBSCRIBER_BACKLOG` numbered frames,
        a sink receives a `Notice`, then `None`, and is dropped. Other sinks
        are counted whole, which cannot under-count the backlog
        (docs/service-contract.md section 6).
        """
        q = queue if queue is not None else Backlog()
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: Any) -> None:
        with self._lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -- the worker's side -------------------------------------------------------------
    def _on_event(self, job: Job, ev: E.Event) -> None:
        """Captures shot diagnostics and open-node counts on the worker thread.

        Diagnostics are read beside the shot they describe. Node counts follow
        event order before processing crosses to asyncio.
        """
        diagnostics = None
        if isinstance(ev, E.StepDone):
            try:
                diagnostics = self.bench.shot_diagnostics()
            except Exception:                               # noqa: BLE001
                diagnostics = None
        if job.kind == "run":
            rec = self._records.get(job.id)
            if rec is not None:
                self._track(rec, job, ev)
        self._dispatch(job, ev, diagnostics=diagnostics)

    @staticmethod
    def _track(rec: RunRecord, job: Job, ev: E.Event) -> None:
        """Tracks open nodes and module counts on the worker thread only.

        No lock is needed. The data supplies `NodeDone` when abort prevents it.
        """
        if isinstance(ev, E.NodeStarted):
            if ev.node_path not in rec.open_nodes:
                rec.open_nodes.append(ev.node_path)
            if ev.node_path in rec.schedule.node_paths:
                rec.tally[ev.node_path] = {"module": ev.kind, "kept": None, "requested": None}
        elif isinstance(ev, E.NodeDone):
            if ev.node_path in rec.open_nodes:
                rec.open_nodes.remove(ev.node_path)
        else:
            tally = rec.tally.get(job.node_path)
            if tally is None:
                return
            if isinstance(ev, E.RunStarted):
                tally["requested"], tally["kept"] = ev.n_shots, 0
            elif isinstance(ev, JVStarted):
                tally["requested"], tally["kept"] = ev.n_curves, 0
            elif isinstance(ev, (E.StepDone, JVCurveDone)):
                tally["kept"] = (tally["kept"] or 0) + 1

    def _on_state(self, job: Job, state: str, reason: str) -> None:
        """Routes worker state onto the event path without mutating run records.

        Cancellation can call this on the caller's thread. `_apply` processes
        `RunStateChanged` after preceding events, preserving record order
        (docs/service-contract.md section 2).
        """
        ts = time.time()
        if job.kind == "run":
            with self._lock:
                rec = self._records.get(job.id)
                if rec is not None and state == "stopping" and rec.stopping:
                    return                                  # already said by `stop()`
            if rec is not None and state in TERMINAL and state != "cancelled":
                # Abort closes the generator, and worker failure can prevent its
                # final report. Synthesize outcomes with recorder folders and shot
                # counts so files on disk remain visible in registry and journal.
                for node_path in reversed(list(rec.open_nodes)):
                    self._dispatch(job, E.NodeDone(node_path=node_path, outcome=state,
                                                   detail=self._synthesized_detail(
                                                       rec, node_path, state)),
                                   node_path=node_path, ts=ts)
                rec.open_nodes.clear()
            self._dispatch(job, E.RunStateChanged(state, reason), node_path="", ts=ts)
        elif state == "parked" or state in TERMINAL:
            # Action and read-back states only wake `_await`; no state journal
            # entry is needed. Either terminal or following `parked` wakes it.
            with self._lock:
                done = self._job_done.get(job.id)
            if done is not None:
                done.set()

    @staticmethod
    def _synthesized_detail(rec: RunRecord, node_path: str, state: str) -> dict:
        tally = rec.tally.get(node_path)
        if tally is None:
            return {"reason": state}
        ctx = rec.contexts.get(node_path)
        folders = list(ctx.folders) if ctx is not None else []
        kept, requested = tally.get("kept"), tally.get("requested")
        verb = "aborted at" if state == "aborted" else f"{state} at"
        summary = (f"{verb} {kept}/{requested}" if kept is not None and requested is not None
                   else state)
        return {"module": tally.get("module"), "kept": kept, "requested": requested,
                "folder": folders[-1] if folders else None, "folders": folders,
                "summary": summary, "reason": state}

    def _dispatch(self, job: Job | None, ev: E.Event, *, node_path: str | None = None,
                  ts: float | None = None, diagnostics: Mapping[str, Any] | None = None) -> None:
        """Dispatches an event to `_ingest` on the event-loop thread.

        Without a loop it runs directly; other threads post to the attached loop.
        """
        run_id = job.id if job is not None and job.kind == "run" else None
        if node_path is None:
            node_path = job.node_path if run_id is not None and job is not None else ""
        when = time.time() if ts is None else ts
        loop = self._loop
        if (loop is not None and threading.current_thread() is not self._loop_thread
                and not loop.is_closed()):
            try:
                loop.call_soon_threadsafe(self._ingest, run_id, node_path, ev, when, diagnostics)
                return
            except RuntimeError:                            # the loop has stopped
                pass
        self._ingest(run_id, node_path, ev, when, diagnostics)

    def _ingest(self, run_id: str | None, node_path: str, ev: E.Event, ts: float,
                diagnostics: Mapping[str, Any] | None) -> None:
        """Builds payloads, journals events, updates replay and records, and broadcasts.

        Live-only events such as `StepPhase` omit sequence, journal and ring.
        They still update the live overlay and reach current subscribers
        (docs/service-contract.md section 3).
        """
        with self._lock:
            if is_ephemeral(ev):
                try:
                    frame = ephemeral_frame(run_id, node_path, ev, ts)
                except Exception as exc:                    # noqa: BLE001
                    self._error(f"{type(ev).__name__} for {run_id or 'the session'} at "
                                f"{node_path!r}: payload: {type(exc).__name__}: {exc}")
                    return
                if run_id is not None:
                    self._apply(run_id, node_path, ev, ts)
                self._fan_out(frame, ephemeral=True)
                return
            # Assign `seq` after payload creation succeeds. Failed conversion
            # must not create a false gap that prompts clients to replay.
            seq = self._seq + 1
            env = make_envelope(seq, run_id, node_path, ev, ts)
            try:
                ws, jl = payloads(env, diagnostics=diagnostics)
            except Exception as exc:                        # noqa: BLE001
                self._error(f"{type(ev).__name__} for {run_id or 'the session'} at "
                            f"{node_path!r}: payload: {type(exc).__name__}: {exc}")
                return
            self._seq = seq
            if jl is not None:
                self._journal(jl)
            if isinstance(ev, E.StepDone):
                self._traces.append(ws)
                self._ring.append(_slim_step_done(ws))
            else:
                self._ring.append(ws)
            if isinstance(ev, E.TemperatureRead):
                self._temperature = {"kelvin": ev.kelvin, "setpoint_k": ev.setpoint_k,
                                     "in_band": ev.in_band, "source": ev.source,
                                     "read_at": ts}
            if run_id is not None:
                self._apply(run_id, node_path, ev, ts, line=jl)
            self._fan_out(ws)

    def _journal(self, line: dict) -> None:
        try:
            self.journal.append(line)
        except Exception as exc:                            # noqa: BLE001
            self._error(f"seq {line.get('seq')} {line.get('type')}: journal: "
                        f"{type(exc).__name__}: {exc}")

    def _apply(self, run_id: str, node_path: str, ev: E.Event, ts: float,
               line: dict | None = None) -> None:
        rec = self._records.get(run_id)
        if rec is None:
            return
        if rec.live is not None:
            rec.live.apply(ev, rec.schedule)
        if line is not None and isinstance(line.get("data"), dict):
            # Journal payloads and reducers supply identical numbers and types
            # to the file and in-memory record.
            data = line["data"]
            if isinstance(ev, E.StepDone):
                count_shot(rec.node_results.setdefault(node_path, {}), data)
            elif isinstance(ev, E.AxisResolved):
                rec.node_results.setdefault(node_path, {}).update(axis_result(data))
            elif isinstance(ev, E.LoopDone):
                rec.node_results.setdefault(node_path, {}).update(loop_result(data))
            elif isinstance(ev, E.RunFinished):
                rec.node_results.setdefault(node_path, {}).update(finished_result(data))
            elif isinstance(ev, JVCurveDone):
                add_curve(rec.node_results.setdefault(node_path, {}), data)
            elif isinstance(ev, JVFinished):
                rec.node_results.setdefault(node_path, {}).update(jv_result(data))
        if isinstance(ev, E.RunStateChanged):
            self._apply_state(rec, ev.state, ts)
        elif isinstance(ev, E.NodeStarted):
            rec.node_outcomes[ev.node_path] = {
                "kind": ev.kind, "label": ev.label, "outcome": None,
                "started_at": ts, "finished_at": None, "detail": {}}
        elif isinstance(ev, E.NodeDone):
            entry = rec.node_outcomes.setdefault(ev.node_path, {
                "kind": _module_of(ev.node_path), "label": ev.node_path, "outcome": None,
                "started_at": None, "finished_at": None, "detail": {}})
            detail = jsonable(dict(ev.detail))
            entry.update(outcome=ev.outcome, finished_at=ts, detail=detail)
            for folder in detail.get("folders") or ([detail["folder"]] if detail.get("folder") else []):
                if folder and folder not in rec.folders:
                    rec.folders.append(folder)
            if ev.node_path in rec.schedule.node_paths:
                kept, requested = detail.get("kept"), detail.get("requested")
                if isinstance(kept, int) and isinstance(requested, int):
                    rec.kept = (rec.kept or 0) + kept
                    rec.requested = (rec.requested or 0) + requested
        elif isinstance(ev, E.Progress):
            if ev.node_path == "":
                rec.progress = {"done": ev.done, "total": ev.total, "elapsed_s": ev.elapsed_s,
                                "eta_s": ev.eta_s, "node_path": node_path}
            elif ev.eta_s is not None:
                # Loop `Progress` carries ETA from this run's measured durations.
                # The record follows it while retaining the submit-time estimate.
                rec.eta = {"eta_s": ev.eta_s, "finish_at": ts + ev.eta_s, "at": ts,
                           "node_path": ev.node_path, "done": ev.done, "total": ev.total}
                cost = dict(rec.cost)
                cost.setdefault("finish_at_submit", cost.get("finish_at"))
                cost["finish_at"] = ts + ev.eta_s
                cost["finish_source"] = "measured"
                rec.cost = cost
        elif isinstance(ev, E.Verdict):
            # Start rechecks submit-time verdicts. Replacement by code and node
            # keeps warning counts from doubling.
            entry = to_wire(ev)[0]
            for i, old in enumerate(rec.verdicts):
                if old.get("code") == ev.code and old.get("node_path", "") == ev.node_path:
                    rec.verdicts[i] = entry
                    break
            else:
                rec.verdicts.append(entry)
        elif isinstance(ev, E.RunFailed):
            rec.error = ev.error
        elif isinstance(ev, E.NeedsOperator):
            rec.pending = {"what": ev.what, "node_path": ev.node_path,
                           "detail": jsonable(dict(ev.detail)), "since": ts}
        elif isinstance(ev, E.OperatorResumed):
            rec.pending = None

    def _apply_state(self, rec: RunRecord, state: str, ts: float) -> None:
        """Applies record state changes in order with run events."""
        if state == "parked":
            rec.parked = True
            rec.ended.set()
            return
        if state == "preflight" and rec.started_at is None:
            rec.started_at = ts
        if rec.stopping and state in ("preflight", "running"):
            # A stop accepted during preflight already reports `stopping`.
            # Subsequent worker startup states must not replace it with `running`.
            return
        rec.state = state
        if state in TERMINAL:
            rec.finished_at = ts
            job = self._jobs.get(rec.run_id)
            if job is not None and job.error and not rec.error:
                rec.error = job.error
            for entry in rec.node_outcomes.values():
                if entry.get("outcome") is None:
                    entry["outcome"] = state
                    entry["finished_at"] = ts
            rec.pending = None
            if state == "cancelled":
                rec.ended.set()

    def _fan_out(self, frame: dict, *, ephemeral: bool = False) -> None:
        # Reconnect uses the last numbered frame retained by the ring.
        # The current frame can be live-only with `seq=None`.
        since = self._seq
        for q in list(self._subscribers):
            try:
                # Staleness counts all frames; replay backlog counts numbered frames.
                # Each limit documents the reason for its bound.
                if ephemeral and q.qsize() > EPHEMERAL_BACKLOG:
                    continue
                q.put_nowait(frame)
                behind = backlog_of(q) > SUBSCRIBER_BACKLOG
            except Exception:                               # noqa: BLE001 -- a dead sink
                behind = True
            if behind:
                self._subscribers.remove(q)
                try:
                    # `seq=None` marks this one-client notice outside the ring.
                    # Reusing a real sequence number could make client deduplication
                    # discard the explanation for disconnection.
                    q.put_nowait({"seq": None, "ts": time.time(), "run_id": None,
                                  "node_path": "", "type": "Notice",
                                  "data": {"level": "warning",
                                           "text": f"dropped: more than {SUBSCRIBER_BACKLOG} "
                                                   "frames behind; reconnect with since="
                                                   f"{since}",
                                           "since": since},
                                  "decimated": {}})
                    q.put_nowait(None)
                except Exception:                           # noqa: BLE001
                    pass

    # -- the power monitor ----------------------------------------------------------
    @property
    def monitor_running(self) -> bool:
        return self._monitor is not None and self._monitor.running

    def start_power_monitor(self, interval_s: float = 1.0) -> dict:
        """Starts the power monitor, refusing a second one with HTTP 409.

        No meter or an invalid interval raises `ValueError`.
        """
        interval_s = float(interval_s)
        if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
            raise ValueError(f"interval_s must be between {MIN_INTERVAL_S:g} and "
                             f"{MAX_INTERVAL_S:g} s, not {interval_s:g}")
        if self.monitor_running:
            raise Conflict("the power monitor is already running")
        meter = self.bench.rig.power
        if meter is None or isinstance(meter, Unavailable):
            raise ValueError("no power meter on this bench: "
                             + self.bench.unavailable.get("power", "the 1918-C console "
                                                                   "is not answering"))
        monitor = PowerMonitor(meter, emit=self._on_power_reading, interval_s=interval_s,
                               console=self.rig_config.power_meter_console)
        self._monitor = monitor
        monitor.start()
        return monitor.info()

    def _on_power_reading(self, ev: E.Event) -> None:
        """Stores and dispatches a monitor reading with one shared timestamp.

        History and stream timestamps must match so clients can merge them
        without duplicating each reading a few microseconds apart.
        """
        when = time.time()
        if isinstance(ev, E.PowerReading):
            with self._lock:
                self._power_history.append((when, float(ev.watts), bool(ev.trustworthy),
                                            ev.wavelength_nm, ev.source, ev.averaged))
        self._dispatch(None, ev, node_path="", ts=when)

    def stop_power_monitor(self) -> bool:
        """Stops power monitoring and returns whether a monitor is running."""
        monitor = self._monitor
        if monitor is None or not monitor.running:
            return False
        monitor.stop()
        return True

    # -- the power history ------------------------------------------------------------
    def power_history(self, *, since: float | None = None,
                      limit: int | None = None) -> dict:
        """Returns retained power history as timestamp, watts and trust rows.

        Rows are oldest first, optionally after `since` and limited to the
        latest `limit` rows. Wavelength, source and averaging come once from
        the latest reading: they describe meter settings rather than sample
        properties (docs/service-contract.md section 8).
        """
        with self._lock:
            rows = list(self._power_history)
        total = len(rows)
        if since is not None:
            rows = [r for r in rows if r[0] > float(since)]
        if limit is not None and limit >= 0:
            rows = rows[-int(limit):] if limit else []
        newest = rows[-1] if rows else None
        monitor = self._monitor
        running = monitor is not None and monitor.running
        return {
            "points": [[r[0], r[1], r[2]] for r in rows],
            "count": len(rows), "total": total, "kept_max": POWER_HISTORY_MAX,
            "first_ts": rows[0][0] if rows else None,
            "last_ts": newest[0] if newest else None,
            "wavelength_nm": newest[3] if newest else None,
            "source": newest[4] if newest else None,
            "averaged": newest[5] if newest else None,
            "running": running,
            "interval_s": monitor.interval_s if running else None,
        }

    def clear_power_history(self) -> int:
        """Clears in-memory power readings and returns the number removed.

        The journal retains those readings on disk.
        """
        with self._lock:
            n = len(self._power_history)
            self._power_history.clear()
        return n

    def power_history_csv(self) -> str:
        """Exports one CSV row per power reading for spreadsheet use.

        Each row retains ISO wall time, Unix seconds, full-precision watts
        and meter status.
        """
        import csv
        import io
        with self._lock:
            rows = list(self._power_history)
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        w.writerow(["time_iso", "unix_s", "watts", "trustworthy", "wavelength_nm",
                    "source", "averaged"])
        for ts, watts, ok, nm, source, averaged in rows:
            w.writerow([datetime.fromtimestamp(ts).isoformat(timespec="milliseconds"),
                        f"{ts:.3f}", repr(float(watts)), int(bool(ok)),
                        "" if nm is None else f"{nm:g}", source or "",
                        "" if averaged is None else int(bool(averaged))])
        return out.getvalue()

    # -- the temperature monitor ----------------------------------------------------
    @property
    def temperature_monitor_running(self) -> bool:
        m = self._temperature_monitor
        return m is not None and m.running

    def start_temperature_monitor(self, interval_s: float = TEMPERATURE_MONITOR_S) -> dict:
        """Starts 331 monitoring with the configured bus-ownership rule.

        Direct GPIB reads require a nonblocking worker bus lock and the driver
        lock; HTTP reads stay off the bus. Only one monitor can run (409).
        No configured 331 or an invalid interval raises `ValueError`
        (docs/service-contract.md section 8).

        `start` enables it by default when `temperature_monitor_s` allows.
        The route can restart it or change its interval with `DELETE` then `POST`.
        """
        interval_s = float(interval_s)
        if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
            raise ValueError(f"interval_s must be between {MIN_INTERVAL_S:g} and "
                             f"{MAX_INTERVAL_S:g} s, not {interval_s:g}")
        if self.temperature_monitor_running:
            raise Conflict("the temperature monitor is already running")
        controller = self.bench.rig.temperature
        # Direct 331 reads share GPIB0 with worker jobs. The monitor holds
        # the worker's bus lock or skips the tick, preventing overlapping
        # transactions. An HTTP controller needs no worker bus lock.
        on_the_bus = getattr(controller, "resource", None) is not None
        monitor = TemperatureMonitor(self.rig_config.temperature_console,
                                     emit=lambda ev: self._dispatch(None, ev, node_path=""),
                                     interval_s=interval_s,
                                     controller=controller,
                                     bus=self.worker.bus if on_the_bus else None)
        self._temperature_monitor = monitor
        monitor.start()
        return monitor.info()

    def stop_temperature_monitor(self) -> bool:
        """Stops temperature monitoring and returns whether a monitor is running."""
        monitor = self._temperature_monitor
        if monitor is None or not monitor.running:
            return False
        monitor.stop()
        return True

    def monitors(self) -> list[dict]:
        """Returns monitor status for `GET /monitors`."""
        return [m.info() for m in (self._monitor, self._temperature_monitor)
                if m is not None and m.running]


def tree_for_module(module: str, params: Mapping[str, Any] | None = None,
                    name: str = "") -> dict:
    """Builds the one-node run tree from `module`, `params` and `name`."""
    tree: dict[str, Any] = {"kind": "module", "module": str(module),
                            "params": dict(params or {})}
    if name:
        tree["name"] = str(name)
    return tree


__all__ = [
    "Session", "RunRecord", "SubmitRefused", "Conflict", "Busy", "NotPaused",
    "UnknownRun", "DataUnavailable", "NodeRequired", "BlockedAtStart", "tree_for_module",
    "RING_SIZE", "RING_TRACES_KEPT", "SUBSCRIBER_BACKLOG", "EPHEMERAL_BACKLOG",
    "DATA_RUNS_KEPT", "BENCH_STATES", "Backlog", "backlog_of",
]
