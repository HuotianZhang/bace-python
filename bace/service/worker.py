"""Serialises instrument jobs and implements stop and abort.

VISA calls block, VISA sessions are not thread-safe, and the relay interlock
requires one ordered sequence of calls. Runs, read-backs and bench actions
are `Job` objects on a FIFO queue. The `RunWorker` thread executes one job
at a time under `RunWorker.bus`, the bench lock. A second run waits in the
queue (docs/service-contract.md section 2).

`on_event` and `on_state` run on the worker thread between instrument calls.
The session bridges them to asyncio with `loop.call_soon_threadsafe`, so
journaling and socket fan-out cannot stall instrument I/O. This module has
no asyncio dependency and also accepts plain callbacks in tests.

Both stop modes end with park (docs/service-contract.md section 2):

- `after_shot` sets the job's stop flag. Generators poll it before each
  transient step, J-V curve or executor node. The shot in flight completes
  and is kept. The generator yields `RunAborted(reason="requested")`,
  unwinds its `finally` blocks and ends `stopped`.
- `abort` cannot interrupt blocking VISA I/O. At the next event the worker
  calls `gen.close()`. `GeneratorExit` unwinds every `finally`: outputs off,
  shutter shut, recorders flushed. The worker emits
  `RunAborted(reason="aborted")` with the observed shot count. A completed
  shot recorded before that event is kept and reported; no later shot
  starts. During `Job.wait_for_operator`, `AbortNow` unwinds the generator
  instead. The terminal state is `aborted`.

States follow `queued -> preflight -> running (<-> paused) -> stopping`,
then `done`, `stopped`, `aborted`, `failed` or `blocked`. The worker reports
that terminal state, calls `job.park` and reports `parked`.
`job.state` retains the terminal state; `job.parked` records completion of
park. A cancelled queued job never touches the bench and needs no park.
`Blocked` from `make` means a safety check refuses Start before a generator
exists. It remains distinct from an execution failure. `NeedsOperator` and
`OperatorResumed` drive `paused` and `running` on the event path.

`Job.resume` retains operator detail only during the open pause, from the
worker's receipt of `NeedsOperator` through the return of
`Job.wait_for_operator`. An unpaused resume is refused with HTTP 409 and
discarded. Retaining it could answer the next temperature pause without
confirmation and label a subtree with an unconfirmed cryostat temperature.

Exceptions from a generator, `make`, callbacks or park are recorded in
`job.error`. The job ends `failed` or retains its terminal state. The worker
continues with the next job, so a failed job cannot strand the queue.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable, Iterator

from ..experiment.events import (Event, NeedsOperator, OperatorResumed, Progress,
                                 RunAborted, RunFailed, RunStarted, StepDone)
from ..experiment.jv import JVCurveDone, JVStarted

log = logging.getLogger(__name__)


class StopMode:
    AFTER_SHOT = "after_shot"
    ABORT = "abort"
    ALL: tuple[str, ...] = (AFTER_SHOT, ABORT)


class AbortNow(Exception):
    """Signals abort during `Job.wait_for_operator`.

    Propagation runs the generator's `finally` blocks. The worker reports
    `aborted`.
    """


class Blocked(Exception):
    """Signals that a safety check refuses Start before instrument writes.

    A live output or excessive compliance ends the job `blocked`, followed
    by park. The console distinguishes an unsafe bench from an execution
    failure without parsing an error string.
    """


TERMINAL: frozenset[str] = frozenset(
    {"done", "stopped", "aborted", "failed", "blocked", "cancelled"})
JOB_KINDS: tuple[str, ...] = ("run", "readback", "action")


class Job:
    """Represents one unit of work for the worker thread.

    `make(job)` runs on that thread and returns an event generator. The
    generator polls `job.abort_check`, waits in `job.wait_for_operator`
    and updates `node_path` for synthesized `RunFailed.where` values.
    `park` runs after terminal states for executed jobs; the session
    installs `Rig.park` (docs/service-contract.md section 2).
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
        """Retains the module's last `Progress` with an empty `node_path`.
        A loop counts iterations and cannot supply the shot count.
        """
        self.shots_done = 0
        """Counts `StepDone` and `JVCurveDone` events in the active module.
        The synthesized `RunAborted` also uses this count because a module's
        `Progress` describes the shot before its latest completed shot.
        """
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
        """Returns whether either stop mode is requested.

        Generators finish the shot in flight and start no further shot.
        """
        return self.stop_mode is not None

    def wait_for_operator(self, *, poll_s: float = 0.2,
                          on_poll: Callable[[], None] | None = None) -> dict:
        """Waits on the worker thread for resume, stop or abort.

        Returns resume detail such as `{"temperature_k": 250.1, "note": ...}`.
        `after_shot` returns `{"stopped": True}` so the generator can emit
        `RunAborted(reason="requested")` and unwind. `abort` raises `AbortNow`.
        `on_poll` runs every `poll_s` without a lock held. The executor uses
        it for 331 readings and `TemperatureRead` events during the pause.

        A resume between `NeedsOperator` and this call must be retained:
        the worker arms `paused` before publishing that event. `Job.resume`
        discards replies outside a pause. This wait clears remaining detail
        on exit, so only an operator's reply to the next pause can answer it
        (docs/service-contract.md section 2).
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
        """Requests `after_shot` or `abort` from any thread.

        Returns the mode in force. An abort cannot be downgraded to
        `after_shot`.
        """
        if mode not in StopMode.ALL:
            raise ValueError(f"stop mode must be one of {StopMode.ALL}, not {mode!r}")
        with self._cv:
            if self.stop_mode != StopMode.ABORT:
                self.stop_mode = mode
            self._cv.notify_all()
            return self.stop_mode

    def resume(self, detail: dict | None = None) -> bool:
        """Answers an open `NeedsOperator` pause from any thread.

        Returns whether the job is paused. Otherwise the session returns 409
        and the detail is discarded, so it cannot answer a later pause.
        """
        with self._cv:
            if not self.paused:
                return False
            self._resume_detail = dict(detail or {})
            self._cv.notify_all()
        return True


class RunWorker:
    """Owns the instruments on one thread and executes jobs in FIFO order."""

    def __init__(self, *, on_event: Callable[[Job, Event], None],
                 on_state: Callable[[Job, str, str], None]):
        self._on_event = on_event
        self._on_state = on_state
        self._queue: collections.deque[Job] = collections.deque()
        self._cv = threading.Condition()
        self.bus = threading.Lock()
        """Holds the bench lock throughout each worker job.
        `monitors.TemperatureMonitor` uses a nonblocking acquire when this
        process owns the 331 GPIB session. It skips a tick if the lock is busy.
        Testing `idle` cannot protect a multi-query read: a job can start
        between the test and the read. The observer must not block on this
        lock because a run can hold it for hours
        (docs/service-contract.md section 8).
        """
        self._thread: threading.Thread | None = None
        self._stopping = False
        self.current: Job | None = None
        self.jobs_run = 0

    # -- the queue --------------------------------------------------------
    def submit(self, job: Job) -> int:
        """Queues `job` and returns its zero-based queue position.

        Position 0 starts after any active job. This runs on the caller's
        thread, so the session journals `RunQueued` and `queued` itself
        instead of using `on_state`.
        """
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
        """Removes a queued job and returns whether it is present.

        A running job requires `Job.request_stop`. The `cancelled` report is
        the sole `on_state` call on the caller's thread; the session's
        `loop.call_soon_threadsafe` bridge accepts it.
        """
        with self._cv:
            hit = next((j for j in self._queue if j.id == job_id), None)
            if hit is None:
                return False
            self._queue.remove(hit)
        self._cancel(hit, "removed from the queue")
        return True

    def stop_runs(self, reason: str = "park requested") -> tuple[list[Job], Job | None]:
        """Cancels queued runs and aborts the active run as one operation.

        The queue and current job are read under one lock, so a run cannot
        escape cancellation and complete ahead of park. Returns the cancelled
        jobs and the aborted job. Queued read-backs and actions remain in
        place. Calls are allowed from any thread
        (docs/service-contract.md section 4).
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
        """Waits for an empty queue and idle worker, for tests and shutdown."""
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
        """Cancels the queue, aborts the active job and joins the thread.

        Returns whether the thread ends within the timeout. A blocking VISA
        call cannot be interrupted.
        """
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
        """Waits for the thread after `shutdown` and returns whether it ends.

        A blocking VISA call must return before another thread releases the
        bench (docs/service-contract.md section 9).
        """
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
                    # `_finish` parks the bench before reporting `parked`, so the
                    # journal's safety state agrees with the attempted park.
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
            # The `crit` verdicts precede this error and retain their text
            # under the run's `blocked` state.
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
                    # The wire carries the error text; the service log carries the
                    # traceback, which identifies the instrument call and line.
                    log.error("run %s failed in %s: %s", job.id,
                              job.node_path or job.kind, job.error, exc_info=exc)
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
                    # This yield is the first point where abort can close the generator.
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
