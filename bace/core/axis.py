"""The swept axis, as a parameter.

One shot is `(V_pre, V_coll, t_delay, illumination) -> transient`. A run is
that shot repeated along **one** axis while the other two are pinned. Which
axis is swept *is* the choice of experiment:

    Axis("vpre",     ...)   carrier density vs bias   -- BACE
    Axis("delay_ns", ...)   recombination kinetics    -- TDCF
    Axis("vcoll",    ...)   field dependence of extraction

The original had this generalisation half-built: `calcLoopParameters` computes
`Vcoll1Loop` and `Delay1Loop` as per-step arrays, one entry per sweep point,
and then fills both with a constant (`Vpre1Loop*0 + Vcoll`). No VI in the
folder ever gave them a value. Making the axis a parameter finishes that work
rather than adding a feature.

**Repeats are loops, not axis points.** "Q at V_oc, three times" is a
zero-width axis with three loops, not a three-point sweep around V_oc. The
original did the latter, and averaging three points that straddle V_oc buys a
curvature bias in Q(V) for nothing. Both spellings are available here and they
run the same code path; the zero-width one is the honest way to say "repeat".

Nothing in this module knows about instruments.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterator, Literal, Mapping

import numpy as np

AxisName = Literal["vpre", "vcoll", "delay_ns"]

UNITS: dict[str, str] = {"vpre": "V", "vcoll": "V", "delay_ns": "ns"}



def voc_flags(values: Mapping[str, Any]) -> tuple[bool, bool]:
    """`(centre_on_voc, vpre_on_voc)` **as they apply** to `axis_name`.

    The two are one decision, and the axis decides which: `centre_on_voc`
    belongs to a swept `vpre`, `vpre_on_voc` to a pinned one. The console
    shows whichever applies and hides the other -- so a flag left `true`
    behind a hidden field, after the axis was switched, is not a request. It
    was refused as one (`Axis` raised on `centre_on_voc` under `delay_ns`),
    which disabled Shot and Scan with no field on screen to clear. Read the
    flags through this and the stale one is simply inert.
    """
    swept_vpre = values.get("axis_name") == "vpre"
    centre = bool(values.get("centre_on_voc", False)) and swept_vpre
    pinned = bool(values.get("vpre_on_voc", False)) and not swept_vpre
    return centre, pinned

class AxisError(ValueError):
    pass


@dataclass(frozen=True)
class Axis:
    """A one-dimensional sweep specification.

    `step` is the *spacing*; the point count follows from the span. A zero-width
    axis (`start == stop`) is one point and ignores `step`.

    `centre_on_voc` makes `start`/`stop` offsets relative to a V_oc that is not
    known until it has been measured under the illumination in force. Only
    meaningful for `vpre`.
    """

    name: AxisName
    start: float
    stop: float
    step: float = 0.0
    centre_on_voc: bool = False

    def __post_init__(self) -> None:
        if self.name not in ("vpre", "vcoll", "delay_ns"):
            raise AxisError(f"unknown axis {self.name!r}")
        if self.centre_on_voc and self.name != "vpre":
            raise AxisError(
                f"centre_on_voc is only meaningful for the prebias axis; "
                f"{self.name!r} has nothing to do with V_oc"
            )
        if self.start != self.stop and self.step <= 0:
            raise AxisError(
                f"axis {self.name!r} spans {self.start} to {self.stop} but has "
                f"step {self.step} — a non-zero span needs a positive step"
            )

    # -- geometry ---------------------------------------------------------
    @property
    def is_point(self) -> bool:
        """True if this axis is a single setpoint — i.e. a repeat, not a sweep."""
        return self.start == self.stop

    @property
    def n_points(self) -> int:
        if self.is_point:
            return 1
        return int(round(abs(self.stop - self.start) / self.step)) + 1

    @property
    def unit(self) -> str:
        return UNITS[self.name]

    def resolve(self, voc: float | None = None) -> "Axis":
        """Return this axis with `centre_on_voc` folded into absolute limits.

        Raises if the axis needs a V_oc and none was supplied — the failure that
        would otherwise centre a whole sweep on zero and produce a plausible,
        wrong Q(V).
        """
        if not self.centre_on_voc:
            return self
        if voc is None:
            raise AxisError(
                f"axis {self.name!r} is centred on V_oc but no V_oc was given. "
                "V_oc must be measured under the illumination in force before "
                "the axis is built."
            )
        return replace(self, start=self.start + voc, stop=self.stop + voc,
                       centre_on_voc=False)

    def values(self, voc: float | None = None) -> np.ndarray:
        """The setpoints along this axis, in sweep order.

        The point count is rounded, not truncated. LabVIEW's
        `N = abs(a-b)/dV + 1` truncated, and floating-point V_oc values push
        that ratio just under the integer often enough to matter (23 % of V_oc
        values at dV = 5 mV), silently dropping the last point.
        """
        a = self.resolve(voc)
        if a.is_point:
            return np.array([a.start], dtype=float)
        return np.linspace(a.start, a.stop, a.n_points)

    def __str__(self) -> str:
        tag = " (centred on V_oc)" if self.centre_on_voc else ""
        if self.is_point:
            return f"{self.name} = {self.start:g} {self.unit}{tag}"
        return (f"{self.name}: {self.start:g} -> {self.stop:g} step {self.step:g} "
                f"{self.unit} ({self.n_points} pts){tag}")


@dataclass(frozen=True)
class Setpoint:
    """One shot's worth of bias conditions. All three always have a value."""

    vpre: float          # V at the device
    vcoll: float         # V at the device
    delay_ns: float      # ns after the LED falling edge, before the offset

    def as_dict(self) -> dict[str, float]:
        return {"vpre": self.vpre, "vcoll": self.vcoll, "delay_ns": self.delay_ns}


@dataclass(frozen=True)
class ScanStep:
    """A setpoint together with where it sits in the run."""

    index: int           # 0-based position in the flattened run
    loop: int            # 1-based loop number
    step: int            # 1-based position within one loop
    setpoint: Setpoint


@dataclass(frozen=True)
class ScanPlan:
    """A resolved run: the axis values, the pinned conditions, and the loops.

    Flattened loop-major, matching the original engine: all steps of loop 1,
    then all steps of loop 2, and so on. That ordering is what lets a running
    average update after every single acquisition instead of at the end.
    """

    axis: Axis                 # already resolved (no centre_on_voc)
    values: np.ndarray         # (n_steps,) the swept quantity
    setpoints: tuple[Setpoint, ...]   # (n_steps,) one loop's worth
    n_loops: int

    @property
    def n_steps(self) -> int:
        return len(self.setpoints)

    @property
    def n_shots(self) -> int:
        return self.n_steps * self.n_loops

    def __iter__(self) -> Iterator[ScanStep]:
        i = 0
        for loop in range(1, self.n_loops + 1):
            for step, sp in enumerate(self.setpoints, start=1):
                yield ScanStep(index=i, loop=loop, step=step, setpoint=sp)
                i += 1

    def value_of(self, step: int) -> float:
        """The swept quantity at 1-based position `step` within a loop."""
        return float(self.values[step - 1])

    def describe(self) -> str:
        s = self.setpoints[0]
        pinned = [f"{k} = {v:g}" for k, v in s.as_dict().items() if k != self.axis.name]
        return (f"{self.axis} x {self.n_loops} loop(s) = {self.n_shots} shots; "
                f"pinned: {', '.join(pinned)}")


@dataclass(frozen=True)
class ScanSpec:
    """What the user asks for: one axis, the pinned values, and a loop count.

    The pinned value for the swept quantity is ignored — the axis supplies it.
    """

    axis: Axis
    vpre: float = 0.0
    vcoll: float = -1.0
    delay_ns: float = 90.0
    n_loops: int = 1

    def __post_init__(self) -> None:
        if self.n_loops < 1:
            raise AxisError("n_loops must be >= 1")

    def plan(self, voc: float | None = None) -> ScanPlan:
        axis = self.axis.resolve(voc)
        values = axis.values()
        pinned = {"vpre": self.vpre, "vcoll": self.vcoll, "delay_ns": self.delay_ns}
        setpoints = []
        for v in values:
            d = dict(pinned)
            d[axis.name] = float(v)
            setpoints.append(Setpoint(**d))
        return ScanPlan(axis=axis, values=values,
                        setpoints=tuple(setpoints), n_loops=self.n_loops)


# -- ready-made axes ------------------------------------------------------
def bace_at_voc(n_loops: int = 3) -> ScanSpec:
    """Q at V_oc, repeated. The zero-width axis: no curvature bias."""
    return ScanSpec(axis=Axis("vpre", 0.0, 0.0, centre_on_voc=True), n_loops=n_loops)


def bace_legacy_three_point(dv: float = 0.010, n_loops: int = 1) -> ScanSpec:
    """The original's three prebias points straddling V_oc.

    Kept so archived runs can be reproduced exactly. For new work prefer
    `bace_at_voc`, which repeats at V_oc instead of averaging across it.
    """
    return ScanSpec(axis=Axis("vpre", -dv, +dv, dv, centre_on_voc=True),
                    n_loops=n_loops)


def bace_sweep(start: float, stop: float, step: float, n_loops: int = 1) -> ScanSpec:
    """Q(V) over an absolute prebias range — e.g. 0 -> 1 V step 20 mV."""
    return ScanSpec(axis=Axis("vpre", start, stop, step), n_loops=n_loops)


def tdcf_delay(start_ns: float, stop_ns: float, step_ns: float,
               vpre: float, n_loops: int = 1) -> ScanSpec:
    """Charge vs delay after the LED falling edge."""
    return ScanSpec(axis=Axis("delay_ns", start_ns, stop_ns, step_ns),
                    vpre=vpre, n_loops=n_loops)


def field_dependence(start: float, stop: float, step: float,
                     vpre: float, n_loops: int = 1) -> ScanSpec:
    """Charge vs collection field.

    Note this axis changes the *swing* from point to point, so the dark
    reference changes with it — which is correct, and is why the dark trace is
    reacquired at every step rather than once per run.
    """
    return ScanSpec(axis=Axis("vcoll", start, stop, step), vpre=vpre, n_loops=n_loops)
