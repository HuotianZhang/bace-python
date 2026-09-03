#!/usr/bin/env python3
"""How much light actually reaches the sample position? Measure it.

    py -3 tools/lightpower.py                  10 s of light, with dark before and after
    py -3 tools/lightpower.py --seconds 30     longer
    py -3 tools/lightpower.py --off-level      also hold the LED at its "off" level
    py -3 tools/lightpower.py --dry            print the plan and stop

Every conclusion about the illumination so far has been inferred from a device
current. This is the first thing that looks at the light itself.

Sequence, with the meter logging continuously throughout:

    dark    shutter shut, 33220A output OFF     -- the meter's own background
    light   33220A pulsing, shutter open        -- what the sample sees
    off     33220A held at its low level        -- only with --off-level
    dark    shutter shut again                  -- did the background move?

The `off` phase is the one that settles a question open since 2026-09-01: the
recipes drive the LED between 1.000 V and 0.400 V and call the low level "off",
but nothing has ever checked that 0.400 V is actually below the LED's turn-on.
If it is not, the light never stops, and a BACE delay axis measures the delay to
an event that does not happen.

The meter is opened by this program (`bace.drivers.newport1918c`), so nothing
else has to be running. Only one process can hold it over USB, so close the
meter's own console first if it happens to be open -- or name it in
`[power_meter] console` and this goes through its HTTP API instead.

This program does not touch the 81150A or the relay. It moves the shutter and
the 33220A, and puts both back the way it found them.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from datetime import datetime


def _phase(meter, shutter_state: str, label: str, seconds: float,
           interval: float, rows: list, t0: float) -> dict:
    """Poll the meter for `seconds`, appending to `rows`. Returns a summary."""
    vals: list[float] = []
    bad = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r = meter.read()
        now = time.monotonic() - t0
        vals.append(r.watts)
        if not r.trustworthy:
            bad += 1
        rows.append({"t_s": round(now, 4), "phase": label,
                     "shutter": shutter_state, "watts": r.watts,
                     "saturated": int(r.saturated), "overrange": int(r.overrange)})
        print(f"    {now:7.2f} s  {label:<6}  {r.watts: .6e} W"
              + ("  <-- not trustworthy" if not r.trustworthy else ""))
        time.sleep(max(0.0, interval))
    if not vals:
        return {"label": label, "n": 0}
    return {"label": label, "n": len(vals), "mean": statistics.fmean(vals),
            "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals), "bad": bad}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rig", default=None)
    ap.add_argument("--run", default=None, help="only for the default frequency")
    ap.add_argument("--high", type=float, default=1.000, help="LED high level, V")
    ap.add_argument("--low", type=float, default=0.400, help="LED low level, V")
    ap.add_argument("--duty", type=float, default=50.0)
    ap.add_argument("--freq", type=float, default=None, help="default: run.toml")
    ap.add_argument("--normal", action="store_true",
                    help="drive :OUTP:POL NORM instead of INV")
    ap.add_argument("--seconds", type=float, default=10.0, help="per light phase")
    ap.add_argument("--dark-seconds", type=float, default=4.0)
    ap.add_argument("--interval", type=float, default=0.25, help="poll period, s")
    ap.add_argument("--off-level", action="store_true",
                    help="also hold the LED at --low as DC, shutter still open")
    ap.add_argument("--out", default=None, help="CSV path (default: ./runs)")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)

    here = __import__("os").path.dirname(__import__("os").path.dirname(
        __import__("os").path.abspath(__file__)))
    sys.path.insert(0, here)

    from bace.config import load_rig, load_run

    rig_cfg = load_rig(a.rig or __import__("os").path.join(here, "rig.toml"))
    freq = a.freq
    if freq is None:
        try:
            _, run_cfg, *_ = load_run(a.run or __import__("os").path.join(here, "run.toml"))
            freq = run_cfg.pulse_frequency_hz
        except Exception:                                   # noqa: BLE001
            freq = 500.0
    pol = "NORM" if a.normal else "INV"

    print("light power measurement")
    print(f"  33220A            {rig_cfg.led_address}")
    print(f"  pulse             {a.high:g} / {a.low:g} V   {freq:g} Hz   "
          f"{a.duty:g} %   :OUTP:POL {pol}")
    print(f"  shutter           DIO module {rig_cfg.dio_module_id}, "
          f"module_nr {rig_cfg.shutter_module_nr}")
    print(f"  power meter       {rig_cfg.power_meter_console or '1918-C on USB'}  "
          f"at {rig_cfg.power_meter_wavelength_nm:g} nm")
    plan = ["dark %.0f s" % a.dark_seconds, "light %.0f s" % a.seconds]
    if a.off_level:
        plan.append("off-level %.0f s" % a.seconds)
    plan.append("dark %.0f s" % a.dark_seconds)
    print(f"  phases            {'  ->  '.join(plan)}   polled every {a.interval:g} s")
    print("  the 81150A and the relay are not touched")
    if a.dry:
        return 0

    import os

    import pyvisa

    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.newport1918c import PowerMeterError, open_power_meter
    from bace.drivers.shutter import Shutter

    try:
        meter = open_power_meter(rig_cfg)
    except PowerMeterError as exc:
        print(f"\n  {exc}")
        print("  Only one process can hold the meter over USB: if the meter's")
        print("  own console is open, close it and run this again.")
        return 2
    probe = meter.read()
    print(f"\n  meter reads {probe.watts: .6e} W right now"
          + ("" if probe.trustworthy else "  <-- saturated/overrange already"))

    rm = pyvisa.ResourceManager()
    led_res = rm.open_resource(rig_cfg.led_address)
    led = Agilent33220A(led_res)
    print(f"  {led.identify()}")

    # What we found, so we can put it back.
    was = {
        "shape": str(led_res.query("FUNC:SHAP?")).strip().upper(),
        "pol": str(led_res.query(":OUTP:POL?")).strip().upper(),
        "out": str(led_res.query(":OUTP?")).strip().startswith("1"),
        "freq": str(led_res.query(":FREQ?")).strip(),
        "high": str(led_res.query(":VOLT:HIGH?")).strip(),
        "low": str(led_res.query(":VOLT:LOW?")).strip(),
    }
    print(f"  found the 33220A at {was['shape']} / :OUTP:POL {was['pol']} / "
          f"output {'ON' if was['out'] else 'OFF'} / {was['high']}..{was['low']} V")

    shutter = Shutter(rig_cfg.dio_dll_path or None,
                      module_id=rig_cfg.dio_module_id,
                      module_nr=rig_cfg.shutter_module_nr).open()

    rows: list[dict] = []
    summaries: list[dict] = []
    t0 = time.monotonic()
    try:
        # -- dark: shutter shut, LED output off ---------------------------
        led.enable_output(False)
        shutter.shut()
        time.sleep(0.5)
        print("\n  [dark] shutter shut, 33220A output OFF")
        summaries.append(_phase(meter, "shut", "dark1", a.dark_seconds,
                                a.interval, rows, t0))

        # -- light: the pulse the recipes ask for -------------------------
        led.set_pulse(a.high, a.low, frequency_hz=freq, duty_percent=a.duty)
        led.set_polarity(pol == "INV")
        led.enable_output(True)
        time.sleep(0.5)
        shutter.unblock()
        time.sleep(1.0)                       # mechanical shutter, then settle
        print(f"\n  [light] pulsing {a.high:g}/{a.low:g} V, shutter OPEN")
        summaries.append(_phase(meter, "open", "light", a.seconds,
                                a.interval, rows, t0))

        # -- the low level on its own -------------------------------------
        if a.off_level:
            # DC at the low level: the LED sees exactly what it sees during
            # the "off" half of the pulse, with nothing else changing.
            led.set_dc(a.low)
            time.sleep(1.0)
            print(f"\n  [off-level] LED held at {a.low:g} V, shutter still OPEN")
            summaries.append(_phase(meter, "open", "offlvl", a.seconds,
                                    a.interval, rows, t0))

        # -- dark again ----------------------------------------------------
        shutter.shut()
        led.enable_output(False)
        time.sleep(1.0)
        print("\n  [dark] shutter shut again")
        summaries.append(_phase(meter, "shut", "dark2", a.dark_seconds,
                                a.interval, rows, t0))
    except KeyboardInterrupt:
        print("\n  interrupted")
    finally:
        try:
            shutter.shut()
        finally:
            pass
        # Put the 33220A back exactly as found.
        try:
            led_res.write(f"FUNC:SHAP {was['shape']};")
            led_res.write(f":FREQ {was['freq']};")
            led_res.write(f":VOLT:HIGH {was['high']};")
            led_res.write(f":VOLT:LOW {was['low']};")
            led_res.write(f":OUTP:POL {was['pol']};")
            led_res.write(f":OUTP {'ON' if was['out'] else 'OFF'};")
            print("\n  33220A restored; shutter shut")
        except Exception as exc:                            # noqa: BLE001
            print(f"\n  could not fully restore the 33220A: {exc}")
        try:
            shutter.close()
        except Exception:                                   # noqa: BLE001
            pass

    # -- output -----------------------------------------------------------
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # `lightpower_<stamp>.csv`, not `lightpower<stamp>.csv`: this sits at the
    # top of `runs/` beside `bare_<stamp>` and `<T>K_..._<stamp>`, which
    # separate the stamp. (The files *inside* a run folder deliberately do
    # not -- `0_parameters20260807_111521.txt` is the archive's own shape.)
    out = a.out or os.path.join(here, "runs", f"lightpower_{stamp}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["t_s", "phase", "shutter", "watts",
                                           "saturated", "overrange"])
        w.writeheader()
        w.writerows(rows)

    print("\n  summary")
    by = {s["label"]: s for s in summaries if s.get("n")}
    for s in summaries:
        if not s.get("n"):
            continue
        print(f"    {s['label']:<7} n={s['n']:<4} mean {s['mean']: .6e} W   "
              f"sd {s['sd']:.2e}   min {s['min']: .3e}   max {s['max']: .3e}"
              + (f"   ({s['bad']} untrustworthy)" if s["bad"] else ""))

    dark = None
    if "dark1" in by and "dark2" in by:
        dark = (by["dark1"]["mean"] + by["dark2"]["mean"]) / 2.0
        drift = by["dark2"]["mean"] - by["dark1"]["mean"]
        print(f"    background     {dark: .6e} W   (drifted {drift:+.2e} between "
              "the two dark phases)")
    if dark is not None and "light" in by:
        print(f"    light - dark   {by['light']['mean'] - dark: .6e} W"
              "   <-- what the sample sees, on average over the LED cycle")
    if dark is not None and "offlvl" in by:
        off = by["offlvl"]["mean"] - dark
        lit = by["light"]["mean"] - dark
        print(f"    off - dark     {off: .6e} W")
        if lit != 0:
            print(f"    off / light    {off / lit * 100:.1f} %"
                  "   <-- if this is not near zero, the LED never turns off")

    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
