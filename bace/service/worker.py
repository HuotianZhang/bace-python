"""The bench lock: one thread, one job at a time, and the two honest stops.

**Why one thread.** Every instrument call is blocking VISA I/O, VISA sessions
are not thread-safe, and the relay interlock only holds when one sequence of
calls reaches it. So a run, a bench read-back and a bench action are all the
same thing here -- a `Job` on a FIFO queue, executed by the single
`RunWorker` thread -- and the bench lock is not a lock object anyone can
forget to take: it is the fact that there is exactly one thread. Anything
that needs the bus waits its turn; a second Start while a run is live is
queued, not refused and not run beside it.

**Why the callbacks run on the worker thread.** `on_event` and `on_state`
are called from inside the job, between one instrument call and the next.
The session hands them to asyncio with `loop.call_soon_threadsafe`; this
module never imports asyncio, so it can be tested with a list and two
functions, and so the worker cannot stall the event loop.

**The two stops**, from the plan, both ending in `parked`:

- `after_shot` sets the job's abort flag. Every generator polls it at its
  natural boundary (transient: before each step; jv: before each curve;
  executor: before each node); the shot in flight completes and is kept,
  the generator yields `RunAborted(reason="requested", done, total)` and
  unwinds through its own `finally`. Terminal state `stopped`.
- `abort` cannot interrupt a blocking VISA call, so the worker acts at the
  next event it receives: it calls `gen.close()`, which raises
  `GeneratorExit` at the yield and unwinds every `finally` in the chain
  (outputs off, shutter shut, recorders flushed), then emits
  `RunAborted(reason="aborted", done, total)` itself -- the generator is
  gone and cannot -- counting the shots it saw go by. A shot that completed
  between the request and that event was already recorded by the generator
  and is reported as it came; nothing after it starts. Terminal state
  `aborted`. An abort that arrives while the job is paused in
  `wait_for_operator` raises `AbortNow` inside the generator instead, which
  unwinds the same way.

**States.** `queued -> preflight -> running (<-> paused) -> stopping ->` a
terminal state (`done`, `stopped`, `aborted`, `failed`, `blocked`), then
`parked`. The contract says every terminal state is reached through
`parked`; here that is the terminal state set on `job.state` and reported,
then `job.park()` (the callable the session installs, which runs
`Rig.park()`), then one more `on_state(job, "parked", <terminal state>)`.
`job.state` stays the terminal state -- `parked` is a transition every
ending passes through, not a place a run rests -- and `job.parked` records
that it happened. `cancelled` is the exception: a job removed from the queue
never touched the bench, so nothing is parked and the `cancelled` report is
the last one. `blocked` is what `make()` raising `Blocked` ends in: a safety
check refused the run at Start, before the generator existed, and the run
must not be mistaken for one that crashed. `paused` and the return to
`running` are reported when the worker sees `NeedsOperator` and
`OperatorResumed` go by, so the state follows the events rather than a
second bookkeeping path.

**A resume answers a pause, never the next one.** `Job.resume` keeps the
operator's detail only while the job is paused -- from the `NeedsOperator`
the worker saw to the moment `wait_for_operator` returns. A resume that
lands while the run is measuring is refused (the session's 409) *and*
discarded: kept, it would answer the next temperature pause the instant it
opened, and the subtree below would be measured with the cryostat wherever
it was, tagged with a temperature nobody confirmed.

**The thread never dies on a job.** An exception from the generator, from
`make`, from a callback or from `park()` is recorded in `job.error`; the job
ends `failed` (or keeps the terminal state it had reached) and the next job
runs. A worker that died with a queue behind it would leave the operator
with a bench that answers nothing.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Iterator

from ..experiment.events import (Event, NeedsOperator, OperatorResumed, Progress,
                                 RunAborted, RunFailed, RunStarted, StepDone)
from ..experiment.jv import JVCurveDone, JVStarted


class StopMode:
    AFTER_SHOT = "after_shot"
    ABORT = "abort"
    ALL: tuple[str, ...] = (AFTER_SHOT, ABORT)


class AbortNow(Exception):
    """Raised inside a generator by `Job.wait_for_operator` when an abort was
    requested while it waited. Let it propagate: the generator's `finally`
    blocks run on the way out, and the worker turns it into `aborted`."""


class Blocked(Exception):
    """Raised by `make()` when a safety check refuses the run at Start -- a
    live output on the read-back, a compliance over the ceiling. The job
    ends `blocked` (and is parked like every ending), not `failed`: nothing
    was touched and nothing crashed, and the console must be able to tell
    "the bench was unsafe" from "the scope fell over" without parsing an
    error string."""


TERMINAL: frozenset[str] = frozenset(
    {"done", "stopped", "aborted", "failed", "blocked", "cancelled"})
JOB_KINDS: tuple[str, ...] = ("run", "readback", "action")


class Job:
    """One unit of work for the worker thread.

    `make(job)` is called on the worker thread and must return a generator
    of events; it gets the job so the generator can poll `job.abort_check`
    and block in `job.wait_for_operator`. `park` is the callable run after
    every terminal state (the session installs `Rig.park`); `node_path` is
    for the generator to update as it moves through a tree, and is what a
    synthesized `RunFailed` reports as `where`.
    """

    def __init__(self, id: str, kind: str, make: Callable[["Job"], Iterator[Event]], *,
                 park: Callable[[], None] | None = None, name: str = ""):
        if kind not in JOB_KINDS:
            raise ValueError(f"job kind must be one of {JOB_KINDS}, not {kind!r}")
        if not callable(make):
            raise TypeError("make must be a callable taking the Job and returning "
                            "a generator of events")
        self.id = str(id)
        self.kind = kind
        self.make = make
        self.park = park
        self.name = name
        self.state = "queued"
        self.stop_mode: str | None = None
        self.error: str | None = None
        self.node_path = ""
        self.last_progress: Progress | None = None
        """The run's own last `Progress` (node_path ""): a loop's progress
        counts iterations, not shots, and must not stand in for it."""
        self.shots_done = 0
        """`StepDone`/`JVCurveDone` seen since the module in flight started.
        The synthesized `RunAborted` counts from here as well as from the
        last `Progress`, because the `Progress` a module yields describes
        the shot *before* the one it just reported."""
        self.parked = False
        self.paused = False
        self.queued_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._cv = threading.Condition()
        self._resume_detail: dict | None = None

    def __repr__(self) -> str:
        return f"Job({self.id!r}, {self.kind}, state={self.state})"

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    # -- the generator's side ------------------------------------------
    def abort_check(self) -> bool:
        """True once a stop of either mode was requested. The generators'
        abort callable: the shot in flight finishes, the next is not started."""
        return self.stop_mode is not None

    def wait_for_operator(self, *, poll_s: float = 0.2,
                          on_poll: Callable[[], None] | None = None) -> dict:
        """Block the worker thread until `resume`, a stop, or an abort.

        Returns the resume detail (`{"temperature_k": 250.1, "note": ...}`);
        returns `{"stopped": True}` when `after_shot` was requested, so the
        generator can yield its `RunAborted(reason="requested")` and unwind;
        raises `AbortNow` on `abort`. `on_poll` runs every `poll_s` with no
        lock held -- the executor uses it to read the 331 console and emit
        `TemperatureRead` while the operator works.

        A resume that arrived between the `NeedsOperator` and this call is
        honoured, not discarded: the worker marks the job paused the moment
        it sees `NeedsOperator`, microseconds before the generator gets
        here, and an operator's click that landed in that window must not
        be lost. A resume from before the pause was never kept (see
        `resume`), and whatever is left when the wait ends is dropped here,
        so nothing can answer the next pause but the operator.
        """
        with self._cv:
            self.paused = True
        try:
            while True:
                with self._cv:
                    if self._cv.wait_for(self._ready, timeout=poll_s):
                        if self.stop_mode == StopMode.ABORT:
                            raise AbortNow(f"abort requested while {self.id} waited "
                                           "for the operator")
                        if self.stop_mode == StopMode.AFTER_SHOT:
                            return {"stopped": True}
                        detail = self._resume_detail
                        self._resume_detail = None
                        return dict(detail or {})
                if on_poll is not None:
                    on_poll()
        finally:
            with self._cv:
                self.paused = False
                self._resume_detail = None

    def _ready(self) -> bool:
        return self.stop_mode is not None or self._resume_detail is not None

    # -- the session's side ---------------------------------------------
    def request_stop(self, mode: str) -> str:
        """Ask for `after_shot` or `abort`. An abort is never downgraded to
        an after_shot; the stronger request stands. Returns the mode in
        force. Callable from any thread."""
        if mode not in StopMode.ALL:
            raise ValueError(f"stop mode must be one of {StopMode.ALL}, not {mode!r}")
        with self._cv:
            if self.stop_mode != StopMode.ABORT:
                self.stop_mode = mode
            self._cv.notify_all()
            return self.stop_mode

    def resume(self, detail: dict | None = None) -> bool:
        """Answer a `NeedsOperator`. Returns whether the job was paused at
        the time -- the session's 409 when it was not -- and keeps the
        detail only then: a resume with nothing to answer is dropped, not
        stored for the next pause. Callable from any thread."""
        with self._cv:
            if not self.paused:
                return False
            self._resume_detail = dict(detail or {})
            self._cv.notify_all()
        return True


class RunWorker:
    """The one thread that owns the instruments. FIFO, one job at a time."""

    def __init__(self, *, on_event: Callable[[Job, Event], None],
                 on_state: Callable[[Job, str, str], None]):
        self._on_event = on_event
        self._on_state = on_state
        self._queue: collections.deque[Job] = collections.deque()
        self._cv = threading.Condition()
        self.bus = threading.Lock()
        """Held for the whole of a job. The bench lock made concrete, for the
        one caller that needs to ask rather than be the worker: an observer
        whose instrument is on the bus (`monitors.TemperatureMonitor` when
        this process owns the 331's GPIB session) tries to take it without
        blocking and skips its tick when it cannot. Asking `idle` instead
        raced -- true, then a job starts, then the observer's multi-query read
        interleaves with the run's own GPIB traffic. Never taken by the
        observer while blocking: a run holds this for hours."""
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.current: Job | None = None
        self.jobs_run = 0

    # -- the queue --------------------------------------------------------
    def submit(self, job: Job) -> int:
        """Queue `job`; returns its position (0 = nothing ahead of it, it
        starts when the current job, if any, ends). The `queued` state is
        not reported through `on_state` -- this is the caller's thread --
        so the session journals `RunQueued` and `queued` itself."""
        with self._cv:
            if self._stopping:
                raise RuntimeError("the worker is shutting down; a job queued now "
                                   "would never run")
            taken = {j.id for j in self._queue}
            if self.current is not None:
                taken.add(self.current.id)
            if job.id in taken:
                raise ValueError(f"job id {job.id!r} is already queued or running")
            job.state = "queued"
            job.queued_at = time.time()
            self._queue.append(job)
            position = len(self._queue) - 1
            self._cv.notify_all()
        return position

    def cancel(self, job_id: str) -> bool:
        """Remove a queued job. False when it is not in the queue -- the
        running job is stopped with `request_stop`, not cancelled. The
        `cancelled` report is the one `on_state` call made on the caller's
        thread; `loop.call_soon_threadsafe` does not mind."""
        with self._cv:
            hit = next((j for j in self._queue if j.id == job_id), None)
            if hit is None:
                return False
            self._queue.remove(hit)
        self._cancel(hit, "removed from the queue")
        return True

    def stop_runs(self, reason: str = "park requested") -> tuple[list[Job], Job | None]:
        """Cancel every queued run and abort the one in flight, as one step.

        The queue is emptied of runs and the current job read under the
        same lock, so a run the thread pops between "cancel the queue" and
        "abort the current" cannot slip through and run to completion ahead
        of a park. Returns `(cancelled, aborted)`; read-backs and actions
        already queued are left where they are. Callable from any thread.
        """
        with self._cv:
            runs = [j for j in self._queue if j.kind == "run"]
            for job in runs:
                self._queue.remove(job)
            current = self.current
            self._cv.notify_all()
        for job in runs:
            self._cancel(job, reason)
        if current is not None and current.kind == "run" and not current.terminal:
            current.request_stop(StopMode.ABORT)
            return runs, current
        return runs, None

    @property
    def queued(self) -> list[Job]:
        with self._cv:
            return list(self._queue)

    @property
    def idle(self) -> bool:
        with self._cv:
            return self.current is None and not self._queue

    def wait_idle(self, timeout_s: float | None = None) -> bool:
        """Block until no job is running or queued. For tests and shutdown."""
        with self._cv:
            return self._cv.wait_for(lambda: self.current is None and not self._queue,
                                     timeout=timeout_s)

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        with self._cv:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._stopping:
                raise RuntimeError("a RunWorker cannot be restarted after shutdown")
            self._thread = threading.Thread(target=self._loop, name="bace-worker",
                                            daemon=True)
            self._thread.start()

    def shutdown(self, timeout_s: float = 5.0) -> bool:
        """Cancel the queue, abort the current job, join. Returns whether the
        thread ended in time -- a job inside a long VISA call cannot be
        interrupted, and the caller should know rather than assume."""
        with self._cv:
            self._stopping = True
            leftovers = list(self._queue)
            self._queue.clear()
            current = self.current
            thread = self._thread
            self._cv.notify_all()
        for job in leftovers:
            self._cancel(job, "worker shut down")
        if current is not None and not current.terminal:
            current.request_stop(StopMode.ABORT)
        if thread is not None and thread.is_alive():
            thread.join(timeout_s)
            return not thread.is_alive()
        return True

    def join(self, timeout_s: float | None = None) -> bool:
        """Wait for the thread to end after `shutdown`; True when it has.
        A job inside a blocking VISA call ends when the call does, and the
        bench must not be released from another thread before then."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout_s)
        return not thread.is_alive()

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # -- the thread -------------------------------------------------------
    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: bool(self._queue) or self._stopping)
                if self._stopping:
                    return
                job = self._queue.popleft()
                self.current = job
            try:
                with self.bus:
                    self._execute(job)
            except BaseException as exc:            # noqa: BLE001 -- the thread outlives any job
                job.error = _append(job.error, f"worker: {_describe(exc)}")
                if not job.terminal:
                    # Through `_finish`, so the bench is parked: a "parked"
                    # report for a job that skipped park() would be a lie
                    # in the journal about the state the bench was left in.
                    self._finish(job, "failed", job.error)
            finally:
                with self._cv:
                    self.current = None
                    self.jobs_run += 1
                    self._cv.notify_all()

    def _execute(self, job: Job) -> None:
        job.started_at = time.time()
        self._set_state(job, "preflight", "building the generator")
        try:
            gen = job.make(job)
            if not (hasattr(gen, "close") and hasattr(gen, "__next__")):
                raise TypeError(
                    f"make() returned {type(gen).__name__}, not a generator; abort "
                    "works by closing the generator so its finally blocks park the "
                    "bench, and there is nothing to close here")
        except Blocked as exc:
            # The crit verdicts went out before the raise; this carries
            # their text as the run's error, under its own state.
            job.error = _describe(exc)
            self._emit(job, RunFailed(error=job.error, where="preflight"))
            self._finish(job, "blocked", job.error)
            return
        except Exception as exc:
            job.error = _describe(exc)
            self._emit(job, RunFailed(error=job.error, where=job.node_path or "preflight"))
            self._finish(job, "failed", job.error)
            return

        self._set_state(job, "running", "started")
        outcome, reason = "done", "finished"
        aborted_reported = failed_reported = stopping_reported = False
        try:
            while True:
                try:
                    ev = next(gen)
                except StopIteration:
                    break
                except AbortNow as exc:
                    outcome, reason = "aborted", str(exc)
                    if not aborted_reported:
                        self._synthetic_abort(job)
                        aborted_reported = True
                    break
                except Exception as exc:
                    job.error = _describe(exc)
                    outcome, reason = "failed", job.error
                    if not failed_reported:
                        self._emit(job, RunFailed(error=job.error,
                                                  where=job.node_path or job.kind))
                        failed_reported = True
                    break

                if isinstance(ev, NeedsOperator):
                    # Arm the pause before the event goes out, not after:
                    # a listener that answers the pause the instant it sees
                    # `NeedsOperator` (a fast operator, or a test hook) calls
                    # `resume` before the generator has reached
                    # `wait_for_operator`, and `resume` keeps a detail only
                    # while the job is paused. Armed here, that answer is
                    # kept; a resume that arrives while the run is measuring,
                    # between one pause's answer and the next pause, finds
                    # `paused` False and is dropped.
                    job.paused = True
                self._emit(job, ev)
                if isinstance(ev, Progress):
                    if ev.node_path == "":
                        job.last_progress = ev
                elif isinstance(ev, (RunStarted, JVStarted)):
                    job.shots_done = 0
                    job.last_progress = None
                elif isinstance(ev, (StepDone, JVCurveDone)):
                    job.shots_done += 1
                elif isinstance(ev, RunAborted):
                    aborted_reported = True
                    if job.stop_mode == StopMode.ABORT:
                        outcome, reason = "aborted", ev.reason
                    else:
                        outcome, reason = "stopped", ev.reason
                elif isinstance(ev, RunFailed):
                    failed_reported = True
                    job.error = ev.error
                    outcome, reason = "failed", ev.error
                elif isinstance(ev, NeedsOperator):
                    job.paused = True
                    self._set_state(job, "paused", ev.what)
                elif isinstance(ev, OperatorResumed):
                    self._set_state(job, "running", "operator resumed")

                if job.stop_mode == StopMode.ABORT:
                    # The next yield was the earliest honest moment; this is it.
                    self._close(gen, job)
                    if not aborted_reported:
                        self._synthetic_abort(job)
                        aborted_reported = True
                    outcome, reason = "aborted", "abort requested"
                    break
                if (job.stop_mode == StopMode.AFTER_SHOT and not stopping_reported
                        and not aborted_reported):
                    stopping_reported = True
                    self._set_state(job, "stopping", "after_shot requested")
        finally:
            self._close(gen, job)
        self._finish(job, outcome, reason)

    def _finish(self, job: Job, outcome: str, reason: str) -> None:
        job.finished_at = time.time()
        self._set_state(job, outcome, reason)
        if job.park is not None:
            try:
                job.park()
            except Exception as exc:                # noqa: BLE001 -- recorded, never fatal
                job.error = _append(job.error, f"park() raised: {_describe(exc)}")
        job.parked = True
        self._report(job, "parked", outcome)

    def _cancel(self, job: Job, reason: str) -> None:
        job.state = "cancelled"
        job.finished_at = time.time()
        self._report(job, "cancelled", reason)

    def _synthetic_abort(self, job: Job) -> None:
        p = job.last_progress
        done = max(p.done if p else 0, job.shots_done)
        self._emit(job, RunAborted(reason="aborted", done=done, total=p.total if p else 0))

    @staticmethod
    def _close(gen, job: Job) -> None:
        try:
            gen.close()
        except Exception as exc:                    # noqa: BLE001 -- unwinding is best effort
            job.error = _append(job.error, f"unwinding raised: {_describe(exc)}")

    def _set_state(self, job: Job, state: str, reason: str) -> None:
        job.state = state
        self._report(job, state, reason)

    def _report(self, job: Job, state: str, reason: str) -> None:
        try:
            self._on_state(job, state, reason)
        except Exception as exc:                    # noqa: BLE001 -- a broken listener is not a broken run
            job.error = _append(job.error, f"on_state({state}) raised: {_describe(exc)}")

    def _emit(self, job: Job, ev: Event) -> None:
        try:
            self._on_event(job, ev)
        except Exception as exc:                    # noqa: BLE001 -- the recorders are inside the generator
            job.error = _append(job.error,
                                f"on_event({type(ev).__name__}) raised: {_describe(exc)}")


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _append(existing: str | None, text: str) -> str:
    return text if not existing else f"{existing}; {text}"
