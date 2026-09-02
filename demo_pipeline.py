#!/usr/bin/env python3
"""End-to-end dry run of the BACE measurement core — no hardware required.

Builds a prebias plan the way the LabVIEW program does, maps each step to
81150A pulse levels, generates synthetic light/dark traces, and runs the full
dark-subtraction / baseline / running-average / charge-integration chain.

    python demo_pipeline.py
"""
import numpy as np

from bace.core.sequence import build_plan
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
    plan = build_plan(VOC - DV, VOC + DV, DV, VCOLL, DELAY_NS, N_LOOPS)
    print(f"prebias grid      {np.round(plan.vpre_step, 6)}  V")
    print(f"loops x steps     {plan.n_loops} x {plan.n_steps} = {plan.n_all_steps} acquisitions")
    print(f"trigger offset    {TRIGGER_OFFSET_S * 1e9:g} ns\n")

    print("81150A levels per prebias point (volts at the generator output)")
    print(f"  {'Vpre':>9} {'hi_light':>9} {'lo_light':>9} {'hi_dark':>9} {'lo_dark':>9} {'delay/ns':>9}")
    for v in plan.vpre_step:
        p = pulse_levels(v, plan.vcoll, PULSE_AMP, plan.delay_ns, WIDTH_NS)
        print(f"  {v:9.6f} {p.high_light:9.5f} {p.low_light:9.5f} "
              f"{p.high_dark:9.5f} {p.low_dark:9.5f} {p.delay_s * 1e9:9.2f}")

    rng = np.random.default_rng(20260831)
    avg = RunningAverage(plan.n_steps, 2500)
    acc = ChargeAccumulator(plan.n_loops, plan.n_steps)

    for k in range(plan.n_all_steps):
        loop, step = int(plan.loop_index[k]), int(plan.step_index[k])
        light_v, dark_v, dt, _intensity = synthetic_traces(rng=rng)
        light = to_current(light_v, RESISTOR, PROBE_ATT)
        dark = to_current(dark_v, RESISTOR, PROBE_ATT)
        photo = photocurrent(light, dark, dt, offset_correct=True)
        avg.update(step, loop, photo)
        acc.add(loop, step, charge(photo, dt, T0_INT))

    mean_q, std_q = acc.summary()
    print("\nextracted charge")
    print(f"  {'Vpre / V':>9} {'mean Q / C':>13} {'std Q / C':>13}")
    for v, m, s in zip(plan.vpre_step, mean_q, std_q):
        print(f"  {v:9.6f} {m:13.6e} {s:13.6e}")

    dq_dv = np.gradient(mean_q, plan.vpre_step)
    print(f"\ndifferential capacitance dQ/dVpre at Voc:  {dq_dv[len(dq_dv) // 2]:.4e} F")
    print("(synthetic data — the number is meaningless, the plumbing is not)")


if __name__ == "__main__":
    main()
