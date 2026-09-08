#!/usr/bin/env python3
"""Run a full intensity series on simulated instruments.

The wrapper: for each LED level, measure V_oc / J_sc / J_sat on the SourceMeter,
switch to pulsed illumination at the same level, and run a transient scan whose
prebias axis is centred on the V_oc just measured.

    python examples/demo_series.py
"""
from __future__ import annotations

import tempfile
from datetime import datetime

from bace.core.axis import bace_at_voc
from bace.drivers.simulated import make_bench
from bace.experiment import events as E
from bace.experiment import intensity_series as S
from bace.experiment.jv import current_density
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig
from bace.storage.naming import RunMetadata
from bace.storage.series import SeriesRecorder, record


AREA_CM2 = 0.04          # the pixel, and the only reason there is a density


def main() -> int:
    sim = make_bench(seed=21)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), smu=sim.smu, led=sim.led, power=sim.power,
              router=sim.router)

    series = S.SeriesConfig(led_start_v=1.010, led_stop_v=1.070,
                            led_step_v=0.020, led_settle_s=0.0,
                            pixel_area_cm2=AREA_CM2)
    spec = bace_at_voc(n_loops=5)
    run = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0)

    meta = RunMetadata(sample="s9", material="SIM", pixel="pxa",
                       temperature_k=290, started=datetime.now())
    root = tempfile.mkdtemp(prefix="bace-series-")
    rec = SeriesRecorder(root, meta, pixel_area_cm2=AREA_CM2)

    print(f"{'LED/V':>7} {'Voc/V':>8} {'Jsc/mA cm-2':>13} {'Q/C':>12} {'sd':>10}")
    for ev in record(S.run_intensity_series(rig, series, spec, run,
                                            sleep=lambda s: None), rec):
        if isinstance(ev, S.SeriesPointDone):
            j = float(current_density(ev.dc.jsc, AREA_CM2))
            print(f"{ev.level_v:7.3f} {ev.voc:8.4f} {j:13.3f} "
                  f"{ev.q_mean[0]:12.4e} {ev.q_std[0]:10.2e}")
        elif isinstance(ev, E.Notice):
            print(f"  [{ev.level}] {ev.text}")

    import os
    print(f"\nwrote {len(rec.written)} files under {root}")
    for name in sorted(os.listdir(rec.folder)):
        print("   ", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
