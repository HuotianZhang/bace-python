"""Experiment plans: what an agent may run, as approved by a person.

A plan states a goal and an envelope: the modules an agent may run, the
parameters it may set in a submitted tree and their bounds, the loops it may
use and their values, and caps on the number of runs, the estimated time and
the plan's age. A person approves the plan in the console; the service then
checks every submission that names the plan against the envelope and refuses
what lies outside it. Inside the envelope no further confirmation is asked.

The envelope bounds what the submitted tree states. A parameter the tree
does not set keeps the value its layers give it (run.toml, the last run, the
operator's edits), which the agent cannot change; every check of the
pipeline catalogue, the interlocks and park apply as for any run.

Plans live in the session's memory. A restart forgets them, so an approval
never outlives the process it was given to. Proposals and decisions are
journaled with the plan's fingerprint.

The contract is `docs/service-contract.md`, section 8b.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .pipeline import LOOPS, Loop, Module, Node, walk

STATES: tuple[str, ...] = ("proposed", "approved", "rejected", "closed")
"""A plan is proposed, then approved or rejected; an approved plan is closed by
a person or the agent, or ends when it expires or its budget is spent."""

MAX_EXPIRES_S = 7 * 24 * 3600.0
"""The longest an approval may last."""


PLAN_CHECK: tuple[str, str, str, str] = (
    "plan.envelope", "invalid",
    "A run submitted under a plan sets a module, parameter or loop value outside the "
    "plan's envelope, or the plan is not approved, has expired, or has spent its runs "
    "or its time.",
    "Submit within the plan, or propose a plan that covers the run and have a person "
    "approve it.")
"""The submission check of a plan, as manual 4.2 lists it (`tools/build_manual.py`)."""


class PlanError(ValueError):
    """A plan that cannot be accepted as written (422)."""


class PlanConflict(RuntimeError):
    """A plan in a state that refuses the request (409)."""


def _bound(where: str, spec: Any) -> dict[str, Any]:
    """`{"min": a, "max": b}` with either side optional, or `{"choices": [...]}`."""
    if not isinstance(spec, Mapping) or not spec:
        raise PlanError(f"{where}: a bound is {{\"min\": …, \"max\": …}} or "
                        f"{{\"choices\": [...]}}, not {spec!r}")
    unknown = set(spec) - {"min", "max", "choices"}
    if unknown:
        raise PlanError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
    if "choices" in spec:
        if set(spec) != {"choices"} or not isinstance(spec["choices"], list) \
                or not spec["choices"]:
            raise PlanError(f"{where}: choices is a non-empty list, on its own")
        return {"choices": list(spec["choices"])}
    out: dict[str, Any] = {}
    for side in ("min", "max"):
        if side in spec:
            value = spec[side]
            if isinstance(value, bool) or not isinstance(value, (int, float))                     or not math.isfinite(value):
                raise PlanError(f"{where}: {side} is a finite number, not {value!r}")
            out[side] = float(value)
    if "min" in out and "max" in out and out["min"] > out["max"]:
        raise PlanError(f"{where}: min {out['min']:g} is above max {out['max']:g}")
    return out


def _within(value: Any, bound: Mapping[str, Any]) -> bool:
    if "choices" in bound:
        return value in bound["choices"]
    values = value if isinstance(value, (list, tuple)) else [value]
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
        if "min" in bound and v < bound["min"]:
            return False
        if "max" in bound and v > bound["max"]:
            return False
    return True


def _describe(bound: Mapping[str, Any]) -> str:
    if "choices" in bound:
        return "one of " + ", ".join(repr(c) for c in bound["choices"])
    lo, hi = bound.get("min"), bound.get("max")
    if lo is not None and hi is not None:
        return f"between {lo:g} and {hi:g}"
    return f"at least {lo:g}" if lo is not None else f"at most {hi:g}"


@dataclass(frozen=True)
class Envelope:
    """What an agent may submit under a plan.

    `modules` maps each allowed module to the parameters a module node may set
    and their bounds; `loops` maps each allowed loop to the loop values and
    loop parameters it may carry. `max_runs` and `max_total_s` cap the
    submissions and their summed cost estimate; `expires_s` is how long the
    approval lasts.
    """

    modules: dict[str, dict[str, dict[str, Any]]]
    loops: dict[str, dict[str, dict[str, Any]]]
    max_runs: int
    max_total_s: float
    expires_s: float

    @classmethod
    def from_wire(cls, data: Any, *, catalogue: Any = None) -> "Envelope":
        if not isinstance(data, Mapping):
            raise PlanError("envelope: an object")
        unknown = set(data) - {"modules", "loops", "max_runs", "max_total_s", "expires_s"}
        if unknown:
            raise PlanError(f"envelope: unknown key(s) {', '.join(sorted(unknown))}")
        modules_in = data.get("modules")
        if not isinstance(modules_in, Mapping) or not modules_in:
            raise PlanError("envelope.modules: at least one module")
        known = set(catalogue.names()) if catalogue is not None else None
        modules: dict[str, dict[str, dict[str, Any]]] = {}
        for name, params in modules_in.items():
            if known is not None and name not in known:
                raise PlanError(f"envelope.modules: no module {name!r}")
            if not isinstance(params, Mapping):
                raise PlanError(f"envelope.modules.{name}: an object of bounds")
            specs = ({s.name for s in catalogue.param_specs(name)}
                     if catalogue is not None else None)
            modules[name] = {}
            for p, spec in params.items():
                if specs is not None and p not in specs:
                    raise PlanError(f"envelope.modules.{name}: {name} has no parameter {p!r}")
                modules[name][p] = _bound(f"envelope.modules.{name}.{p}", spec)
        loops_in = data.get("loops") or {}
        if not isinstance(loops_in, Mapping):
            raise PlanError("envelope.loops: an object")
        loops: dict[str, dict[str, dict[str, Any]]] = {}
        for loop, params in loops_in.items():
            if loop not in LOOPS:
                raise PlanError(f"envelope.loops: {loop!r} is not one of {', '.join(LOOPS)}")
            if not isinstance(params, Mapping):
                raise PlanError(f"envelope.loops.{loop}: an object of bounds")
            loops[loop] = {p: _bound(f"envelope.loops.{loop}.{p}", spec)
                           for p, spec in params.items()}
        try:
            max_runs = int(data.get("max_runs", 1))
            max_total_s = float(data.get("max_total_s", 3600.0))
            expires_s = float(data.get("expires_s", 24 * 3600.0))
        except (TypeError, ValueError):
            raise PlanError("envelope: max_runs, max_total_s and expires_s are numbers") from None
        if not (math.isfinite(max_total_s) and math.isfinite(expires_s)):
            raise PlanError("envelope: max_total_s and expires_s are finite numbers")
        if max_runs < 1 or max_total_s <= 0 or not 0 < expires_s <= MAX_EXPIRES_S:
            raise PlanError(f"envelope: max_runs ≥ 1, max_total_s > 0 and expires_s in "
                            f"(0, {MAX_EXPIRES_S:g}] s")
        return cls(modules, loops, max_runs, max_total_s, expires_s)

    def as_wire(self) -> dict[str, Any]:
        return {"modules": self.modules, "loops": self.loops, "max_runs": self.max_runs,
                "max_total_s": self.max_total_s, "expires_s": self.expires_s}

    def violations(self, tree: Node) -> list[str]:
        """Every way `tree` lies outside the envelope; empty when it fits."""
        out: list[str] = []
        for node, _ in walk(tree):
            if isinstance(node, Module):
                allowed = self.modules.get(node.module)
                if allowed is None:
                    out.append(f"module {node.module} is not in the plan")
                    continue
                for p, value in node.params.items():
                    bound = allowed.get(p)
                    if bound is None:
                        out.append(f"{node.module}.{p} is set, and the plan does not "
                                   "let it be set")
                    elif not _within(value, bound):
                        out.append(f"{node.module}.{p} = {value!r}, the plan allows "
                                   f"{_describe(bound)}")
            elif isinstance(node, Loop):
                allowed = self.loops.get(node.loop)
                if allowed is None:
                    out.append(f"a {node.loop} loop is not in the plan")
                    continue
                key = {"temperature": "values_k", "illumination": "levels_v",
                       "repeat": "count"}[node.loop]
                bound = allowed.get(key)
                values = list(node.values)
                if bound is None:
                    out.append(f"{node.loop} loop: the plan sets no bound on {key}")
                elif not _within(values if node.loop != "repeat" else len(values), bound):
                    out.append(f"{node.loop} loop: {key} {values!r}, the plan allows "
                               f"{_describe(bound)}")
                for p, value in node.params.items():
                    b = allowed.get(p)
                    if b is None:
                        out.append(f"{node.loop} loop: {p} is set, and the plan does not "
                                   "let it be set")
                    elif not _within(value, b):
                        out.append(f"{node.loop} loop: {p} = {value!r}, the plan allows "
                                   f"{_describe(b)}")
        return out


@dataclass
class Plan:
    """One plan and what has been spent under it."""

    plan_id: str
    title: str
    goal: str
    envelope: Envelope
    proposed_at: float
    state: str = "proposed"
    fingerprint: str = ""
    decided_at: float | None = None
    reason: str = ""
    runs: list[str] = field(default_factory=list)
    spent_s: float = 0.0

    def __post_init__(self) -> None:
        if not self.fingerprint:
            self.fingerprint = fingerprint(self.title, self.goal, self.envelope)

    @property
    def expires_at(self) -> float | None:
        return None if self.decided_at is None or self.state != "approved" \
            else self.decided_at + self.envelope.expires_s

    def open_reason(self, now: float) -> str | None:
        """Why submissions under this plan are refused now, or None."""
        if self.state != "approved":
            return f"plan {self.plan_id} is {self.state}, not approved"
        if self.expires_at is not None and now > self.expires_at:
            return f"plan {self.plan_id} expired"
        if len(self.runs) >= self.envelope.max_runs:
            return f"plan {self.plan_id} has used its {self.envelope.max_runs} run(s)"
        return None

    def as_wire(self, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        return {"plan_id": self.plan_id, "title": self.title, "goal": self.goal,
                "envelope": self.envelope.as_wire(), "state": self.state,
                "fingerprint": self.fingerprint, "proposed_at": self.proposed_at,
                "decided_at": self.decided_at, "expires_at": self.expires_at,
                "reason": self.reason, "runs": list(self.runs),
                "spent_s": self.spent_s, "open": self.open_reason(now) is None}


def fingerprint(title: str, goal: str, envelope: Envelope) -> str:
    """The first 16 hex digits of the SHA-256 of the plan's canonical JSON: what
    an approval names, so an approval cannot apply to a different plan."""
    text = json.dumps({"title": title, "goal": goal, "envelope": envelope.as_wire()},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class PlanBook:
    """The session's plans, numbered within the session."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._plans: dict[str, Plan] = {}

    def propose(self, data: Any, *, catalogue: Any = None, now: float | None = None) -> Plan:
        if not isinstance(data, Mapping):
            raise PlanError("a plan is an object with title, goal and envelope")
        unknown = set(data) - {"title", "goal", "envelope"}
        if unknown:
            raise PlanError(f"plan: unknown key(s) {', '.join(sorted(unknown))}")
        title = str(data.get("title") or "").strip()
        goal = str(data.get("goal") or "").strip()
        if not title or not goal:
            raise PlanError("plan: title and goal are required")
        envelope = Envelope.from_wire(data.get("envelope"), catalogue=catalogue)
        plan_id = f"{self.session_id}-P{len(self._plans) + 1:02d}"
        plan = Plan(plan_id, title, goal, envelope,
                    proposed_at=time.time() if now is None else now)
        self._plans[plan_id] = plan
        return plan

    def get(self, plan_id: str) -> Plan:
        try:
            return self._plans[plan_id]
        except KeyError:
            raise KeyError(plan_id) from None

    def all(self) -> list[Plan]:
        return list(self._plans.values())

    def decide(self, plan_id: str, decision: str, *, fingerprint_seen: str = "",
               reason: str = "", now: float | None = None) -> Plan:
        """`approve` or `reject` a proposed plan, or `close` an approved one.

        Approval names the fingerprint the person saw; a different one is a
        conflict, so an approval cannot land on a plan other than the one shown.
        """
        plan = self.get(plan_id)
        now = time.time() if now is None else now
        if decision in ("approve", "reject"):
            if plan.state != "proposed":
                raise PlanConflict(f"plan {plan_id} is {plan.state}; only a proposed "
                                   "plan is approved or rejected")
            if decision == "approve" and fingerprint_seen != plan.fingerprint:
                raise PlanConflict(f"plan {plan_id} has fingerprint {plan.fingerprint}, "
                                   f"not {fingerprint_seen or '(none)'}: approve the plan "
                                   "as shown")
            plan.state = "approved" if decision == "approve" else "rejected"
        elif decision == "close":
            if plan.state != "approved":
                raise PlanConflict(f"plan {plan_id} is {plan.state}; only an approved "
                                   "plan is closed")
            plan.state = "closed"
        else:
            raise PlanError(f"decision: approve, reject or close, not {decision!r}")
        plan.decided_at = now if decision != "close" else plan.decided_at
        plan.reason = str(reason or "")
        return plan

    def check(self, plan_id: str, tree: Node, cost_s: float, *,
              now: float | None = None) -> list[str]:
        """Why a submission of `tree` under `plan_id` is refused; empty when it fits."""
        try:
            plan = self.get(plan_id)
        except KeyError:
            return [f"no plan {plan_id} in this session"]
        now = time.time() if now is None else now
        closed = plan.open_reason(now)
        if closed:
            return [closed]
        out = plan.envelope.violations(tree)
        if plan.spent_s + cost_s > plan.envelope.max_total_s:
            out.append(f"the estimated {cost_s:.0f} s would bring plan {plan_id} to "
                       f"{plan.spent_s + cost_s:.0f} s of its {plan.envelope.max_total_s:.0f} s")
        return out

    def charge(self, plan_id: str, run_id: str, cost_s: float) -> None:
        plan = self.get(plan_id)
        plan.runs.append(run_id)
        plan.spent_s += cost_s
