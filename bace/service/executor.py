"""The executor: one worker job that walks a schedule and applies the three bindings.

`run_pipeline` is a synchronous generator, run as one `Job` on the worker
thread, that takes the flat schedule `pipeline.resolve` produced and executes
it step by step: `loop-enter` and `loop-exit` steps become `NodeStarted`,
`Progress` and `NodeDone` events about the loop; `module` steps become
`catalogue.build(...)` and every event the module yields, forwarded unchanged.
It never re-derives structure from the tree -- what runs, in which order, was
decided once in `pipeline.py`, where it can be tested without a rig, and the
Dry run the operator saw is the schedule this walks.

**The three bindings** (the plan, "Pipeline 执行器 -- 唯一的新逻辑", docs/service-plan.md;
contract section 7). The pipeline resolved them between *parameters*; this
is where they meet the instruments:

1. **An illumination loop owns `led_v`.** Entering an illumination iteration
   sets `led_v`/`led_low_v` on the context every module inside it is built
   with (`RunContext.led_v`), and the module builder drives the 33220A from
   that -- `jv_bace` measures one light curve at it, `bace` pulses at it. The
   loop touches no instrument itself: the LED is set by the module that needs
   it, so a loop that ends with no module inside never leaves the LED on.
2. **`jv_bace` -> V_oc -> `bace` at the same `led_v`.** Every light
   `JVCurveDone` a providing module yields is kept as a `VocSource` keyed by
   the drive it was measured at; a later `bace` in scope gets that source as
   `RunContext.voc` when its own `led_v` matches to 1e-9 V, and the builder
   centres the prebias axis on it. The schedule says which provider serves
   which `bace` (`Step.detail["voc"]`); this file supplies the number, which
   only exists once the curve has been measured. `on_voc` is called with each
   new source so the session can update the bench's V_oc rail.
3. **A relay transition at every `jv_* <-> bace` boundary.** `run_jv` runs
   inside `router.dc()` and `bace` inside `router.transient()`, both from
   the module builder; the interlock in the router is the guard. The schedule
   marks each transition, and this file announces it with a `Notice` so the
   log shows "relay sourcemeter -> amplifier" where it happened.

The failure the bindings prevent is the one the plan names: a V_oc typed an
hour ago at another intensity, or a bace that pulsed the LED at the card's
level while the loop said another, producing a full set of plausible files
centred on the wrong number. Here the V_oc handed to a bace is always the one
measured at the drive it is about to pulse, or nothing -- and nothing is a
refusal from the builder, before an instrument is touched.

**A temperature iteration settles or pauses** (`service.temperature.settle`,
shared with the `temperature` module). With the 331 console attached
(`Rig.temperature`) the setpoint is written, the console polled every
`TEMPERATURE_POLL_S` through `RunContext.sleep`, each reading yielded as a
`TemperatureRead`, and the node is done once the reading has held inside the
band for `hold_s`; a refusal from the console, a timeout or a silent
instrument falls back to the pause. Without a controller the node pauses as
it always did: `NeedsOperator` goes out, the worker thread blocks in
`Job.wait_for_operator` (handing each console reading to the `emit` hook
*while* it waits -- the reading is for the person watching the cryostat
settle, and a reading delivered after they have decided it settled is
history), and the resume's `temperature_k` becomes the context temperature of
the subtree. Either way the temperature the subtree is measured at -- the
last reading, or the operator's number -- goes into the folder names and the
metadata of everything measured below the node -- with the `how` and `source`
that say which it was (`RunMetadata.temperature_how`/`temperature_source`; a
folder called `220K` reads the same whether the console settled there, a loop
only asked and nothing answered, or a person typed it) -- and the settle it
took feeds the ETA. Without an `emit` hook (a script, a test) the readings
taken during a pause are yielded after the resume instead. A `temperature`
*module* binds the same way for the nodes after it, and for the rest of the
run rather than the rest of one iteration: the cryostat is where it settled
and stays there until another temperature node moves it, so what it settled
at (or the operator typed) is written into the root *and* into every loop
node already open, and a `bace` that follows it -- in this iteration or the
next -- is labelled with the temperature the cryostat is at, not the
session's. A temperature loop still wins for its own subtree; each iteration
writes its setpoint again on the way in.

**Stopping.** `after_shot` is polled before every node and reported by the
module in flight (`RunAborted(reason="requested")`); the open loop nodes are
closed with `NodeDone(outcome="stopped")` so a tree view has no node left
hanging, and when the module ended without saying so (a `run_jv` that returns
early) the executor says it. `abort` arrives as `GeneratorExit` at the next
yield or as `AbortNow` from the wait, and both are left to propagate: the
`finally` parks the rig, and the worker reports the state. An exception in a
module is reported as `NodeDone(outcome="failed")` for the node and every
loop above it, then `RunFailed(where=<node path>)`, and re-raised so the job
ends `failed` with the error recorded.

**The ETA is re-derived from what this run has measured** (the plan's "ETA
随实测重算"). Every loop `Progress` carries `eta_s`: the sum of what is left
in the schedule, where a module's cost is the median duration of the same
module in this run once one has completed (the schedule's estimate until
then), and a temperature's settle is the median of the settles the operator
has taken *in this run* once one has been measured (the journal's median
from the schedule until then, or nothing). After the first temperature
settled in two hours instead of the journal's twenty minutes, the next
`Progress` says so, and the session copies it into the record's `finish_at`.

Nothing here sleeps except through `RunContext.sleep` and the job's wait.
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
from .modules import RunContext, VocSource
from .pipeline import TEMPERATURE_DEFAULTS, Schedule, Step
from .temperature import TEMPERATURE_POLL_S, settle  # noqa: F401 -- the poll clock, re-exported
from .worker import AbortNow, Job

LED_MATCH_V = 1e-9
"""Two drive levels closer than this are the same illumination -- the
tolerance `core.illumination.assert_axis_centre` uses."""

_RELAY_SIDE = {"dc": "sourcemeter", "transient": "amplifier"}
"""The relay position each `ModuleSpec.relay` needs, in the words the bench
read-back uses, so the notice reads like the bench card."""


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
    """A loop iteration that has started and not ended: what it binds for
    the nodes below it, and what its `NodeDone` needs."""

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
    """What one module's events say about it, for its `NodeDone` and for
    the session's card: counts, the folder, and a one-line summary."""

    module: str
    kept: int | None = None
    requested: int | None = None
    summary: str = ""
    aborted: E.RunAborted | None = None
    finished: bool = False
    """A measurement's own "done" (`RunFinished`/`JVFinished`) was seen. A
    stop requested during the last shot leaves the module complete, and a
    complete node is `ok`, not `stopped`; the stop then acts at the next
    node. Only a measurement can say so; a utility that ends without a
    `RunAborted` simply ended."""
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
            self.summary = _finished_text(ev, self.kept, self.requested)
        elif isinstance(ev, E.RunAborted):
            self.aborted = ev
            self.kept, self.requested = ev.done, ev.total
            verb = "stopped after" if ev.reason == "requested" else "aborted at"
            self.summary = f"{verb} {ev.done}/{ev.total}"
        elif isinstance(ev, JVStarted):
            self.requested, self.kept = ev.n_curves, 0
        elif isinstance(ev, JVCurveDone):
            self.kept = (self.kept or 0) + 1
            # `is False`, not `not`: an unknown curve is not a light one, and
            # its "V_oc" may be a dark curve's noise crossing. This number is
            # what the card advertises as the run's result.
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


def _finished_text(ev: E.RunFinished, kept: int | None, requested: int | None) -> str:
    tail = f" · {kept}/{requested}" if kept is not None and requested is not None else ""
    q_mean = [float(x) for x in ev.q_mean]
    q_std = [float(x) for x in ev.q_std]
    if len(q_mean) == 1 and math.isfinite(q_mean[0]):
        text = f"Q {q_mean[0]:.3e}"
        if q_std and math.isfinite(q_std[0]):
            text += f" ± {q_std[0]:.1e}"
        return text + " C" + tail
    return f"{len(q_mean)} point" + ("" if len(q_mean) == 1 else "s") + tail


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
        """What a `temperature` module at the top level of the tree binds for
        the nodes after it: the level a loop would have been, when there is
        no loop open to hold the number."""
        self.open: list[_Open] = []
        self.providers: dict[str, list[VocSource]] = {}
        """`VocSource`s measured so far, by the node path of the module that
        measured them. The schedule names the provider of every centred
        `bace`; the value is looked up here at the bace's own drive level."""
        self.modules_total = len(schedule.modules)
        self.modules_done = 0
        self.node_path = ""
        self.t_start = time.monotonic()
        self.durations: dict[str, list[float]] = {}
        """Seconds each completed module took, by module name, for the ETA."""
        self.settles: list[float] = []
        """Seconds each temperature node took to settle this run -- the
        console's polls, or the operator's answer."""

    # -- the ETA --------------------------------------------------------------
    def eta_s(self, from_index: int) -> float:
        """Seconds left from schedule step `from_index` on, from what this
        run has measured where it has, and the schedule's estimates where
        it has not. A temperature with no settle known from either source
        contributes its hold only, so the number is a floor until the first
        settle is measured -- the session's cost keeps `lower_bound`."""
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
        """Every event yielded from now on is about `node_path`: the worker
        stamps envelopes with `job.node_path`, so it is set before the yield,
        never after."""
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
            # On completion, on a stop, on abort (GeneratorExit / AbortNow)
            # and on failure alike: outputs off, shutter shut. The worker
            # parks once more afterwards; a second park is two "off"s.
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
        # `resolve` fills every key from TEMPERATURE_DEFAULTS; a hand-built
        # Step that leaves one out gets the same value, not a zero that
        # would pause at the first reading outside the band.
        detail = {"setpoint_k": setpoint,
                  **{k: float(d.get(k, v)) for k, v in TEMPERATURE_DEFAULTS.items()},
                  "index": index, "count": count}
        # What was asked for, until the settle says what was reached. If it
        # never does -- a timeout with not one reading -- this setpoint is what
        # the folder names carry, and `how` says it was never confirmed.
        node.temperature_k = setpoint
        node.temperature_how, node.temperature_source = "setpoint", ""
        ctx = self.ctx_factory(step)
        # The settle -- through the console, or the operator's pause -- is
        # one helper shared with the `temperature` module; what comes back
        # is the temperature the subtree is measured at and what it took.
        outcome = yield from settle(self.rig, ctx, detail, node_path=path,
                                    emit=self.emit, job=self.job)
        if outcome.stopped:
            yield from self._stop()
            return True
        # What it actually took: the ETA of every temperature still to come
        # is re-derived from it.
        self.settles.append(float(outcome.settle_s))
        if outcome.temperature_k is not None:
            # What was measured, or what the operator typed -- not the
            # setpoint: it goes into every folder name and metadata below
            # this node, and `how`/`source` travel with it so the file can
            # say which of the two it was.
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
        """What the open loops bind for a node under them: the innermost
        illumination's level, the innermost temperature's reading."""
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
        # The whole triple, not just the number: a `temperature` module that
        # settles at exactly the number already in scope still changes what
        # that number is worth, and that change must be recorded.
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
            # An abort that arrived while a module waited for the operator
            # (the `temperature` module's hook): not a failure of the node,
            # and the worker turns it into `aborted` -- so it is not reported
            # here, only unwound through.
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
                # Close the module's generator explicitly: on GeneratorExit
                # from the worker's abort, the inner generator's finally
                # (bias off, shutter shut, recorder flushed) must run now,
                # not whenever the garbage collector gets to it.
                gen.close()

        if ctx.voc is not None and ctx.voc is not given and self.on_voc is not None:
            # The module measured its own V_oc (measure_dc): the session's
            # rail should show it, as it shows a jv_bace curve's.
            self.on_voc(ctx.voc)
        if ctx.temperature_k is not None and (
                ctx.temperature_k, ctx.temperature_how,
                ctx.temperature_source) != bound_temperature:
            # A `temperature` module settled (or the operator answered it).
            # The cryostat is now there and stays there until another
            # temperature node moves it, so this binds the rest of the run,
            # not just the rest of this iteration: `root`, because the next
            # iteration of an enclosing loop builds a fresh `_Open` that
            # copies from the outer scope, and every node already open,
            # which would otherwise shadow `root` with the value it copied
            # on its way in. A temperature loop is unaffected -- its `_enter`
            # writes its own setpoint again for each iteration.
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
            # A module that ran to its end is what the next one of its kind
            # will cost; a stopped one says nothing about that.
            self.durations.setdefault(name, []).append(time.time() - started)
        # A measurement that reported its own end is complete whatever the
        # flag says now; one that went quiet under the flag (run_jv returns
        # early with a Notice) was stopped. A utility has no "finished"
        # event: ended without RunAborted is ended.
        measurement = self._is_measurement(name)
        stopped = tally.aborted is not None or (self.abort() and measurement
                                                and not tally.finished)
        outcome = "stopped" if stopped else "ok"
        yield E.NodeDone(node_path=path, outcome=outcome,
                         detail=self._node_detail(tally, ctx, started, values=values))
        if stopped:
            yield from self._close_open("stopped", "stop requested")
            if tally.aborted is None:
                # The module ended without reporting (run_jv returns early on
                # abort): the run still has to say it stopped, honestly.
                yield E.RunAborted(reason="requested", done=self.modules_done,
                                   total=self.modules_total)
            return True
        return False

    def _hooks(self, ctx: RunContext) -> None:
        """What a module that waits needs from the job, when the factory did
        not set it: the out-of-band `emit` for readings taken during a pause
        and the job's stop mode, so a `temperature` module's settle polls
        the console live and tells an abort from an after_shot exactly as a
        loop's does."""
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
        """Bindings 1 and 2 onto the context this module is built with.
        Returns the V_oc source handed in, so a change can be noticed."""
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
                # The session's V_oc, resolved into the schedule as a value.
                # Handed over as the source it is (a jv_bace measured
                # earlier this session), not as a typed number: the
                # builder would otherwise record `how = "typed"`, and the
                # validator's warning about typed values is exactly the
                # difference this preserves. The session's own object is
                # used when it is the one the schedule named; otherwise it
                # is rebuilt from what the schedule kept.
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
        """Binding 2, the measuring half: a light curve with a V_oc becomes a
        source keyed by the drive it was measured at.

        **`dark is False`, not `not dark`.** Since the `jv`/`light` split
        `JVCurveDone.dark` is three-valued, and `None` -- the bench could not
        say whether light reached the sample -- is falsy. A curve nobody
        confirmed was lit may be a dark curve, whose "V_oc" is the noise
        crossing `metrics` refuses to call one for a *known* dark curve; and
        this source is what a `bace` centres its axis on. So an unknown curve
        provides nothing, and the operator sets the light and runs again.

        A `jv` that *was* read as lit does provide one, and legitimately: the
        level is the read-back the curve was labelled with, which is the same
        number the coupling check compares. That is why the capture keys on
        the event and not on `spec.provides_voc` -- the spec answers the
        validator's question ("will this node have produced a V_oc by then?"),
        which `jv` cannot promise before it runs, and this answers the
        run's ("did one come out?").

        **And lit is not enough: an as-found curve must have been found under
        DC.** `light(led_mode="pulse", shutter="open")` leaves the lamp
        chopping at 500 Hz, and the read-back reports that honestly -- lit, at
        the pulse high level. But the Keithley integrates across the pulse's
        light and dark phases, so the crossing it interpolates is a time
        average that is nobody's V_oc. `_build_bace` already knows this: it is
        exactly why `measure_dc` switches the 33220A to DC before it measures
        one. A `bace` centred on the pulse level would then take that average
        as its axis centre, and the coupling check -- which compares drive
        levels, and would find them equal -- cannot tell.

        Only *as-found* curves are asked: `illumination` is None on a `manage`
        curve, where `_set_illumination` set the LED to DC itself, and on
        `jv_bace`, whose light is its own business.
        """
        if not isinstance(ev, JVCurveDone) or ev.dark is not False:
            return
        if ev.metrics.voc is None or ev.led_level_v is None:
            return
        found = ev.illumination
        if found is not None and str(found.get("led_mode") or "").upper() != "DC":
            # Said out loud, because nothing else would say it. An unknown
            # curve is already warned about by `run_jv`; this one is a
            # perfectly good lit curve, and the only sign that its V_oc was
            # not taken would be a later `bace` failing to find a source.
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
        # The drive level this node ran at: the loop's when it is inside one,
        # its own single-level parameter otherwise (None for a jv_bace range),
        # so the journal's per-node record can key a V_oc by its level.
        led_v = ctx.led_v if ctx.led_v is not None else (values or {}).get("led_v")
        if led_v is not None:
            detail["led_v"] = led_v
        if ctx.temperature_k is not None:
            detail["temperature_k"] = ctx.temperature_k
            detail["temperature_how"] = ctx.temperature_how
            detail["temperature_source"] = ctx.temperature_source
        if error is not None:
            detail["error"] = error
        return detail

    # -- stopping -------------------------------------------------------------
    def _stop(self) -> Iterator[E.Event]:
        """A stop noticed between nodes: close what is open and report."""
        yield from self._close_open("stopped", "stop requested")
        yield E.RunAborted(reason="requested", done=self.modules_done,
                           total=self.modules_total)

    def _close_open(self, outcome: str, reason: str) -> Iterator[E.Event]:
        """`NodeDone(outcome)` for every open loop, innermost first, so a tree
        view has no node left hanging when the run ends early."""
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
    """Execute `schedule` on `rig`, yielding every event, as one worker job.

    `ctx_factory(step)` builds the `RunContext` a step is executed with
    (run id, output folder, metadata, sleep, the data sink); the executor then
    sets the bindings on it -- `led_v`/`led_low_v`, `temperature_k`, `voc` --
    so the session never has to know what a loop decided. `job` is the
    worker's job: its `node_path` is kept current so envelopes are stamped
    with the node each event belongs to, its `abort_check` is the stop flag
    unless `abort` is given, and its `wait_for_operator` is what a
    temperature pause blocks in. Without a job (tests) a temperature node
    that pauses needs `RunContext.wait_for_operator` from the factory; one
    that settles through `rig.temperature` needs only the context's `sleep`
    and `abort`. `on_voc` is called with every V_oc measured. `session_voc`
    is the session's current source, handed as itself to a manual run the
    schedule resolved against it. `emit` delivers an event out of band while
    the generator is blocked in a wait -- the console readings taken during a
    temperature pause; the session binds it to its own event path.

    The generator parks the rig in its `finally`, whichever way it ends.
    """
    return _Executor(rig, schedule, catalogue=catalogue, ctx_factory=ctx_factory, job=job,
                     on_voc=on_voc, abort=abort, session_voc=session_voc, emit=emit).run()
