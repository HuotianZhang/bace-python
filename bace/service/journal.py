"""Appends session events to JSONL and reconstructs runs and query history.

`<out>/journal/<session_id>.jsonl` contains one wire envelope per line under
`service.wire`'s journal payload policy. `SessionStarted` is the first line,
with mode, configuration paths and code fingerprint identifying the source
(docs/service-contract.md section 3, Journal).

Every line is flushed. The file is append-only and never rewritten or
truncated. Process failure leaves the flushed event prefix for replay.
Opening an existing file continues it without a second `SessionStarted`
and resumes `seq` from the last line.

Queries depend on these events for every run:

- `RunQueued` identifies kind, module, tree, parameters, name and folder.
  The session uses its single event path; `run_queued` builds the same
  envelope for scripts and tests. `params` contains edited and carried
  last-used choices for `last_used_params`. `resolved` records every
  executed value. Feeding resolved defaults back as last-used would mask
  later recipe edits. `sample` carries per-run identity because resuming a
  journal retains its header. Processes starting in the same second can
  share a file, so the header alone cannot identify each run's device.
  `run_index` exposes the per-run sample.
- `RunStateChanged` records every transition. `run_index` reports the
  terminal state, which `last_used_params` filters on. The following
  `parked` records park completion without replacing that terminal state.
- `NeedsOperator` and `OperatorResumed` bracket a pause with matching
  `run_id` and `node_path`. `settle_history` learns from temperature pauses.
- A `temperature.settled` verdict supplies `data.setpoint_k` and
  `data.settle_s`, giving automatic settle durations to the same cost model.
- `NodeDone` supplies folder, summary and module counts. Pipeline counts
  sum module nodes in both journal and registry, keeping `GET /runs` and
  `GET /runs/{id}` consistent. `node_record` retains module outcomes, V_oc,
  LED level, temperature provenance, folders and reduced measurements.
  Transients retain axes and per-point Q statistics; J-V retains curve
  metrics. Shot counts retain intensity availability and digitiser flags.
  Another session can serve run and results grids without opening HDF5.
- `Progress` with empty `node_path` counts module shots. Loop progress
  counts iterations and must not replace those counts.
- `StepDone`, `RunFinished`, `RunAborted`, `RunFailed`, `JVCurveDone` and
  `JVFinished` supply timing and outcome text.

Queries visit this file first, then other journal files newest first, up to
`MAX_HISTORY_FILES` total. Session IDs use `YYYYMMDD_HHMMSS`, so name order
matches time order. Unparseable lines are skipped: a process can stop during
a write, and a torn line must not disable queries.

`append` folds this file incrementally. Other files are parsed once and
cached by size and modification time. Queries then walk memory rather than
re-parsing files for every node in a Dry run. The canonical 90-module
schedule asks for parameters at every node; repeated parsing would block
the event loop and scale with session count.

`shot_time_s` and `settle_history` accept only headers matching this
session's `mode` and `fast`. Simulated fast timing must not teach the real
bench cost model. Headerless files remain eligible because their bench
cannot be classified. `last_used_params` and `run_index` read every mode:
operator choices and run records remain useful across bench modes.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from typing import Any, Iterable

import numpy as np

from ..experiment.events import Envelope, RunQueued
from ..experiment.wire import envelope_to_wire

JOURNAL_DIR = "journal"
MAX_HISTORY_FILES = 20
"""Limits a history query to 20 session files, including this file.
This bounds cache memory while retaining weeks of typical bench use
(docs/service-contract.md section 3, Journal).
"""

TERMINAL_STATES: frozenset[str] = frozenset(
    {"done", "stopped", "aborted", "failed", "blocked", "cancelled"})
COMPLETED_STATES: frozenset[str] = frozenset({"done", "stopped"})
"""Selects completed and after-shot-stopped runs for last-used parameters.
These runs execute their supplied parameters. An aborted, failed or
blocked run may execute none of them.
"""

RUN_KINDS: tuple[str, ...] = ("manual", "pipeline")


def run_queued(*, seq: int, ts: float | None, run_id: str, kind: str,
               module: str | None, tree: dict | None, params: dict | None,
               name: str = "", folder: str | None = None,
               resolved: dict | None = None, sample: dict | None = None) -> dict:
    """Builds the same `RunQueued` envelope as the session event path.

    `experiment.events.RunQueued` makes script and test lines byte-exact
    with session lines. `kind` is `manual` or `pipeline`; `module` names a
    manual module and is `None` for a pipeline. `params` contains operator
    choices for `last_used_params`; `resolved` contains all executed
    values. `sample` retains the queued identity. The dataclass refuses an
    invalid kind or a manual run without a module
    (docs/service-contract.md section 3, Journal).
    """
    event = RunQueued(kind=kind, module=module, tree=tree, params=params,
                      resolved=resolved, name=name, folder=folder, sample=sample)
    return envelope_to_wire(Envelope(seq=int(seq), ts=time.time() if ts is None else float(ts),
                                     run_id=run_id, node_path="", event=event))


class Journal:
    """Owns an append-only session journal and queries session files.

    `append` accepts calls from any thread. One lock serialises write,
    flush and incremental folding. Queries walk parsed records under that
    same lock, so a worker append cannot mutate a dictionary being read
    on the event-loop thread.
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
        """Identifies this session's bench for cost-model file filtering."""
        self.seq = 0
        """Records the last written `seq`, starting at 0 for `SessionStarted`.
        The session counter starts above this value when a file is resumed.
        """
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
        """Writes and flushes one wire envelope as a JSON line.

        `envelope` follows `service.wire.journal_payload` or comes from
        `run_queued`. `allow_nan=False` enforces strict JSON: payload conversion
        must turn NaN into null. A remaining NaN is a caller error and must
        not produce an unreadable journal line
        (docs/service-contract.md section 3, Journal).
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
            # Folding the written dictionary exposes the line to queries
            # immediately without re-reading the file.
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
        """Returns the latest chosen parameters for a `done` or `stopped` module.

        Returns `None` when no queried session has an eligible run.
        """
        with self._lock:
            for parsed in self._history():
                for run in reversed(parsed.runs):
                    if (run.module == module and run.state in COMPLETED_STATES
                            and isinstance(run.params, dict)):
                        return dict(run.params)
        return None

    def run_index(self, session: str = "this") -> list[dict]:
        """Returns one summary per run, newest first.

        `session` selects `this`, `all` or a session ID.
        """
        out: list[dict] = []
        with self._lock:
            for parsed in self._history(session):
                out.extend(run.summary() for run in reversed(parsed.runs))
        return out

    def run_record(self, run_id: str) -> dict | None:
        """Returns a run's summary, tree and nodes from its journal, or `None`.

        This serves `GET /runs/{id}` for runs outside the session registry.
        The run ID identifies its session file. The bounded history search
        is needed only if that file cannot supply the record.
        """
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
        """Returns settle durations in seconds by setpoint rounded to 0.1 K.

        Matched temperature pauses contribute resume time minus pause time.
        A `temperature timeout` pause also includes prior `elapsed_s`, matching
        the executor's ETA. Automatic `temperature.settled` verdicts contribute
        `settle_s` (docs/service-contract.md section 7).

        Refused setpoints and unanswered pauses teach no settle duration.
        Files from another bench mode are excluded. List order is irrelevant:
        the cost model takes the median.
        """
        out: dict[float, list[float]] = {}
        with self._lock:
            for parsed in self._history():
                if not self._same_bench(parsed):
                    continue
                for key, samples in parsed.settle.items():
                    out.setdefault(key, []).extend(samples)
        return out

    def shot_time_s(self, module: str = "bace") -> float | None:
        """Returns median shot seconds from the latest eligible module run.

        Eligible runs end `done` or `stopped`. Consecutive `StepDone` intervals
        match the node-path leaf, so pipeline and manual modules both count.
        Intervals stay within one node; an intervening J-V cannot inflate a
        transient shot time. A single-shot run supplies no interval, so the
        search continues. Other bench modes are excluded. Returns `None`
        without an eligible interval.
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
        """Returns cached parsed files with this session first.

        `append` updates this session's object. Other files are parsed on
        first use and re-parsed only when size or modification time changes.
        """
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
            # Files beyond the `MAX_HISTORY_FILES` query limit need no cache.
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
    """Extracts `bace` from a node path such as `T=250K/led=1.020V/bace#2`."""
    return node_path.rsplit("/", 1)[-1].split("#", 1)[0]


class _Parsed:
    """Folds a session file into its header, ordered runs and settle pairs."""

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
            # Prior automatic-settle time belongs to a timeout pause's total.
            # Counting only operator wait would underestimate measured settle.
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
            # Automatic and operator settle durations feed the same cost model.
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
    """Reconstructs one run from journal lines in file order."""

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
        """Retains non-`ok` verdicts once per `code` and `node_path`.
        Start read-back verdicts supersede submit-time checks.
        """
        self.sample: dict | None = None
        self.nodes: dict[str, dict] = {}
        """Stores module `NodeDone` data reduced by `node_record`.
        This supplies run details and results grids across sessions.
        """
        self._started: dict[str, float | None] = {}
        self._results: dict[str, dict] = {}
        """Collects node measurements before `NodeDone`, keyed by node path.
        It retains transient axes and Q statistics, reduced J-V curves and
        `StepDone` counts.
        """
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
            self.outcome_text = finished_text(data, self.kept, self.requested)
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
            # `info`, including `trigger.auto`, matters when judging Q near zero.
            # Keep one entry per code and node, with Start read-back replacing
            # submit-time checks, just as in the session registry.
            if data.get("level") in ("info", "warn", "crit", "invalid"):
                entry = {"level": data.get("level"), "code": data.get("code"),
                         "text": data.get("text"),
                         "node_path": data.get("node_path") or line.get("node_path") or "",
                         "ts": ts}
                for i, old in enumerate(self.verdicts):
                    if old["code"] == entry["code"] and old["node_path"] == entry["node_path"]:
                        self.verdicts[i] = entry
                        break
                else:
                    self.verdicts.append(entry)
        elif kind == "JVCurveDone":
            metrics = data.get("metrics") or {}
            # Only `dark is False` confirms light. Falsy `None` means unknown
            # illumination. Its crossing can be dark noise and must not enter
            # the persisted V_oc summary as a light measurement.
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
        """Formats V_oc for one light curve or the range across several levels.

        A `jv_bace` summary describes the sweep rather than only its last point.
        """
        if self._last_voc is None:
            return ""
        if self.light_curves > 1 and self.voc_min != self.voc_max:
            return f" · V_oc {self.voc_min:.4f} … {self.voc_max:.4f} V"
        return f" · V_oc {self._last_voc:.3f} V"

    def record(self) -> dict:
        """Returns `summary` with tree and node reductions for `GET /runs/{id}`."""
        return {**self.summary(), "tree": self.tree, "nodes": dict(self.nodes),
                "verdicts": list(self.verdicts)}

    def summary(self) -> dict:
        kept, requested = self.kept, self.requested
        text = self.outcome_text
        if self.kind == "pipeline":
            # Summing module nodes matches the session registry's counts.
            # Pipeline text counts module runs, rather than only the last module.
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
"""Selects measurement keys that `node_record` adds to `NodeDone` data."""


def node_record(node_path: str, outcome: Any, detail: dict, result: dict | None, *,
                started_at: float | None, finished_at: float | None) -> dict:
    """Builds one module's `nodes[<path>]` record for `GET /runs/{id}`.

    The registry and journal use this shape, so results grids need no
    process-specific interpretation. `detail` comes from `NodeDone`;
    `result` holds prior measurements from `finished_result`, `jv_result`
    and `count_shot` (docs/service-contract.md section 6).

    Small one-dimensional results comprise transient axis values and
    per-point `q_mean` and `q_std`, or J-V curve metrics. Traces and sweep
    arrays are omitted. `GET /runs/{id}/data` supplies full arrays while
    the run is in memory; HDF5 retains them on disk.
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
        # Temperature provenance distinguishes a settled reading, a requested
        # setpoint and a typed value, even when the numbers agree.
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
        # Without `LoopDone`, retain the latest running statistics at each
        # point reached before the stop.
        n = max(running)
        out["q_mean"] = [_num(running[i][0]) if i in running else None for i in range(1, n + 1)]
        out["q_std"] = [_num(running[i][1]) if i in running else None for i in range(1, n + 1)]
    return out


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def axis_result(data: dict) -> dict:
    """Retains `AxisResolved` values before any shot completes.

    A stop within the first loop must still identify the scanned axis.
    """
    return {"axis": data.get("axis"), "values": _floats(data.get("values"))}


def loop_result(data: dict) -> dict:
    """Retains the latest `LoopDone` mean and standard deviation per point.

    `RunFinished` supersedes this at completion. A stopped node therefore
    keeps statistics over its completed loops.
    """
    return {"q_mean": _floats(data.get("q_mean")), "q_std": _floats(data.get("q_std"))}


def finished_result(data: dict) -> dict:
    """Retains `RunFinished` axis values and per-point Q statistics.

    `q_all` and traces remain outside this reduced node result.
    """
    return {"axis": data.get("axis"), "values": _floats(data.get("values")),
            "q_mean": _floats(data.get("q_mean")), "q_std": _floats(data.get("q_std"))}


JV_CURVE_KEYS: tuple[str, ...] = ("label", "dark", "led_level_v", "direction", "n_points")


def jv_result(data: dict) -> dict:
    """Reduces `JVFinished` curves to labels and interpolated metrics.

    The full list replaces incremental `add_curve` results. The console
    labels these metrics derived, as docs/ui-rules.md section 6 requires.
    """
    return {"curves": [_curve(c) for c in data.get("curves") or [] if isinstance(c, dict)]}


def add_curve(result: dict, data: dict) -> None:
    """Appends one reduced `JVCurveDone` to the node's curve list.

    `after_shot` acts between curves, so a stopped J-V may have no
    `JVFinished`. Its completed curves remain available, just as a
    stopped transient retains `LoopDone` statistics.
    """
    result.setdefault("curves", []).append(_curve(data))


def _curve(curve: dict) -> dict:
    entry = {key: curve.get(key) for key in JV_CURVE_KEYS}
    entry["metrics"] = dict(curve["metrics"]) if isinstance(curve.get("metrics"), dict) else None
    return entry


def count_shot(result: dict, data: dict) -> None:
    """Counts a shot, its intensity reading and any non-`ok` digitiser verdict.

    The summary distinguishes missing intensity from a flagged shot.
    """
    result["shots"] = int(result.get("shots") or 0) + 1
    # `StepDone` statistics include this shot at this point. Retaining
    # them gives partial first loops per-point results before `LoopDone`.
    # `LoopDone` or `RunFinished` replaces the array.
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


def finished_text(data: dict, kept: int | None, requested: int | None) -> str:
    """Formats Q and its standard error over loops in nC.

    Standard error (`sem`) describes precision of the mean, while `q_std`
    is loop scatter. `data` holds `RunFinished` fields. Both run list and
    node summary use this text.
    """
    tail = f" · {kept}/{requested}" if kept is not None and requested is not None else ""
    def nc(key: str) -> np.ndarray:
        values = data.get(key)
        return np.array([np.nan if q is None else q for q in ([] if values is None else values)],
                        dtype=float) * 1e9
    q_mean, q_std = nc("q_mean"), nc("q_std")
    q_all = data.get("q_all")
    n = (np.isfinite(np.asarray(q_all, dtype=float).reshape(-1, q_mean.size)).sum(axis=0)
         if q_all is not None and q_mean.size else np.zeros(q_mean.size, dtype=int))
    if not np.isfinite(q_mean).any():
        return f"{q_mean.size} point" + ("" if q_mean.size == 1 else "s") + tail
    sem = np.full(q_mean.size, np.nan)
    if q_std.size == q_mean.size:
        sem = np.where(n > 1, q_std / np.sqrt(np.maximum(n, 1)), np.nan)
    loops = int(n.max())
    of = f", {loops} loop" + ("" if loops == 1 else "s") if loops else ""
    if q_mean.size == 1:
        text = f"Q {q_mean[0]:#.3g}"
        text += f" ± {sem[0]:.2g} nC (sem)" if np.isfinite(sem[0]) else " nC"
        return f"{text}{of}{tail}"
    text = f"{q_mean.size} points, Q {np.nanmin(q_mean):#.3g} to {np.nanmax(q_mean):#.3g} nC"
    if np.isfinite(sem).any():
        text += f", sem ≤ {np.nanmax(sem):.2g}"
    return f"{text}{of}{tail}"


_LOOP_HEAD = {"temperature": "T", "illumination": "led", "repeat": "rep"}


def _tree_summary(tree: Any) -> str:
    """Summarises a tree, such as `T×9/led×5/jv_bace+bace`.

    A manual run uses its module name. The text is never parsed.
    """
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
    """Counts module executions as leaves times enclosing loop counts.

    Returns `None` if a loop count cannot be read.
    """
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
