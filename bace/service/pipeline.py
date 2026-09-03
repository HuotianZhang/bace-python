"""Pipelines: the tree, its schedule, the check catalogue and the cost model.

Pure logic. Nothing here touches an instrument, a thread, a socket or a file:
`validate` is the Dry run button, and a Dry run that could change the bench
would not be one. The executor runs what this module produces and never
re-derives structure from the tree, so every decision about *what happens in
which order* is made exactly once, here, where it can be tested without a rig.

Four things live here.

**The tree** (`parse_tree`). What the console posts: loops over temperature,
illumination and repeats, with module leaves. Shapes are checked on the way in
and unknown keys are refused, for the same reason `config.load_run` refuses
them: a misspelt `led_low_v` that fell back to a default would drive the LED
at a level nobody chose, and the tree would look right on screen.

**The schedule** (`resolve`). The tree flattened into ordered steps, each
carrying the module's parameters *with provenance*. This is where the three
bindings of the plan happen: an illumination loop's `led_v` enters every
child as `Source.INHERITED`; a `jv_bace` earlier in the same illumination step
becomes the `Source.DERIVED` V_oc of the `bace` after it; and each `jv_* <->
bace` boundary is marked as a relay transition. They are bindings between
*parameters*, made before anything runs, so the console can show the operator
what the run will use and where each number came from -- the failure this
prevents is the one `bace.params` describes: a V_oc typed an hour ago still
shown as "edited" while the loop centres on a fresh measurement.

**The checks** (`validate`). A fixed catalogue with stable ids, one `Verdict`
per id at least, so "16 checks, 15 ok, 1 warn" is a count the UI can render
and an operator can scan. `invalid` and `crit` block Start; everything else
states evidence and leaves the decision to the person at the bench. Nothing
is corrected here: a `fix` in a verdict's data names a bench action the
operator may click. The rules themselves are borrowed, not re-implemented --
`Axis`, `ScanSpec`, `LedDrive`, `assert_axis_centre`, `pulse_levels` and
`config.check_smu_limits` are asked to refuse, and their refusal becomes the
verdict -- so a rule cannot be tightened in the engine and stay loose here.

**The cost** (`estimate`). A sum of what is known. The settle time at a
temperature comes from the journal's history of operator waits or it is
`None`; it is never invented, because a made-up 20 minutes per temperature
becomes a made-up finish time that someone plans an evening around.
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..config import ConfigError, check_smu_limits
from ..core.axis import Axis, AxisError, ScanSpec
from ..core.illumination import IlluminationError, LedDrive, assert_axis_centre
from ..core.pulses import pulse_levels
from ..drivers.keithley2400 import SourceMeterConfig
from ..experiment.events import Verdict
from ..experiment.rig import RigConfig
from ..experiment.transient import RunConfig
from ..experiment.wire import to_wire
from ..params import ParamError, ParamSet, ParamValue, Source

LOOPS: tuple[str, ...] = ("temperature", "illumination", "repeat")

VOC_PARAM = "voc"
"""The module parameter a V_oc source fills (contract §5, the `bace` row)."""

TEMPERATURE_DEFAULTS: dict[str, float] = {"tolerance_k": 0.2, "hold_s": 60.0,
                                          "timeout_s": 1800.0}
"""What a temperature loop uses for the keys it leaves out -- the values on
the console's temperature card. Constants rather than a lookup into the
catalogue's `temperature` module so the schedule of a tree does not depend on
which modules the catalogue happens to list."""

ILLUMINATION_SETTLE_S = 2.0
"""The loop's own settle in the cost model when it does not say
(`SeriesConfig.led_settle_s`, the original's "LED stab. time")."""

T_SHOT_DEFAULT_S = 0.8
"""One light + dark shot when the journal has no bace run to measure it from."""

SETTLE_KEY_DECIMALS = 1
"""`Journal.settle_history()` keys setpoints rounded to 0.1 K."""

CHAIN_STALE_S = 600.0
LONG_RUN_S = 8 * 3600.0
STORE_SHOTS_BYTES = 2e9
LED_MATCH_V = 1e-9
"""The tolerance `assert_axis_centre` uses; two drive levels closer than this
are the same illumination."""

CHECKS: tuple[str, ...] = (
    "tree.shape", "tree.owned-param", "voc.source", "voc.coupling", "led.levels",
    "axis.geometry", "bench.instrument", "smu.ceiling", "relay.interlock",
    "bench.live-at-start", "voc.typed", "chain.led-polarity", "chain.bias-arm",
    "chain.bias-polarity", "temperature.not-wired", "temperature.inside-illumination",
    "trigger.auto", "power.console", "intensity.factor", "cost.long", "chain.stale",
)

VOC_FLAGS: tuple[str, ...] = ("centre_on_voc", "vpre_on_voc")
"""The `bace` parameters that make a V_oc source mandatory: a swept prebias
centred on it, or a pinned prebias set as an offset from it (TDCF at V_oc,
the design's inherited "vpre = V_oc + 0.000 V")."""

ADVISORY_NEEDS: frozenset[str] = frozenset({"voc", "power", "temperature"})
"""`Catalogue.needs` codes that are the V_oc source's business (`voc.source`
reports it) or a warning, not a missing instrument -- except for the `power`
module itself, whose instrument *is* the meter. `temperature` is advisory
because a temperature node runs either way: through the 331 console, or as
a pause for the operator (`temperature.not-wired` says which)."""
"""The catalogue, in the order `validate` reports it. Stable: the UI keys on
these strings, and a renamed id is a check that silently stops showing."""


# -- the tree ---------------------------------------------------------------
class TreeError(ValueError):
    """A tree that cannot be executed as posted. `node_path` says where: the
    structural key of the offending node (`temperature/illumination/bace`)
    when no iteration exists yet, the iteration path once it does."""

    def __init__(self, message: str, *, node_path: str = ""):
        super().__init__(message)
        self.node_path = node_path


@dataclass(frozen=True)
class Module:
    """A leaf: run `module` with `params` typed over the bench's ParamSet.
    `key` is the structural position (`temperature/illumination/bace#2`), the
    same for every iteration; `name` is the folder stem and is only read at
    the root."""

    module: str
    params: dict[str, Any] = field(default_factory=dict)
    label: str = ""
    name: str = ""
    key: str = ""

    def as_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "module", "module": self.module,
                               "params": dict(self.params)}
        if self.label:
            out["label"] = self.label
        if self.name:
            out["name"] = self.name
        return out


@dataclass(frozen=True)
class Loop:
    """A loop over `values`, in the order given. `params` holds only the keys
    the tree stated (`tolerance_k`/`hold_s`/`timeout_s`, or
    `led_low_v`/`led_settle_s`); `resolve` fills the rest, so the wire form
    can still tell a typed value from a default."""

    loop: str
    values: tuple
    params: dict[str, Any]
    children: tuple["Node", ...]
    label: str = ""
    name: str = ""
    key: str = ""

    def as_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": "loop", "loop": self.loop}
        if self.label:
            out["label"] = self.label
        if self.name:
            out["name"] = self.name
        if self.loop == "temperature":
            out["values_k"] = list(self.values)
        elif self.loop == "illumination":
            out["levels_v"] = list(self.values)
        else:
            out["count"] = len(self.values)
        out.update(self.params)
        out["children"] = [c.as_wire() for c in self.children]
        return out


Node = Loop | Module

_COMMON_KEYS = {"kind", "loop", "label", "children"}
_LOOP_KEYS: dict[str, set[str]] = {
    "temperature": {"values_k", "start_k", "stop_k", "step_k",
                    "tolerance_k", "hold_s", "timeout_s"},
    "illumination": {"levels_v", "led_start_v", "led_stop_v", "led_step_v",
                     "led_low_v", "led_settle_s"},
    "repeat": {"count"},
}
_RANGES: dict[str, tuple[str, tuple[str, str, str]]] = {
    "temperature": ("values_k", ("start_k", "stop_k", "step_k")),
    "illumination": ("levels_v", ("led_start_v", "led_stop_v", "led_step_v")),
}
_MODULE_KEYS = {"kind", "module", "params", "label"}


def range_values(start: float, stop: float, step: float) -> tuple[float, ...]:
    """`start` to `stop` inclusive at spacing `step`, with a **rounded** point
    count -- the rule `core.axis.Axis.values` uses. Truncation (the LabVIEW
    `N = abs(a-b)/dV + 1`) drops the last level whenever floating point puts
    the ratio just under the integer, which for 1.010 -> 1.030 step 0.005 is a
    four-level loop that was written as five. Values are rounded to nine
    decimals so `led=1.015V` is 1.015 and not 1.0149999999999999 on the
    wire; the V_oc coupling tolerance is 1e-9, so nothing is lost.
    """
    if start == stop:
        return (start,)
    if step <= 0:
        raise ValueError(f"a range from {start:g} to {stop:g} needs a positive "
                         f"step, not {step:g}")
    n = int(round(abs(stop - start) / step)) + 1
    return tuple(round(start + (stop - start) * i / (n - 1), 9) for i in range(n))


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _number(obj: Mapping, key: str, where: str, *, minimum: float | None = None,
            strict: bool = False) -> float:
    v = obj[key]
    if not _is_number(v):
        raise TreeError(f"{key}: {v!r} is not a finite number", node_path=where)
    if minimum is not None and (v <= minimum if strict else v < minimum):
        rel = "above" if strict else "at least"
        raise TreeError(f"{key}: {v!r} must be {rel} {minimum:g}", node_path=where)
    return v


def _sibling_tokens(children: Iterable[Mapping | Node]) -> list[str]:
    """`bace`, `bace#2`, `bace#3` for duplicate siblings; loops are named by
    their kind. The first keeps the bare name so a tree with no duplicates
    has the paths the contract spells."""
    names = []
    for c in children:
        if isinstance(c, Mapping):
            names.append(c.get("loop") if c.get("kind") == "loop" else c.get("module"))
        else:
            names.append(c.loop if isinstance(c, Loop) else c.module)
    seen: dict[Any, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(f"{n}" if seen[n] == 1 else f"{n}#{seen[n]}")
    return out


def _join(prefix: str, token: str) -> str:
    return f"{prefix}/{token}" if prefix else token


def parse_tree(obj: Mapping[str, Any]) -> Node:
    """The posted tree as `Loop`/`Module` nodes, or `TreeError`.

    Shape only: module names and parameter values are the catalogue's to
    judge, at `resolve`. Loop values from `start/stop/step` use the rounded
    count; a loop needs at least one child; `name` is accepted at the root
    only, where it becomes the folder stem.
    """
    if not isinstance(obj, Mapping):
        raise TreeError(f"the tree must be an object, not {type(obj).__name__}")
    return _parse_node(obj, key="", root=True)


def _parse_node(obj: Mapping[str, Any], *, key: str, root: bool) -> Node:
    if not isinstance(obj, Mapping):
        raise TreeError(f"a node must be an object, not {type(obj).__name__}",
                        node_path=key)
    kind = obj.get("kind")
    if kind == "module":
        return _parse_module(obj, key=key, root=root)
    if kind == "loop":
        return _parse_loop(obj, key=key, root=root)
    raise TreeError(f"kind: {kind!r} is not 'loop' or 'module'", node_path=key)


def _parse_module(obj: Mapping[str, Any], *, key: str, root: bool) -> Module:
    name = obj.get("module")
    if not isinstance(name, str) or not name:
        raise TreeError(f"module: {name!r} is not a module name", node_path=key)
    key = key or name
    allowed = _MODULE_KEYS | ({"name"} if root else set())
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise TreeError(f"{name}: unknown key(s) {', '.join(unknown)}; refusing "
                        "rather than ignoring them", node_path=key)
    params = obj.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, Mapping):
        raise TreeError(f"{name}: params must be an object of name -> value, not "
                        f"{type(params).__name__}", node_path=key)
    label = obj.get("label", "")
    if not isinstance(label, str):
        raise TreeError(f"{name}: label must be text", node_path=key)
    folder = obj.get("name", "") if root else ""
    if folder is None:
        folder = ""
    if not isinstance(folder, str):
        raise TreeError("name: must be text", node_path=key)
    return Module(module=name, params=dict(params), label=label, name=folder, key=key)


def _parse_loop(obj: Mapping[str, Any], *, key: str, root: bool) -> Loop:
    loop = obj.get("loop")
    if loop not in LOOPS:
        raise TreeError(f"loop: {loop!r} is not one of {', '.join(LOOPS)}",
                        node_path=key or str(loop))
    key = key or loop
    allowed = _COMMON_KEYS | _LOOP_KEYS[loop] | ({"name"} if root else set())
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise TreeError(f"{loop} loop: unknown key(s) {', '.join(unknown)}; "
                        "refusing rather than ignoring them", node_path=key)
    values = _loop_values(obj, loop, key)
    params: dict[str, Any] = {}
    if loop == "temperature":
        for k, minimum, strict in (("tolerance_k", 0.0, True), ("hold_s", 0.0, False),
                                   ("timeout_s", 0.0, True)):
            if k in obj:
                params[k] = _number(obj, k, key, minimum=minimum, strict=strict)
    elif loop == "illumination":
        if "led_low_v" in obj:
            params["led_low_v"] = _number(obj, "led_low_v", key)
        if "led_settle_s" in obj:
            params["led_settle_s"] = _number(obj, "led_settle_s", key, minimum=0.0)
    children = obj.get("children")
    if not isinstance(children, (list, tuple)) or not children:
        raise TreeError(f"{loop} loop: needs at least one child; a loop over "
                        "nothing would run nothing and look finished", node_path=key)
    tokens = _sibling_tokens(children)
    kids = tuple(_parse_node(c, key=_join(key, t), root=False)
                 for c, t in zip(children, tokens))
    label = obj.get("label", "")
    if not isinstance(label, str):
        raise TreeError(f"{loop} loop: label must be text", node_path=key)
    folder = obj.get("name", "") if root else ""
    if folder is None:
        folder = ""
    if not isinstance(folder, str):
        raise TreeError("name: must be text", node_path=key)
    return Loop(loop=loop, values=values, params=params, children=kids,
                label=label, name=folder, key=key)


def _loop_values(obj: Mapping[str, Any], loop: str, key: str) -> tuple:
    if loop == "repeat":
        if "count" not in obj:
            raise TreeError("repeat loop: count is required", node_path=key)
        count = obj["count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise TreeError(f"count: {count!r} must be a whole number of at least 1",
                            node_path=key)
        return tuple(range(1, count + 1))
    list_key, range_keys = _RANGES[loop]
    has_list, has_range = list_key in obj, any(k in obj for k in range_keys)
    if has_list and has_range:
        raise TreeError(f"{loop} loop: give either {list_key} or "
                        f"{'/'.join(range_keys)}, not both", node_path=key)
    if has_list:
        values = obj[list_key]
        if not isinstance(values, (list, tuple)) or not values:
            raise TreeError(f"{list_key}: must be a non-empty list", node_path=key)
        for v in values:
            if not _is_number(v):
                raise TreeError(f"{list_key}: {v!r} is not a finite number",
                                node_path=key)
        return tuple(values)
    if has_range:
        missing = [k for k in range_keys if k not in obj]
        if missing:
            raise TreeError(f"{loop} loop: {', '.join(missing)} missing; a range "
                            "needs all three", node_path=key)
        start, stop, step = (_number(obj, k, key) for k in range_keys)
        try:
            return range_values(start, stop, step)
        except ValueError as exc:
            raise TreeError(f"{loop} loop: {exc}", node_path=key) from None
    raise TreeError(f"{loop} loop: give {list_key} or {'/'.join(range_keys)}",
                    node_path=key)


def iteration_token(loop: str, value: Any) -> str:
    """`T=250K`, `led=1.020V`, `rep=2` -- the contract's spellings."""
    if loop == "temperature":
        return f"T={value:g}K"
    if loop == "illumination":
        return f"led={value:.3f}V"
    return f"rep={value}"


def walk(node: Node, *, ancestors: tuple[Loop, ...] = ()) -> Iterable[tuple[Node, tuple[Loop, ...]]]:
    """Every node with the loops above it, depth first."""
    yield node, ancestors
    if isinstance(node, Loop):
        for child in node.children:
            yield from walk(child, ancestors=ancestors + (node,))


# -- the schedule -----------------------------------------------------------
@dataclass
class Step:
    """One line of the schedule. `params` is the module's full ParamSet as
    resolved for this step, provenance included; `detail` carries what the
    executor and the UI need beyond that (the V_oc source, the relay
    transition, the loop's setpoint bookkeeping) as plain JSON-able values."""

    node_path: str
    kind: str                          # "loop-enter" | "module" | "loop-exit"
    loop: str | None
    value: float | int | None
    module: str | None
    params: dict[str, ParamValue]
    needs_operator: bool
    relay: str | None
    estimate_s: float | None
    detail: dict[str, Any] = field(default_factory=dict)

    def values(self) -> dict[str, Any]:
        """Plain `{name: value}`, what `Catalogue.build` is handed."""
        return {n: pv.value for n, pv in self.params.items()}

    def as_wire(self) -> dict[str, Any]:
        return {"node_path": self.node_path, "kind": self.kind, "loop": self.loop,
                "value": self.value, "module": self.module,
                "params": {n: pv.as_dict() for n, pv in self.params.items()},
                "needs_operator": self.needs_operator, "relay": self.relay,
                "estimate_s": self.estimate_s, "detail": _jsonable(self.detail)}


@dataclass
class Schedule:
    """What Dry run shows and what the executor runs: the steps in order, the
    four counters of the design, and the module node paths (the addresses
    `GET /runs/{id}/data?node=` accepts)."""

    steps: list[Step]
    counters: dict[str, int]
    node_paths: list[str]
    tree: Node

    @property
    def modules(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "module"]

    def as_wire(self) -> dict[str, Any]:
        return {"steps": [s.as_wire() for s in self.steps],
                "counters": dict(self.counters), "node_paths": list(self.node_paths),
                "tree": self.tree.as_wire()}


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, Source):
        return v.value
    return v


@dataclass
class _Ctx:
    """What the loops above a node have decided. Immutable per level: a child
    gets a copy with its loop's contribution added, never the parent's own."""

    led_v: float | None = None
    led_low_v: float | None = None
    led_settle_s: float | None = None
    in_illumination: bool = False
    in_temperature: bool = False
    temperature_k: float | None = None
    scopes: tuple[dict, ...] = field(default_factory=lambda: ({},))
    """V_oc providers in scope, innermost last: `{led_v: (node_path, module)}`
    per open loop iteration. A temperature iteration starts a fresh chain,
    because a V_oc measured at 295 K says nothing about 250 K; illumination
    and repeat iterations extend the chain, because a J-V taken at this
    temperature before the loop is still the J-V for this temperature.

    A factory, not a literal `({},)`: the tuple is immutable but the dict
    inside it is not, and a literal default would be one dict shared by
    every resolve in the process -- a jv_bace registered while resolving one
    tree would then serve as the V_oc source of a bace in the next."""


def temperature_automatic(bench: Mapping[str, Any] | None) -> bool | None:
    """Whether a temperature node will settle through the 331 console, from
    the bench snapshot: the controller attached (`wired`) and the console's
    instrument answering (`connected` True). None before the first
    read-back -- no snapshot, or the bench's stub with a controller attached
    and `connected` still None -- when nothing is known and the schedule
    assumes the pause (contract section 7)."""
    if not bench:
        return None
    t = (bench.get("instruments") or {}).get("temperature") or {}
    if not t.get("wired", False):
        return False
    connected = t.get("connected")
    return None if connected is None else bool(connected)


class _Resolver:
    def __init__(self, catalogue, *, session_voc, history, bench=None):
        self.catalogue = catalogue
        self.session_voc = session_voc
        self.history = history
        automatic = temperature_automatic(bench)
        self.needs_operator = True if automatic is None else not automatic
        """What a temperature step says on the Dry run: a pause unless the
        bench read-back showed the 331 attached and answering."""
        self.steps: list[Step] = []
        self.last_relay: str | None = None

    # -- walking ----------------------------------------------------------
    def run(self, tree: Node) -> Schedule:
        if isinstance(tree, Module):
            self._module(tree, "", tree.module, _Ctx())
        else:
            self._loop(tree, "", "", _Ctx())
        counters = self._counters(tree)
        node_paths = [s.node_path for s in self.steps if s.kind == "module"]
        return Schedule(steps=self.steps, counters=counters, node_paths=node_paths,
                        tree=tree)

    def _counters(self, tree: Node) -> dict[str, int]:
        temperatures = levels = 0
        for node, _ in walk(tree):
            if isinstance(node, Loop):
                if node.loop == "temperature":
                    temperatures += len(node.values)
                elif node.loop == "illumination":
                    levels += len(node.values)
        modules = [s for s in self.steps if s.kind == "module"]
        return {"temperatures": temperatures, "levels": levels,
                "modules": len(modules),
                "shots": sum(int(s.detail.get("shots") or 0) for s in modules)}

    def _children(self, node: Loop, prefix: str, ctx: _Ctx) -> None:
        for child, token in zip(node.children, _sibling_tokens(node.children)):
            if isinstance(child, Module):
                self._module(child, prefix, token, ctx)
            else:
                suffix = token[len(child.loop):]          # "" or "#2"
                self._loop(child, prefix, suffix, ctx)

    def _loop(self, node: Loop, prefix: str, suffix: str, ctx: _Ctx) -> None:
        count = len(node.values)
        for i, value in enumerate(node.values):
            path = _join(prefix, iteration_token(node.loop, value) + suffix)
            detail: dict[str, Any] = {"node_key": node.key, "index": i, "count": count,
                                      "label": node.label}
            estimate: float | None = 0.0
            if node.loop == "temperature":
                p = {k: node.params.get(k, d) for k, d in TEMPERATURE_DEFAULTS.items()}
                settle = settle_s(self.history, value)
                detail.update(setpoint_k=value, settle_s=settle, **p)
                estimate = None if settle is None else settle + p["hold_s"]
                inner = _Ctx(led_v=ctx.led_v, led_low_v=ctx.led_low_v,
                             led_settle_s=ctx.led_settle_s,
                             in_illumination=ctx.in_illumination,
                             in_temperature=True, temperature_k=float(value),
                             scopes=({},))
            elif node.loop == "illumination":
                led_low = node.params.get("led_low_v")
                led_settle = node.params.get("led_settle_s")
                detail.update(led_v=value, led_low_v=led_low,
                              led_settle_s=(ILLUMINATION_SETTLE_S if led_settle is None
                                            else led_settle))
                estimate = detail["led_settle_s"]
                inner = _Ctx(led_v=float(value), led_low_v=led_low,
                             led_settle_s=led_settle, in_illumination=True,
                             in_temperature=ctx.in_temperature,
                             temperature_k=ctx.temperature_k,
                             scopes=ctx.scopes + ({},))
            else:
                inner = _Ctx(led_v=ctx.led_v, led_low_v=ctx.led_low_v,
                             led_settle_s=ctx.led_settle_s,
                             in_illumination=ctx.in_illumination,
                             in_temperature=ctx.in_temperature,
                             temperature_k=ctx.temperature_k,
                             scopes=ctx.scopes + ({},))
            self.steps.append(Step(node_path=path, kind="loop-enter", loop=node.loop,
                                   value=value, module=None, params={},
                                   needs_operator=(node.loop == "temperature"
                                                   and self.needs_operator),
                                   relay=None, estimate_s=estimate, detail=detail))
            self._children(node, path, inner)
            self.steps.append(Step(node_path=path, kind="loop-exit", loop=node.loop,
                                   value=value, module=None, params={},
                                   needs_operator=False, relay=None, estimate_s=0.0,
                                   detail={"node_key": node.key, "index": i,
                                           "count": count}))

    # -- one module -------------------------------------------------------
    def _module(self, node: Module, prefix: str, token: str, ctx: _Ctx) -> None:
        path = _join(prefix, token)
        try:
            spec = self.catalogue.spec(node.module)
            ps: ParamSet = self.catalogue.param_set(node.module)
        except (ValueError, KeyError) as exc:
            raise TreeError(f"{node.module}: {exc}", node_path=path) from None
        try:
            ps.update_layer(Source.EDITED, node.params, "pipeline node")
        except ParamError as exc:
            raise TreeError(f"{node.module}: {exc}", node_path=path) from None
        if ctx.in_illumination:
            inherited = _inherited(ps, ctx)
            if inherited:
                ps.set_layer(Source.INHERITED, inherited, "illumination loop")

        led_v = ps.get("led_v").value if "led_v" in ps else None
        detail: dict[str, Any] = {
            "node_key": node.key, "label": node.label, "title": spec.title,
            "module_kind": spec.kind, "status": spec.status, "led_v": led_v,
            "temperature_k": ctx.temperature_k,
            "in_illumination": ctx.in_illumination,
        }
        # Which flags make a V_oc mandatory for this module: the spec's own
        # (`centre_on_voc`) and the pinned-offset one, whichever it has.
        flags = [n for n in (spec.needs_voc_param, *VOC_FLAGS)
                 if n and n in ps and bool(ps.get(n).value)]
        detail["centre_on_voc"] = bool(flags)
        detail["voc_needed_by"] = sorted(set(flags))
        if VOC_PARAM in ps:
            detail["voc"], detail["voc_rejected"] = self._voc(ps, path, ctx, led_v)
        if spec.provides_voc:
            for level in light_levels(ps.values()):
                _register(ctx.scopes[-1], level, (path, node.module))

        detail["shots"] = _shots(ps)
        detail["relay_transition"] = (spec.relay is not None
                                      and self.last_relay is not None
                                      and spec.relay != self.last_relay)
        detail["relay_from"] = self.last_relay if spec.relay is not None else None
        if spec.relay is not None:
            self.last_relay = spec.relay

        values = ps.values()
        try:
            estimate: float | None = float(self.catalogue.estimate_s(node.module, values))
        except (ValueError, KeyError, TypeError, ArithmeticError):
            estimate = None
        self.steps.append(Step(node_path=path, kind="module", loop=None, value=None,
                               module=node.module, params=ps.resolve(),
                               needs_operator=(node.module == "temperature"
                                               and self.needs_operator),
                               relay=spec.relay, estimate_s=estimate, detail=detail))

    def _voc(self, ps: ParamSet, path: str, ctx: _Ctx, led_v: float | None
             ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Which V_oc this module will centre on, and why. Sets the `voc`
        parameter's derived layer when the source is a measurement.

        Order (contract section 5): a J-V in scope at this drive level beats
        everything, because a measurement made inside the loop is the most
        recent statement about the device (the `bace.params` doctrine,
        derived over edited); then the module's own DC measurement; then a
        value typed by hand, which the checks flag; then, outside any
        illumination or temperature loop, the session's V_oc at the same
        level. `measure_dc` comes before a typed value because when it is on
        the run *will* measure, and a schedule that said "typed" would
        describe a value the run ignores. A typed value passed over by a
        measurement is kept in the source as `overrides`, so `voc.typed` can
        say the number did not run rather than let it vanish from the wire.
        """
        pv = ps.get(VOC_PARAM)
        typed = None
        if pv.value is not None and pv.source not in (Source.INHERITED, Source.DERIVED):
            typed = {"value": float(pv.value), "source": pv.source.value,
                     "detail": pv.detail}
        if led_v is not None:
            hit = _lookup(ctx.scopes, led_v)
            if hit is not None:
                # A jv_bace in scope taking over a typed value is the plain
                # precedence the operator expects; the typed number is not
                # recorded as overridden, because nothing warns about it.
                src_path, src_module = hit
                ps.set_layer(Source.DERIVED, {VOC_PARAM: None},
                             f"{src_module} {src_path}")
                return ({"how": src_module, "node_path": src_path, "led_v": led_v,
                         "value": None}, None)
        if "measure_dc" in ps and bool(ps.get("measure_dc").value):
            src = {"how": "measure_dc", "node_path": path, "led_v": led_v, "value": None}
            if typed is not None:
                # measure_dc and a typed voc on the same node: the run
                # measures and the typed number never runs. Kept so
                # voc.typed can say so rather than let it vanish.
                src["overrides"] = typed
            ps.set_layer(Source.DERIVED, {VOC_PARAM: None}, "measure_dc on this node")
            return (src, None)
        if typed is not None:
            return ({"how": "typed", "node_path": path, "led_v": None, **typed}, None)
        sv = self.session_voc
        if sv is not None and led_v is not None and not (ctx.in_illumination
                                                         or ctx.in_temperature):
            candidate = {"how": sv.how, "node_path": sv.node_path, "led_v": sv.led_v,
                         "value": sv.value, "run_id": sv.run_id, "session": True}
            if abs(float(sv.led_v) - float(led_v)) <= LED_MATCH_V:
                ps.set_layer(Source.DERIVED, {VOC_PARAM: sv.value},
                             f"{sv.how} {sv.node_path} (this session)")
                return candidate, None
            return None, candidate
        return None, None


def _inherited(ps: ParamSet, ctx: _Ctx) -> dict[str, Any]:
    """The illumination loop's binding, spelled for whichever LED parameters
    this module has: a single `led_v`, a `led_start_v`/`led_stop_v` range
    collapsed to one level, or a `led_levels_v` list of one."""
    out: dict[str, Any] = {}
    for name in ("led_v", "led_start_v", "led_stop_v"):
        if name in ps:
            out[name] = ctx.led_v
    if "led_levels_v" in ps:
        out["led_levels_v"] = (ctx.led_v,)
    if ctx.led_low_v is not None and "led_low_v" in ps:
        out["led_low_v"] = ctx.led_low_v
    if ctx.led_settle_s is not None and "led_settle_s" in ps:
        out["led_settle_s"] = ctx.led_settle_s
    return out


def light_levels(values: Mapping[str, Any]) -> tuple[float, ...]:
    """The drive levels a module illuminates at, from whichever spelling its
    parameters use. Empty when it has none or the range is malformed (the
    `led.levels` check reports that; here it just provides no V_oc)."""
    try:
        if "led_levels_v" in values:
            return tuple(float(v) for v in values["led_levels_v"])
        if "led_start_v" in values and "led_stop_v" in values:
            return range_values(float(values["led_start_v"]), float(values["led_stop_v"]),
                                float(values.get("led_step_v", 0.0)))
        if "led_v" in values:
            return (float(values["led_v"]),)
    except (ValueError, TypeError):
        pass
    return ()


def _register(scope: dict, led_v: float, provider: tuple[str, str]) -> None:
    for key in list(scope):
        if abs(key - led_v) <= LED_MATCH_V:
            del scope[key]
    scope[float(led_v)] = provider


def _lookup(scopes: tuple[dict, ...], led_v: float) -> tuple[str, str] | None:
    for scope in reversed(scopes):
        for key, provider in scope.items():
            if abs(key - led_v) <= LED_MATCH_V:
                return provider
    return None


def _axis_of(values: Mapping[str, Any]) -> Axis:
    return Axis(name=values["axis_name"], start=float(values["axis_start"]),
                stop=float(values["axis_stop"]), step=float(values.get("axis_step", 0.0)),
                centre_on_voc=bool(values.get("centre_on_voc", False)))


def _shots(ps: ParamSet) -> int:
    """Shots a module takes, for the counters: loops x axis points. Zero for
    a module without an axis, and zero for an axis the engine refuses (the
    `axis.geometry` check reports why)."""
    if "axis_name" not in ps:
        return 0
    v = ps.values()
    try:
        return int(v.get("n_loops", 1)) * _axis_of(v).n_points
    except (AxisError, ValueError, TypeError, KeyError):
        return 0


def resolve(tree: Node, catalogue, *, session_voc=None, history=None,
            bench: Mapping[str, Any] | None = None) -> Schedule:
    """The tree as a flat schedule. Raises `TreeError` for a module the
    catalogue does not know or a parameter it refuses, naming the node.
    `bench` is the read-back, read for whether a temperature step pauses
    (`needs_operator`) or settles through the 331; without it every
    temperature step says it pauses."""
    return _Resolver(catalogue, session_voc=session_voc, history=history,
                     bench=bench).run(tree)


# -- the cost model -----------------------------------------------------------
def settle_s(history, setpoint_k: float) -> float | None:
    """Median of the operator's past settle times at this setpoint, or None.
    Never a guess: `None` renders as "--" in the per-temperature table and
    makes the total a lower bound."""
    if history is None:
        return None
    try:
        table = history.settle_history() or {}
    except Exception:
        return None
    key = round(float(setpoint_k), SETTLE_KEY_DECIMALS)
    samples = None
    for k, v in table.items():
        try:
            if abs(float(k) - key) < 10 ** -(SETTLE_KEY_DECIMALS + 1):
                samples = list(v)
                break
        except (TypeError, ValueError):
            continue
    if not samples:
        return None
    return float(statistics.median(samples))


def shot_time(history) -> tuple[float, str]:
    """`(seconds per shot, "journal" | "default")`."""
    if history is not None:
        try:
            t = history.shot_time_s("bace")
        except Exception:
            t = None
        if t is not None and math.isfinite(float(t)) and float(t) > 0:
            return float(t), "journal"
    return T_SHOT_DEFAULT_S, "default"


def estimate(schedule: Schedule, history, *, now: float | None = None) -> dict[str, Any]:
    """The cost dict of the contract.

    `t_module` is each module step's `estimate_s` as the catalogue gave it;
    an illumination level adds its settle; a temperature adds the journal's
    settle at that setpoint (or nothing, and then the total is a lower bound)
    plus `hold_s`. `now` is an argument so a test can pin `finish_at`.
    """
    t_shot, t_shot_source = shot_time(history)
    measuring = 0.0
    waiting = 0.0
    lower_bound = False
    unestimated = 0
    per_temperature: list[dict[str, Any]] = []
    open_temperatures: list[dict[str, Any]] = []

    def measured(t: float) -> None:
        nonlocal measuring
        measuring += t
        for entry in open_temperatures:
            entry["measure_s"] += t

    for step in schedule.steps:
        if step.kind == "module":
            if step.estimate_s is None:
                unestimated += 1
                lower_bound = True
            else:
                measured(float(step.estimate_s))
        elif step.kind == "loop-enter" and step.loop == "illumination":
            measured(float(step.detail.get("led_settle_s") or 0.0))
        elif step.kind == "loop-enter" and step.loop == "temperature":
            settle = settle_s(history, float(step.value))
            hold = float(step.detail.get("hold_s") or 0.0)
            entry = {"setpoint_k": step.value, "settle_s": settle, "hold_s": hold,
                     "measure_s": 0.0}
            per_temperature.append(entry)
            open_temperatures.append(entry)
            if settle is None:
                lower_bound = True
            else:
                waiting += settle
            waiting += hold
        elif step.kind == "loop-exit" and step.loop == "temperature":
            if open_temperatures:
                open_temperatures.pop()

    total = measuring + waiting
    when = time.time() if now is None else float(now)
    return {
        "total_s": total,
        "measuring_s": measuring,
        "waiting_s": None if lower_bound else waiting,
        "per_temperature": per_temperature,
        "lower_bound": lower_bound,
        "finish_at": None if lower_bound else when + total,
        "t_shot_s": t_shot,
        "t_shot_source": t_shot_source,
        "unestimated_modules": unestimated,
    }


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "--"
    s = float(seconds)
    if s < 60:
        return f"{s:.0f} s"
    if s < 3600:
        return f"{s / 60:.0f} min"
    h, rest = divmod(s, 3600)
    return f"{int(h)} h {int(rest // 60):02d}"


# -- validation ---------------------------------------------------------------
@dataclass
class Validation:
    valid: bool
    checks: list[Verdict]
    schedule: Schedule | None
    counters: dict[str, int]
    cost: dict[str, Any]
    node_paths: list[str]

    def by_code(self, code: str) -> list[Verdict]:
        return [c for c in self.checks if c.code == code]

    def as_wire(self) -> dict[str, Any]:
        return {"valid": self.valid,
                "checks": [to_wire(c)[0] for c in self.checks],
                "schedule": None if self.schedule is None else self.schedule.as_wire(),
                "counters": dict(self.counters), "cost": _jsonable(self.cost),
                "node_paths": list(self.node_paths)}


def validate(tree_or_obj, catalogue, *, bench: Mapping[str, Any] | None = None,
             history=None, session_voc=None, rig_config: RigConfig,
             power_available: bool | None = None,
             now: float | None = None) -> Validation:
    """Every check in the catalogue, in order, on one tree.

    `bench` is the `/bench` snapshot (or None before the first read-back:
    the bench-side checks then say so as `info` rather than guessing);
    `power_available` overrides the snapshot's view of the 1918-C console
    when the caller has just asked it. Touches nothing.
    """
    when = time.time() if now is None else float(now)
    error: TreeError | None = None
    tree: Node | None = None
    schedule: Schedule | None = None
    try:
        tree = tree_or_obj if isinstance(tree_or_obj, (Loop, Module)) else parse_tree(tree_or_obj)
        schedule = resolve(tree, catalogue, session_voc=session_voc, history=history,
                           bench=bench)
    except TreeError as exc:
        error = exc
    cost = estimate(schedule, history, now=when) if schedule is not None else {}
    facts = _Facts(tree=tree, schedule=schedule, error=error, catalogue=catalogue,
                   bench=bench, history=history, session_voc=session_voc,
                   rig=rig_config, power_available=power_available, cost=cost, now=when)
    checks: list[Verdict] = []
    for code in CHECKS:
        checks.extend(_CHECKS[code](facts))
    valid = not any(c.level in ("invalid", "crit") for c in checks)
    return Validation(valid=valid, checks=checks, schedule=schedule,
                      counters=dict(schedule.counters) if schedule else {},
                      cost=cost, node_paths=list(schedule.node_paths) if schedule else [])


@dataclass
class _Facts:
    tree: Node | None
    schedule: Schedule | None
    error: TreeError | None
    catalogue: Any
    bench: Mapping[str, Any] | None
    history: Any
    session_voc: Any
    rig: RigConfig
    power_available: bool | None
    cost: dict[str, Any]
    now: float

    @property
    def modules(self) -> list[Step]:
        return self.schedule.modules if self.schedule else []

    @property
    def transients(self) -> list[Step]:
        return [s for s in self.modules if s.relay == "transient"]

    def first_path(self, key: str) -> str:
        """The first iteration's path for a structural key, so a verdict about
        a tree node points at a place the schedule shows."""
        for s in (self.schedule.steps if self.schedule else ()):
            if s.detail.get("node_key") == key:
                return s.node_path
        return key

    def instrument(self, name: str) -> Mapping[str, Any]:
        if not self.bench:
            return {}
        return (self.bench.get("instruments") or {}).get(name) or {}

    def chain_item(self, key: str) -> Mapping[str, Any] | None:
        if not self.bench:
            return None
        for item in (self.bench.get("chain") or {}).get("items") or []:
            if item.get("key") == key:
                return item
        return None


def _v(level: str, code: str, text: str, node_path: str = "", **data: Any) -> Verdict:
    return Verdict(level=level, code=code, text=text, node_path=node_path,
                   data=_jsonable(data))


def _skipped(code: str) -> list[Verdict]:
    return [_v("info", code, "not evaluated: the tree did not resolve")]


def _grouped(code: str, level: str, failures: list[tuple[Step, str, dict]]) -> list[Verdict]:
    """One verdict per *tree node*, not per iteration: a bad low level on a
    bace inside 9 x 5 loops is one mistake, not forty-five lines. The first
    iteration's path is the address; `data` counts the rest."""
    groups: dict[str, list[tuple[Step, str, dict]]] = {}
    for step, text, data in failures:
        groups.setdefault(step.detail.get("node_key", step.node_path), []).append((step, text, data))
    out = []
    for key, items in groups.items():
        step, text, data = items[0]
        n = len(items)
        suffix = f" (the same at {n - 1} more iteration{'s' if n > 2 else ''})" if n > 1 else ""
        out.append(_v(level, code, text + suffix, step.node_path,
                      node_key=key, count=n,
                      node_paths=[s.node_path for s, _, _ in items[:10]], **data))
    return out


# -- the checks, in catalogue order -------------------------------------------
def _c_tree_shape(f: _Facts) -> list[Verdict]:
    if f.error is not None:
        return [_v("invalid", "tree.shape", str(f.error), f.error.node_path)]
    n_loops = sum(1 for node, _ in walk(f.tree) if isinstance(node, Loop))
    return [_v("ok", "tree.shape",
               f"tree resolves: {n_loops} loop{'s' if n_loops != 1 else ''}, "
               f"{len(f.modules)} module run{'s' if len(f.modules) != 1 else ''}",
               loops=n_loops, modules=len(f.modules))]


def _c_owned_param(f: _Facts) -> list[Verdict]:
    if f.tree is None:
        return _skipped("tree.owned-param")
    out = []
    for node, ancestors in walk(f.tree):
        if not isinstance(node, Module) or not any(a.loop == "illumination" for a in ancestors):
            continue
        try:
            owned = tuple(f.catalogue.spec(node.module).led_params)
        except (ValueError, KeyError):
            continue
        typed = [p for p in owned if p in node.params]
        if typed:
            out.append(_v("invalid", "tree.owned-param",
                          f"{node.module} sets {', '.join(typed)} inside an illumination "
                          "loop; the loop owns the LED level, so the typed value would "
                          "either be ignored or centre the scan on a V_oc measured at "
                          "another level", f.first_path(node.key),
                          node_key=node.key, params=typed))
    if out:
        return out
    return [_v("ok", "tree.owned-param",
               "no module inside an illumination loop types an LED parameter")]


def _c_voc_source(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("voc.source")
    failures: list[tuple[Step, str, dict]] = []
    how: dict[str, int] = {}
    for s in f.modules:
        if "voc" not in s.detail:
            continue
        src = s.detail.get("voc")
        if src is not None:
            how[src["how"]] = how.get(src["how"], 0) + 1
        if s.detail.get("centre_on_voc") and src is None and not s.detail.get("voc_rejected"):
            # A session V_oc at another level is `voc.coupling`'s to report;
            # saying "no source" as well would hide the actual cause.
            led = s.detail.get("led_v")
            at = f"at {led:g} V " if led is not None else ""
            where = ("in this illumination step" if s.detail.get("in_illumination")
                     else "before it")
            by = ", ".join(s.detail.get("voc_needed_by") or ["centre_on_voc"])
            failures.append((s, f"{s.module} needs V_oc ({by}) but nothing in scope "
                                f"measures it: no jv_bace {at}{where}, "
                                "measure_dc is off, and nothing was typed",
                             {"led_v": led, "needed_by": s.detail.get("voc_needed_by")}))
    if failures:
        return _grouped("voc.source", "invalid", failures)
    centred = sum(1 for s in f.modules if s.detail.get("centre_on_voc"))
    if not centred:
        return [_v("ok", "voc.source", "no module centres on V_oc", sources=how)]
    parts = ", ".join(f"{n} from {k}" for k, n in how.items())
    return [_v("ok", "voc.source",
               f"V_oc source in scope for every centred module ({parts})",
               sources=how, centred=centred)]


def _c_voc_coupling(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("voc.coupling")
    failures = []
    checked = 0
    for s in f.modules:
        if not s.detail.get("centre_on_voc"):
            continue
        led_v = s.detail.get("led_v")
        src = s.detail.get("voc") or s.detail.get("voc_rejected")
        if led_v is None or src is None or src.get("led_v") is None:
            continue
        checked += 1
        measured_at = float(src["led_v"])
        try:
            drive = LedDrive(level=float(led_v),
                             low_level=float(s.values().get("led_low_v", 0.4)),
                             threshold_v=f.rig.led_threshold_v)
        except IlluminationError:
            # The drive itself is refused (led.levels says so). The coupling
            # question is still the same comparison, made directly.
            drive = None
        if drive is not None:
            try:
                assert_axis_centre(drive, voc_measured_at=measured_at)
                continue
            except IlluminationError as exc:
                text = str(exc)
        elif abs(float(led_v) - measured_at) <= LED_MATCH_V:
            continue
        else:
            text = (f"V_oc was measured at {measured_at} V drive but the transient "
                    f"will run at {led_v} V")
        failures.append((s, f"{s.module}: {text} ({src['how']} {src['node_path']})",
                         {"led_v": led_v, "voc_led_v": measured_at,
                          "voc_how": src["how"], "voc_node_path": src["node_path"]}))
    if failures:
        return _grouped("voc.coupling", "invalid", failures)
    return [_v("ok", "voc.coupling",
               f"every centred scan runs at the drive its V_oc was measured at "
               f"({checked} checked)", checked=checked)]


def _c_led_levels(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("led.levels")
    failures = []
    levels: list[float] = []
    lows: set[float] = set()
    thr = f.rig.led_threshold_v
    for s in f.modules:
        v = s.values()
        # `jv_bace.led_v` is nullable: None means "sweep led_start_v to
        # led_stop_v", so only a value is a single level; None falls
        # through to the range below.
        if v.get("led_v") is not None:
            low = float(v.get("led_low_v", 0.4))
            try:
                LedDrive(level=float(v["led_v"]), low_level=low,
                         frequency_hz=float(v.get("pulse_frequency_hz", 500.0)),
                         duty_percent=float(v.get("duty_percent", 50.0)),
                         threshold_v=thr)
                levels.append(float(v["led_v"]))
                lows.add(low)
            except IlluminationError as exc:
                failures.append((s, f"{s.module}: {exc}",
                                 {"led_v": v["led_v"], "led_low_v": low, "threshold_v": thr}))
        elif "led_start_v" in v or "led_levels_v" in v:
            low = float(v.get("led_low_v", 0.4))
            module_levels = light_levels(v)
            if not module_levels and not bool(v.get("dark", True)):
                failures.append((s, f"{s.module}: no light levels and no dark curve; "
                                    "nothing to measure", {}))
            for level in module_levels:
                try:
                    LedDrive(level=level, low_level=low, threshold_v=thr)
                    levels.append(level)
                    lows.add(low)
                except IlluminationError as exc:
                    failures.append((s, f"{s.module}: {exc}",
                                     {"led_v": level, "led_low_v": low, "threshold_v": thr}))
                    break
    if failures:
        return _grouped("led.levels", "invalid", failures)
    if not levels:
        return [_v("ok", "led.levels", "no module drives the LED")]
    return [_v("ok", "led.levels",
               f"{len(set(levels))} drive level{'s' if len(set(levels)) != 1 else ''} "
               f"between {min(levels):.3f} and {max(levels):.3f} V, low level "
               f"{', '.join(f'{x:g}' for x in sorted(lows))} V under the {thr:g} V threshold",
               levels=sorted(set(levels)), low_v=sorted(lows), threshold_v=thr)]


def _c_axis_geometry(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("axis.geometry")
    failures = []
    described: list[str] = []
    for s in f.modules:
        v = s.values()
        if "axis_name" not in v:
            continue
        try:
            axis = _axis_of(v)
            spec = ScanSpec(axis=axis, vpre=float(v.get("vpre", 0.0)),
                            vcoll=float(v.get("vcoll", -1.0)),
                            delay_ns=float(v.get("delay_ns", 90.0)),
                            n_loops=int(v.get("n_loops", 1)))
        except (AxisError, ValueError, TypeError) as exc:
            failures.append((s, f"{s.module}: {exc}", {"axis": {k: v.get(k) for k in
                             ("axis_name", "axis_start", "axis_stop", "axis_step",
                              "centre_on_voc", "n_loops")}}))
            continue
        if bool(v.get("vpre_on_voc")) and axis.name == "vpre":
            # `centre_on_voc` is how a *swept* prebias follows V_oc; the
            # pinned offset only means something when vpre is pinned.
            failures.append((s, f"{s.module}: vpre_on_voc: vpre is the swept axis here, "
                                "so there is no pinned prebias to offset from V_oc; "
                                "centre_on_voc is the flag for a swept vpre",
                             {"axis_name": axis.name, "vpre_on_voc": True}))
            continue
        delay = (min(axis.start, axis.stop) if axis.name == "delay_ns" else spec.delay_ns)
        try:
            pulse_levels(spec.vpre, spec.vcoll, f.rig.pulse_amp, delay,
                         float(v.get("pulse_width_ns", 5000.0)),
                         trigger_offset_s=f.rig.trigger_offset_s)
        except ValueError as exc:
            failures.append((s, f"{s.module}: {exc}",
                             {"delay_ns": delay, "trigger_offset_s": f.rig.trigger_offset_s}))
            continue
        described.append(f"{axis} x {spec.n_loops}")
    if failures:
        return _grouped("axis.geometry", "invalid", failures)
    if not described:
        return [_v("ok", "axis.geometry", "no scan in this tree")]
    distinct = list(dict.fromkeys(described))
    return [_v("ok", "axis.geometry",
               f"{len(described)} scan{'s' if len(described) != 1 else ''}: "
               + "; ".join(distinct[:3]) + (" ..." if len(distinct) > 3 else ""),
               scans=len(described), axes=distinct[:10])]


def _c_bench_instrument(f: _Facts) -> list[Verdict]:
    """Every module step's instruments are on this bench. `Catalogue.needs`
    knows what each module cannot run without and what the last read-back
    said is unavailable; without it a run on an unplugged Keithley was
    accepted, queued and failed at preflight, when the Start button should
    have said no."""
    if f.schedule is None:
        return _skipped("bench.instrument")
    needs = getattr(f.catalogue, "needs", None)
    if not callable(needs):
        return [_v("ok", "bench.instrument", "the catalogue reports no instrument needs")]
    if f.bench is None:
        return [_v("info", "bench.instrument",
                   "instruments not read yet; which are on the bench is read at Start")]
    failures = []
    checked = 0
    for s in f.modules:
        try:
            items = list(needs(str(s.module), s.values(), bench=f.bench))
        except Exception:                                   # noqa: BLE001 -- the builder will say
            continue
        checked += 1
        missing = [n for n in items
                   if n.get("code") not in ADVISORY_NEEDS or
                   (n.get("code") == "power" and s.module == "power")]
        if missing:
            failures.append((s, f"{s.module}: " + "; ".join(str(n.get("text")) for n in missing),
                             {"missing": [n.get("code") for n in missing]}))
    if failures:
        return _grouped("bench.instrument", "invalid", failures)
    return [_v("ok", "bench.instrument",
               f"every instrument the tree needs is on this bench ({checked} module run"
               f"{'s' if checked != 1 else ''} checked)", checked=checked)]


def _c_smu_ceiling(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("smu.ceiling")
    failures = []
    asked: set[tuple[float, float]] = set()
    for s in f.modules:
        v = s.values()
        if "smu_current_compliance_a" not in v and "smu_voltage_compliance_v" not in v:
            continue
        cfg = SourceMeterConfig(
            current_compliance_a=float(v.get("smu_current_compliance_a",
                                             SourceMeterConfig.current_compliance_a)),
            voltage_compliance_v=float(v.get("smu_voltage_compliance_v",
                                             SourceMeterConfig.voltage_compliance_v)))
        try:
            check_smu_limits(cfg, f.rig)
            asked.add((cfg.current_compliance_a, cfg.voltage_compliance_v))
        except ConfigError as exc:
            failures.append((s, f"{s.module}: {exc}",
                             {"current_compliance_a": cfg.current_compliance_a,
                              "voltage_compliance_v": cfg.voltage_compliance_v,
                              "max_current_compliance_a": f.rig.max_current_compliance_a,
                              "max_voltage_compliance_v": f.rig.max_voltage_compliance_v}))
    if failures:
        return _grouped("smu.ceiling", "crit", failures)
    if not asked:
        return [_v("ok", "smu.ceiling", "no module sources the SourceMeter")]
    text = "; ".join(f"{i:g} A / {u:g} V" for i, u in sorted(asked))
    return [_v("ok", "smu.ceiling",
               f"compliance {text} under the bench ceiling "
               f"{f.rig.max_current_compliance_a:g} A / {f.rig.max_voltage_compliance_v:g} V",
               asked=[list(x) for x in sorted(asked)],
               max_current_compliance_a=f.rig.max_current_compliance_a,
               max_voltage_compliance_v=f.rig.max_voltage_compliance_v)]


def _live_sources(f: _Facts) -> tuple[list[str], list[str]]:
    """(names reporting output ON, names whose state is unknown)."""
    live, unknown = [], []
    for name, label in (("bias", "81150A bias"), ("smu", "Keithley 2400")):
        state = f.instrument(name).get("output")
        if state is True:
            live.append(label)
        elif state is None:
            unknown.append(label)
    return live, unknown


def _c_relay_interlock(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("relay.interlock")
    relays = {s.relay for s in f.modules if s.relay}
    transitions = sum(1 for s in f.modules if s.detail.get("relay_transition"))
    if len(relays) < 2:
        return [_v("ok", "relay.interlock", "no relay transition in this tree",
                   transitions=0)]
    if f.bench is None:
        return [_v("info", "relay.interlock",
                   f"{transitions} relay transition{'s' if transitions != 1 else ''} "
                   "scheduled; the relay and the outputs have not been read yet "
                   "(read at Start)", transitions=transitions)]
    relay = f.instrument("relay")
    if not relay or relay.get("how") == "unavailable":
        return [_v("crit", "relay.interlock",
                   f"the tree moves the relay {transitions} time"
                   f"{'s' if transitions != 1 else ''} (jv <-> bace) but no Router is "
                   "available: the SourceMeter and the amplifier would share the device "
                   "node", transitions=transitions, relay=dict(relay))]
    live, _ = _live_sources(f)
    if live:
        return [_v("crit", "relay.interlock",
                   f"the relay would have to move but {' and '.join(live)} report"
                   f"{'s' if len(live) == 1 else ''} output ON; the interlock refuses to "
                   "switch while a source drives", transitions=transitions, live=live)]
    return [_v("ok", "relay.interlock",
               f"relay on {relay.get('position', 'unknown')} ({relay.get('how', '?')}), "
               f"both sources off; {transitions} transition"
               f"{'s' if transitions != 1 else ''} scheduled",
               transitions=transitions, position=relay.get("position"))]


def _c_live_at_start(f: _Facts) -> list[Verdict]:
    if f.bench is None:
        return [_v("info", "bench.live-at-start",
                   "outputs not read yet; they are read at Start")]
    live, unknown = _live_sources(f)
    read_at = f.bench.get("read_at")
    if live:
        return [_v("crit", "bench.live-at-start",
                   f"{' and '.join(live)} output{'s' if len(live) > 1 else ''} ON on the "
                   "last read-back; a run must start with both sources off",
                   live=live, read_at=read_at)]
    if unknown:
        return [_v("info", "bench.live-at-start",
                   f"output state unknown for {' and '.join(unknown)}",
                   unknown=unknown, read_at=read_at)]
    return [_v("ok", "bench.live-at-start", "bias and SourceMeter outputs off",
               read_at=read_at)]


def _c_voc_typed(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("voc.typed")
    out = []
    for s in f.modules:
        src = s.detail.get("voc")
        if not src:
            continue
        over = src.get("overrides")
        if src["how"] != "typed":
            # A jv_bace in scope taking over a typed value is the plain
            # precedence the operator expects (the loop's measurement is
            # the point), and stays quiet. `measure_dc` on the same node as
            # a typed `voc` is the surprise: both were set by hand, the run
            # measures and the typed number never runs, so it is said here
            # rather than let vanish from the schedule.
            if over and src["how"] == "measure_dc":
                out.append((s, f"{s.module}.voc = {over['value']:g} V typed by hand "
                               f"({over.get('source', 'edited')}) is not used: measure_dc "
                               "on this node measures the V_oc the scan centres on",
                            {"value": over["value"], "source": over.get("source"),
                             "used": "measure_dc"}))
            continue
        if src.get("session"):
            text = (f"{s.module}.voc = {src['value']:g} V is the session's typed V_oc, "
                    "not a measurement")
        else:
            inside = (" -- inside an illumination loop, so it is not at a known drive "
                      "level" if s.detail.get("in_illumination") else "")
            text = (f"{s.module}.voc = {src['value']:g} V typed by hand "
                    f"({src.get('source', 'edited')}), not measured this session{inside}")
        out.append((s, text, {"value": src["value"], "source": src.get("source")}))
    if out:
        return _grouped("voc.typed", "warn", out)
    return [_v("ok", "voc.typed", "no V_oc typed by hand")]


def _chain_not_read(code: str) -> list[Verdict]:
    return [_v("info", code, "trigger chain not read yet; it is re-read at Start")]


def _c_chain_led_polarity(f: _Facts) -> list[Verdict]:
    if f.bench is None:
        return _chain_not_read("chain.led-polarity")
    item = f.chain_item("led_polarity")
    if item is None:
        return [_v("info", "chain.led-polarity", "33220A polarity not in the read-back")]
    value = str(item.get("value", "?")).upper()
    fix = item.get("fix") or "set-33220a-pol-inv"
    if value.startswith("INV"):
        return [_v("ok", "chain.led-polarity", "33220A :OUTP:POL INV", value=value)]
    # `warn` when a transient in this tree uses the Sync edge, `info` when
    # none does (a J-V-only tree does not care what the 33220A rests at).
    # The bench card (rigs.chain) always warns because it describes the
    # bench, not a tree; the contract records the difference.
    transient = bool(f.transients)
    level = "warn" if transient or f.schedule is None else "info"
    if value == "?":
        # Unreadable is as unproven as NORM for a transient: nothing says
        # which edge the Sync's rising edge means.
        return [_v(level, "chain.led-polarity",
                   "33220A :OUTP:POL unreadable"
                   + (": a transient in this tree arms on the Sync edge, and an "
                      "unreadable polarity is as unproven as NORM" if transient else ""),
                   value=value, expected="INV", fix=fix)]
    return [_v(level, "chain.led-polarity",
               f"33220A :OUTP:POL reads {value}: the Sync's rising edge then means light "
               "ON, so extraction would happen during illumination"
               + ("" if transient else " (no transient in this tree uses it)"),
               value=value, expected="INV", fix=fix)]


def _c_chain_bias_arm(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("chain.bias-arm")
    wants = [s for s in f.transients if bool(s.values().get("external_trigger", True))]
    if not wants:
        return [_v("ok", "chain.bias-arm", "no module arms the 81150A externally")]
    if f.bench is None:
        return _chain_not_read("chain.bias-arm")
    src, slope = f.chain_item("bias_arm"), f.chain_item("bias_arm_slope")
    if src is None or slope is None:
        return [_v("info", "chain.bias-arm", "81150A arming not in the read-back")]
    want_pos = bool(wants[0].values().get("trigger_slope_positive", True))
    exp_slope = "POS" if want_pos else "NEG"
    got_src, got_slope = str(src.get("value", "?")).upper(), str(slope.get("value", "?")).upper()
    fix = src.get("fix") or "arm-81150a-ext"
    if got_src.startswith("EXT") and got_slope.startswith(exp_slope):
        return [_v("ok", "chain.bias-arm", f"81150A armed {got_src} / {got_slope}",
                   arm_source=got_src, arm_slope=got_slope)]
    return [_v("warn", "chain.bias-arm",
               f"81150A reads ARM {got_src} / SLOP {got_slope}; the recipe wants EXT / "
               f"{exp_slope} and writes it at run start, so this is what the bench holds "
               "now, not what the run will use",
               arm_source=got_src, arm_slope=got_slope, expected_source="EXT",
               expected_slope=exp_slope, fix=fix)]


def _c_chain_bias_polarity(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("chain.bias-polarity")
    if not f.transients:
        return [_v("ok", "chain.bias-polarity", "no transient in this tree")]
    v = f.transients[0].values()
    try:
        instruction = RunConfig(output_polarity=str(v.get("output_polarity", "auto")),
                                inverted_output=bool(v.get("inverted_output", False))
                                ).polarity_instruction()
    except ValueError as exc:
        return [_v("info", "chain.bias-polarity", f"recipe polarity unreadable: {exc}")]
    if f.bench is None:
        return _chain_not_read("chain.bias-polarity")
    item = f.chain_item("bias_polarity")
    value = str(item.get("value", "?")).upper() if item else "?"
    if instruction is None:
        return [_v("info", "chain.bias-polarity",
                   f"the recipe leaves :OUTP1:POL as found; the 81150A reads {value}",
                   value=value, expected="leave")]
    expected = "INV" if instruction else "NORM"
    if value.startswith(expected):
        return [_v("ok", "chain.bias-polarity", f"81150A :OUTP1:POL {value}",
                   value=value, expected=expected)]
    if value == "?":
        # As the bench card reports it: the 81150A tells its polarity once
        # a run configures the shape, and this run writes `expected` then.
        return [_v("info", "chain.bias-polarity",
                   f"81150A :OUTP1:POL not read yet; the run writes {expected} at start",
                   value=value, expected=expected)]
    return [_v("warn", "chain.bias-polarity",
               f"81150A :OUTP1:POL reads {value}; the recipe writes {expected} at run "
               "start (which level the device rests at between pulses)",
               value=value, expected=expected)]


def _temperature_loops(f: _Facts) -> list[tuple[Loop, tuple[Loop, ...]]]:
    if f.tree is None:
        return []
    return [(n, a) for n, a in walk(f.tree)
            if isinstance(n, Loop) and n.loop == "temperature"]


def _c_temperature_not_wired(f: _Facts) -> list[Verdict]:
    """Which path every temperature node of this tree takes. `ok` when the
    read-back showed the 331 console attached and its instrument answering
    (the node settles through it); `warn` when there is no controller (the
    run pauses at each temperature for a manual set), when the console is
    named but was silent at start-up, when it answered at start-up and does
    not now (the read raised: start the console), when it is up with the
    instrument silent behind it, or when a controller is attached and not
    read back yet -- each pauses, and the text says which. The run makes
    the same decision at the node from a fresh read, so a console that
    answers again by then settles after all."""
    if f.tree is None:
        return _skipped("temperature.not-wired")
    loops = _temperature_loops(f)
    if not loops:
        return [_v("ok", "temperature.not-wired", "no temperature loop")]
    setpoints = [float(v) for n, _ in loops for v in n.values]
    n = len(setpoints)
    where = (f"each of the {n} temperature{'s' if n != 1 else ''} "
             f"({min(setpoints):g}-{max(setpoints):g} K)")
    console = f.rig.temperature_console
    path = f.first_path(loops[0][0].key)
    t = f.instrument("temperature")
    # Where the 331 is, in words a verdict can end with. An empty `console` is
    # the normal case now -- this process owns the instrument -- so it must
    # never be rendered as a blank URL.
    at = console or f.rig.temperature_address or "no address"
    data: dict[str, Any] = {"setpoints": setpoints, "console": console, "at": at,
                            "wired": bool(t.get("wired", False)) if f.bench else None,
                            "connected": t.get("connected") if f.bench else None}
    what = {"simulated": "simulated 331", "console": "331 console",
            "instrument": "331"}.get(t.get("source"), "331")
    if f.bench is not None and t.get("wired") and t.get("connected") is True:
        params = {k: loops[0][0].params.get(k, d) for k, d in TEMPERATURE_DEFAULTS.items()}
        text = (f"{what} attached: settles automatically at {where}, "
                f"+/-{params['tolerance_k']:g} K, hold {fmt_duration(params['hold_s'])}, "
                f"timeout {fmt_duration(params['timeout_s'])}; the 350 K ceiling "
                "and the heater range are read, never driven")
        return [_v("ok", "temperature.not-wired", text, path, **data,
                   tolerance_k=params["tolerance_k"], hold_s=params["hold_s"],
                   timeout_s=params["timeout_s"])]
    if f.bench is not None and t.get("wired") and t.get("error"):
        text = (f"{what} attached at Start and not answering now -- {at} did not "
                f"answer the read-back ({t['error']}): the run pauses at {where} for "
                "the operator unless it answers again by then")
        return [_v("warn", "temperature.not-wired", text, path, **data, error=t.get("error"))]
    if f.bench is not None and t.get("wired") and t.get("connected") is None:
        text = (f"{what} attached and not read back yet: the run pauses at {where} "
                "for the operator unless it answers at the node")
        return [_v("warn", "temperature.not-wired", text, path, **data)]
    if f.bench is not None and t.get("wired"):
        text = (f"{what} attached but silent -- {at} is reachable and the "
                f"instrument is not answering: the run pauses at {where} for "
                "the operator")
        return [_v("warn", "temperature.not-wired", text, path, **data,
                   status=t.get("status_text"))]
    text = f"331 not wired: the run pauses at {where} for a manual set"
    if f.bench is None:
        text += (f"; {at} is not read back yet -- settles through it if it "
                 "answers at Start")
    else:
        text += (f"; {at} is not answering"
                 + (" (start the 331 console)" if console else ""))
        reason = (f.bench.get("unavailable") or {}).get("temperature")
        if reason:
            data["reason"] = reason
    return [_v("warn", "temperature.not-wired", text, path, **data)]


def _c_temperature_inside_illumination(f: _Facts) -> list[Verdict]:
    if f.tree is None:
        return _skipped("temperature.inside-illumination")
    out = []
    for node, ancestors in _temperature_loops(f):
        outer = [a for a in ancestors if a.loop == "illumination"]
        if not outer:
            continue
        n_levels = math.prod(len(a.values) for a in outer)
        n_t = len(node.values)
        settles, instead = n_t * n_levels, n_t
        known = [s for s in (settle_s(f.history, float(v)) for v in node.values)
                 if s is not None]
        text = (f"temperature loop ({n_t} setpoint{'s' if n_t != 1 else ''}) nested "
                f"inside the illumination loop ({n_levels} level"
                f"{'s' if n_levels != 1 else ''}): the cryostat settles {settles} times "
                f"instead of {instead}")
        extra = None
        if known:
            med = statistics.median(known)
            extra = med * (settles - instead)
            text += (f"; at the median settle of {fmt_duration(med)} that is "
                     f"{fmt_duration(extra)} more waiting")
        else:
            text += "; no settle history, so the cost in hours is unknown"
        out.append(_v("warn", "temperature.inside-illumination", text,
                      f.first_path(node.key), settles=settles, instead_of=instead,
                      extra_s=extra, node_key=node.key))
    if out:
        return out
    return [_v("ok", "temperature.inside-illumination",
               "no temperature loop sits inside an illumination loop")]


def _c_trigger_auto(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("trigger.auto")
    auto = [s for s in f.transients
            if str(s.values().get("trigger_sweep", "AUTO")).upper() == "AUTO"]
    if auto:
        return [_v("info", "trigger.auto",
                   f"trigger_sweep = AUTO on {len(auto)} scan{'s' if len(auto) != 1 else ''}: "
                   "the scope sweeps without a trigger, so a loose Sync cable gives "
                   "untriggered noise that subtracts to a plausible charge",
                   auto[0].node_path, scans=len(auto))]
    if not f.transients:
        return [_v("ok", "trigger.auto", "no transient in this tree")]
    return [_v("ok", "trigger.auto", "trigger_sweep = TRIG: a missing trigger times out")]


def _reads_intensity(f: _Facts) -> int:
    n = sum(1 for s in f.transients if bool(s.values().get("read_intensity", False)))
    n += sum(1 for s in f.modules if s.module == "power")
    return n


def _power_available(f: _Facts) -> bool | None:
    if f.power_available is not None:
        return bool(f.power_available)
    if f.bench is None:
        return None
    state = f.instrument("power").get("available")
    return None if state is None else bool(state)


def _c_power_console(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("power.console")
    n = _reads_intensity(f)
    if not n:
        return [_v("ok", "power.console", "no module reads the power meter")]
    available = _power_available(f)
    console = f.rig.power_meter_console
    # As for the 331: with no console named this process owns the meter, so
    # the verdict must name the meter and not an empty URL.
    at = console or "the 1918-C on USB"
    if available is None:
        return [_v("info", "power.console", f"{at} not read yet",
                   console=console, at=at, readers=n)]
    if available:
        watts = f.instrument("power").get("watts")
        return [_v("ok", "power.console",
                   f"{at} answering"
                   + (f" ({watts:.3e} W)" if isinstance(watts, (int, float)) else ""),
                   console=console, at=at, readers=n, watts=watts)]
    return [_v("warn", "power.console",
               f"{at} not answering: intensity will be NaN on {n} "
               f"reader{'s' if n != 1 else ''}", console=console, at=at, readers=n)]


def _c_intensity_factor(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("intensity.factor")
    n = _reads_intensity(f)
    if not n:
        return [_v("ok", "intensity.factor", "no intensity is read")]
    # No module carries a calibration factor: the original's "Factor" is not
    # recoverable from the binaries (`SeriesConfig.intensity_factor_mw_cm2_per_w`
    # says so), so the service records watts at the meter and says so here
    # rather than pretending to look for a number nothing can supply.
    return [_v("info", "intensity.factor",
               "no calibration factor: intensity is recorded in watts at the meter, "
               f"not as irradiance at the sample ({n} reader{'s' if n != 1 else ''})",
               readers=n)]


def _c_cost_long(f: _Facts) -> list[Verdict]:
    if f.schedule is None:
        return _skipped("cost.long")
    total = float(f.cost.get("total_s") or 0.0)
    nbytes = 0.0
    for s in f.transients:
        v = s.values()
        if bool(v.get("store_shots", False)):
            # `RunRecorder` keeps one float64 photocurrent record per shot.
            nbytes += int(s.detail.get("shots") or 0) * int(v.get("record_length", 5000)) * 8
    reasons = []
    if total > LONG_RUN_S:
        reasons.append(f"estimated {fmt_duration(total)}"
                       + (" at least" if f.cost.get("lower_bound") else "")
                       + f", over {LONG_RUN_S / 3600:g} h")
    if nbytes > STORE_SHOTS_BYTES:
        reasons.append(f"store_shots keeps about {nbytes / 1e9:.1f} GB of per-shot traces")
    data = {"total_s": total, "lower_bound": bool(f.cost.get("lower_bound")),
            "store_shots_bytes": nbytes}
    if reasons:
        return [_v("warn", "cost.long", "; ".join(reasons), **data)]
    return [_v("ok", "cost.long",
               f"about {fmt_duration(total)}" + (" at least" if f.cost.get("lower_bound") else "")
               + (f"; {nbytes / 1e6:.0f} MB of per-shot traces" if nbytes else ""), **data)]


def _c_chain_stale(f: _Facts) -> list[Verdict]:
    if f.bench is None:
        return _chain_not_read("chain.stale")
    read_at = (f.bench.get("chain") or {}).get("read_at") or f.bench.get("read_at")
    if not isinstance(read_at, (int, float)):
        return [_v("info", "chain.stale", "no chain read-back yet; it is read at Start")]
    age = f.now - float(read_at)
    if age > CHAIN_STALE_S:
        return [_v("info", "chain.stale",
                   f"chain read-back is {fmt_duration(age)} old; it is re-read at Start",
                   read_at=read_at, age_s=age)]
    return [_v("ok", "chain.stale", f"chain read back {fmt_duration(max(age, 0.0))} ago",
               read_at=read_at, age_s=age)]


_CHECKS: dict[str, Callable[[_Facts], list[Verdict]]] = {
    "tree.shape": _c_tree_shape,
    "tree.owned-param": _c_owned_param,
    "voc.source": _c_voc_source,
    "voc.coupling": _c_voc_coupling,
    "led.levels": _c_led_levels,
    "axis.geometry": _c_axis_geometry,
    "bench.instrument": _c_bench_instrument,
    "smu.ceiling": _c_smu_ceiling,
    "relay.interlock": _c_relay_interlock,
    "bench.live-at-start": _c_live_at_start,
    "voc.typed": _c_voc_typed,
    "chain.led-polarity": _c_chain_led_polarity,
    "chain.bias-arm": _c_chain_bias_arm,
    "chain.bias-polarity": _c_chain_bias_polarity,
    "temperature.not-wired": _c_temperature_not_wired,
    "temperature.inside-illumination": _c_temperature_inside_illumination,
    "trigger.auto": _c_trigger_auto,
    "power.console": _c_power_console,
    "intensity.factor": _c_intensity_factor,
    "cost.long": _c_cost_long,
    "chain.stale": _c_chain_stale,
}
assert tuple(_CHECKS) == CHECKS
