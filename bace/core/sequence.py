"""Prebias sweep planning.

Ported from the `calcLoopParameters` MathScript node of TDCF-BACE_20160223.vi:

    N          = abs(Vpre1-Vpre2)/dV + 1
    Vpre1Loop  = linspace(Vpre1, Vpre2, N)
    Vcoll1Loop = Vpre1Loop*0 + Vcoll
    Delay1Loop = Vpre1Loop*0 + Delay
    VpreAllLoops = reshape(repmat(Vpre1Loop, 1, NLoops), 1, [])
    LoopIndexAll = reshape(repmat(1:NLoops, NumStepsPerLoop, 1), 1, [])

In the BACE experiment the caller passes Vpre1 = Voc - dV and Vpre2 = Voc + dV,
which yields the three prebias points that become the Vpre1/2/3 and Q1/2/3
columns of the parameter file.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class SweepPlan:
    """One BACE acquisition plan, flattened over loops."""

    vpre_step: np.ndarray      # (n_steps,)      prebias grid, one loop
    vpre_all: np.ndarray       # (n_all_steps,)  prebias, repeated over loops
    loop_index: np.ndarray     # (n_all_steps,)  1-based loop number per step
    step_index: np.ndarray     # (n_all_steps,)  1-based position within the loop
    vcoll: float
    delay_ns: float

    @property
    def n_steps(self) -> int:
        return int(self.vpre_step.size)

    @property
    def n_loops(self) -> int:
        return int(self.loop_index.max()) if self.loop_index.size else 0

    @property
    def n_all_steps(self) -> int:
        return int(self.vpre_all.size)


def build_plan(vpre_start: float, vpre_end: float, dv: float, vcoll: float,
               delay_ns: float, n_loops: int) -> SweepPlan:
    """Build the prebias grid and its loop repetition.

    `dv` sets the *spacing*; the number of points follows from the span.

    Deliberate deviation from the original. LabVIEW computed
    `N = abs(Vpre1-Vpre2)/dV + 1` and truncated. With Vpre1 = Voc - dV and
    Vpre2 = Voc + dV, floating-point Voc values make that ratio land just below
    the integer — e.g. Voc = 0.720555, dV = 0.01 gives 1.99999999999999,
    truncating to N = 2 and silently dropping the middle prebias point. Here the
    ratio is rounded to nearest instead, so the intended grid always comes out.
    """
    if dv <= 0:
        raise ValueError("dv must be positive")
    if n_loops < 1:
        raise ValueError("n_loops must be >= 1")

    n = int(round(abs(vpre_start - vpre_end) / dv)) + 1
    vpre_step = np.linspace(vpre_start, vpre_end, n)

    vpre_all = np.tile(vpre_step, n_loops)
    loop_index = np.repeat(np.arange(1, n_loops + 1), n)
    step_index = np.tile(np.arange(1, n + 1), n_loops)

    return SweepPlan(
        vpre_step=vpre_step,
        vpre_all=vpre_all,
        loop_index=loop_index,
        step_index=step_index,
        vcoll=float(vcoll),
        delay_ns=float(delay_ns),
    )
