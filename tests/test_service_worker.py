"""The bench lock and the two honest stops, on a worker with no instruments.

A job here is a generator that yields events and polls `job.abort_check()`
where a real run polls its abort callable, so what is under test is the
worker's contract and not any measurement: jobs serialise, events arrive
in order, `after_shot` lets the shot in flight finish and the generator
report the stop, `abort` closes the generator so its `finally` runs, a
raising generator ends `failed` and does not take the worker down, and
every terminal state is followed by `parked`. Nothing sleeps: the tests
wait on events the worker sets.
"""
from __future__ import annotations

import threading

import pytest

from bace.experiment import events as E
from bace.service.wire import make_envelope
from bace.service.worker import AbortNow, Job, RunWorker, StopMode

TIMEOUT = 5.0


def progress(i: int, n: int) -> E.Progress:
    return E.Progress(done=i, total=n, elapsed_s=0.0, eta_s=None)


class Harness:
    """Collects what the worker reports, on the worker's own thread."""

    def __init__(self, on_event=None):
        self.events: list[tuple[str, E.Event]] = []
        self.states: list[tuple[str, str, str]] = []
        self.paused = threading.Event()
        self._hook = on_event
        self.worker = RunWorker(on_event=self._on_event, on_state=self._on_state)

    def _on_event(self, job, ev):
        self.events.append((job.id, ev))
        if self._hook is not None:
            self._hook(job, ev)

    def _on_state(self, job, state, reason):
        self.states.append((job.id, state, reason))
        if state == "paused":
            self.paused.set()

    def start(self, *jobs):
        positions = [self.worker.submit(j) for j in jobs]
        self.worker.start()
        return positions

    def finish(self):
        assert self.worker.wait_idle(TIMEOUT), "the worker never went idle"
        assert self.worker.shutdown(TIMEOUT)
        return self

    def run(self, *jobs):
        self.start(*jobs)
        return self.finish()

    def states_of(self, job_id):
        return [s for jid, s, _ in self.states if jid == job_id]

    def events_of(self, job_id):
        return [ev for jid, ev in self.events if jid == job_id]


def counting(n: int, log: list, tag: str):
    """A job body that logs each shot and its own unwinding."""
    def make(job):
        def gen():
            try:
                for i in range(n):
                    log.append((tag, i))
                    yield progress(i + 1, n)
            finally:
                log.append((tag, "finally"))
        return gen()
    return make


def polite(n: int, log: list):
    """A job body that honours `abort_check` at its shot boundary, the way
    `run_transient_scan` honours its abort callable."""
    def make(job):
        def gen():
            done = 0
            try:
                for i in range(n):
                    if job.abort_check():
                        yield E.RunAborted(reason="requested", done=done, total=n)
                        return
                    log.append(("shot", i))
                    done += 1
                    yield progress(done, n)
            finally:
                log.append(("finally", done))
        return gen()
    return make


def pausing(log: list, polled: threading.Event):
    """A job body that pauses for the operator, as the executor does at a
    temperature node."""
    def make(job):
        def gen():
            try:
                yield E.NeedsOperator(what="temperature", node_path="T=250K",
                                      detail={"setpoint_k": 250.0})
                detail = job.wait_for_operator(poll_s=0.01, on_poll=polled.set)
                log.append(("resumed", detail))
                if detail.get("stopped"):
                    yield E.RunAborted(reason="requested", done=0, total=1)
                    return
                yield E.OperatorResumed(node_path="T=250K", note=detail.get("note", ""),
                                        detail=detail)
                yield progress(1, 1)
            finally:
                log.append(("finally",))
        return gen()
    return make


# -- the bench lock -----------------------------------------------------------
def test_two_jobs_run_strictly_one_after_the_other():
    log, parks = [], []
    a = Job("a", "run", counting(3, log, "a"), park=lambda: parks.append("a"))
    b = Job("b", "run", counting(2, log, "b"), park=lambda: parks.append("b"))
    h = Harness()
    assert h.start(a, b) == [0, 1]
    h.finish()

    assert log == [("a", 0), ("a", 1), ("a", 2), ("a", "finally"),
                   ("b", 0), ("b", 1), ("b", "finally")], "b never overlapped a"
    assert h.states_of("a") == ["preflight", "running", "done", "parked"]
    assert h.states_of("b") == ["preflight", "running", "done", "parked"]
    assert [jid for jid, _, _ in h.states] == ["a"] * 4 + ["b"] * 4
    assert parks == ["a", "b"], "every terminal state passes through park()"
    assert a.state == b.state == "done" and a.parked and b.parked
    assert h.worker.current is None and h.worker.queued == []
    assert a.started_at is not None and a.finished_at >= a.started_at


def test_events_arrive_in_order_and_take_a_monotonic_seq():
    seqs, envelopes = iter(range(1, 100)), []
    h = Harness(on_event=lambda job, ev: envelopes.append(
        make_envelope(next(seqs), job.id, job.node_path or "bace", ev)))
    h.run(Job("a", "run", counting(5, [], "a")), Job("b", "run", counting(2, [], "b")))
    assert [e.seq for e in envelopes] == list(range(1, 8))
    assert [e.event.done for e in envelopes] == [1, 2, 3, 4, 5, 1, 2]
    assert [e.run_id for e in envelopes] == ["a"] * 5 + ["b"] * 2
    assert [ev.done for _, ev in h.events] == [1, 2, 3, 4, 5, 1, 2]


# -- the two stops ------------------------------------------------------------
def test_after_shot_keeps_the_shot_in_flight_and_the_generator_reports_the_stop():
    log, parks = [], []

    def stop_after_two(job, ev):
        if isinstance(ev, E.Progress) and ev.done == 2:
            job.request_stop(StopMode.AFTER_SHOT)

    job = Job("a", "run", polite(5, log), park=lambda: parks.append(1))
    h = Harness(on_event=stop_after_two).run(job)

    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["Progress", "Progress", "RunAborted"]
    assert evs[-1] == E.RunAborted(reason="requested", done=2, total=5)
    assert log == [("shot", 0), ("shot", 1), ("finally", 2)], "the second shot completed"
    assert h.states_of("a") == ["preflight", "running", "stopping", "stopped", "parked"]
    assert job.state == "stopped" and parks == [1] and job.error is None


def test_abort_closes_the_generator_mid_iteration_and_its_finally_runs():
    log, parks = [], []

    def abort_after_three(job, ev):
        if isinstance(ev, E.Progress) and ev.done == 3:
            job.request_stop(StopMode.ABORT)

    job = Job("a", "run", counting(100, log, "a"), park=lambda: parks.append(1))
    h = Harness(on_event=abort_after_three).run(job)

    assert log == [("a", 0), ("a", 1), ("a", 2), ("a", "finally")], (
        "closed at the third yield: no fourth shot, and the finally ran")
    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["Progress"] * 3 + ["RunAborted"]
    assert evs[-1] == E.RunAborted(reason="aborted", done=3, total=100), (
        "synthesized by the worker from the last Progress it saw")
    assert h.states_of("a") == ["preflight", "running", "aborted", "parked"]
    assert job.state == "aborted" and parks == [1]


def test_abort_is_never_downgraded_and_a_bad_mode_is_refused():
    job = Job("a", "run", counting(1, [], "a"))
    assert job.abort_check() is False
    assert job.request_stop(StopMode.ABORT) == "abort"
    assert job.request_stop(StopMode.AFTER_SHOT) == "abort"
    assert job.abort_check() is True
    with pytest.raises(ValueError, match="stop mode"):
        job.request_stop("now")
    with pytest.raises(ValueError, match="job kind"):
        Job("b", "scan", counting(1, [], "b"))


# -- failure ------------------------------------------------------------------
def test_a_raising_generator_fails_parks_and_the_next_job_still_runs():
    log, parks = [], []

    def make(job):
        def gen():
            try:
                yield progress(1, 3)
                raise RuntimeError("scope fell over")
            finally:
                log.append("finally")
        return gen()

    a = Job("a", "run", make, park=lambda: parks.append("a"))
    b = Job("b", "run", counting(2, log, "b"), park=lambda: parks.append("b"))
    h = Harness().run(a, b)

    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["Progress", "RunFailed"]
    assert "scope fell over" in evs[-1].error and evs[-1].where == "run"
    assert h.states_of("a") == ["preflight", "running", "failed", "parked"]
    assert a.state == "failed" and a.error == "RuntimeError: scope fell over"
    assert log == ["finally", ("b", 0), ("b", 1), ("b", "finally")]
    assert h.states_of("b") == ["preflight", "running", "done", "parked"]
    assert parks == ["a", "b"]
    assert h.worker.jobs_run == 2


def test_a_generator_that_reports_its_own_failure_is_not_reported_twice():
    def make(job):
        def gen():
            job.node_path = "T=250K/led=1.020V/bace"
            yield E.RunFailed(error="ValueError: bad axis", where=job.node_path)
        return gen()

    a = Job("a", "run", make)
    h = Harness().run(a)
    assert [type(e).__name__ for e in h.events_of("a")] == ["RunFailed"]
    assert a.state == "failed" and a.error == "ValueError: bad axis"
    assert h.states_of("a") == ["preflight", "running", "failed", "parked"]


def test_a_failing_make_is_a_preflight_failure():
    def make(job):
        raise ValueError("centre_on_voc with no V_oc source")

    a = Job("a", "run", make, park=lambda: None)
    h = Harness().run(a)
    (failed,) = h.events_of("a")
    assert isinstance(failed, E.RunFailed) and failed.where == "preflight"
    assert h.states_of("a") == ["preflight", "failed", "parked"]

    not_a_generator = Job("b", "run", lambda job: [progress(1, 1)])
    h = Harness().run(not_a_generator)
    assert not_a_generator.state == "failed"
    assert "not a generator" in not_a_generator.error


def test_a_broken_listener_is_recorded_and_the_run_goes_on():
    def bad_listener(job, ev):
        if isinstance(ev, E.Progress) and ev.done == 2:
            raise RuntimeError("websocket gone")

    a = Job("a", "run", counting(3, [], "a"))
    b = Job("b", "run", counting(1, [], "b"))
    h = Harness(on_event=bad_listener).run(a, b)
    assert a.state == "done" and "websocket gone" in a.error
    assert len(h.events_of("a")) == 3 and b.state == "done"


def test_a_failing_park_is_recorded_but_the_job_keeps_its_state():
    def park():
        raise OSError("VISA timeout")

    a = Job("a", "run", counting(1, [], "a"), park=park)
    h = Harness().run(a)
    assert a.state == "done" and a.parked and "park() raised: OSError" in a.error
    assert h.states_of("a")[-2:] == ["done", "parked"]


# -- pause --------------------------------------------------------------------
def test_wait_for_operator_returns_the_resume_detail():
    log, polled = [], threading.Event()
    job = Job("a", "run", pausing(log, polled))
    h = Harness()
    h.start(job)
    assert h.paused.wait(TIMEOUT)
    assert polled.wait(TIMEOUT), "the wait polls, and on_poll runs each time"
    assert job.paused is True and job.state == "paused"
    assert job.resume({"temperature_k": 250.1, "note": "set by hand"}) is True
    h.finish()

    assert ("resumed", {"temperature_k": 250.1, "note": "set by hand"}) in log
    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "OperatorResumed", "Progress"]
    assert evs[1].detail == {"temperature_k": 250.1, "note": "set by hand"}
    assert h.states_of("a") == ["preflight", "running", "paused", "running", "done", "parked"]
    assert job.paused is False


def test_a_resume_that_lands_before_the_wait_is_not_lost():
    log = []

    def resume_at_once(job, ev):
        if isinstance(ev, E.NeedsOperator):
            job.resume({"temperature_k": 250.0})       # before the generator waits

    job = Job("a", "run", pausing(log, threading.Event()))
    Harness(on_event=resume_at_once).run(job)
    assert ("resumed", {"temperature_k": 250.0}) in log and job.state == "done"


def test_abort_while_paused_raises_abort_now_inside_the_generator():
    log, polled = [], threading.Event()
    job = Job("a", "run", pausing(log, polled), park=lambda: log.append("park"))
    h = Harness()
    h.start(job)
    assert h.paused.wait(TIMEOUT) and polled.wait(TIMEOUT)
    job.request_stop(StopMode.ABORT)
    h.finish()

    assert log == [("finally",), "park"], "unwound through finally, then parked"
    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "RunAborted"]
    assert evs[-1].reason == "aborted"
    assert h.states_of("a") == ["preflight", "running", "paused", "aborted", "parked"]
    assert job.state == "aborted"


def test_after_shot_while_paused_returns_stopped_so_the_generator_can_report():
    log, polled = [], threading.Event()
    job = Job("a", "run", pausing(log, polled))
    h = Harness()
    h.start(job)
    assert h.paused.wait(TIMEOUT) and polled.wait(TIMEOUT)
    job.request_stop(StopMode.AFTER_SHOT)
    h.finish()

    assert ("resumed", {"stopped": True}) in log
    evs = h.events_of("a")
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "RunAborted"]
    assert evs[-1].reason == "requested"
    assert h.states_of("a") == ["preflight", "running", "paused", "stopped", "parked"]


def test_wait_for_operator_outside_a_worker():
    """The generator's side alone: no worker, the test thread is the operator.

    A resume only answers a pause that is open: one that lands while the job
    is not paused is dropped, so it cannot answer the next pause with a
    number nobody meant for it."""
    job = Job("a", "run", counting(1, [], "a"))
    assert job.resume({"note": "stale"}) is False, "not paused: nothing to answer"

    answered = threading.Event()

    def operator():
        while not job.paused:
            pass
        job.resume({"note": "real"})
        answered.set()

    t = threading.Thread(target=operator)
    t.start()
    assert job.wait_for_operator(poll_s=0.01) == {"note": "real"}, "the dropped one did not answer"
    assert answered.wait(TIMEOUT)
    t.join()
    job.request_stop(StopMode.ABORT)
    with pytest.raises(AbortNow):
        job.wait_for_operator(poll_s=0.01)
    assert job.paused is False


def test_a_resume_while_running_does_not_answer_the_next_pause():
    """The stale-resume guard: an operator answers one pause, then clicks
    Resume again while the run measures; that second click must not silently
    answer the next pause with a temperature nobody confirmed."""
    log, first, second = [], threading.Event(), threading.Event()

    def two_pauses(job):
        def gen():
            yield E.NeedsOperator(what="temperature", node_path="T=295K",
                                  detail={"setpoint_k": 295.0})
            log.append(("resumed", job.wait_for_operator(poll_s=0.01)))
            yield E.OperatorResumed(node_path="T=295K", note="", detail={})
            first.set()
            # A stale resume lands here, while the job is running; it must
            # not survive to the next pause.
            assert second.wait(TIMEOUT)
            yield E.NeedsOperator(what="temperature", node_path="T=250K",
                                  detail={"setpoint_k": 250.0})
            log.append(("resumed", job.wait_for_operator(poll_s=0.01)))
            yield E.OperatorResumed(node_path="T=250K", note="", detail={})
        return gen()

    job = Job("a", "run", two_pauses)
    h = Harness()
    h.start(job)
    assert h.paused.wait(TIMEOUT)                       # paused at 295 K
    assert job.resume({"temperature_k": 295.0}) is True
    assert first.wait(TIMEOUT)                          # resumed, now running
    h.paused.clear()
    assert job.resume({"temperature_k": 999.0}) is False, "not paused: the stale click is dropped"
    second.set()                                        # let the generator reach 250 K
    assert h.paused.wait(TIMEOUT), "the run paused at 250 K instead of running through it"
    assert job.resume({"temperature_k": 250.0}) is True
    h.finish()
    assert log == [("resumed", {"temperature_k": 295.0}), ("resumed", {"temperature_k": 250.0})], (
        "the 999 K stale click never reached a pause")


# -- the queue ------------------------------------------------------------------
def test_cancel_removes_a_queued_job_and_only_a_queued_job():
    log = []
    a = Job("a", "run", counting(2, log, "a"))
    b = Job("b", "readback", counting(1, log, "b"))
    c = Job("c", "action", counting(1, log, "c"))
    h = Harness()
    assert [h.worker.submit(j) for j in (a, b, c)] == [0, 1, 2]
    assert h.worker.cancel("b") is True
    assert h.worker.cancel("b") is False and h.worker.cancel("nope") is False
    assert [j.id for j in h.worker.queued] == ["a", "c"]
    assert b.state == "cancelled" and b.finished_at is not None
    assert h.states == [("b", "cancelled", "removed from the queue")]
    with pytest.raises(ValueError, match="already queued"):
        h.worker.submit(Job("a", "run", counting(1, log, "dup")))

    h.worker.start()
    h.finish()
    assert [t for t, i in log if i != "finally"] == ["a", "a", "c"], "b never ran"
    assert h.states_of("b") == ["cancelled"], "cancelled is not followed by parked"
    assert a.state == c.state == "done"


def test_shutdown_aborts_the_current_job_cancels_the_rest_and_joins():
    log, polled = [], threading.Event()
    stuck = Job("a", "run", pausing(log, polled))
    later = Job("b", "run", counting(1, log, "b"))
    h = Harness()
    h.start(stuck, later)
    assert h.paused.wait(TIMEOUT) and polled.wait(TIMEOUT)
    assert h.worker.shutdown(TIMEOUT) is True
    assert stuck.state == "aborted" and ("finally",) in log
    assert later.state == "cancelled" and ("b", 0) not in log
    assert h.states_of("a")[-2:] == ["aborted", "parked"]
    with pytest.raises(RuntimeError, match="shutting down"):
        h.worker.submit(Job("c", "run", counting(1, log, "c")))
