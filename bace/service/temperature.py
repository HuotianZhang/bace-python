"""The temperature settle: one node, two paths, one helper.

`settle` is what a temperature node does -- the executor's `loop-enter` of a
temperature loop and the `temperature` module both call it, so the two
cannot drift apart in what "reached" means or in which events a pause
sends. It is a generator, like everything the worker pumps, and it hands its
result back through the generator's return value (`Settled`) so the caller
learns the temperature the subtree is measured at without a second channel.

**Two paths, one API.** When `Rig.temperature` holds a controller (the 331
console named in `rig.toml` and answering at start-up), the node settles on
its own: the console is read first -- a setpoint is written to an
instrument that answers, never into a console with nothing behind it -- then
the setpoint is written, the console is polled every `TEMPERATURE_POLL_S`,
each usable reading goes out as a `TemperatureRead`, and the node is done
once the reading has stayed inside `tolerance_k` of the setpoint -- with no
ramp still walking the setpoint (RAMPST?) -- for `hold_s`. That is the dwell
of the design pack (01-modules, docs/ui-rules.md): the hold starts when the band is entered, not when
the setpoint is written. When there is no controller the node pauses exactly
as it did before the console was wired: `NeedsOperator`, the worker blocks in
`Job.wait_for_operator`, the resume's `temperature_k` becomes the subtree's
temperature, `hold_s` is slept after the resume. The plan
(docs/history/service-plan.md, P3) promised that wiring the 331 would automate this
node with the API unchanged, and it is unchanged: no new route, no new tree
field, no new event type. The pause is still there, as the fallback and as
the path every failure takes.

**The clock.** A settle counts time as polls times `TEMPERATURE_POLL_S`, and
sleeps between polls through `RunContext.sleep` -- the same clock, so under
`--fast` (a no-op sleep) a settle takes as many polls as the simulator needs
and no wall time, and a test can count them. The first poll is the one
before the write, at zero on that clock; the timeout is measured on it too.
The operator's wait is wall time, as before: it is how long the person took,
and the ETA of the temperatures still to come is re-derived from it.

**What is never done here.** The setpoint ceiling is the console's
(`ls331/config.py`, 350 K): a refusal is passed through as a `crit`
verdict and a pause, never clamped or retried at 350 K. The heater range,
the ramp rate and the PID are the console's too -- there is no route for
them and this helper asks for none; a heater range of 0 while the setpoint
is above the reading is *said* (a `warn`), not fixed. A node that timed
out, or whose instrument went silent, or whose console stopped answering,
does **not** proceed to measure at a temperature it did not reach: it
pauses, and the operator decides -- resume accepting the reading
(optionally typing the temperature), or stop. A stop or an abort is
honoured at every poll, so the abort button is not dead for the length of a
settle.

**A refusal is not an outage.** The driver tells the two apart
(`TemperatureError.refused`), and so does this helper: a setpoint above the
ceiling is `temperature.refused` (crit, the controller's own sentence); a
write that went unanswered is read back once -- a slow bus can apply a
setpoint after the client gave up waiting -- and either settles as if the
write had answered (with a notice) or pauses as `temperature.timeout` with
`reason = "unreachable"`, which tells the operator to fix the connection,
not to reconsider the setpoint.

Both controllers raise the same `TemperatureError`, so this helper does not
know or care which owns the instrument: `controller.DirectTemperatureController`
on this process's GPIB session (the default) or `ConsoleTemperatureController`
over HTTP when the 331 console holds the bus.

Every temperature node is marked in the controller's audit trail at the
setpoint write and at the settle, best effort: a log mark must never
stop a run.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterator

from ..experiment import events as E
from ..experiment.rig import Rig
from .pipeline import TEMPERATURE_DEFAULTS, fmt_duration
from .worker import AbortNow, Job, StopMode

if TYPE_CHECKING:                              # modules imports this file
    from .modules import RunContext

TEMPERATURE_POLL_S = 5.0
"""How often the 331 is read while a node settles or a paused run waits
(contract section 7). The console is HTTP, not VISA, so reading it beside
the worker breaks no rule; five seconds is what a person watching a
cryostat settle needs, the console's own slow-poll interval, and nothing
the console minds. A settle's clock is counted in units of this."""

OPERATOR_POLL_S = 0.2
"""How often `Job.wait_for_operator` wakes to look at the stop flags."""

MAX_POLLED_READS = 1000
"""Temperature readings kept during one operator wait when there is no
`emit` hook: at one every five seconds that is well over an hour of
settling, and a wait longer than that would be several thousand events on
the stream at the moment of resume."""

SILENT_READS = 3
"""Consecutive unusable readings -- `connected` False (the console up, the
instrument behind it not answering) or the console itself not answering --
before the node gives up waiting and asks the operator; the console's own
watchdog cuts the heater after the same count of bad polls. Before the
setpoint is written the same count decides that there is nothing to write
to."""

SETPOINT_MATCH_K = 1e-3
"""How close the setpoint the console reads back must be to the one asked
for to count as the same setpoint, when a write did not answer and the
state is read back to see whether it landed."""


def _float_or_none(v: Any) -> float | None:
    try:
        f = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    return f if f is None or math.isfinite(f) else None


def source_of(controller: Any) -> str:
    """The `source` every `TemperatureRead`, settle verdict and
    `RunMetadata.temperature_source` carries, so a file written under `--sim`
    says so.

    Delegates to `rigs.temperature_source` rather than deciding again. This
    was two functions until 2026-09-03 and they disagreed the moment the
    direct driver arrived: the bench read-back said `instrument` and the
    settle path -- the one that writes the *file* -- still tested only
    `base_url` and fell through to `simulated`, stamping every real 331
    reading as made up. One classifier, so they cannot drift apart again.
    """
    from .rigs import temperature_source
    return temperature_source(controller)


def _note(controller: Any, text: str) -> bool:
    """A line into the console's audit log, best effort: a driver that
    raised, or one that answered False, is a mark not made and nothing
    more."""
    try:
        return bool(controller.note(text))
    except Exception:                                   # noqa: BLE001
        return False


def _usable(reading: Any) -> bool:
    """A reading the settle can judge by: the instrument answered and the
    number is a number. A console that holds no reading yet reports NaN."""
    return (reading is not None and bool(getattr(reading, "connected", False))
            and _float_or_none(getattr(reading, "kelvin", None)) is not None)


@dataclass
class Settled:
    """What the settle came to.

    `how` is `settled` (the controller reached the band), `operator` (a
    person resumed the pause -- the only path without a controller, and the
    fallback after a refusal, a timeout, a silent instrument or a console
    that stopped answering) or `stopped` (an `after_shot` arrived; the
    caller closes the tree). `temperature_k` is what the subtree is measured
    at: the last reading on the automatic path, the operator's typed number
    on the pause path, the last reading when they typed none, None when
    nothing was read at all -- the setpoint then stands, and the executor
    records it as `how = "setpoint"`, because nothing confirmed it). `how`
    and `source` are not only for the card: they are copied onto the context
    and from there into `RunMetadata.temperature_how`/`temperature_source`,
    so a stored run says whether its temperature was measured, merely asked
    for, or typed by a person. `settle_s` is what
    the ETA learns: polls times the poll interval, plus the wall time a
    person took.
    """

    how: str
    temperature_k: float | None
    settle_s: float
    source: str | None
    polls: int = 0
    answer: dict = field(default_factory=dict)

    @property
    def stopped(self) -> bool:
        return self.how == "stopped"


@dataclass
class _Polls:
    """The settle's bookkeeping: how many reads on the poll clock, how many
    consecutive misses and of which kind (`silent` -- the console answered
    with `connected` False or no number; `unreachable` -- the read itself
    raised), the last usable reading and the last error text."""

    polls: int = 0
    elapsed: float = 0.0
    misses: int = 0
    kind: str | None = None
    error: str | None = None
    last: Any = None
    reading: Any = None

    def take(self, controller: Any) -> bool:
        """One read on the poll clock; True when it is usable."""
        self.polls += 1
        try:
            self.reading = controller.read()
        except Exception as exc:                        # noqa: BLE001 -- counted as a miss
            self.reading = None
            self.misses += 1
            self.kind = "unreachable"
            self.error = str(exc)
            return False
        if not _usable(self.reading):
            self.misses += 1
            self.kind = "silent"
            self.error = str(getattr(self.reading, "status_text", "") or "") or None
            return False
        self.misses = 0
        self.kind = None
        self.error = None
        self.last = self.reading
        return True

    @property
    def last_k(self) -> float | None:
        return None if self.last is None else float(self.last.kelvin)


def _stop_requested(job: Job | None, ctx: "RunContext") -> bool:
    """`after_shot` -> True, so the caller closes the tree as it does between
    nodes; `abort` -> `AbortNow`, so the generator unwinds through its
    `finally` blocks and the worker reports `aborted`, exactly as an abort
    that lands in `Job.wait_for_operator` does. Without a job the context's
    `stop_mode` says which (the executor fills it from the job for the
    module path); with neither, the context's flag cannot tell the two
    apart and is treated as a stop."""
    if job is not None:
        mode = job.stop_mode
    elif getattr(ctx, "stop_mode", None) is not None:
        mode = ctx.stop_mode()
    else:
        return bool(ctx.abort())
    if mode == StopMode.ABORT:
        raise AbortNow(f"abort requested while {ctx.node_path} settled the temperature")
    return mode is not None


def _landed(controller: Any, setpoint: float) -> bool:
    """After a setpoint write the console did not answer: does its state
    show the setpoint in force? One read, and no when it cannot be read."""
    try:
        reading = controller.read()
    except Exception:                                   # noqa: BLE001
        return False
    confirmed = _float_or_none(getattr(reading, "setpoint_k", None))
    return confirmed is not None and abs(confirmed - setpoint) <= SETPOINT_MATCH_K


def settle(rig: Rig, ctx: "RunContext", detail: dict, *, node_path: str,
           emit: Callable[[E.Event], None] | None = None,
           job: Job | None = None) -> Iterator[E.Event]:
    """Bring the cryostat to `detail["setpoint_k"]` and hold it, or pause
    for the operator; returns a `Settled` through the generator.

    `detail` is the node's `{setpoint_k, tolerance_k, hold_s, timeout_s}`
    (the keys it leaves out take `pipeline.TEMPERATURE_DEFAULTS`, the same
    values the tree fills in) plus whatever the caller wants the pause to
    carry (`index`, `count`). `emit` delivers a reading out of band while the
    generator is blocked in a wait (the session binds it to its event path;
    the context's own `emit` is used when none is given, which is how the
    module path gets it); `job` is the worker's job, whose stop flags are
    read at every poll and whose `wait_for_operator` a pause blocks in.
    Without a job the pause uses `ctx.wait_for_operator`, and a stop is
    `ctx.stop_mode()` (or `ctx.abort()`).
    """
    setpoint = float(detail["setpoint_k"])
    tolerance = float(detail.get("tolerance_k", TEMPERATURE_DEFAULTS["tolerance_k"]))
    hold_s = float(detail.get("hold_s", TEMPERATURE_DEFAULTS["hold_s"]))
    timeout_s = float(detail.get("timeout_s", TEMPERATURE_DEFAULTS["timeout_s"]))
    if emit is None:
        emit = getattr(ctx, "emit", None)
    controller = rig.temperature
    if controller is None:
        return (yield from _pause(rig, ctx, detail, node_path=node_path, emit=emit, job=job,
                                  what="temperature"))

    source = source_of(controller)
    console = getattr(controller, "base_url", None)
    p = _Polls()
    written = False
    heater_said = False
    steady_polls = 0
    reached_s = 0.0
    why: str | None = None
    while True:
        if _stop_requested(job, ctx):
            return Settled(how="stopped", temperature_k=None, settle_s=p.elapsed,
                           source=source, polls=p.polls)
        if not p.take(controller):
            steady_polls = 0
            if p.misses >= SILENT_READS:
                why = p.kind
                break
        else:
            reading = p.last
            in_band = abs(reading.kelvin - setpoint) <= tolerance
            yield E.TemperatureRead(kelvin=float(reading.kelvin), setpoint_k=setpoint,
                                    in_band=in_band, source=source)
            if not written:
                # The first usable reading proves there is an instrument to
                # write to; now the setpoint goes to the console -- once.
                _note(controller, f"bace {ctx.run_id} {node_path}: setpoint {setpoint:g} K "
                                  f"+/-{tolerance:g} K hold {hold_s:g} s")
                try:
                    controller.set_setpoint(setpoint)
                except Exception as exc:                # noqa: BLE001 -- the console said no, or nothing
                    if getattr(exc, "refused", False):
                        # The console's refusal, in its words: above its
                        # ceiling, or a body it would not take. Nothing is
                        # clamped or retried; the operator decides what the
                        # setpoint should have been.
                        message = str(getattr(exc, "console_message", None) or exc)
                        yield E.Verdict(level="crit", code="temperature.refused", text=message,
                                        node_path=node_path,
                                        data={"setpoint_k": setpoint, "error": str(exc),
                                              "status": getattr(exc, "status", None),
                                              "source": source, "console": console})
                        return (yield from _pause(
                            rig, ctx, detail, node_path=node_path, emit=emit, job=job,
                            what="temperature refused", extra={"error": message},
                            source=source, fallback_k=p.last_k))
                    if not _landed(controller, setpoint):
                        why = "unreachable"
                        p.error = str(exc)
                        break
                    # The write outlived the client's patience and landed
                    # anyway: the bus is slow, not gone.
                    yield E.Notice("warning",
                                   f"{node_path}: the 331 did not answer the setpoint "
                                   f"write in time but reports {setpoint:g} K in force "
                                   f"-- settling ({exc})")
                written = True
            if reading.heater_range == 0 and setpoint - reading.kelvin > tolerance \
                    and not heater_said:
                # The range is the console's to set; here it is only said,
                # at once, so the operator raises it there instead of
                # finding out at the timeout.
                heater_said = True
                yield E.Verdict(
                    level="warn", code="temperature.heater-off",
                    text=(f"{setpoint:g} K asked for with the heater range off on the 331 "
                          f"(reading {reading.kelvin:.2f} K): the setpoint is written but "
                          "nothing will drive toward it until the range is raised on the "
                          "front panel"),
                    node_path=node_path,
                    data={"setpoint_k": setpoint, "kelvin": float(reading.kelvin),
                          "heater_range": 0, "source": source, "console": console})
            if in_band and not reading.ramping:
                if steady_polls == 0:
                    reached_s = p.elapsed
                steady_polls += 1
                # The dwell: the first steady poll enters the band, every
                # further one adds a poll interval of hold.
                if (steady_polls - 1) * TEMPERATURE_POLL_S >= hold_s:
                    break
            else:
                steady_polls = 0
        if steady_polls == 0 and written and p.elapsed >= timeout_s:
            why = "timeout"
            break
        ctx.sleep(TEMPERATURE_POLL_S)
        p.elapsed += TEMPERATURE_POLL_S

    if why is None:
        kelvin = float(p.last.kelvin)
        held = (steady_polls - 1) * TEMPERATURE_POLL_S
        yield E.Verdict(
            level="ok", code="temperature.settled",
            text=f"{setpoint:g} K reached in {fmt_duration(reached_s)}, held {fmt_duration(held)}",
            node_path=node_path,
            data={"setpoint_k": setpoint, "kelvin": kelvin, "settle_s": reached_s,
                  "hold_s": hold_s, "held_s": held, "polls": p.polls, "source": source,
                  "tolerance_k": tolerance})
        # What was measured, not what was asked for: the folder names and
        # the metadata of everything below this node carry this number.
        ctx.temperature_k = kelvin
        # `source` distinguishes the console from the stand-in, so a file
        # written under `--sim` cannot be mistaken for a measured one.
        ctx.temperature_how, ctx.temperature_source = "settled", source
        _note(controller, f"bace {ctx.run_id} {node_path}: settled at {kelvin:.3f} K")
        return Settled(how="settled", temperature_k=kelvin, settle_s=reached_s,
                       source=source, polls=p.polls)

    last_k = p.last_k
    last_text = f"; last reading {last_k:.2f} K" if last_k is not None else ""
    stage = "" if written else " before the setpoint was written"
    if why == "silent":
        text = (f"{setpoint:g} K: the 331 console answered {p.misses} polls with no "
                f"instrument behind it{stage}" + (f" ({p.error})" if p.error else "")
                + last_text + " -- pausing for the operator")
    elif why == "unreachable":
        n = f"{p.misses} polls{stage}" if p.misses else "the setpoint write"
        text = (f"{setpoint:g} K: the 331 did not answer {n}"
                + (f" ({p.error})" if p.error else "") + last_text
                + " -- pausing for the operator; fix the connection, then resume "
                  "or stop")
    else:
        text = (f"{setpoint:g} K not reached in {fmt_duration(p.elapsed)}: "
                + (f"last reading {last_k:.2f} K" if last_k is not None else "no reading")
                + f", band +/-{tolerance:g} K -- pausing for the operator")
    yield E.Verdict(level="warn", code="temperature.timeout", text=text, node_path=node_path,
                    data={"setpoint_k": setpoint, "tolerance_k": tolerance, "kelvin": last_k,
                          "elapsed_s": p.elapsed, "timeout_s": timeout_s, "polls": p.polls,
                          "source": source, "reason": why, "written": written,
                          "error": p.error, "console": console})
    out = yield from _pause(rig, ctx, detail, node_path=node_path, emit=emit, job=job,
                            what="temperature timeout",
                            extra={"kelvin": last_k, "elapsed_s": p.elapsed, "reason": why,
                                   "written": written, "error": p.error},
                            source=source, fallback_k=last_k)
    out.settle_s += p.elapsed
    out.polls += p.polls
    return out


def _pause(rig: Rig, ctx: "RunContext", detail: dict, *, node_path: str,
           emit: Callable[[E.Event], None] | None, job: Job | None, what: str,
           extra: dict | None = None, source: str | None = None,
           fallback_k: float | None = None) -> Iterator[E.Event]:
    """The operator path: `NeedsOperator`, the wait, `OperatorResumed`, the
    hold. `what` is `temperature` for a node with no controller and names
    the reason otherwise (`temperature timeout`, `temperature refused`), so
    the card can say why it is asking. While the wait lasts a controller,
    if there is one, is polled and each reading handed to `emit` -- the
    reading is for the person watching the cryostat, and one delivered
    after they decided it settled is history -- or, without a hook, yielded
    after the resume. `fallback_k` is the temperature the subtree takes when
    the operator types none: the last reading, when there was one.
    """
    setpoint = float(detail["setpoint_k"])
    tolerance = float(detail.get("tolerance_k", TEMPERATURE_DEFAULTS["tolerance_k"]))
    hold_s = float(detail.get("hold_s", TEMPERATURE_DEFAULTS["hold_s"]))
    controller = rig.temperature
    yield E.NeedsOperator(what=what, node_path=node_path,
                          detail={**dict(detail), **dict(extra or {})})
    paused_at = time.monotonic()
    answer, reads, polled_k = _wait(ctx, detail, job=job, emit=emit, controller=controller,
                                    source=source)
    for read in reads:
        yield read
    if answer.get("stopped"):
        return Settled(how="stopped", temperature_k=None, settle_s=0.0, source=source,
                       answer=answer)
    # What the operator actually took: the ETA of every temperature still
    # to come is re-derived from it.
    settle_s = time.monotonic() - paused_at
    yield E.OperatorResumed(node_path=node_path, note=str(answer.get("note", "")),
                            detail=answer)
    typed = _float_or_none(answer.get("temperature_k"))
    if typed is not None:
        # The operator's number, not the setpoint, is what the subtree is
        # measured at: it goes into every folder name and metadata below
        # this node, and onto the stream as a reading.
        yield E.TemperatureRead(kelvin=typed, setpoint_k=setpoint,
                                in_band=abs(typed - setpoint) <= tolerance,
                                source="operator")
    temperature_k = typed if typed is not None else (
        polled_k if polled_k is not None else fallback_k)
    if temperature_k is not None:
        ctx.temperature_k = temperature_k
        # `how` is `operator` either way -- a person decided this node was
        # settled. `source` is what produced the number: their keyboard, or
        # the console they were watching while they decided.
        ctx.temperature_how = "operator"
        ctx.temperature_source = "operator" if typed is not None else (source or "")
    ctx.sleep(hold_s)
    if controller is not None:
        _note(controller, f"bace {ctx.run_id} {node_path}: operator resumed"
                          + (f" at {temperature_k:.3f} K" if temperature_k is not None else "")
                          + (f" ({answer['note']})" if answer.get("note") else ""))
    return Settled(how="operator", temperature_k=temperature_k, settle_s=settle_s,
                   source="operator" if typed is not None else source, answer=answer)


def _wait(ctx: "RunContext", detail: dict, *, job: Job | None,
          emit: Callable[[E.Event], None] | None, controller: Any,
          source: str | None) -> tuple[dict, list[E.Event], float | None]:
    """Block until the operator resumes (or stops). Returns the resume
    detail, the readings taken meanwhile when there is no `emit` hook (with
    the hook each went out as it was taken and the list is empty), and the
    last kelvin polled. The context's hook is called as
    `wait_for_operator(detail, on_poll=poll)` so the module path polls the
    console while it waits exactly as the loop path does through the job."""
    reads: list[E.Event] = []
    last_t = [float("-inf")]
    last_k: list[float | None] = [None]
    setpoint = float(detail["setpoint_k"])
    tolerance = float(detail.get("tolerance_k", TEMPERATURE_DEFAULTS["tolerance_k"]))

    def poll() -> None:
        if controller is None:
            return
        now = time.monotonic()
        if now - last_t[0] < TEMPERATURE_POLL_S:
            return
        last_t[0] = now
        try:
            reading = controller.read()
        except Exception:                               # noqa: BLE001 -- the next poll may answer
            return
        if not _usable(reading):
            return
        last_k[0] = float(reading.kelvin)
        read = E.TemperatureRead(kelvin=float(reading.kelvin), setpoint_k=setpoint,
                                 in_band=abs(float(reading.kelvin) - setpoint) <= tolerance,
                                 source=source or source_of(controller))
        if emit is not None:
            emit(read)
            return
        if len(reads) >= MAX_POLLED_READS:
            del reads[0]
        reads.append(read)

    if job is not None:
        answer = job.wait_for_operator(poll_s=OPERATOR_POLL_S, on_poll=poll)
    elif ctx.wait_for_operator is not None:
        answer = ctx.wait_for_operator(dict(detail), on_poll=poll)
    else:
        raise RuntimeError(
            f"{ctx.node_path}: a temperature node waits for the operator, and this run "
            "has neither a job nor a wait_for_operator hook to wait with")
    return dict(answer or {}), reads, last_k[0]


__all__ = ["MAX_POLLED_READS", "OPERATOR_POLL_S", "SETPOINT_MATCH_K", "SILENT_READS",
           "TEMPERATURE_POLL_S", "Settled", "settle", "source_of"]
