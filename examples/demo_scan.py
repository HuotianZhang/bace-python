#!/usr/bin/env python3
"""Run the standalone transient measurement on simulated instruments.

No hardware, no VISA. Shows the whole chain: axis -> pulse levels -> light and
dark acquisition -> subtraction -> charge -> running statistics, driven by the
same event stream the service and the UI will consume.

    python -m examples.demo_scan                 # prebias sweep
    python -m examples.demo_scan voc             # Q at V_oc, repeated (zero-width axis)
    python -m examples.demo_scan delay           # TDCF: charge vs delay
    python -m examples.demo_scan field           # charge vs collection field
"""
from __future__ import annotations

import sys

from bace.core.axis import bace_at_voc, bace_sweep, field_dependence, tdcf_delay
from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig, run_transient_scan

LED_DRIVE = 1.020


def main(which: str = "sweep") -> int:
    sim = make_bench(seed=7)
    sim.led.set_pulse(LED_DRIVE, 0.4)

    # V_oc must be measured under the illumination that is in force. On the real
    # rig this happens through the relay on the SourceMeter; here it is the same
    # call on the simulated one.
    with sim.router.dc():
        sim.smu.enable_output(True)
        dc = sim.smu.measure_dc(v_sat=-1.0)
    print(f"V_oc = {dc.voc*1000:.1f} mV   J_sc = {dc.jsc*1e6:.1f} uA "
          f"at {LED_DRIVE:g} V LED drive\n")

    spec = {
        "sweep": bace_sweep(0.80, 1.00, 0.02, n_loops=3),
        "voc": bace_at_voc(n_loops=5),
        "delay": tdcf_delay(20, 200, 20, vpre=dc.voc, n_loops=3),
        "field": field_dependence(-0.5, -3.5, 0.5, vpre=dc.voc, n_loops=3),
    }[which]

    cfg = RunConfig(n_averages=200, settle_s=0.0, dark_settle_s=0.0)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), power=sim.power, smu=sim.smu, router=sim.router)

    with sim.router.transient():
        for ev in run_transient_scan(rig, spec, cfg, voc=dc.voc,
                                     sleep=lambda s: None):
            if isinstance(ev, E.RunStarted):
                print(ev.description, "\n")
            elif isinstance(ev, E.LoopDone):
                print(f"  loop {ev.loop} complete")
            elif isinstance(ev, E.RunFinished):
                unit = ev.axis.unit
                print(f"\n  {ev.axis.name:>9} [{unit}]   {'Q [C]':>13}   {'sd':>9}")
                for v, m, s in zip(ev.values, ev.q_mean, ev.q_std):
                    # Magnitudes: every Q carries the rig's sign convention
                    # (`current_sign = -1` since 2026-09-02), so a ratio of
                    # signed values would divide by the least-negative Q
                    # and print hundreds of characters per row.
                    top = float(abs(ev.q_mean).max()) or 1.0
                    bar = "#" * int(60 * abs(m) / top)
                    print(f"  {v:12.4g}   {m:13.4e}   {s:9.2e}  {bar}")
                print(f"\n  {ev.q_all.size} shots, {ev.photo_averaged.shape[1]} "
                      f"samples each, dt = {ev.dt*1e9:g} ns, "
                      f"{ev.elapsed_s:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "sweep"))
