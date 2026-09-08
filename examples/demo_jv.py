#!/usr/bin/env python3
"""Run a J-V scan on simulated instruments — dark, light, and hysteresis.

    python -m examples.demo_jv
"""
from __future__ import annotations

from bace.drivers.simulated import make_bench
from bace.experiment.jv import (JVConfig, JVCurveDone, JVFinished, current_density,
                                run_jv)
from bace.experiment.rig import Rig, RigConfig


def main() -> int:
    sim = make_bench(seed=11)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led, router=sim.router)
    cfg = JVConfig(start_v=-0.2, stop_v=1.1, step_v=0.01, dark=True,
                   led_levels_v=(1.020, 1.060), both_directions=True,
                   pixel_area_cm2=0.04)

    print(f"{'curve':>14} {'dir':>8} {'Voc/V':>9} {'Jsc/mA cm-2':>13} "
          f"{'FF':>7} {'Pmax/mW':>9}")
    for ev in run_jv(rig, cfg, sleep=lambda s: None):
        if isinstance(ev, JVCurveDone):
            m = ev.metrics
            # The one conversion, so the header's unit and the number agree.
            j = ("" if m.jsc is None
                 else f"{float(current_density(m.jsc, cfg.pixel_area_cm2)):13.3f}")
            voc = "" if m.voc is None else f"{m.voc:9.4f}"
            ff = "" if m.fill_factor is None else f"{m.fill_factor:7.3f}"
            pm = "" if m.p_max is None else f"{m.p_max * 1e3:9.4f}"
            print(f"{ev.label:>14} {ev.direction:>8} {voc:>9} {j:>13} {ff:>7} {pm:>9}")
        elif isinstance(ev, JVFinished):
            print(f"\n{len(ev.curves)} curves in {ev.elapsed_s:.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
