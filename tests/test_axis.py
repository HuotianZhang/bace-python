"""Axis geometry, and its agreement with the sweep planner it replaced.

`core.sequence.build_plan` was the first port of `calcLoopParameters` and the
one validated against the 2026-08-07 archive. It is gone -- one planner, not
two -- so the grid it produced is written out here and the general axis path
has to reproduce it exactly.
"""
from __future__ import annotations

import numpy as np
import pytest

from bace.core.axis import (Axis, AxisError, ScanSpec, bace_at_voc,
                            bace_legacy_three_point, bace_sweep, tdcf_delay)


def test_point_count_is_rounded_not_truncated():
    """The latent bug in the original, reproduced and fixed.

    LabVIEW computed N = |a-b|/dV + 1 and truncated. With V_pre centred on a
    floating-point V_oc the ratio lands just under the integer often enough to
    matter, silently dropping a point.
    """
    dv, voc = 0.01, 0.990001
    a = Axis("vpre", -dv, +dv, dv, centre_on_voc=True)
    assert a.resolve(voc).n_points == 3

    # what truncation would have given, computed the way LabVIEW did:
    lo, hi = voc - dv, voc + dv
    assert int(abs(lo - hi) / dv) + 1 == 2


def test_how_often_truncation_would_have_dropped_a_point():
    """How much the original's truncation actually mattered, measured.

    Over V_oc from 0.5 to 1.1 V at microvolt resolution, the fraction of
    values for which `int(|a-b|/dV) + 1` loses the last point:

        dV = 20 mV   1.7 %
        dV = 10 mV   0.8 %
        dV =  5 mV  17.1 %

    Not the disaster a first reading suggests at the dV values actually used,
    and sharply worse at 5 mV. Pinned here so the claim stays checkable.
    """
    def truncated(voc, dv):
        return int(abs((voc - dv) - (voc + dv)) / dv) + 1

    rates = {}
    for dv in (0.020, 0.010, 0.005):
        n = bad = 0
        for k in range(500_000, 1_100_001, 7):
            n += 1
            if truncated(k / 1e6, dv) < 3:
                bad += 1
        rates[dv] = 100.0 * bad / n
    assert rates[0.020] == pytest.approx(1.7, abs=0.4)
    assert rates[0.010] == pytest.approx(0.8, abs=0.3)
    assert rates[0.005] == pytest.approx(17.1, abs=1.0)


def test_zero_width_axis_is_one_point():
    a = Axis("vpre", 0.0, 0.0, centre_on_voc=True)
    assert a.is_point and a.n_points == 1
    assert a.values(0.906) == pytest.approx([0.906])


def test_repeats_come_from_loops_not_axis_points():
    plan = bace_at_voc(n_loops=3).plan(0.906)
    assert plan.n_steps == 1
    assert plan.n_loops == 3
    assert plan.n_shots == 3
    assert [s.loop for s in plan] == [1, 2, 3]


def test_descending_sweep_keeps_its_direction():
    v = Axis("vcoll", -0.5, -4.0, 0.5).values()
    assert v[0] == pytest.approx(-0.5) and v[-1] == pytest.approx(-4.0)
    assert np.all(np.diff(v) < 0)


def test_centre_on_voc_requires_a_voc():
    with pytest.raises(AxisError, match="no V_oc was given"):
        Axis("vpre", -0.01, 0.01, 0.01, centre_on_voc=True).values()


def test_centre_on_voc_only_makes_sense_for_vpre():
    with pytest.raises(AxisError, match="nothing to do with V_oc"):
        Axis("delay_ns", 0, 100, 10, centre_on_voc=True)


def test_span_without_step_is_rejected():
    with pytest.raises(AxisError, match="positive step"):
        Axis("vpre", 0.0, 1.0, 0.0)


def test_pinned_values_ride_along_unchanged():
    plan = tdcf_delay(10, 50, 10, vpre=0.9).plan()
    assert plan.n_steps == 5
    assert {s.setpoint.vpre for s in plan} == {0.9}
    assert [s.setpoint.delay_ns for s in plan] == [10, 20, 30, 40, 50]


def test_flattening_is_loop_major_like_the_original():
    plan = bace_sweep(0.0, 0.02, 0.01, n_loops=2).plan()
    order = [(s.loop, s.step) for s in plan]
    assert order == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)]


def test_reproduces_the_archive_validated_sweep_planner():
    """The grid `core.sequence.build_plan` produced for the 2026-08-07
    archive's parameters (V_oc 0.906, dV 1 mV, three loops), as that module
    computed it from `calcLoopParameters`:

        N          = round(|Vpre1 - Vpre2| / dV) + 1     (rounded, not truncated)
        Vpre1Loop  = linspace(Vpre1, Vpre2, N)
        VpreAll    = tile(Vpre1Loop, NLoops)
        LoopIndex  = repeat(1..NLoops, N)                  loop-major
        StepIndex  = tile(1..N, NLoops)

    The general axis path has to reproduce every one of those arrays. The
    rounding half of the guarantee is `test_point_count_is_rounded_not_truncated`
    above; this is the flattening half."""
    voc, dv, n_loops = 0.906, 0.001, 3
    plan = ScanSpec(axis=Axis("vpre", -dv, +dv, dv, centre_on_voc=True),
                    vcoll=-1.0, delay_ns=88.0, n_loops=n_loops).plan(voc)

    n = int(round(abs((voc - dv) - (voc + dv)) / dv)) + 1
    vpre_step = np.linspace(voc - dv, voc + dv, n)
    assert n == 3
    assert plan.n_steps == n
    assert plan.n_shots == n * n_loops
    np.testing.assert_allclose(plan.values, vpre_step)
    np.testing.assert_allclose(plan.values, [0.905, 0.906, 0.907], atol=1e-12)
    np.testing.assert_allclose([s.setpoint.vpre for s in plan],
                               np.tile(vpre_step, n_loops))
    assert [s.loop for s in plan] == list(np.repeat(np.arange(1, n_loops + 1), n))
    assert [s.step for s in plan] == list(np.tile(np.arange(1, n + 1), n_loops))
    # the pinned conditions ride along unchanged, as Vcoll1Loop / Delay1Loop did
    assert {(s.setpoint.vcoll, s.setpoint.delay_ns) for s in plan} == {(-1.0, 88.0)}


def test_legacy_three_point_helper_matches_the_archive():
    plan = bace_legacy_three_point(dv=0.001).plan(0.906)
    np.testing.assert_allclose(plan.values, [0.905, 0.906, 0.907], atol=1e-12)


def test_a_delay_before_the_trigger_is_refused_not_sent():
    """`:PULS:DEL1` is measured from the generator's own trigger, so a negative
    one is nonsense. It reaches the instrument as a rejected command, and
    `run_transient_scan` does not read the error queue — so those steps would
    keep the previous delay and draw a flat stretch in Q(delay) that reads as
    physics. A delay scan is exactly where someone reaches past the limit."""
    import pytest

    from bace.core.pulses import TRIGGER_OFFSET_S, pulse_levels

    ok = pulse_levels(0.92, -1.0, 4.0, -TRIGGER_OFFSET_S * 1e9 + 1.0, 5000.0)
    assert ok.delay_s > 0

    with pytest.raises(ValueError, match="before its own trigger"):
        pulse_levels(0.92, -1.0, 4.0, -TRIGGER_OFFSET_S * 1e9 - 1.0, 5000.0)
