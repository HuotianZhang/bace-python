"""The session journal: one append-only JSONL file, and the queries it answers.

`<out>/journal/<session_id>.jsonl`, one JSON object per line, each line a
wire envelope under the journal payload policy (`service.wire`). The first
line is `SessionStarted` with the header the session was opened with -- mode,
config paths, the code fingerprint -- so a file read months later says what
produced it.

**Append-only, flushed after every line, never rewritten.** A run that dies
with the process still leaves every line up to the last event on disk, and a
file that is only ever appended to can be replayed for failure recovery
later without the format changing now. Nothing here truncates: a `Journal`
opened on a file that already exists continues it, does not write a second
`SessionStarted`, and picks its `seq` up from the last line.

**What the queries key on.** The history queries reconstruct runs from these
lines and nothing else, so the session must journal them for every run:

- `RunQueued` -- the `experiment.events.RunQueued` dataclass, sent by the
  session down its one event path like every other frame (`run_queued()`
  here builds the same line for a test or a script): `data = {kind, module,
  tree, params, resolved, name, folder}`. It is the only line that knows
  which module a manual run is and what parameters it was given. `params`
  is what the operator chose (the edited layer, and last-used values carried
  forward) and is what `last_used_params` hands back; `resolved` is every
  value the module ran with, for the record. The two are separate because a
  journal that fed every resolved default back as "last-used" would, after
  one run, show the whole card as last-used and hide a recipe edit behind it
  for twenty sessions. `sample` is the `[sample]` block the run was queued
  under -- its identity, per run rather than only in the header
  (`docs/naming-plan.md` rule 1): `Journal` resumes an existing file and
  writes `SessionStarted` only when it did not, so two processes started in
  the same second share a file and the second one's device would otherwise
  be attributed to the first's. `run_index` exposes it on every row.
- `RunStateChanged` -- `data = {state, reason}`, on every transition. The
  terminal state (`done`, `stopped`, `aborted`, `failed`, `blocked`,
  `cancelled`) is what `run_index` reports and what `last_used_params`
  filters on; the `parked` transition that follows it is noted but never
  replaces it.
- `NeedsOperator` / `OperatorResumed` -- the pair that brackets a pause;
  `settle_history` pairs a `NeedsOperator(what="temperature")` with the
  next `OperatorResumed` carrying the same `run_id` and `node_path`.
- `Verdict` with code `temperature.settled` -- a node the 331 console
  settled on its own; `settle_history` reads `data.setpoint_k` and
  `data.settle_s` off it, so the cost model learns from automated runs as
  it does from the operator's pauses.
- `NodeDone` -- `detail.folder`, `detail.summary` and, for a module node,
  `detail.kept`/`detail.requested` feed the run index. A pipeline's counts
  are the sum over its module nodes, the same sum the session's run record
  makes, so `GET /runs` and `GET /runs/{id}` cannot disagree about a run.
  Each module node is also kept whole (`nodes[<path>]`, the shape
  `node_record()` builds: module, outcome, counts, the V_oc it centred on,
  its LED level and the temperature triple, its folder, and what it
  measured -- a transient's axis with `q_mean`/`q_std` per point from its
  `RunFinished`, a J-V's curves reduced to their metrics from `JVFinished`,
  and how many of its shots carried an intensity or a digitiser verdict) so
  a run from an earlier session can still answer `GET /runs/{id}` -- the
  pipeline tab's grey V_oc grid is the *previous* run's, that record died
  with the process that made it, and the results tab draws its grid from
  these without opening a single HDF5.
- `Progress` with `node_path == ""` -- the run's own progress. A loop's
  `Progress` carries the loop's path and counts iterations, not shots, and
  is not folded into the counts.
- `StepDone`, `RunFinished`, `RunAborted`, `RunFailed`, `JVCurveDone`,
  `JVFinished` -- timing and the outcome text.

**Reading.** Every query reads this session's file first and then older
`<out>/journal/*.jsonl` newest first, at most `MAX_HISTORY_FILES` in all.
Session ids are `YYYYMMDD_HHMMSS`, so sorting by name is sorting by time. A
line that does not parse is skipped: the last line of a session that died
mid-write is torn by construction, and a torn line in a file from last week
must not take this session's history queries down with it.

**Parsed once.** This session's file is folded line by line as `append`
writes it, and an older file is parsed once and kept, keyed on its size and
modification time -- a query is then a walk over objects in memory, not a
re-read of twenty files. The Dry run resolves the canonical tree's ninety
module steps and asks for last-used parameters at each; re-parsing a
week's journals ninety times per click was seconds on the event loop after
one long run, and grew with every session.

**Which files teach the cost model.** `shot_time_s` and `settle_history`
read only files whose `SessionStarted` header says the same `mode` and
`fast` as this session: a `--sim --fast` session measures a shot in a
millisecond and settles a "cryostat" in the time a click takes, and the lab
PC's Dry run must not learn either. A file with no header (nothing says
where it came from) is read. `last_used_params` and `run_index` read
everything: what the operator typed on the sim console is still what they
typed.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from typing import Any, Iterable

from ..experiment.events import Envelope, RunQueued
from ..experiment.wire import envelope_to_wire

JOURNAL_DIR = "journal"
MAX_HISTORY_FILES = 20
"""How many session files a history query reads, this one included. Twenty
sessions is weeks of bench time; beyond that the memory spent on history
nobody asks for is not worth it."""

TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "stopped", "aborted", "failed", "blocked", "cancelled"})
COMPLETED_STATES: frozenset[str] = frozenset({"done", "stopped"})
"""States in which what ran is worth remembering: a run that finished or
was stopped after a shot ran with the parameters it was given. An aborted,
failed or blocked run may have run with none of them."""

RUN_KINDS: tuple[str, ...] = ("manual", "pipeline")


def run_queued(*, seq: int, ts: float | None, run_id: str, kind: str,
               module: str | None, tree: dict | None, params: dict | None,
               name: str = "", folder: str | None = None,
               resolved: dict | None = None, sample: dict | None = None) -> dict:
    """The `RunQueued` line as the session writes it: the
    `experiment.events.RunQueued` dataclass in a wire envelope, so a line
    built here for a test or a script is byte-for-byte what the one event
    path produces. `kind` is "manual" (one module node) or "pipeline";
    `module` is the module name for a manual run and None for a pipeline;
    `params` is what the operator chose for the module (what
    `last_used_params` hands back next session); `resolved` is every value
    it will run with; `sample` the `[sample]` block it was queued under.
    The dataclass refuses a bad `kind` and a manual run with no module.
    """
    event = RunQueued(kind=kind, module=module, tree=tree, params=params,
                      resolved=resolved, name=name, folder=folder, sample=sample)
    return envelope_to_wire(Envelope(seq=int(seq), ts=time.time() if ts is None else float(ts),
                                     run_id=run_id, node_path="", event=event))


class Journal:
    """One session's append-only log, plus the history queries over all of them.

    `append` may be called from any thread; a lock serialises the write, the
    flush and the fold into this session's parsed history. The queries walk
    the parsed files under the same lock, so a line appended on the worker
    thread cannot change a dict a query on the loop thread is iterating.
    """

    def __init__(self, out: str, session_id: str, *, header: dict):
        if not isinstance(header, dict):
            raise TypeError(f"header must be a dict, not {type(header).__name__}")
        self.out = str(out)
        self.session_id = str(session_id)
        self._dir = os.path.join(self.out, JOURNAL_DIR)
        os.makedirs(self._dir, exist_ok=True)
        self.path = os.path.join(self._dir, f"{self.session_id}.jsonl")
        self._lock = threading.RLock()
        self.mode = str(header.get("mode", "")) or None
        self.fast = bool(header.get("fast", False))
        """This session's bench, for the cost-model queries' file filter."""
        self.seq = 0
        """The last `seq` written. 0 after a fresh open (the SessionStarted
        line); the session's own counter starts above it."""
        self.lines_written = 0
        self._own = _Parsed(self.session_id)
        self._cache: dict[str, tuple[tuple[int, int], _Parsed]] = {}
        self.resumed = os.path.isfile(self.path) and os.path.getsize(self.path) > 0
        if self.resumed:
            seqs = []
            for line in _read(self.path):
                self._own.apply(line)
                if isinstance(line.get("seq"), int) and not isinstance(line["seq"], bool):
                    seqs.append(int(line["seq"]))
            self.seq = max(seqs, default=0)
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        if not self.resumed:
            data = dict(header)
            given = data.setdefault("session_id", self.session_id)
            if given != self.session_id:
                self._fh.close()
                self._fh = None
                raise ValueError(f"header names session {given!r} but the journal "
                                 f"is {self.session_id!r}")
            self.append({"seq": 0, "ts": time.time(), "run_id": None, "node_path": "",
                         "type": "SessionStarted", "data": data, "decimated": {}})

    # -- writing ----------------------------------------------------------
    def append(self, envelope: dict) -> None:
        """Write one line and flush it. `envelope` is a wire payload
        (`service.wire.journal_payload`) or a `run_queued()` dict.

        `allow_nan=False`: the payload policy has already turned every NaN
        into null, so a NaN reaching here is a bug in a caller, and a bare
        `NaN` token in the file would be a line no browser and no strict
        parser could read back.
        """
        if not isinstance(envelope, dict) or not isinstance(envelope.get("type"), str):
            raise TypeError("a journal line is a dict with a string 'type'; got "
                            f"{type(envelope).__name__}")
        text = json.dumps(envelope, allow_nan=False, separators=(",", ":"))
        with self._lock:
            if self._fh is None:
                raise RuntimeError(f"journal {self.path} is closed")
            self._fh.write(text + "\n")
            self._fh.flush()
            seq = envelope.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                self.seq = seq
            self.lines_written += 1
            # Folded from the same dict that was written, so a query made
            # a moment later sees this line without the file being re-read.
            self._own.apply(json.loads(text))

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def __enter__(self) -> "Journal":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the queries ------------------------------------------------------
    def last_used_params(self, module: str) -> dict | None:
        """The `params` of the most recent `RunQueued` for `module` whose run
        reached `done` or `stopped`; None when no session has one."""
        with self._lock:
            for parsed in self._history():
                for run in reversed(parsed.runs):
                    if (run.module == module and run.state in COMPLETED_STATES
                            and isinstance(run.params, dict)):
                        return dict(run.params)
        return None

    def run_index(self, session: str = "this") -> list[dict]:
        """One summary per run, newest first. `session` is "this", "all", or
        a session id."""
        out: list[dict] = []
        with self._lock:
            for parsed in self._history(session):
                out.extend(run.summary() for run in reversed(parsed.runs))
        return out

    def run_record(self, run_id: str) -> dict | None:
        """One run's record from the journal -- its summary plus `tree` and
        `nodes` -- from whichever session file holds it; None when no file
        does. What `GET /runs/{id}` answers for a run this process never
        held: the run ids carry the session id, so the file is found first
        and the twenty-file history walked only when it is not there."""
        session = run_id.rsplit("-", 1)[0] if "-" in run_id else "all"
        with self._lock:
            for which in (session, "all"):
                for parsed in self._history(which):
                    run = parsed.find(run_id)
                    if run is not None:
                        return run.record()
                if session == "all":
                    break
        return None

    def settle_history(self) -> dict[float, list[float]]:
        """Seconds each temperature took to settle, keyed by setpoint
        rounded to 0.1 K: between `NeedsOperator(what="temperature")` and
        the `OperatorResumed` that answered it, the same for a
        `temperature timeout` pause plus the `elapsed_s` the console had
        already taken before it (what the executor's own ETA learns from
        that node, so the Dry run and the run agree), and the `settle_s` of
        every `temperature.settled` verdict the 331 console produced. A
        refused setpoint's pause is not a settle and teaches nothing. Order
        inside a list is not significant; the cost model takes the median.
        A pause that was never answered contributes nothing; a session on
        another kind of bench (see the module docstring) contributes nothing
        either."""
        out: dict[float, list[float]] = {}
        with self._lock:
            for parsed in self._history():
                if not self._same_bench(parsed):
                    continue
                for key, samples in parsed.settle.items():
                    out.setdefault(key, []).extend(samples)
        return out

    def shot_time_s(self, module: str = "bace") -> float | None:
        """Median seconds between consecutive `StepDone` of the last run of
        `module` that reached `done` or `stopped`, or None.

        Matched on the leaf of each `StepDone`'s node path, so a pipeline's
        `T=250K/led=1.020V/bace` counts as much as a manual `bace`; gaps are
        taken within one node only, so the J-V between two bace nodes never
        looks like a slow shot. A run with a single shot has no interval and
        the search moves on to the run before it. Runs from another kind of
        bench are skipped.
        """
        with self._lock:
            for parsed in self._history():
                if not self._same_bench(parsed):
                    continue
                for run in reversed(parsed.runs):
                    if run.state not in COMPLETED_STATES:
                        continue
                    gaps: list[float] = []
                    for node_path, stamps in run.step_ts.items():
                        leaf = _leaf(node_path) if node_path else (run.module or "")
                        if leaf != module:
                            continue
                        gaps += [b - a for a, b in zip(stamps, stamps[1:])]
                    if gaps:
                        return float(statistics.median(gaps))
        return None

    def _same_bench(self, parsed: "_Parsed") -> bool:
        header = parsed.header
        if header is None:
            return True
        mode = str(header.get("mode", "")) or None
        return mode == self.mode and bool(header.get("fast", False)) == self.fast

    # -- files ------------------------------------------------------------
    def _files(self, session: str = "all") -> list[str]:
        if session == "this":
            return [self.path] if os.path.isfile(self.path) else []
        if session != "all":
            p = os.path.join(self._dir, f"{session}.jsonl")
            return [p] if os.path.isfile(p) else []
        try:
            names = sorted((n for n in os.listdir(self._dir) if n.endswith(".jsonl")),
                           reverse=True)
        except OSError:
            names = []
        mine = os.path.basename(self.path)
        ordered = ([self.path] if mine in names else []) + [
            os.path.join(self._dir, n) for n in names if n != mine]
        return ordered[:MAX_HISTORY_FILES]

    def _history(self, session: str = "all") -> Iterable["_Parsed"]:
        """The parsed files a query walks, this session's first. This
        session's is the object `append` folds into; an older file is parsed
        on first sight and kept until its size or mtime changes."""
        for path in self._files(session):
            if path == self.path:
                yield self._own
                continue
            yield self._parsed(path)

    def _parsed(self, path: str) -> "_Parsed":
        try:
            st = os.stat(path)
            stamp = (int(st.st_size), int(st.st_mtime_ns))
        except OSError:
            stamp = (-1, -1)
        with self._lock:
            hit = self._cache.get(path)
            if hit is not None and hit[0] == stamp:
                return hit[1]
        parsed = _Parsed(os.path.splitext(os.path.basename(path))[0])
        for line in _read(path):
            parsed.apply(line)
        with self._lock:
            self._cache[path] = (stamp, parsed)
            # Bounded: a query lists at most MAX_HISTORY_FILES, so anything
            # beyond that in the cache is a file that fell off the end.
            for old in [p for p in self._cache if p not in set(self._files("all"))]:
                self._cache.pop(old, None)
        return parsed


# -- reconstruction -------------------------------------------------------
def _read(path: str) -> list[dict]:
    out: list[dict] = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError:
        return out
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue                    # torn by a crash mid-write
            if isinstance(line, dict):
                out.append(line)
    return out


def _leaf(node_path: str) -> str:
    """`T=250K/led=1.020V/bace#2` -> `bace`."""
    return node_path.rsplit("/", 1)[-1].split("#", 1)[0]


class _Parsed:
    """One session file, folded: its header, its runs in order of first
    appearance, and its settle pairs."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.header: dict | None = None
        self.runs: list[_Run] = []
        self._by_id: dict[str, _Run] = {}
        self.settle: dict[float, list[float]] = {}
        self._pending: dict[tuple, tuple[float, float]] = {}

    def apply(self, line: dict) -> None:
        kind, data = line.get("type"), line.get("data") or {}
        if not isinstance(data, dict):
            return
        if kind == "SessionStarted":
            self.header = dict(data)
        elif kind == "NeedsOperator" and data.get("what") in ("temperature",
                                                                "temperature timeout"):
            detail = data.get("detail") or {}
            setpoint = detail.get("setpoint_k")
            ts = line.get("ts")
            # The console's own time before a timeout pause counts: the
            # operator's wait alone would say the settle was shorter than
            # the run measured it.
            try:
                before = max(0.0, float(detail.get("elapsed_s") or 0.0))
            except (TypeError, ValueError):
                before = 0.0
            if setpoint is not None and ts is not None:
                self._pending[self._pause_key(line, data)] = (float(ts) - before,
                                                              float(setpoint))
        elif kind == "OperatorResumed":
            hit = self._pending.pop(self._pause_key(line, data), None)
            ts = line.get("ts")
            if hit is not None and ts is not None:
                t0, setpoint = hit
                self.settle.setdefault(round(setpoint, 1), []).append(float(ts) - t0)
        elif kind == "Verdict" and data.get("code") == "temperature.settled":
            # The 331 console's own settle: the second source of the cost
            # model's per-temperature time, beside the operator's pauses.
            inner = data.get("data") or {}
            try:
                setpoint, settle = float(inner["setpoint_k"]), float(inner["settle_s"])
            except (KeyError, TypeError, ValueError):
                setpoint = settle = None
            if setpoint is not None and settle is not None and settle >= 0.0:
                self.settle.setdefault(round(setpoint, 1), []).append(settle)
        run_id = line.get("run_id")
        if not isinstance(run_id, str):
            return
        run = self._by_id.get(run_id)
        if run is None:
            run = self._by_id[run_id] = _Run(run_id, self.session_id)
            self.runs.append(run)
        run.apply(line)

    def find(self, run_id: str) -> "_Run | None":
        return self._by_id.get(run_id)

    @staticmethod
    def _pause_key(line: dict, data: dict) -> tuple:
        return (line.get("run_id"), data.get("node_path") or line.get("node_path", ""))


class _Run:
    """One run, folded from its lines in file order."""

    def __init__(self, run_id: str, session_id: str):
        self.run_id = run_id
        self.session_id = session_id
        self.kind: str | None = None
        self.name = ""
        self.module: str | None = None
        self.tree: dict | None = None
        self.params: dict | None = None
        self.state = "unknown"
        self.parked = False
        self.queued_at: float | None = None
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.kept: int | None = None
        self.requested: int | None = None
        self.node_kept: int | None = None
        self.node_requested: int | None = None
        self.modules_done = 0
        self.outcome_text = ""
        self.folder = ""
        self.folders: list[str] = []
        self.error: str | None = None
        self.step_ts: dict[str, list[float]] = {}
        self.verdicts: list[dict] = []
        """Every `warn`/`crit` verdict this run's lines carried, in order."""
        self.sample: dict | None = None
        self.nodes: dict[str, dict] = {}
        """Each module node's `NodeDone`, reduced by `node_record()`: what
        the pipeline tab's V_oc grid, the results tab and `GET /runs/{id}`
        need from a run of another session."""
        self._started: dict[str, float | None] = {}
        self._results: dict[str, dict] = {}
        """Per node path, what the node measured before its `NodeDone`:
        `RunFinished`'s axis and per-point statistics, `JVFinished`'s
        reduced curves, and the counts folded from its `StepDone` lines."""
        self._last_voc: float | None = None
        self.voc_min: float | None = None
        self.voc_max: float | None = None
        self.light_curves = 0

    def apply(self, line: dict) -> None:
        kind, data, ts = line.get("type"), line.get("data") or {}, line.get("ts")
        if not isinstance(data, dict):
            return
        if kind == "RunQueued":
            self.kind = data.get("kind")
            self.name = data.get("name") or ""
            self.module = data.get("module")
            self.tree = data.get("tree")
            self.params = data.get("params")
            self.queued_at = ts
            if data.get("folder"):
                self.folder = str(data["folder"])
            if isinstance(data.get("sample"), dict):
                self.sample = dict(data["sample"])
        elif kind == "NodeStarted":
            self._started[str(data.get("node_path") or line.get("node_path") or "")] = ts
        elif kind == "RunStateChanged":
            state = data.get("state")
            if state == "parked":
                self.parked = True
            elif isinstance(state, str):
                self.state = state
                if state in ("preflight", "running") and self.started_at is None:
                    self.started_at = ts
                if state in TERMINAL_STATES:
                    self.finished_at = ts
                    if state == "cancelled":
                        self.outcome_text = "cancelled"
        elif kind == "Progress":
            if not data.get("node_path"):
                self.requested = _int(data.get("total"), self.requested)
                self.kept = _int(data.get("done"), self.kept)
        elif kind == "RunAborted":
            self.kept = _int(data.get("done"), self.kept)
            self.requested = _int(data.get("total"), self.requested)
            reason = data.get("reason")
            verb = {"requested": "stopped after", "aborted": "aborted at"}.get(reason, reason)
            self.outcome_text = f"{verb} {self.kept}/{self.requested}"
        elif kind == "RunFinished":
            if self.requested is not None:
                self.kept = self.requested
            self.outcome_text = _finished_text(data, self.kept, self.requested)
            self._result(line).update(finished_result(data))
        elif kind == "RunFailed":
            self.error = str(data.get("error", ""))
            self.outcome_text = f"failed: {self.error}"
        elif kind == "StepDone":
            if ts is not None:
                self.step_ts.setdefault(line.get("node_path") or "", []).append(float(ts))
            count_shot(self._result(line), data)
        elif kind == "AxisResolved":
            self._result(line).update(axis_result(data))
        elif kind == "LoopDone":
            self._result(line).update(loop_result(data))
        elif kind == "Verdict":
            if data.get("level") in ("warn", "crit"):
                self.verdicts.append({
                    "level": data.get("level"), "code": data.get("code"),
                    "text": data.get("text"),
                    "node_path": data.get("node_path") or line.get("node_path") or "",
                    "ts": ts})
        elif kind == "JVCurveDone":
            metrics = data.get("metrics") or {}
            # `dark is False`, not `not dark`. On the wire the field is
            # three-valued since the `jv`/`light` split, and `None` -- nobody
            # could read whether light reached the sample -- is falsy. Counting
            # such a curve as light writes its interpolated V_oc into the
            # *persisted* summary, so `/runs` would advertise a noise crossing
            # as a measurement for as long as the journal lives.
            if (data.get("dark") is False and isinstance(metrics, dict)
                    and metrics.get("voc") is not None):
                voc = float(metrics["voc"])
                self._last_voc = voc
                self.light_curves += 1
                self.voc_min = voc if self.voc_min is None else min(self.voc_min, voc)
                self.voc_max = voc if self.voc_max is None else max(self.voc_max, voc)
            add_curve(self._result(line), data)
        elif kind == "JVFinished":
            n = data.get("n_curves", len(data.get("curves") or []))
            self.outcome_text = f"{n} curve" + ("" if n == 1 else "s")
            self.outcome_text += self._voc_text()
            self._result(line).update(jv_result(data))
        elif kind == "NodeDone":
            detail = data.get("detail") or {}
            if not isinstance(detail, dict):
                return
            folder = detail.get("folder")
            if folder:
                self.folders.append(str(folder))
                if not self.folder:
                    self.folder = str(folder)
            if detail.get("module"):
                self.modules_done += 1
                kept, requested = _int(detail.get("kept"), None), _int(detail.get("requested"), None)
                if kept is not None and requested is not None:
                    self.node_kept = (self.node_kept or 0) + kept
                    self.node_requested = (self.node_requested or 0) + requested
                path = str(data.get("node_path") or line.get("node_path") or "")
                self.nodes[path] = node_record(
                    path, data.get("outcome"), detail, self._results.get(path),
                    started_at=self._started.get(path), finished_at=ts)

    def _result(self, line: dict) -> dict:
        return self._results.setdefault(str(line.get("node_path") or ""), {})

    def _voc_text(self) -> str:
        """` · V_oc 0.906 V` for one light curve; the range over the levels
        (` · V_oc 1.0269 … 1.0479 V`) when there were several, because the
        session log's jv_bace line is the sweep, not its last point."""
        if self._last_voc is None:
            return ""
        if self.light_curves > 1 and self.voc_min != self.voc_max:
            return f" · V_oc {self.voc_min:.4f} … {self.voc_max:.4f} V"
        return f" · V_oc {self._last_voc:.3f} V"

    def record(self) -> dict:
        """`summary()` plus the tree and the per-node reductions: what
        `GET /runs/{id}` serves for a run from the journal."""
        return {**self.summary(), "tree": self.tree, "nodes": dict(self.nodes),
                "verdicts": list(self.verdicts)}

    def summary(self) -> dict:
        kept, requested = self.kept, self.requested
        text = self.outcome_text
        if self.kind == "pipeline":
            # The session's run record sums its module nodes; the same sum
            # here, so the two endpoints agree, and a pipeline's text counts
            # module runs rather than repeating the last module's line.
            if self.node_requested is not None:
                kept, requested = self.node_kept, self.node_requested
            total = _module_runs(self.tree)
            text = f"{self.modules_done}/{total if total is not None else '?'} module runs"
            if kept is not None and requested is not None:
                text += f" · {kept}/{requested} shots"
            if self.state == "stopped":
                text += " · stopped"
            elif self.state == "aborted":
                text += " · aborted"
            elif self.state in ("failed", "blocked"):
                text += f" · {self.state}: {self.error or ''}".rstrip(": ")
            elif self.state == "cancelled":
                text = "cancelled"
        elif self.state == "blocked":
            text = f"blocked: {self.error or ''}".rstrip(": ")
        return {"run_id": self.run_id, "session_id": self.session_id, "kind": self.kind,
                "name": self.name, "module": self.module,
                "sample": dict(self.sample) if self.sample else None,
                "tree_summary": _tree_summary(self.tree),
                "node_count": _node_count(self.tree), "state": self.state,
                "parked": self.parked, "queued_at": self.queued_at,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "kept": kept, "requested": requested,
                "outcome_text": text, "folder": self.folder,
                "folders": list(self.folders), "error": self.error,
                "voc_min": self.voc_min, "voc_max": self.voc_max,
                "light_curves": self.light_curves, "node_count_done": len(self.nodes)}


# -- the node record: one shape from both sources ---------------------------
NODE_RESULT_KEYS: tuple[str, ...] = ("axis", "values", "q_mean", "q_std", "curves",
                                     "shots", "intensity_recorded", "shots_flagged")
"""What `node_record` takes from a node's measurement beside its `NodeDone`."""


def node_record(node_path: str, outcome: Any, detail: dict, result: dict | None, *,
                started_at: float | None, finished_at: float | None) -> dict:
    """One module node, reduced: the shape `nodes[<path>]` has in
    `GET /runs/{id}` **whichever process answers** -- this one from its
    `RunRecord`, or a later one from the journal file. Built here, once, so
    the two cannot drift: the results tab draws its grid from these and
    must not have to know which side of a restart a run is on.

    `detail` is the `NodeDone`'s; `result` is what the node measured before
    it (`finished_result`, `jv_result`, `count_shot`). Only the scalars a
    grid or a list needs: a transient's per-point `q_mean`/`q_std` over its
    axis `values`, never a trace; a J-V's curves as their metrics, never a
    sweep. Both are 1-D and small; `GET /runs/{id}/data` has the rest while
    the run is in memory, the HDF5 has it forever.
    """
    voc = detail.get("voc")
    out = {
        "node_path": node_path,
        "module": detail.get("module"), "outcome": outcome,
        "kept": _int(detail.get("kept"), None), "requested": _int(detail.get("requested"), None),
        "voc": voc.get("value") if isinstance(voc, dict) else voc,
        "voc_how": voc.get("how") if isinstance(voc, dict) else None,
        "voc_led_v": voc.get("led_v") if isinstance(voc, dict) else None,
        "led_v": detail.get("led_v"), "temperature_k": detail.get("temperature_k"),
        # Beside the number, as `voc_how` sits beside the V_oc: a node
        # recorded at 220 K is worth knowing whether the console settled
        # there, a loop only asked, or somebody typed it into `[sample]`.
        "temperature_how": detail.get("temperature_how"),
        "temperature_source": detail.get("temperature_source"),
        "offset_corrected": detail.get("offset_corrected"),
        "summary": detail.get("summary"), "folder": detail.get("folder"),
        "error": detail.get("error"), "reason": detail.get("reason"),
        "started_at": started_at, "finished_at": finished_at,
        "elapsed_s": detail.get("elapsed_s"),
    }
    for key in NODE_RESULT_KEYS:
        out[key] = (result or {}).get(key)
    running = (result or {}).get("running")
    if out["q_mean"] is None and isinstance(running, dict) and running:
        # No LoopDone ever came: the per-point running statistics of the
        # last shot at each point, over the points reached.
        n = max(running)
        out["q_mean"] = [_num(running[i][0]) if i in running else None for i in range(1, n + 1)]
        out["q_std"] = [_num(running[i][1]) if i in running else None for i in range(1, n + 1)]
    return out


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def axis_result(data: dict) -> dict:
    """`AxisResolved`: the axis and its values, before a single shot -- so a
    node stopped inside its first loop still says what it was sweeping."""
    return {"axis": data.get("axis"), "values": _floats(data.get("values"))}


def loop_result(data: dict) -> dict:
    """`LoopDone`: the service's mean and σ over every point so far. The
    newest wins, and `RunFinished` replaces it when the node completes --
    so a node that was stopped after its third loop carries the statistics
    of three loops, which is what "kept as it is" means (R2·3)."""
    return {"q_mean": _floats(data.get("q_mean")), "q_std": _floats(data.get("q_std"))}


def finished_result(data: dict) -> dict:
    """The per-point statistics of a `RunFinished`, as the journal keeps
    them: the axis, its values, and `q_mean`/`q_std` per point. `q_all`
    and the traces stay where they are."""
    return {"axis": data.get("axis"), "values": _floats(data.get("values")),
            "q_mean": _floats(data.get("q_mean")), "q_std": _floats(data.get("q_std"))}


JV_CURVE_KEYS: tuple[str, ...] = ("label", "dark", "led_level_v", "direction", "n_points")


def jv_result(data: dict) -> dict:
    """A `JVFinished`'s curves reduced to their labels and metrics -- the
    interpolated numbers, which `ui-rules` §6 says to label derived. The
    whole list, replacing the one `add_curve` built as they arrived."""
    return {"curves": [_curve(c) for c in data.get("curves") or [] if isinstance(c, dict)]}


def add_curve(result: dict, data: dict) -> None:
    """One `JVCurveDone`, reduced, onto the node's list. A stop asked for
    `after_shot` is honoured between curves and the node ends without a
    `JVFinished`, so a J-V that kept one curve of two must still say so --
    the same reason a stopped transient keeps its `LoopDone`."""
    result.setdefault("curves", []).append(_curve(data))


def _curve(curve: dict) -> dict:
    entry = {key: curve.get(key) for key in JV_CURVE_KEYS}
    entry["metrics"] = dict(curve["metrics"]) if isinstance(curve.get("metrics"), dict) else None
    return entry


def count_shot(result: dict, data: dict) -> None:
    """Fold one `StepDone` into a node's counts: how many shots there were,
    how many carried an intensity reading (the power meter answered) and
    how many carried a digitiser verdict that was not `ok` -- the two
    absences and the one failure R2·3's summary strip states outright."""
    result["shots"] = int(result.get("shots") or 0) + 1
    # The running mean and σ *at this point, including this shot* -- the
    # service's own, carried on every StepDone -- so a node stopped inside
    # its first loop, before any LoopDone, still has a number per point it
    # reached. A later LoopDone or RunFinished replaces the whole array.
    step = data.get("step")
    if isinstance(step, int) and not isinstance(step, bool) and step >= 1:
        running = result.setdefault("running", {})
        running[step] = (data.get("q_mean"), data.get("q_std"))
    if data.get("intensity_w") is not None:
        result["intensity_recorded"] = int(result.get("intensity_recorded") or 0) + 1
    else:
        result.setdefault("intensity_recorded", 0)
    verdict = data.get("verdict")
    flagged = isinstance(verdict, dict) and verdict.get("level") in ("warn", "crit")
    result["shots_flagged"] = int(result.get("shots_flagged") or 0) + (1 if flagged else 0)


def _floats(values: Any) -> list | None:
    if not isinstance(values, list):
        return None
    out = []
    for v in values:
        out.append(float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
    return out


def _int(value: Any, fallback: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return int(value)


def _finished_text(data: dict, kept: int | None, requested: int | None) -> str:
    q_mean = data.get("q_mean") or []
    q_std = data.get("q_std") or []
    tail = f" · {kept}/{requested}" if kept is not None and requested is not None else ""
    if len(q_mean) == 1 and q_mean[0] is not None:
        text = f"Q {q_mean[0]:.3e}"
        if q_std and q_std[0] is not None:
            text += f" ± {q_std[0]:.1e}"
        return text + " C" + tail
    return f"{len(q_mean)} point" + ("" if len(q_mean) == 1 else "s") + tail


_LOOP_HEAD = {"temperature": "T", "illumination": "led", "repeat": "rep"}


def _tree_summary(tree: Any) -> str:
    """`T×9/led×5/jv_bace+bace` for the canonical tree; the module name for
    a manual run. Descriptive only, never parsed."""
    if not isinstance(tree, dict):
        return ""
    kind = tree.get("kind")
    if kind == "module":
        return str(tree.get("module", "?"))
    if kind == "loop":
        loop = str(tree.get("loop", "?"))
        head = f"{_LOOP_HEAD.get(loop, loop)}×{_loop_count(tree)}"
        inner = "+".join(s for s in (_tree_summary(c) for c in tree.get("children") or [])
                         if s)
        return f"{head}/{inner}" if inner else head
    return ""


def _loop_count(tree: dict) -> Any:
    for key in ("values_k", "levels_v"):
        if isinstance(tree.get(key), list):
            return len(tree[key])
    if "count" in tree:
        return tree["count"]
    for start, stop, step in (("start_k", "stop_k", "step_k"),
                              ("led_start_v", "led_stop_v", "led_step_v")):
        if start in tree and stop in tree:
            try:
                a, b, s = float(tree[start]), float(tree[stop]), float(tree.get(step, 0.0))
            except (TypeError, ValueError):
                return "?"
            return 1 if a == b or s <= 0 else int(round(abs(b - a) / s)) + 1
    return "?"


def _node_count(tree: Any) -> int:
    if not isinstance(tree, dict):
        return 0
    if tree.get("kind") == "module":
        return 1
    return sum(_node_count(c) for c in tree.get("children") or [])


def _module_runs(tree: Any) -> int | None:
    """Module executions in a tree: every leaf times the counts of the loops
    above it. None when a loop's count is not readable."""
    if not isinstance(tree, dict):
        return None
    if tree.get("kind") == "module":
        return 1
    count = _loop_count(tree)
    if not isinstance(count, int):
        return None
    total = 0
    for child in tree.get("children") or []:
        n = _module_runs(child)
        if n is None:
            return None
        total += n
    return count * total
