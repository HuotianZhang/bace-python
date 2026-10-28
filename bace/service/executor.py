"""Executes a resolved schedule as one worker job and applies its bindings.

`run_pipeline` is a synchronous generator. It walks the flat schedule from
`pipeline.resolve`; tree structure and execution order are resolved there,
so execution follows the Dry run. Loop steps emit `NodeStarted`, `Progress`
and `NodeDone`. Module steps call `catalogue.build` and forward its events
unchanged (docs/service-contract.md section 7).

Three bindings connect resolved parameters to instruments:

1. An illumination loop owns `led_v` and `led_low_v` in every descendant's
   `RunContext`. Module builders drive the 33220A at those levels:
   `jv_bace` measures a light curve and `bace` pulses. The loop performs no
   instrument writes, so an empty loop cannot leave the LED on.
2. A light `JVCurveDone` supplies a `VocSource` keyed by LED level. The
   schedule identifies the provider in `Step.detail["voc"]`; execution
   supplies its measured value when the receiving `bace` level matches
   within 1e-9 V. The builder centres V_pre on that V_oc. `on_voc` publishes
   each source to the session. Missing or mismatched sources are refused
   before instrument writes, preventing plausible data centred on V_oc
   from a different illumination.
3. Module builders run J-V inside `router.dc` and `bace` inside
   `router.transient`. The router enforces the relay interlock. Scheduled
   transitions emit a `Notice`, making each relay boundary visible.

`service.temperature.settle` serves temperature loops and modules. With a
controller it writes the setpoint, polls through `RunContext.sleep`, emits
readings and requires `hold_s` inside the band. Refusal, timeout or silence
falls back to `NeedsOperator`. Without a controller the worker waits in
`Job.wait_for_operator`. The `emit` hook delivers readings during the pause,
when the operator needs them. Without a hook, readings are buffered and
yielded after resume. The last reading or operator's `temperature_k`
labels descendant folders and metadata. `temperature_how` and
`temperature_source` distinguish a settled reading, unconfirmed setpoint
and typed value even when the folder names match. Settle time feeds ETA.

A temperature module binds the remaining run, including later iterations
of enclosing loops. Its result updates the root and every open loop, so
copied bindings cannot shadow it. A temperature loop owns its subtree and
writes its setpoint at each iteration.

`after_shot` is checked before each node. The active module reports
`RunAborted(reason="requested")`; if it returns early without that event,
the executor supplies it. Open loops close with `NodeDone(outcome="stopped")`.
A measurement that reports `RunFinished` or `JVFinished` is complete even
if stop is requested during its last shot; stop applies at the next node.
`abort` propagates as `GeneratorExit` at a yield or `AbortNow` during a
wait. The `finally` parks the rig and the worker reports the terminal state.
A module exception yields failed `NodeDone` events for the module and open
loops, then `RunFailed` with the node path. It is re-raised so the worker
records the error and ends `failed`.

Each loop `Progress.eta_s` sums the remaining schedule. Completed modules
supply median durations by module within this run; before one completes,
the schedule's estimate applies. Temperature nodes supply the median
settle time in this run, falling back to the journal estimate or no settle
estimate. A missing settle estimate makes ETA a lower bound. An unusually
long first settle therefore changes the next ETA and the record's
`finish_at`, rather than leaving the journal estimate in place.
All sleeps use `RunContext.sleep` or the job's operator wait.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from ..experiment import events as E
from ..experiment.jv import JVCurveDone, JVFinished, JVStarted
from ..experiment.rig import Rig
from .journal import finished_text
from .modules import RunContext, VocSource
from .pipeline import TEMPERATURE_DEFAULTS, Schedule, Step
from .temperature import TEMPERATURE_POLL_S, settle  # noqa: F401 -- the poll clock, re-exported
from .worker import AbortNow, Job

LED_MATCH_V = 1e-9
"""Defines the LED-level matching tolerance in V.
It matches `core.illumination.assert_axis_centre`.
"""

_RELAY_SIDE = {"dc": "sourcemeter", "transient": "amplifier"}
"""Maps `ModuleSpec.relay` values to bench read-back position names.
Transition notices therefore use the bench card's vocabulary.
"""


def _never() -> bool:
    return False


def _float_or_none(v: Any) -> float | None:
    try:
        f = None if v is None else float(v)
    except (TypeError, ValueError):
        return None
    return f if f is None or math.isfinite(f) else None


def _leaf(node_path: str) -> str:
    return node_path.rsplit("/", 1)[-1]


@dataclass
class _Open:
    """Carries an open loop iteration's bindings and `NodeDone` data."""

    node_path: str
    loop: str
    index: int
    count: int
    started_at: float
    led_v: float | None = None
    led_low_v: float | None = None
    temperature_k: float | None = None
    temperature_how: str = ""
    temperature_source: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class _Tally:
    """Reduces module events to counts, folders and a summary for `NodeDone`."""

    module: str
    kept: int | None = None
    requested: int | None = None
    summary: str = ""
    aborted: E.RunAborted | None = None
    finished: bool = False
    """Records whether `RunFinished` or `JVFinished` confirms completion.
    A stop during the final shot leaves a completed measurement `ok` and
    acts at the next node. A utility has no completion event and counts as
    complete if it ends without `RunAborted`
    (docs/service-contract.md section 7).
    """
    last_voc: float | None = None
    curves: int = 0

    def handle(self, ev: E.Event) -> None:
        if isinstance(ev, E.RunStarted):
            self.requested, self.kept = ev.n_shots, 0
        elif isinstance(ev, E.StepDone):
            self.kept = (self.kept or 0) + 1
        elif isinstance(ev, E.RunFinished):
            self.kept = self.requested
            self.finished = True
            self.summary = finished_text({"q_mean": ev.q_mean, "q_std": ev.q_std, "q_all": ev.q_all},
                                         self.kept, self.requested)
        elif isinstance(ev, E.RunAborted):
            self.aborted = ev
            self.kept, self.requested = ev.done, ev.total
            verb = "stopped after" if ev.reason == "requested" else "aborted at"
            self.summary = f"{verb} {ev.done}/{ev.total}"
        elif isinstance(ev, JVStarted):
            self.requested, self.kept = ev.n_curves, 0
        elif isinstance(ev, JVCurveDone):
            self.kept = (self.kept or 0) + 1
            # `is False` excludes unknown illumination. An unknown curve's
            # apparent V_oc can be a dark curve's noise crossing; it must not
            # become the run's advertised result.
            if ev.dark is False and ev.metrics.voc is not None:
                self.last_voc = float(ev.metrics.voc)
        elif isinstance(ev, JVFinished):
            n = len(ev.curves)
            self.finished = True
            self.summary = f"{n} curve" + ("" if n == 1 else "s")
            if self.last_voc is not None:
                self.summary += f" · V_oc {self.last_voc:.3f} V"
        elif isinstance(ev, E.DCMeasured):
            self.summary = f"V_oc {ev.dc.voc:.3f} V (DC)"
        elif isinstance(ev, E.PowerReading):
            self.summary = f"{ev.watts:.3e} W"
        elif isinstance(ev, E.TemperatureRead):
            self.summary = f"{ev.kelvin:g} K"
        elif isinstance(ev, E.Notice) and not self.summary:
            self.summary = ev.text


class _Executor:
    def __init__(self, rig: Rig, schedule: Schedule, *, catalogue: Any,
                 ctx_factory: Callable[[Step], RunContext], job: Job | None,
                 on_voc: Callable[[VocSource], None] | None,
                 abort: Callable[[], bool] | None,
                 session_voc: VocSource | None,
                 emit: Callable[[E.Event], None] | None):
        self.rig = rig
        self.schedule = schedule
        self.catalogue = catalogue
        self.ctx_factory = ctx_factory
        self.job = job
        self.on_voc = on_voc
        self.session_voc = session_voc
        self.emit = emit
        self.abort = abort if abort is not None else (job.abort_check if job else _never)
        self.root = _Open(node_path="", loop="", index=0, count=0, started_at=0.0)
        """Carries temperature-module bindings outside any enclosing loop.
        The root retains them for subsequent nodes and iterations.
        """
        self.open: list[_Open] = []
        self.providers: dict[str, list[VocSource]] = {}
        """Indexes measured `VocSource` values by provider node path.
        The schedule selects the provider; the receiving `bace` selects the
        source at its own LED level.
        """
        self.modules_total = len(schedule.modules)
        self.modules_done = 0
        self.node_path = ""
        self.t_start = time.monotonic()
        self.durations: dict[str, list[float]] = {}
        """Records completed module durations in seconds by module for ETA."""
        self.settles: list[float] = []
        """Records this run's temperature settle durations in seconds.
        They include controller polling and operator wait time.
        """

    # -- the ETA --------------------------------------------------------------
    def eta_s(self, from_index: int) -> float:
        """Estimates seconds remaining from schedule index `from_index`.

        Measured durations in this run supersede schedule estimates. Without
        any settle estimate, a temperature contributes only its hold, making
        the result a floor; the session retains `lower_bound`.
        """
        total = 0.0
        for step in self.schedule.steps[from_index:]:
            if step.kind == "module":
                seen = self.durations.get(str(step.module))
                total += (statistics.median(seen) if seen
                          else float(step.estimate_s or 0.0))
            elif step.kind == "loop-enter":
                if step.loop == "temperature":
                    settle = step.detail.get("settle_s")
                    if self.settles:
                        settle = statistics.median(self.settles)
                    total += float(settle or 0.0) + float(step.detail.get("hold_s") or 0.0)
                elif step.loop == "illumination":
                    total += float(step.detail.get("led_settle_s") or 0.0)
        return total

    # -- position -----------------------------------------------------------
    def _at(self, node_path: str) -> None:
        """Assigns `node_path` to subsequent events before yielding them.

        The worker uses `job.node_path` to stamp envelopes, so setting it after
        a yield would attribute that event to the wrong node.
        """
        self.node_path = node_path
        if self.job is not None:
            self.job.node_path = node_path

    def _elapsed(self) -> float:
        return time.monotonic() - self.t_start

    # -- the walk -----------------------------------------------------------
    def run(self) -> Iterator[E.Event]:
        try:
            for i, step in enumerate(self.schedule.steps):
                if step.kind == "loop-enter":
                    if self.abort():
                        yield from self._stop()
                        return
                    if (yield from self._enter(step, i)):
                        return
                elif step.kind == "loop-exit":
                    yield from self._exit(step, i)
                elif step.kind == "module":
                    if self.abort():
                        yield from self._stop()
                        return
                    if (yield from self._module(step)):
                        return
                else:
                    raise ValueError(f"{step.node_path}: unknown step kind {step.kind!r}")
        finally:
            # Completion, stop, abort (`GeneratorExit` or `AbortNow`) and
            # failure all park: outputs off, shutter shut. The worker's second
            # park is safe because repeating output-off commands is idempotent.
            self.rig.park()

    # -- loops ----------------------------------------------------------------
    def _enter(self, step: Step, index_in_schedule: int) -> Iterator[E.Event]:
        d = step.detail
        index, count = int(d.get("index", 0)), int(d.get("count", 1))
        path = step.node_path
        self._at(path)
        yield E.NodeStarted(node_path=path, kind=str(step.loop), label=_leaf(path))
        yield E.Progress(done=index, total=count, elapsed_s=self._elapsed(),
                         eta_s=self.eta_s(index_in_schedule), node_path=path)
        node = _Open(node_path=path, loop=str(step.loop), index=index, count=count,
                     started_at=time.time(), detail={"index": index, "count": count,
                                                     "loop": step.loop, "value": step.value})
        outer = self._context()
        node.led_v, node.led_low_v = outer.led_v, outer.led_low_v
        node.temperature_k = outer.temperature_k
        node.temperature_how = outer.temperature_how
        node.temperature_source = outer.temperature_source
        if step.loop == "illumination":
            node.led_v = _float_or_none(d.get("led_v", step.value))
            node.led_low_v = _float_or_none(d.get("led_low_v"))
        self.open.append(node)
        if step.loop != "temperature":
            return False

        setpoint = float(d.get("setpoint_k", step.value))
        # `resolve` fills every key from `TEMPERATURE_DEFAULTS`. Hand-built
        # `Step` objects need those same defaults: zero would pause on the
        # first out-of-band reading.
        detail = {"setpoint_k": setpoint,
                  **{k: float(d.get(k, v)) for k, v in TEMPERATURE_DEFAULTS.items()},
                  "index": index, "count": count}
        # The setpoint labels the subtree only if settle supplies no reading.
        # `how` then records that the temperature is unconfirmed.
        node.temperature_k = setpoint
        node.temperature_how, node.temperature_source = "setpoint", ""
        ctx = self.ctx_factory(step)
        # The shared helper keeps loop and module settle criteria consistent.
        # It returns the subtree's temperature and actual settle duration.
        outcome = yield from settle(self.rig, ctx, detail, node_path=path,
                                    emit=self.emit, job=self.job)
        if outcome.stopped:
            yield from self._stop()
            return True
        # Actual settle duration updates ETA for remaining temperatures.
        self.settles.append(float(outcome.settle_s))
        if outcome.temperature_k is not None:
            # Readings or typed temperatures label descendant folders and
            # metadata. `how` and `source` retain the distinction from a
            # requested setpoint.
            node.temperature_k = float(outcome.temperature_k)
            node.temperature_how = outcome.how
            node.temperature_source = outcome.source or ""
        node.detail["temperature_k"] = node.temperature_k
        node.detail["temperature_how"] = node.temperature_how
        node.detail["temperature_source"] = node.temperature_source
        return False

    def _exit(self, step: Step, index_in_schedule: int) -> Iterator[E.Event]:
        path = step.node_path
        if not self.open or self.open[-1].node_path != path:
            raise ValueError(f"{path}: loop-exit does not match the open loop "
                             f"{self.open[-1].node_path if self.open else '(none)'}; "
                             "the schedule is not the one resolve() produces")
        node = self.open.pop()
        self._at(path)
        yield E.Progress(done=node.index + 1, total=node.count, elapsed_s=self._elapsed(),
                         eta_s=self.eta_s(index_in_schedule + 1), node_path=path)
        yield E.NodeDone(node_path=path, outcome="ok",
                         detail={**node.detail, "elapsed_s": time.time() - node.started_at})

    def _context(self) -> _Open:
        """Returns the innermost illumination and temperature bindings for a node."""
        out = _Open(node_path="", loop="", index=0, count=0, started_at=0.0)
        for node in (self.root, *self.open):
            if node.led_v is not None:
                out.led_v, out.led_low_v = node.led_v, node.led_low_v
            if node.temperature_k is not None:
                out.temperature_k = node.temperature_k
                out.temperature_how = node.temperature_how
                out.temperature_source = node.temperature_source
        return out

    # -- modules ------------------------------------------------------------
    def _module(self, step: Step) -> Iterator[E.Event]:
        path, name = step.node_path, str(step.module)
        d = step.detail
        self._at(path)
        yield E.NodeStarted(node_path=path, kind=name,
                            label=str(d.get("label") or d.get("title") or name))
        if d.get("relay_transition") and d.get("relay_from") and step.relay:
            yield E.Notice("info", f"relay {_RELAY_SIDE.get(d['relay_from'], d['relay_from'])} "
                                   f"-> {_RELAY_SIDE.get(step.relay, step.relay)} for {name}")

        tally = _Tally(module=name)
        ctx = self.ctx_factory(step)
        ctx.node_path = path
        self._hooks(ctx)
        values = step.values()
        given = self._bind(ctx, step, values)
        # An unchanged temperature can have different provenance. Comparing
        # the whole triple preserves that change in the record.
        bound_temperature = (ctx.temperature_k, ctx.temperature_how, ctx.temperature_source)
        started = time.time()
        gen = None
        try:
            gen = self.catalogue.build(name, values, ctx, self.rig)
            for ev in gen:
                tally.handle(ev)
                refused = self._capture(ev, step, ctx)
                yield ev
                if refused is not None:
                    yield E.Notice("warning", refused)
        except AbortNow:
            # Abort during a module's operator wait propagates to the worker.
            # It unwinds without marking the node failed; the worker reports
            # `aborted`.
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            yield E.NodeDone(node_path=path, outcome="failed",
                             detail=self._node_detail(tally, ctx, started, error=error,
                                                      values=values))
            yield from self._close_open("failed", error)
            yield E.RunFailed(error=error, where=path)
            raise
        finally:
            if gen is not None:
                # Explicit close runs the inner generator's `finally` during
                # abort: bias off, shutter shut, recorder flushed. Safety cannot
                # depend on garbage-collection timing.
                gen.close()

        if ctx.voc is not None and ctx.voc is not given and self.on_voc is not None:
            # `measure_dc` supplies the module's V_oc to the session rail,
            # just as a `jv_bace` light curve does.
            self.on_voc(ctx.voc)
        if ctx.temperature_k is not None and (
                ctx.temperature_k, ctx.temperature_how,
                ctx.temperature_source) != bound_temperature:
            # A temperature module binds the remaining run because the cryostat
            # retains its temperature between nodes. Update `root` for subsequent
            # iterations and every open loop whose copied value would shadow it.
            # A temperature loop still owns its subtree: `_enter` writes its
            # setpoint at each iteration.
            kelvin = float(ctx.temperature_k)
            for holder in (self.root, *self.open):
                holder.temperature_k = kelvin
                holder.temperature_how = ctx.temperature_how
                holder.temperature_source = ctx.temperature_source
                if holder is not self.root:
                    holder.detail.update(temperature_k=kelvin,
                                         temperature_how=ctx.temperature_how,
                                         temperature_source=ctx.temperature_source)
        self.modules_done += 1
        if tally.aborted is None:
            # Only complete module durations estimate another module's cost.
            # A stopped module does not measure its full duration.
            self.durations.setdefault(name, []).append(time.time() - started)
        # A measurement's completion event takes precedence over the stop
        # flag. Early `run_jv` return with a notice means stopped. A utility
        # has no completion event and ends normally without `RunAborted`.
        measurement = self._is_measurement(name)
        stopped = tally.aborted is not None or (self.abort() and measurement
                                                and not tally.finished)
        outcome = "stopped" if stopped else "ok"
        yield E.NodeDone(node_path=path, outcome=outcome,
                         detail=self._node_detail(tally, ctx, started, values=values))
        if stopped:
            yield from self._close_open("stopped", "stop requested")
            if tally.aborted is None:
                # `run_jv` can return early without `RunAborted`; the executor
                # must still report the stop.
                yield E.RunAborted(reason="requested", done=self.modules_done,
                                   total=self.modules_total)
            return True
        return False

    def _hooks(self, ctx: RunContext) -> None:
        """Supplies missing pause hooks from the job to a module context.

        Out-of-band `emit` delivers live readings. The job's stop mode lets
        the module distinguish abort from `after_shot`, matching loop settle.
        """
        if ctx.emit is None:
            ctx.emit = self.emit
        job = self.job
        if ctx.stop_mode is None and job is not None:
            ctx.stop_mode = lambda: job.stop_mode

    def _is_measurement(self, name: str) -> bool:
        try:
            return str(self.catalogue.spec(name).kind) == "measurement"
        except (KeyError, ValueError, AttributeError):
            return False

    def _bind(self, ctx: RunContext, step: Step, values: dict) -> VocSource | None:
        """Applies illumination and V_oc bindings to the module context.

        Returns the supplied V_oc source so changes can be reported.
        """
        outer = self._context()
        if outer.led_v is not None:
            ctx.led_v = outer.led_v
            if outer.led_low_v is not None:
                ctx.led_low_v = outer.led_low_v
        if outer.temperature_k is not None:
            ctx.temperature_k = outer.temperature_k
            ctx.temperature_how = outer.temperature_how
            ctx.temperature_source = outer.temperature_source

        src = step.detail.get("voc")
        ctx.voc = None
        if isinstance(src, dict):
            how = src.get("how")
            if src.get("session"):
                # A session V_oc retains its measured source. Treating it as a
                # typed number would record `how="typed"` and change the validator's
                # warning. Use `session_voc` when it matches the scheduled source;
                # otherwise reconstruct the source from the resolved schedule.
                value, led_v = _float_or_none(src.get("value")), _float_or_none(src.get("led_v"))
                sv = self.session_voc
                if (sv is not None and value is not None and led_v is not None
                        and sv.value == value and abs(sv.led_v - led_v) <= LED_MATCH_V):
                    ctx.voc = sv
                elif value is not None and led_v is not None:
                    ctx.voc = VocSource(value=value, led_v=led_v,
                                        run_id=str(src.get("run_id") or ""),
                                        node_path=str(src.get("node_path") or ""),
                                        how=str(how or "jv_bace"))
                if ctx.voc is not None and "voc" in values:
                    values["voc"] = None
            elif how not in ("measure_dc", "typed"):
                led_v = _float_or_none(src.get("led_v"))
                provider = str(src.get("node_path") or "")
                if led_v is not None:
                    ctx.voc = self._lookup(provider, led_v)
        return ctx.voc

    def _lookup(self, provider: str, led_v: float) -> VocSource | None:
        for source in reversed(self.providers.get(provider, [])):
            if abs(source.led_v - led_v) <= LED_MATCH_V:
                return source
        return None

    def _capture(self, ev: E.Event, step: Step, ctx: RunContext) -> str | None:
        """Captures a light curve's V_oc at its measured LED level.

        `JVCurveDone.dark` is three-valued. Only `dark is False` confirms
        light: falsy `None` means unknown illumination, and its crossing
        can be dark noise. Such a curve supplies no V_oc for centring `bace`;
        the operator must establish the light and repeat the measurement.

        A `jv` curve confirmed lit can supply V_oc at its read-back LED
        level, which the coupling check compares. Capture therefore follows
        the event, not `spec.provides_voc`: validation asks whether a node
        promises V_oc, whereas execution checks what it actually measures.

        Confirmed light also requires DC for an as-found curve. With
        `light(led_mode="pulse", shutter="open")`, the LED chops at 500 Hz.
        The source-meter integrates across light and dark phases. Its
        interpolated crossing is a time average rather than V_oc. Matching
        pulse-high levels cannot expose that error in the coupling check.
        `modules.Catalogue._build_bace` avoids it by using `measure_dc` at DC.

        This check applies only to as-found curves. A managed curve has
        `illumination=None` because `experiment.jv._set_illumination` sets DC itself;
        `jv_bace` also manages its own light.
        """
        if not isinstance(ev, JVCurveDone) or ev.dark is not False:
            return
        if ev.metrics.voc is None or ev.led_level_v is None:
            return
        found = ev.illumination
        if found is not None and str(found.get("led_mode") or "").upper() != "DC":
            # `run_jv` warns about unknown light but accepts this lit pulse
            # curve. An explicit notice explains why it supplies no V_oc;
            # otherwise only a later `bace` source failure would expose it.
            return ("V_oc not taken from this curve: the LED was in "
                    f"{found.get('led_mode')}, and a curve measured under a "
                    "chopped lamp gives a time average, not a V_oc. Set the "
                    "LED to DC (or use jv_bace / measure_dc) for a source.")
        source = VocSource(value=float(ev.metrics.voc), led_v=float(ev.led_level_v),
                           run_id=ctx.run_id, node_path=step.node_path,
                           how=str(step.module))
        self.providers.setdefault(step.node_path, []).append(source)
        if self.on_voc is not None:
            self.on_voc(source)

    def _node_detail(self, tally: _Tally, ctx: RunContext, started: float,
                     error: str | None = None, values: dict | None = None) -> dict:
        folders = list(ctx.folders)
        detail: dict[str, Any] = {
            "module": tally.module, "kept": tally.kept, "requested": tally.requested,
            "folder": folders[-1] if folders else None, "folders": folders,
            "summary": tally.summary, "elapsed_s": time.time() - started,
        }
        if ctx.voc is not None:
            detail["voc"] = ctx.voc.as_dict()
        # The node's LED level keys V_oc in the journal: the loop's binding,
        # or its own single-level parameter. A `jv_bace` range has no one level.
        led_v = ctx.led_v if ctx.led_v is not None else (values or {}).get("led_v")
        if led_v is not None:
            detail["led_v"] = led_v
        # `RunContext.temperature` resolves the same value, `how` and source
        # as the recorder's HDF5 metadata. It uses a node binding or the
        # session's typed temperature. Reading only `ctx.temperature_k`
        # would omit manual runs and pipelines without a temperature node.
        kelvin, how, source = ctx.temperature()
        if kelvin is not None:
            detail["temperature_k"] = kelvin
            detail["temperature_how"] = how
            detail["temperature_source"] = source
        # Baseline correction belongs to the recorded file and must accompany
        # its Q in results. `offset_correct` applies to transients; J-V nodes
        # have no such baseline correction.
        if values and "offset_correct" in values:
            detail["offset_corrected"] = bool(values.get("offset_correct"))
        if error is not None:
            detail["error"] = error
        return detail

    # -- stopping -------------------------------------------------------------
    def _stop(self) -> Iterator[E.Event]:
        """Closes open nodes and reports a stop detected between nodes."""
        yield from self._close_open("stopped", "stop requested")
        yield E.RunAborted(reason="requested", done=self.modules_done,
                           total=self.modules_total)

    def _close_open(self, outcome: str, reason: str) -> Iterator[E.Event]:
        """Emits `NodeDone` for open loops from innermost to outermost.

        An early run end must leave no open node in the tree view.
        """
        while self.open:
            node = self.open.pop()
            self._at(node.node_path)
            yield E.NodeDone(node_path=node.node_path, outcome=outcome,
                             detail={**node.detail, "reason": reason,
                                     "elapsed_s": time.time() - node.started_at})


def run_pipeline(rig: Rig, schedule: Schedule, *, catalogue: Any,
                 ctx_factory: Callable[[Step], RunContext], job: Job | None = None,
                 on_voc: Callable[[VocSource], None] | None = None,
                 abort: Callable[[], bool] | None = None,
                 session_voc: VocSource | None = None,
                 emit: Callable[[E.Event], None] | None = None) -> Iterator[E.Event]:
    """Executes `schedule` on `rig` as one worker job and yields its events.

    `ctx_factory(step)` supplies a `RunContext` with run identity, folder,
    metadata, sleep and data sink. The executor applies `led_v`,
    `led_low_v`, `temperature_k` and `voc`, keeping loop binding logic here
    (docs/service-contract.md section 7).

    `job.node_path` identifies each event. `job.abort_check` supplies the
    stop flag unless `abort` is given. `job.wait_for_operator` handles
    temperature pauses. Without a job, paused nodes need
    `RunContext.wait_for_operator`; automatic settle needs the context's
    `sleep` and `abort` only. `on_voc` receives each measured V_oc.
    `session_voc` retains the source against which a manual run resolves.
    `emit` delivers readings through the session event path while the
    generator blocks in a temperature pause.

    The generator's `finally` parks the rig on every exit path.
    """
    return _Executor(rig, schedule, catalogue=catalogue, ctx_factory=ctx_factory, job=job,
                     on_voc=on_voc, abort=abort, session_voc=session_voc, emit=emit).run()
