#!/usr/bin/env python3
"""End-to-end dry run of the BACE measurement core — no hardware required.

Builds the prebias plan the way the LabVIEW program does (three points
straddling V_oc, repeated over loops — `core.axis.bace_legacy_three_point`),
maps each step to 81150A pulse levels, generates synthetic light/dark traces,
and runs the full dark-subtraction / baseline / running-average /
charge-integration chain.

    python demo_pipeline.py
"""
from dataclasses import replace

import numpy as np

from bace.core.axis import bace_legacy_three_point
from bace.core.pulses import pulse_levels, TRIGGER_OFFSET_S
from bace.core.process import (photocurrent, charge, RunningAverage,
                               ChargeAccumulator, to_current)
from bace.core.simulate import synthetic_traces

# Settings taken from an archived run: 250610_164340_LED_Voc_Jsc_BACE Parameters.dat
VOC        = 0.720555     # V, measured on the Keithley
DV         = 0.010        # V, prebias half-span
VCOLL      = -2.5         # V
DELAY_NS   = 116.0
WIDTH_NS   = 5000.0
PULSE_AMP  = 4.0          # external amplifier gain
N_LOOPS    = 5
T0_INT     = 80e-9        # s, start of the charge integral
RESISTOR   = 5.192        # ohm, sense resistor
PROBE_ATT  = 1.0


def main() -> None:
    # The original's grid: V_oc - dV, V_oc, V_oc + dV. The point count is
    # rounded, not truncated as `calcLoopParameters` did, so a floating-point
    # V_oc cannot drop the middle point -- see `core.axis.Axis.values`.
    spec = replace(bace_legacy_three_point(dv=DV, n_loops=N_LOOPS),
                   vcoll=VCOLL, delay_ns=DELAY_NS)
    plan = spec.plan(VOC)
    print(f"prebias grid      {np.round(plan.values, 6)}  V")
    print(f"loops x steps     {plan.n_loops} x {plan.n_steps} = {plan.n_shots} acquisitions")
    print(f"trigger offset    {TRIGGER_OFFSET_S * 1e9:g} ns\n")

    print("81150A levels per prebias point (volts at the generator output)")
    print(f"  {'Vpre':>9} {'hi_light':>9} {'lo_light':>9} {'hi_dark':>9} {'lo_dark':>9} {'delay/ns':>9}")
    for sp in plan.setpoints:
        p = pulse_levels(sp.vpre, sp.vcoll, PULSE_AMP, sp.delay_ns, WIDTH_NS)
        print(f"  {sp.vpre:9.6f} {p.high_light:9.5f} {p.low_light:9.5f} "
              f"{p.high_dark:9.5f} {p.low_dark:9.5f} {p.delay_s * 1e9:9.2f}")

    rng = np.random.default_rng(20260831)
    avg = RunningAverage(plan.n_steps, 2500)
    acc = ChargeAccumulator(plan.n_loops, plan.n_steps)

    # Loop-major, as the original flattened it: every step of loop 1, then
    # every step of loop 2 -- which is what lets the running average update
    # after each acquisition rather than at the end.
    for s in plan:
        light_v, dark_v, dt, _intensity = synthetic_traces(rng=rng)
        light = to_current(light_v, RESISTOR, PROBE_ATT)
        dark = to_current(dark_v, RESISTOR, PROBE_ATT)
        photo = photocurrent(light, dark, dt, offset_correct=True)
        avg.update(s.step, s.loop, photo)
        acc.add(s.loop, s.step, charge(photo, dt, T0_INT))

    mean_q, std_q = acc.summary()
    print("\nextracted charge")
    print(f"  {'Vpre / V':>9} {'mean Q / C':>13} {'std Q / C':>13}")
    for v, m, sd in zip(plan.values, mean_q, std_q):
        print(f"  {v:9.6f} {m:13.6e} {sd:13.6e}")

    dq_dv = np.gradient(mean_q, plan.values)
    print(f"\ndifferential capacitance dQ/dVpre at Voc:  {dq_dv[len(dq_dv) // 2]:.4e} F")
    print("(synthetic data — the number is meaningless, the plumbing is not)")


if __name__ == "__main__":
    main()
