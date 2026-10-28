"""Settles a temperature node automatically or waits for the operator.

`settle` serves both temperature-loop entry and the `temperature` module.
One helper keeps their reached-temperature criteria and pause events
consistent. It yields events on the worker thread and returns `Settled`
through the generator's return value (docs/service-contract.md section 7).

With `Rig.temperature` attached, a usable reading must precede a setpoint
write. The controller is polled every `TEMPERATURE_POLL_S`, and each usable
reading yields `TemperatureRead`. Completion requires a reading within
`tolerance_k` of the setpoint with no active ramp for `hold_s`. The dwell
starts on band entry, not at the write. Without a controller the node emits
`NeedsOperator` and blocks in `Job.wait_for_operator`. The resume's
`temperature_k` labels the subtree, and `hold_s` follows resume. The same
pause handles every automatic-settle failure.

The settle clock counts polls times `TEMPERATURE_POLL_S`. Sleeps use
`RunContext.sleep`, so `--fast` consumes polls without wall time and tests
can count them. The first read precedes the write at poll time zero.
Timeout uses this clock too. The operator's wait uses wall time, which
updates the ETA for remaining temperature nodes.

The controller enforces its setpoint ceiling, 350 K by default
(docs/service-contract.md section 7). A refusal produces a `crit` verdict
and a pause; no value is clamped or retried at the ceiling. Heater range,
ramp rate and PID remain controller settings. Range 0 with a setpoint above
the reading produces a warning without changing the range. A timeout,
silent instrument or unreachable console pauses instead of measuring at an
unreached temperature. The operator accepts the reading, optionally types a
temperature, or stops. Every poll checks stop and abort.

`TemperatureError.refused` distinguishes refusal from an unanswered write.
A refusal produces `temperature.refused` with the controller's error text.
An unanswered write gets one read-back: a slow bus can apply the setpoint
after the client times out. If read-back confirms it, settle continues with
a notice. Otherwise `temperature.timeout` with `reason="unreachable"`
asks for a connection remedy rather than a different setpoint.

`DirectTemperatureController` on this process's GPIB session and
`ConsoleTemperatureController` over HTTP raise the same `TemperatureError`.
Both paths therefore use the same failure handling. Audit marks accompany
the setpoint write and completion on a best-effort basis. An audit failure
must never stop a run.
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
"""Defines the 331 poll interval in seconds during settle and pauses.
Five seconds matches the console's slow poll and resolves cryostat
settling without excessive traffic. Reads occur on the worker thread,
using GPIB or HTTP as configured. The settle clock counts these intervals
(docs/service-contract.md section 7).
"""

OPERATOR_POLL_S = 0.2
"""Defines the stop-flag poll interval for `Job.wait_for_operator`, in s."""

MAX_POLLED_READS = 1000
"""Limits readings buffered during a pause without an `emit` hook.
At five-second intervals the buffer spans well over an hour. Without a
bound, a long pause could release thousands of events at resume.
"""

SILENT_READS = 3
"""Limits consecutive unusable polls before an operator pause.
A poll is unusable when `connected` is false or the read fails. Three
misses match the controller watchdog's fault count. Before a setpoint
write, the same count establishes that no responsive instrument is
available (docs/service-contract.md section 7).
"""

SETPOINT_MATCH_K = 1e-3
"""Defines the setpoint read-back tolerance in K after an unanswered write.
It determines whether the requested setpoint is in force and settle can
continue without repeating the write.
"""


def _float_or_none(v: Any) -> float | None:
    try:
        f = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    return f if f is None or math.isfinite(f) else None


def source_of(controller: Any) -> str:
    """Classifies the source of temperature events, verdicts and metadata.

    `rigs.temperature_source` supplies one classification for read-back
    and stored measurements. Direct GPIB readings must say `instrument`,
    HTTP readings `console`, and simulated readings `simulated`. A real
    reading must not be labelled simulated merely because it lacks an
    HTTP URL.
    """
    from .rigs import temperature_source
    return temperature_source(controller)


def _note(controller: Any, text: str) -> bool:
    """Attempts an audit mark without affecting the run.

    An exception or false reply means only that the mark is absent.
    """
    try:
        return bool(controller.note(text))
    except Exception:                                   # noqa: BLE001
        return False


def _usable(reading: Any) -> bool:
    """Returns whether the instrument answers with a finite temperature.

    A console without a reading reports NaN.
    """
    return (reading is not None and bool(getattr(reading, "connected", False))
            and _float_or_none(getattr(reading, "kelvin", None)) is not None)


@dataclass
class Settled:
    """Carries a temperature node's outcome and provenance.

    `how` is `settled` for automatic band completion, `operator` after
    resume, or `stopped` after `after_shot`. Operator resume is the path
    without a controller and the fallback for refusal, timeout or outage.
    A stopped caller closes the tree.

    `temperature_k` is the last automatic reading, the operator's typed
    value, or the last reading if none is typed. With no reading it is
    `None`; the executor uses the setpoint and records `how="setpoint"`
    as unconfirmed. `how` and `source` propagate through the context into
    `RunMetadata.temperature_how` and `RunMetadata.temperature_source`.
    The stored run distinguishes measured, requested and typed temperatures
    (docs/service-contract.md section 7).

    `settle_s` feeds the ETA: poll count times interval plus operator
    wall time.
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
    """Tracks poll count, consecutive misses, last reading and last error.

    `silent` means `connected` is false or no number is available.
    `unreachable` means the read raises.
    """

    polls: int = 0
    elapsed: float = 0.0
    misses: int = 0
    kind: str | None = None
    error: str | None = None
    last: Any = None
    reading: Any = None

    def take(self, controller: Any) -> bool:
        """Performs one poll-clock read and returns whether it is usable."""
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
    """Returns true for `after_shot` or raises `AbortNow` for abort.

    The caller closes the tree for a stop. Abort propagates through
    `finally` blocks, and the worker reports `aborted`, as during
    `Job.wait_for_operator`. Without a job, `ctx.stop_mode` distinguishes
    the requests. Without either mode source, the context's boolean flag
    is treated as a stop.
    """
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
    """Checks whether an unanswered write leaves the requested setpoint active.

    One read-back is attempted; failure returns false.
    """
    try:
        reading = controller.read()
    except Exception:                                   # noqa: BLE001
        return False
    confirmed = _float_or_none(getattr(reading, "setpoint_k", None))
    return confirmed is not None and abs(confirmed - setpoint) <= SETPOINT_MATCH_K


def settle(rig: Rig, ctx: "RunContext", detail: dict, *, node_path: str,
           emit: Callable[[E.Event], None] | None = None,
           job: Job | None = None) -> Iterator[E.Event]:
    """Reaches and holds `detail["setpoint_k"]`, or pauses for the operator.

    The generator returns `Settled`. `detail` contains `setpoint_k`,
    `tolerance_k`, `hold_s` and `timeout_s`, plus pause data such as `index`
    and `count`. Missing keys use `pipeline.TEMPERATURE_DEFAULTS`, matching
    tree resolution (docs/service-contract.md section 7).

    `emit` delivers readings during a blocked wait through the session's
    event path. It defaults to `ctx.emit`, also supporting the module
    path. `job` supplies stop flags checked every poll and the
    `wait_for_operator` pause. Without a job, the pause uses
    `ctx.wait_for_operator`, and stop uses `ctx.stop_mode` or `ctx.abort`.
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
                # A usable reading proves an instrument is available before the
                # single setpoint write.
                _note(controller, f"bace {ctx.run_id} {node_path}: setpoint {setpoint:g} K "
                                  f"+/-{tolerance:g} K hold {hold_s:g} s")
                try:
                    controller.set_setpoint(setpoint)
                except Exception as exc:                # noqa: BLE001 -- the console said no, or nothing
                    if getattr(exc, "refused", False):
                        # A refusal retains the controller's error text. No clamp or
                        # retry bypasses its ceiling or input constraints; the operator
                        # chooses an acceptable setpoint.
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
                    # Read-back confirms application despite the client's timeout:
                    # a slow bus does not require another write.
                    yield E.Notice("warning",
                                   f"{node_path}: the 331 did not answer the setpoint "
                                   f"write in time but reports {setpoint:g} K in force "
                                   f"-- settling ({exc})")
                written = True
            if reading.heater_range == 0 and setpoint - reading.kelvin > tolerance \
                    and not heater_said:
                # Heater range belongs to the controller. An immediate warning
                # lets the operator raise it before the settle times out.
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
                # Dwell starts at the first steady in-band poll. Each subsequent
                # steady poll adds one interval.
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
        # The measured temperature labels every descendant's folders and
        # metadata; the requested setpoint does not confirm temperature.
        ctx.temperature_k = kelvin
        # `source` distinguishes instrument, console and simulator, so
        # simulated data cannot be mistaken for bench readings.
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
    """Pauses for the operator, reports resume and honours the hold.

    `what` is `temperature` without a controller, or identifies the
    failure, such as `temperature timeout` or `temperature refused`.
    During the wait, an attached controller is polled and readings go
    straight to `emit`, so the operator can decide from current readings.
    Without that hook, buffered readings are yielded after resume.
    `fallback_k` supplies the last reading when the operator types none.
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
    # Actual operator wait time updates the ETA for remaining
    # temperature nodes.
    settle_s = time.monotonic() - paused_at
    yield E.OperatorResumed(node_path=node_path, note=str(answer.get("note", "")),
                            detail=answer)
    typed = _float_or_none(answer.get("temperature_k"))
    if typed is not None:
        # The operator's temperature labels every descendant's folders
        # and metadata and is emitted as a reading.
        yield E.TemperatureRead(kelvin=typed, setpoint_k=setpoint,
                                in_band=abs(typed - setpoint) <= tolerance,
                                source="operator")
    temperature_k = typed if typed is not None else (
        polled_k if polled_k is not None else fallback_k)
    if temperature_k is not None:
        ctx.temperature_k = temperature_k
        # `how="operator"` records the decision to accept temperature.
        # `source` identifies the typed value or the controller reading.
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
    """Waits for resume or stop and returns detail, readings and last kelvin.

    With `emit`, readings arrive live and the returned list is empty.
    Without it, the list retains buffered readings. The context hook uses
    `wait_for_operator(detail, on_poll=poll)`, so module and loop pauses
    both poll the controller during the wait.
    """
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
