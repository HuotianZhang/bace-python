"""One service process lifetime: the bench, the catalogue, the journal, the
worker and the run registry, and the one path every event takes.

`Session` is what `app.py` wraps in routes and what the tests drive directly.
It owns four things built once per process -- a `Bench` (the rig and its
read-back), a `Catalogue` (the modules and their parameters), a `Journal`
(the append-only log) and a `RunWorker` (the one thread that touches
instruments) -- and it is the only place their events meet: every event a
job yields, every state change the worker reports, every reading the power
monitor takes and every `RunQueued`/`BenchAction` the session itself writes
goes through `_ingest`, which stamps the envelope (`seq`, `ts`, `run_id`,
`node_path`), attaches the per-shot verdict, writes the journal line, keeps
the last 5000 frames for `GET /events?since=`, updates the run registry and
fans the WebSocket frame out. One path, one `seq` counter, one lock: a
client that sees a gap in `seq` knows it missed something, and that promise
only holds if nothing bypasses this. The ring keeps every frame's scalars;
the traces of a `StepDone` are kept for the last `RING_TRACES_KEPT` shots
only, because a ring full of thousand-point lists would be hundreds of
megabytes for one canonical scan.

**Threads.** The worker's callbacks run on the worker thread, between one
instrument call and the next. When an asyncio loop is attached
(`attach_loop`, called by the app at startup) they are handed to it with
`loop.call_soon_threadsafe`, so the loop does the journaling and the fan-out
and the worker never waits on a socket; without a loop (the tests, a script)
`_ingest` runs on the calling thread under the session's lock. The run
record is updated on that same path, when the `RunStateChanged` frame is
applied, and not on the worker thread as the state is reported: a record
that said `stopped` before the `NodeDone` carrying the counts had been
applied answered `GET /runs/{id}` with no folder and no counts for a run
that had both. Two things are done on the worker thread before the
hand-over, because they have to be: the per-shot diagnostics are read off
the digitiser right after the `StepDone` that produced them (a moment later
they describe the next shot), and the worker-side view of which nodes are
open and how many shots each took is kept there, so that an abort -- which
closes the generator before it can report -- can still be turned into a
`NodeDone` per open node with the folder and the counts.

**Runs.** `submit` validates the tree against the cached bench read-back
(the Dry run, contract section 7) and refuses `invalid`/`crit` with the
checks; a valid tree becomes a `RunRecord` and a `Job` whose `make`, on the
worker thread with the bench to itself, re-reads the chain, attaches it to
the run, re-checks the `crit` ids against the fresh read-back and only then
builds `executor.run_pipeline`. A manual run *is* a one-node pipeline: the
same validate, the same executor, the same journal lines, so the history
queries and the console cannot tell them apart except by the tree. A queued
run is not refused and not run beside the current one; it waits its turn on
the worker and can be cancelled while it waits.

**The bench outside a run.** `bench_read` and `bench_action` are jobs on the
same worker -- the bench lock is the thread, not a flag -- and both are
refused with `Busy` while a run is active, except `park`, which aborts the
run and cancels the queue first: a person who clicks park wants the bench
safe now, not after the runs behind the current one. Every action is
journaled as `BenchAction(by="hand")` with what it did or why it was refused,
and followed by a read-back, so the journal shows the state the action
*left*, not the one it claimed.

Nothing here imports FastAPI; nothing here touches an instrument except
through a job.
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
from ..experiment.jv import JVCurveDone, JVStarted
from ..experiment.rig import RigConfig
from ..experiment.transient import RunConfig
from ..experiment.wire import to_wire
from ..params import Source
from . import pipeline
from .executor import run_pipeline
from .journal import Journal
from .live import LiveState
from .modules import Catalogue, RunContext, VocSource, jsonable
from .monitors import MAX_INTERVAL_S, MIN_INTERVAL_S, PowerMonitor, TemperatureMonitor
from .pipeline import VOC_PARAM, Module, Schedule, Step, Validation
from .rigs import ACTIONS, Bench, BenchActionRefused, Unavailable, action_before
from .wire import STEPDONE_ARRAYS, ephemeral_frame, is_ephemeral, make_envelope, payloads
from .worker import TERMINAL, Blocked, Job, RunWorker, StopMode

log = logging.getLogger("bace.service")

BENCH_STATES: tuple[str, ...] = ("idle", "preflight", "running", "paused", "stopping")
"""What `GET /bench` may say (contract section 4). A run's own states are
mapped onto these: `queued` before its preflight frame is applied reads as
`preflight`, and a terminal state still being parked reads as `stopping` --
the bench is being made safe, which is what that word means there."""

RING_SIZE = 5000
"""Envelopes kept for `GET /events?since=`: a 100-loop scan is about 300
frames, so this is many runs of replay for a client that reconnects."""

RING_TRACES_KEPT = 200
"""`StepDone` frames in the ring that still carry their traces. A frame with
four 1000-point lists is about 130 KB, so a ring full of them would be
600 MB for one 100 x 51 scan; older frames keep every scalar and the verdict
(what a reconnecting client redraws Q(loop) from) and drop the traces, with
`decimated` saying so, exactly as the journal line does."""

SUBSCRIBER_BACKLOG = 1000
"""Frames a WebSocket subscriber may have unsent before it is dropped. A
client that cannot keep up with the stream would otherwise hold the
session's memory hostage; it gets a `Notice` and can replay from `since`.
Under `--sim --fast` a thousand-loop scan produces frames faster than any
socket takes them (a shot is a millisecond, its frame twenty kilobytes), so
a client watching one *is* dropped and reconnects with `since`; on the rig
a shot is 0.8 s and the backlog is many minutes of stream."""

EPHEMERAL_BACKLOG = 50
"""Unsent frames beyond which an ephemeral frame (`StepPhase`) is not even
queued for a subscriber: it says where inside the shot the run is *now*,
which is worthless fifty frames late, and seven of them a shot would fill
the backlog seven times faster than the frames a client needs."""

DATA_RUNS_KEPT = 20
"""Runs whose full-precision arrays `GET /runs/{id}/data` still serves from
memory. Beyond that the answer is the folder path and the HDF5."""

WORKER_JOIN_S = 60.0
"""How much longer `close()` waits for the worker after `shutdown()` gave
up. A job inside a blocking VISA call ends when the call times out (the
resources are opened with 20 s, an acquisition waits up to 30 s), and the
bench must not be parked and its resources closed from a second thread
while the first is still talking to them."""

LAST_USED_SOURCES: frozenset[Source] = frozenset({Source.EDITED, Source.LAST_USED})
"""Which of a run's parameters `RunQueued.params` carries, i.e. what comes
back as last-used: what the operator typed, and what they typed before and
kept. Defaults and recipe values are reproduced by their own layers next
time; journaling them too would turn every provenance on the card into
"last-used" after one run and hide a recipe edit for twenty sessions."""

_STEM = re.compile(r"[^A-Za-z0-9_.-]+")


# -- errors the routes map to status codes ------------------------------------
class SubmitRefused(ValueError):
    """`POST /runs` or `/pipelines` with a tree that is `invalid` or `crit`.
    Carries the `Validation` so the 422 can show every check."""

    def __init__(self, validation: Validation):
        blocking = [c for c in validation.checks if c.level in ("invalid", "crit")]
        super().__init__("; ".join(f"{c.level} {c.code}: {c.text}" for c in blocking)
                         or "the tree was refused")
        self.validation = validation


class Conflict(RuntimeError):
    """The request is well-formed but the state refuses it (409)."""


class Busy(Conflict):
    """A run is active, so the bench cannot be read or acted on outside it."""


class NotPaused(Conflict):
    """A resume for a run that is not waiting for the operator."""


class UnknownRun(KeyError):
    """No such run in this session (404)."""


class DataUnavailable(LookupError):
    """The arrays for this run are not in memory (404): evicted, never
    produced, or the node has not run. `folders` names where the HDF5 is."""

    def __init__(self, run_id: str, reason: str, folders: list[str]):
        super().__init__(f"{run_id}: {reason}")
        self.run_id, self.reason, self.folders = run_id, reason, list(folders)


class NodeRequired(ValueError):
    """`GET /runs/{id}/data` on a pipeline run without `node=` (400)."""

    def __init__(self, run_id: str, nodes: list[str]):
        super().__init__(f"{run_id} has {len(nodes)} nodes; say which with node=<path>: "
                         + ", ".join(nodes))
        self.nodes = list(nodes)


class BlockedAtStart(Blocked):
    """A `crit` check failed on the read-back taken at Start: the run never
    touched the bench and ends `blocked` (contract section 2) with the
    verdicts' text as its error."""


# -- the registry -----------------------------------------------------------
@dataclass
class RunRecord:
    """Everything `GET /runs/{id}` serves, kept in memory for the session.

    The journal has the same facts as lines; this is the same facts as an
    object the routes can answer from without re-reading the file. Node
    outcomes are keyed by node path and filled from `NodeStarted`/`NodeDone`;
    `params_as_executed` is the schedule's parameters with provenance, per
    node, which is what ran.
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
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    kept: int | None = None
    requested: int | None = None
    progress: dict | None = None
    eta: dict | None = None
    """The latest loop `Progress` with a measured ETA: `{eta_s, finish_at,
    at, node_path, done, total}`. A manual run has no loop and no entry; its
    own `progress.eta_s` is the module's."""
    pending: dict | None = None
    position: int = 0
    evicted: bool = False
    live: LiveState | None = None
    """What the running step implies about the instruments (`service.live`),
    for `/bench` while this run holds the worker."""
    stopping: bool = False
    """A stop was accepted and `stopping` reported by the session, so the
    worker's own `stopping` (at its next event) is not journaled twice."""
    session_voc: VocSource | None = None
    """The session's V_oc when the tree was resolved -- what a manual bace
    outside any loop centres on, kept with the run so a jv_bace queued
    behind it cannot change what this run was validated against."""
    ended: threading.Event = field(default_factory=threading.Event)
    contexts: dict[str, RunContext] = field(default_factory=dict)
    """The `RunContext` each module node was built with, by node path. Its
    `folders` is where the recorder is writing *now*, which is how a run
    that is aborted before its `NodeDone` still names its folder."""
    open_nodes: list[str] = field(default_factory=list)
    """Node paths started and not yet done, as seen on the worker thread."""
    tally: dict[str, dict] = field(default_factory=dict)
    """Per module node, on the worker thread: `{module, kept, requested}`
    from the module's own events, for the `NodeDone` an abort cannot yield."""

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def all_folders(self) -> list[str]:
        """Every folder written so far: the ones `NodeDone` reported and the
        ones the recorders of nodes still running have opened."""
        out = list(self.folders)
        for ctx in list(self.contexts.values()):
            for folder in list(ctx.folders):
                if folder not in out:
                    out.append(folder)
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
    """A run's state as `GET /bench` says it (`BENCH_STATES`)."""
    if run_state in BENCH_STATES:
        return run_state
    if run_state == "queued":
        return "preflight"
    return "stopping"                     # terminal, the worker still parking


def _slim_step_done(frame: dict) -> dict:
    """The ring's copy of a `StepDone` frame: every scalar and the verdict
    kept, the four traces dropped and declared in `decimated`."""
    data = dict(frame["data"])
    info = dict(frame.get("decimated") or {})
    for name in STEPDONE_ARRAYS:
        if name in data:
            data[name] = None
            info[name] = {"omitted": True, "replay": True}
    return {**frame, "data": data, "decimated": info}


def _stem(name: str) -> str:
    return _STEM.sub("_", name.strip()).strip("_") or "pipeline"


# -- the session --------------------------------------------------------------
class Session:
    """One service process lifetime. See the module docstring."""

    def __init__(self, rig_config: RigConfig, run_toml: Mapping[str, Any] | None = None, *,
                 out: str, mode: str = "sim", fast: bool = False,
                 smu_config: SourceMeterConfig | None = None,
                 run_config_defaults: RunConfig | None = None,
                 sample: Mapping[str, Any] | None = None,
                 rig_path: str | None = None, run_path: str | None = None,
                 session_id: str | None = None, seed: int = 0):
        if mode not in ("sim", "rig"):
            raise ValueError(f"mode must be 'sim' or 'rig', not {mode!r}")
        self.rig_config = rig_config
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
        self.journal = Journal(self.out, self.session_id, header={
            "mode": mode, "rig_toml": rig_path, "run_toml": run_path, "out": self.out,
            "fingerprint": self.bench.fingerprint, "python": platform.python_version(),
            "version": _version(), "fast": self.fast,
            # The instrument writes the assembly made before any job: not a
            # run, not a by-hand action, so this is where they are recorded.
            "startup_writes": list(self.bench.startup_writes)})
        sample_table = sample if sample is not None else self.run_toml.get("sample", {})
        self.catalogue = Catalogue(rig_config=rig_config, run_toml=self.run_toml,
                                   history=self.journal, sample=sample_table)
        self.worker = RunWorker(on_event=self._on_event, on_state=self._on_state)

        self._lock = threading.RLock()
        self._seq = self.journal.seq
        self._ring: collections.deque[dict] = collections.deque(maxlen=RING_SIZE)
        self._traces: collections.deque[dict] = collections.deque(maxlen=RING_TRACES_KEPT)
        """The full `StepDone` frames behind the ring's slim copies."""
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
        """The last `TemperatureRead` on the event path -- the monitor's, the
        executor's poll during a pause, or the operator's typed value -- so
        the temperature card follows the newest reading, not the read-back."""
        self.session_voc: VocSource | None = None
        self._monitor: PowerMonitor | None = None
        self._temperature_monitor: TemperatureMonitor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self.last_validated: dict | None = None
        self.errors: list[str] = []
        """What went wrong on the event path itself (a journal line that
        could not be written, a payload that could not be built) or at
        shutdown. Never raised into a job -- the run is more important than
        its log -- but never silent either: each entry is logged as it is
        appended and `session_info()` counts them, so a gap in the journal
        on the lab PC is visible from the console and the log."""
        self._closed = False

    def _error(self, text: str) -> None:
        self.errors.append(text)
        log.warning("%s", text)

    # -- lifecycle ------------------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> "Session":
        if loop is not None:
            self.attach_loop(loop)
        self.worker.start()
        return self

    def attach_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Hand the event path to an asyncio loop. Call from the loop's own
        thread (an `async` startup hook): what is recorded here is which
        thread may call `_ingest` directly and which must post to the loop."""
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._loop_thread = threading.current_thread()

    def __enter__(self) -> "Session":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self, timeout_s: float = 5.0) -> None:
        """Stop the monitor, abort and drain the worker, park and release the
        bench, close the journal. Idempotent.

        The bench is released only once the worker thread has ended. An
        abort takes effect at the job's next event, and a job inside a VISA
        call reaches that when the call returns; parking and closing the
        resources from this thread meanwhile would interleave with the
        generator's own I/O on the same sessions, and its `finally` would
        then find them closed. If the worker is still alive after
        `WORKER_JOIN_S` on top of `timeout_s`, the bench is left as it is
        and the fact recorded -- the daemon thread ends with the process.
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
                "out": self.out, "rig_toml": self.rig_path, "run_toml": self.run_path,
                "fingerprint": self.bench.fingerprint, "journal": self.journal.path,
                "errors": len(self.errors),
                "last_error": self.errors[-1] if self.errors else None}

    def hello(self) -> dict:
        """The first WebSocket frame's `data`."""
        return {"session": self.session_info(), "seq": self.last_seq,
                "bench": self.bench_snapshot()}

    # -- runs -------------------------------------------------------------------
    def validate_tree(self, tree_obj: Mapping[str, Any], *, name: str = "") -> Validation:
        """The Dry run: every check against the cached read-back; touches nothing."""
        v, _voc = self._validate(tree_obj)
        if v.schedule is not None:
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
        """`<out>/<stem>`: the stem of the folder a pipeline with this name
        records into. The run's own folder is `<stem>_<stamp>`, the stamp
        taken at submit (`pipeline_folder_pattern` spells it)."""
        root_name = tree_obj.get("name", "") if isinstance(tree_obj, Mapping) else ""
        return os.path.join(self.out, _stem(str(name or root_name or "pipeline")))

    def pipeline_folder_pattern(self, tree_obj: Mapping[str, Any] | None, name: str = "") -> str:
        """`<out>/<stem>_YYYYMMDD_HHMMSS`: the folder the run will create,
        with the stamp `submit` takes spelled as a pattern. The Dry run
        cannot know the stamp, and a folder name it invented would name a
        place that will not exist."""
        return self.pipeline_folder(tree_obj, name) + "_YYYYMMDD_HHMMSS"

    def submit(self, tree_obj: Mapping[str, Any], name: str = "",
               kind: str | None = None) -> tuple[str, Validation]:
        """Validate, refuse or queue. Returns `(run_id, validation)`.

        `kind` is "manual" (a one-module tree posted from a bench card) or
        "pipeline"; None infers it from the tree's root. A manual run records
        straight under `out` as `tools/scan.py` does; a pipeline gets
        `<out>/<stem>_<stamp>/` and every module records inside it.
        """
        if self._closed:
            raise Conflict("the session is closed")
        name = str(name or "")
        v, voc_at_submit = self._validate(tree_obj)
        if v.schedule is not None:
            self.last_validated = {"tree": v.schedule.tree.as_wire(), "name": name,
                                   "validated_at": time.time(), "valid": v.valid}
        if not v.valid or v.schedule is None:
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
                            live=LiveState(self.rig_config))
            rec.params_as_executed = {
                s.node_path: {n: pv.as_dict() for n, pv in s.params.items()}
                for s in v.schedule.modules}
            rec.verdicts = [to_wire(c)[0] for c in v.checks if c.level != "ok"]
            # What last_used_params hands back next time: what the operator
            # chose (LAST_USED_SOURCES), never the V_oc. A derived V_oc
            # written here would come back as "last-used" -- a typed-looking
            # number that was a measurement -- and even a typed one is a
            # number for one illumination and one hour, not a default for
            # the next run at whatever level it pulses. `resolved` keeps the
            # full record of what ran.
            params = resolved = None
            if kind == "manual":
                step = v.schedule.modules[0]
                params = {n: pv.value for n, pv in step.params.items()
                          if pv.source in LAST_USED_SOURCES and n != VOC_PARAM}
                resolved = {n: pv.value for n, pv in step.params.items()}
            job = Job(run_id, "run", self._make_run(rec), park=self.bench.rig.park, name=name)
            # Queued on the worker before it is journaled: a worker that is
            # shutting down refuses, and a RunQueued line for a run that
            # never reached the queue would sit in the index as `queued`
            # forever.
            try:
                rec.position = self.worker.submit(job)
            except (RuntimeError, ValueError) as exc:
                self._run_count -= 1
                raise Conflict(f"cannot queue {run_id}: {exc}") from None
            self._records[run_id] = rec
            self._jobs[run_id] = job
            # Down the one event path like every other frame: the same seq
            # counter, the same journal line, the same fan-out.
            self._ingest(run_id, "", E.RunQueued(
                kind=kind, module=module, tree=rec.tree, params=jsonable(params),
                resolved=jsonable(resolved), name=name, folder=folder), queued_at, None)
            self._ingest(run_id, "", E.RunStateChanged("queued", "submitted"), time.time(), None)
        return run_id, v

    def _make_run(self, rec: RunRecord) -> Callable[[Job], Any]:
        def make(job: Job):
            # The bench is this job's alone now. Re-read the chain, attach it
            # to the run, and re-check the safety ids against what the
            # instruments hold *at Start*, not at the moment of the click.
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
            for verdict in self.bench.chain_verdicts(snapshot):
                self._dispatch(job, verdict, node_path="")
            # Against the V_oc the tree was validated with (a jv_bace queued
            # behind it must not change that) and the bench as it is now:
            # a `crit` is the safety block; an `invalid` here is an
            # instrument that went away since the Dry run, and a run the
            # builder would refuse must not be started to fail.
            check = pipeline.validate(rec.tree, self.catalogue, bench=snapshot,
                                      history=self.journal, session_voc=rec.session_voc,
                                      rig_config=self.rig_config)
            blocking = [c for c in check.checks if c.level in ("invalid", "crit")]
            for c in blocking:
                self._dispatch(job, c, node_path="")
            if blocking:
                raise BlockedAtStart("blocked at start: " + "; ".join(
                    f"{c.code}: {c.text}" for c in blocking))
            if rec.kind == "pipeline":
                # The parent folder exists from the moment the run starts,
                # so the path the record and the index report is a place on
                # disk even for a pipeline stopped before its first module
                # recorded anything.
                os.makedirs(rec.out_folder, exist_ok=True)
            return run_pipeline(self.bench.rig, rec.schedule, catalogue=self.catalogue,
                                ctx_factory=self._ctx_factory(rec, job), job=job,
                                on_voc=self._set_voc, session_voc=rec.session_voc,
                                emit=lambda ev: self._dispatch(job, ev))
        return make

    def _ctx_factory(self, rec: RunRecord, job: Job) -> Callable[[Step], RunContext]:
        # The sleep gives way to an abort only: an after_shot keeps the
        # shot in flight, settle and all (see `Bench.sleeper`).
        sleep = self.bench.sleeper(lambda: job.stop_mode == StopMode.ABORT)

        def factory(step: Step) -> RunContext:
            ctx = RunContext(run_id=rec.run_id, node_path=step.node_path,
                             out_folder=rec.out_folder,
                             metadata=self.catalogue.base_metadata(),
                             sleep=sleep, abort=job.abort_check,
                             resolved=self._resolved(step),
                             on_data=self._data_sink(rec.run_id),
                             # `on_poll` is how a temperature pause reads the
                             # 331 live while the worker blocks here.
                             wait_for_operator=lambda detail, on_poll=None:
                                 job.wait_for_operator(on_poll=on_poll))
            if step.kind == "module":
                rec.contexts[step.node_path] = ctx
            return ctx
        return factory

    def _resolved(self, step: Step) -> dict[str, str]:
        """The recorder's `resolved` seed: the trigger chain as last read
        back (the run never reads the 33220A's polarity itself), and for a
        transient the LED levels and frequency it is about to set."""
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
        """`after_shot` or `abort` for a running run; a queued run is
        cancelled whichever mode is asked. 404 unknown, 409 already ended."""
        if mode not in StopMode.ALL:
            raise ValueError(f"stop mode must be one of {StopMode.ALL}, not {mode!r}")
        job, rec = self._job(run_id)
        if job.state == "queued" and self.worker.cancel(run_id):
            return {"run_id": run_id, "state": "cancelled", "mode": mode}
        if job.terminal or rec.terminal:
            raise Conflict(f"{run_id} is already {rec.state}")
        # `rec.stopping` is set before the stop is requested, so that when
        # the worker observes the request and reports its own `stopping`,
        # `_on_state` already sees the flag and suppresses it -- the session
        # is the one that reports `stopping`, once. The new state is said
        # now rather than at the job's next event (seconds away for a long
        # shot): the record, `/bench` and the stream all say `stopping` from
        # the moment the stop was accepted.
        with self._lock:
            first = not rec.stopping
            rec.stopping = True
        in_force = job.request_stop(mode)
        if first:
            self._dispatch(job, E.RunStateChanged("stopping", f"{in_force} requested"),
                           node_path="")
        return {"run_id": run_id, "state": "stopping", "mode": in_force}

    def resume(self, run_id: str, detail: Mapping[str, Any] | None = None) -> dict:
        """Answer a `NeedsOperator`. 409 when the run is not paused."""
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
        """Block until the run has been parked (or cancelled). For tests and
        scripts; the app watches the event stream instead."""
        return self._job(run_id)[1].ended.wait(timeout_s)

    def _job(self, run_id: str) -> tuple[Job, RunRecord]:
        with self._lock:
            job, rec = self._jobs.get(run_id), self._records.get(run_id)
        if job is None or rec is None:
            raise UnknownRun(run_id)
        return job, rec

    def run_record(self, run_id: str) -> dict:
        """`GET /runs/{id}`: the registry's record for a run of this process,
        else the journal's (`from: "journal"` -- the summary plus `tree` and
        `nodes`, which is what the pipeline tab's grey V_oc grid of the
        previous run needs when that run was last week's). 404 when neither
        knows it."""
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
        """`[RunSummary]`, newest first, from the journal -- the same answer
        for this session and for last week's."""
        return self.journal.run_index(session)

    def run_data(self, run_id: str, node_path: str | None = None) -> dict:
        """The full-precision arrays of one node (numpy inside; the route
        applies `modules.jsonable`). 404 unknown or not in memory, 400 when a
        pipeline run is asked without a node."""
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
                # A pipeline run is addressed by node, even one with a single
                # module: the path is part of the address (contract section
                # 6), and a client that learned to omit it on small trees
                # would break on the first real one.
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
        """The id of the run holding or waiting for the bench, else None."""
        current = self.worker.current
        if current is not None and current.kind == "run" and not current.terminal:
            return current.id
        for job in self.worker.queued:
            if job.kind == "run":
                return job.id
        return None

    def bench_read(self, *, wait: bool = True, timeout_s: float = 30.0) -> dict:
        """A read-back job. Returns the `/bench` dict once it has run (or
        `{"job": id}` at once with `wait=False`); `Busy` while a run is active."""
        def make(job: Job):
            def gen():
                snapshot = self._read_back()
                for verdict in self.bench.chain_verdicts(snapshot):
                    yield verdict
            return gen()

        # The check and the queueing under the one lock `submit` takes, so
        # a run cannot be queued between them and put this job behind it.
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
        # The job id rides along so a route that waited can still answer
        # with the job it ran, as the non-waiting form does.
        return {"job": job_id, **self.bench_snapshot()}

    def bench_action(self, name: str, args: Mapping[str, Any] | None = None, *,
                     wait: bool = True, timeout_s: float = 30.0) -> dict:
        """One explicit bench action as a job, journaled `by="hand"`.

        `KeyError` for an unknown name (404), `ValueError` for a bad body
        (422), `BenchActionRefused` when the bench will not do it now (409
        with the verdict), `Busy` while a run is active -- except `park`,
        which aborts the run, cancels the queue and then parks. The
        `BenchAction` carries `result.before`, the read-back's values of
        what the action changes, so the log can say "NORM -> INV".

        A park that has not started within `timeout_s` is *not* cancelled:
        the run it aborted may be inside a VISA call that outlasts the wait,
        and a park that vanished because the operator's click timed out
        would leave the bench where the aborted run's own unwind put it with
        no by-hand line to say so. The answer is then `{"pending": True}`
        and the job runs when the worker frees.
        """
        if name not in ACTIONS:
            raise KeyError(name)
        args = dict(args or {})
        outcome: dict[str, Any] = {}
        # The bace parameters in force are read here, on the caller's
        # thread, not inside the job: `PUT /modules/bace/params` edits the
        # catalogue on the loop thread, and the worker reading the layers
        # while they change is a dict changing size under iteration.
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
                # One step on the worker: the queued runs and the current
                # one are taken under its lock, so a run it pops in between
                # cannot run to completion ahead of the park.
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
        """On the worker thread: read every instrument and cache the result."""
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
        """Wait for a job; True once it has run. A job the caller gave up on
        must not run later, unattended, after whatever was ahead of it, so
        on timeout it is cancelled if it has not started and the caller
        told (`TimeoutError`) either way -- unless `cancel_on_timeout` is
        False (park), when the answer is False and the job stays queued."""
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
        """`GET /bench`: the cached read-back, the session's V_oc, the run in
        progress and the queue. Touches no instrument.

        While a run holds the worker the read-back is the one Start took,
        before the module enabled anything, so the instruments the running
        step implies (`RunRecord.live`) are overlaid on it with
        `how: "inferred"` and listed in `inferred`: the rail says "bias
        LIVE, relay amplifier" while a bace runs because the run's events
        say so, and `read_at` stays the read-back's. The temperature block
        follows the newest `TemperatureRead` on the event path (the monitor,
        the pause's poll, the operator's typed value) when it is newer than
        the read-back.
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
        """The read-back's temperature block, overlaid with the newest
        reading the event path saw when that is newer, plus the monitor's
        state. `wired` stays what the rig says: an operator's typed number
        does not make the 331 wired."""
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
        """The raw last read-back, as `pipeline.validate` and the catalogue
        want it; None before the first."""
        with self._lock:
            return self._snapshot

    # -- modules --------------------------------------------------------------------
    def modules_wire(self) -> dict:
        """`GET /modules`."""
        return {"modules": [self.module_wire(name) for name in self.catalogue.names()]}

    def module_wire(self, name: str) -> dict:
        """One module entry, with `last` from the registry and `needs` from the
        session's V_oc and the cached read-back. `KeyError` for no such module."""
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
        """Frames with `seq` above `seq`, from the ring (last 5000). The last
        `RING_TRACES_KEPT` shots come back with their traces; older `StepDone`
        frames carry their scalars and verdict only."""
        with self._lock:
            full = {f["seq"]: f for f in self._traces}
            return [full.get(f["seq"], f) for f in self._ring if f["seq"] > int(seq)]

    def subscribe(self, queue: Any = None) -> Any:
        """Register a sink with `put_nowait`/`qsize` (an `asyncio.Queue` by
        default) and return it. A sink more than `SUBSCRIBER_BACKLOG` frames
        behind is dropped: it gets a `Notice` frame, then `None`."""
        q = queue if queue is not None else asyncio.Queue()
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
        """Worker thread. The digitiser's diagnostics are read here, beside
        the shot they describe, and the worker-side view of the run's open
        nodes and their shot counts is kept here, in event order."""
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
        """Worker thread only, so no lock: which nodes are open and what
        each module has measured, for the `NodeDone` an abort cannot yield."""
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
        """Worker thread (or the caller's, for `cancelled`). Nothing on the
        record is written here: the `RunStateChanged` goes down the one
        event path and `_apply` updates the record in order with the
        events before it."""
        ts = time.time()
        if job.kind == "run":
            with self._lock:
                rec = self._records.get(job.id)
                if rec is not None and state == "stopping" and rec.stopping:
                    return                                  # already said by `stop()`
            if rec is not None and state in TERMINAL and state != "cancelled":
                # The generator is gone (abort) or never reported (a worker
                # failure): close what it left open, with the folder its
                # recorder opened and the shots it took, so the record and
                # the journal do not say "nothing was written" about a run
                # whose files are on disk.
                for node_path in reversed(list(rec.open_nodes)):
                    self._dispatch(job, E.NodeDone(node_path=node_path, outcome=state,
                                                   detail=self._synthesized_detail(
                                                       rec, node_path, state)),
                                   node_path=node_path, ts=ts)
                rec.open_nodes.clear()
            self._dispatch(job, E.RunStateChanged(state, reason), node_path="", ts=ts)
        elif state == "parked" or state in TERMINAL:
            # A read-back or an action: nothing to journal about its state,
            # only the caller waiting in `_await` to wake. The terminal
            # report comes first and `parked` right after; either will do.
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
        """Route one event to `_ingest`: directly on the loop's thread or with
        no loop attached, otherwise posted to the loop."""
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
        """The one path: envelope, verdict, journal, ring, registry, fan-out.

        An ephemeral event (`StepPhase`) takes the short form of it: no seq,
        no journal line, no place in the ring -- applied to the record (the
        live overlay reads it) and fanned out to whoever is listening now.
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
            # The seq is taken only once the payloads exist: a number spent
            # on an event that could not be framed would be a gap every
            # client reads as a missed frame and reconnects to replay.
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
                self._apply(run_id, node_path, ev, ts)
            self._fan_out(ws)

    def _journal(self, line: dict) -> None:
        try:
            self.journal.append(line)
        except Exception as exc:                            # noqa: BLE001
            self._error(f"seq {line.get('seq')} {line.get('type')}: journal: "
                        f"{type(exc).__name__}: {exc}")

    def _apply(self, run_id: str, node_path: str, ev: E.Event, ts: float) -> None:
        rec = self._records.get(run_id)
        if rec is None:
            return
        if rec.live is not None:
            rec.live.apply(ev, rec.schedule)
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
                # A loop's Progress carries the executor's re-derived ETA
                # (what this run has measured so far): the record's finish
                # time follows it, and the submit-time one is kept beside.
                rec.eta = {"eta_s": ev.eta_s, "finish_at": ts + ev.eta_s, "at": ts,
                           "node_path": ev.node_path, "done": ev.done, "total": ev.total}
                cost = dict(rec.cost)
                cost.setdefault("finish_at_submit", cost.get("finish_at"))
                cost["finish_at"] = ts + ev.eta_s
                cost["finish_source"] = "measured"
                rec.cost = cost
        elif isinstance(ev, E.Verdict):
            # One entry per (code, node) -- a Start re-reads the chain and
            # dispatches the same verdicts the submit-time validation
            # copied, and a "2 warn" count taken off the record must not be 4.
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
        """The record's state bookkeeping, in order with the run's events."""
        if state == "parked":
            rec.parked = True
            rec.ended.set()
            return
        if state == "preflight" and rec.started_at is None:
            rec.started_at = ts
        if rec.stopping and state in ("preflight", "running"):
            # A stop accepted during preflight: the session said `stopping`
            # the moment it was accepted, and the worker's start-of-run
            # reports, which follow, must not put `running` over it.
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
        # The seq to reconnect from is the last *numbered* frame, which is
        # what the ring holds; the frame in hand may be ephemeral (seq None).
        since = self._seq
        for q in list(self._subscribers):
            try:
                if ephemeral and q.qsize() > EPHEMERAL_BACKLOG:
                    continue
                q.put_nowait(frame)
                behind = q.qsize() > SUBSCRIBER_BACKLOG
            except Exception:                               # noqa: BLE001 -- a dead sink
                behind = True
            if behind:
                self._subscribers.remove(q)
                try:
                    # `seq` None: this frame is for one client and is not
                    # in the ring; a real seq here would be one the client
                    # has already seen, and a pump that dedupes on seq
                    # would drop the very notice that says why it is
                    # being closed.
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
        """`POST /monitors/power`. One at most (409 if running); `ValueError`
        when the bench has no meter or the interval is out of range."""
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
        monitor = PowerMonitor(meter, emit=lambda ev: self._dispatch(None, ev, node_path=""),
                               interval_s=interval_s,
                               console=self.rig_config.power_meter_console)
        self._monitor = monitor
        monitor.start()
        return monitor.info()

    def stop_power_monitor(self) -> bool:
        """`DELETE /monitors/power`. False when none was running."""
        monitor = self._monitor
        if monitor is None or not monitor.running:
            return False
        monitor.stop()
        return True

    # -- the temperature monitor ----------------------------------------------------
    @property
    def temperature_monitor_running(self) -> bool:
        m = self._temperature_monitor
        return m is not None and m.running

    def start_temperature_monitor(self, interval_s: float = 5.0) -> dict:
        """`POST /monitors/temperature`: the 331 console read every
        `interval_s` beside whatever the worker is doing (HTTP, never the
        bus). One at most (409); `ValueError` when no console is named in
        rig.toml or the interval is out of range."""
        interval_s = float(interval_s)
        if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
            raise ValueError(f"interval_s must be between {MIN_INTERVAL_S:g} and "
                             f"{MAX_INTERVAL_S:g} s, not {interval_s:g}")
        if self.temperature_monitor_running:
            raise Conflict("the temperature monitor is already running")
        monitor = TemperatureMonitor(self.rig_config.temperature_console,
                                     emit=lambda ev: self._dispatch(None, ev, node_path=""),
                                     interval_s=interval_s,
                                     controller=self.bench.rig.temperature)
        self._temperature_monitor = monitor
        monitor.start()
        return monitor.info()

    def stop_temperature_monitor(self) -> bool:
        """`DELETE /monitors/temperature`. False when none was running."""
        monitor = self._temperature_monitor
        if monitor is None or not monitor.running:
            return False
        monitor.stop()
        return True

    def monitors(self) -> list[dict]:
        """`GET /monitors`."""
        return [m.info() for m in (self._monitor, self._temperature_monitor)
                if m is not None and m.running]


def tree_for_module(module: str, params: Mapping[str, Any] | None = None,
                    name: str = "") -> dict:
    """The one-node tree `POST /runs` builds from `{module, params, name}`."""
    tree: dict[str, Any] = {"kind": "module", "module": str(module),
                            "params": dict(params or {})}
    if name:
        tree["name"] = str(name)
    return tree


__all__ = [
    "Session", "RunRecord", "SubmitRefused", "Conflict", "Busy", "NotPaused",
    "UnknownRun", "DataUnavailable", "NodeRequired", "BlockedAtStart", "tree_for_module",
    "RING_SIZE", "RING_TRACES_KEPT", "SUBSCRIBER_BACKLOG", "DATA_RUNS_KEPT", "BENCH_STATES",
]
