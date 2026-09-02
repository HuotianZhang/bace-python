#!/usr/bin/env python3
"""BACE with nothing initialised. Inherit the VI's setup; move as little as possible.

    py -3 tools/bare.py --as-found --dry    read the bench, print it, stop
    py -3 tools/bare.py --as-found          one point, shutter is the ONLY thing that moves
    py -3 tools/bare.py --invert            three points, levels written per vpre

Run the LabVIEW VI first and leave the bench exactly as it finished.

This program configures **nothing**: not the timebase, not the trigger, not the
averaging or its count, not the vertical range, not the 33220A, not
`:OUTP:POL`, not the arming, not the pulse delay or width. It reads all of it
back, prints it, and writes it into the run folder as `0_instrument_state.txt`
and `.json` -- so the state that produced the data is recorded with the data,
which is the thing the port's own runs could never claim.

`--as-found` is the strictest version: it does not write the levels either. The
81150A keeps whatever voltages the VI left on it, both traces are taken at those
same voltages, and **the shutter is the only thing in the building that moves**.
Any difference between the two traces is then photocurrent by construction --
no level translation, no polarity convention, no per-point arithmetic to get
wrong. One point, no parameters to type.

Without `--as-found` it writes `:VOLT1:HIGH` / `:VOLT1:LOW` per V_pre from
`core.pulses.pulse_levels`, which is the run path's own arithmetic; `--invert`
negates and swaps both pairs, `--dark same` leaves the dark trace at the light
levels.

Acquisition is `:DIGitize` + `*OPC?`: it acquires until the acquisition is
*complete* -- with averaging already on, that is the whole average -- and leaves
the scope stopped. No averager restart, so no setting is touched, and a fresh
acquisition every time, so a light trace can never be a blend with the dark one
before it.

Nothing is restored on the way out except the shutter, which is shut.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

import numpy as np

# (label, SCPI) per instrument. Read-only, every one of them.
SCOPE = [("identity", "*IDN?"),
         (":TIM:RANG", ":TIM:RANG?"), (":TIM:POS", ":TIM:POS?"),
         (":TIM:REF", ":TIM:REF?"), (":TIM:SCAL", ":TIM:SCAL?"),
         (":ACQ:POIN", ":ACQ:POIN?"), (":ACQ:SRAT", ":ACQ:SRAT?"),
         (":ACQ:MODE", ":ACQ:MODE?"), (":ACQ:AVER", ":ACQ:AVER?"),
         (":ACQ:AVER:COUN", ":ACQ:AVER:COUN?"), (":ACQ:INT", ":ACQ:INT?"),
         (":ACQ:COMP", ":ACQ:COMP?"),
         (":TRIG:MODE", ":TRIG:MODE?"), (":TRIG:SWE", ":TRIG:SWE?"),
         (":TRIG:EDGE:SOUR", ":TRIG:EDGE:SOUR?"),
         (":TRIG:EDGE:SLOP", ":TRIG:EDGE:SLOP?")]
SCOPE_CH = [(":RANG", ":RANG?"), (":OFFS", ":OFFS?"), (":SCAL", ":SCAL?"),
            (":INP", ":INP?"), (":PROB", ":PROB?"),
            (":PROB:EXT", ":PROB:EXT?"), (":PROB:EXT:GAIN", ":PROB:EXT:GAIN?"),
            (":DISP", ":DISP?")]
BIAS = [("identity", "*IDN?"), (":FUNC1", ":FUNC1?"), (":FREQ1", ":FREQ1?"),
        (":OUTP1", ":OUTP1?"), (":OUTP1:POL", ":OUTP1:POL?"),
        (":OUTP1:LOAD", ":OUTP1:LOAD?"),
        (":VOLT1:HIGH", ":VOLT1:HIGH?"), (":VOLT1:LOW", ":VOLT1:LOW?"),
        (":VOLT1", ":VOLT1?"), (":VOLT1:OFFS", ":VOLT1:OFFS?"),
        (":PULS:DEL1", ":PULS:DEL1?"),
        (":FUNC1:PULS:WIDT", ":FUNC1:PULS:WIDT?"),
        (":FUNC1:PULS:DCYC", ":FUNC1:PULS:DCYC?"),
        (":FUNC1:PULS:TRAN", ":FUNC1:PULS:TRAN?"),
        (":FUNC1:PULS:HOLD", ":FUNC1:PULS:HOLD?"),
        (":ARM:SOUR1", ":ARM:SOUR1?"), (":ARM:SENS1", ":ARM:SENS1?"),
        (":ARM:SLOP", ":ARM:SLOP?"), (":ARM:LEV", ":ARM:LEV?"),
        (":ARM:IMP", ":ARM:IMP?"), (":ARM:FREQ", ":ARM:FREQ?")]
LED = [("identity", "*IDN?"), ("FUNC:SHAP", "FUNC:SHAP?"), (":FREQ", ":FREQ?"),
       (":OUTP", ":OUTP?"), (":OUTP:POL", ":OUTP:POL?"),
       (":OUTP:LOAD", ":OUTP:LOAD?"),
       (":VOLT:HIGH", ":VOLT:HIGH?"), (":VOLT:LOW", ":VOLT:LOW?"),
       (":VOLT", ":VOLT?"), (":VOLT:OFFS", ":VOLT:OFFS?"),
       ("FUNC:PULS:DCYC", "FUNC:PULS:DCYC?"),
       ("FUNC:PULS:WIDT", "FUNC:PULS:WIDT?"),
       ("FUNC:PULS:HOLD", "FUNC:PULS:HOLD?"),
       ("FUNC:PULS:TRAN", "FUNC:PULS:TRAN?"),
       (":BURS:STAT", ":BURS:STAT?"), (":TRIG:SOUR", ":TRIG:SOUR?")]


def _q(res, cmd: str) -> str:
    """One query. A refusal is recorded as a refusal, not as a missing line."""
    try:
        return str(res.query(cmd)).strip()
    except Exception as exc:                                    # noqa: BLE001
        return f"<{type(exc).__name__}: {exc}>"


def _dump(res, items, prefix="") -> list:
    return [(prefix + label, _q(res, cmd)) for label, cmd in items]


def _acquire(res, scope, a) -> None:
    """One complete acquisition, writing no settings.

    `:DIGitize` acquires until the acquisition is complete -- with `:ACQ:AVER
    ON` and a count already set, that means the whole average -- and leaves the
    scope stopped, ready to fetch. `--acq run` is the fallback: `:RUN`, wait on
    `:ADER?`, `:STOP`, which is what `single_acquisition()` does.
    """
    if a.acq == "dig":
        old = res.timeout
        res.timeout = int(a.timeout * 1000)
        try:
            res.write(":DIG;")
            res.query("*OPC?")
        finally:
            res.timeout = old
    else:
        scope.single_acquisition(a.timeout)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rig", default=None)
    ap.add_argument("--as-found", action="store_true",
                    help="write no levels at all: one point, at the voltages "
                         "the VI left, shutter the only thing that moves")
    ap.add_argument("--vpre", default="1.00354,1.01354,1.02354",
                    help="ignored with --as-found")
    ap.add_argument("--vcoll", type=float, default=-1.00)
    ap.add_argument("--invert", action="store_true")
    ap.add_argument("--dark", choices=("translated", "same"), default="translated")
    ap.add_argument("--settle", type=float, default=1.0,
                    help="s after the shutter moves, before acquiring")
    ap.add_argument("--level-settle", type=float, default=0.3)
    ap.add_argument("--t0", type=float, default=320e-9)
    ap.add_argument("--acq", choices=("dig", "run"), default="dig")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)

    from bace.config import load_rig
    from bace.core.pulses import pulse_levels

    cfg = load_rig(a.rig or os.path.join(here, "rig.toml"))

    import pyvisa

    from bace.drivers.infiniium import Infiniium
    from bace.drivers.routing import TO_AMPLIFIER
    from bace.drivers.shutter import Shutter

    rm = pyvisa.ResourceManager()
    scope_res = rm.open_resource(cfg.scope_address)
    bias_res = rm.open_resource(cfg.bias_address)
    led_res = rm.open_resource(cfg.led_address)

    # -- read everything back, write nothing ------------------------------
    ch = cfg.scope_channel
    state = {
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "scope": _dump(scope_res, SCOPE)
                 + _dump(scope_res, [(l, f":CHAN{ch}{c}") for l, c in SCOPE_CH],
                         prefix=f":CHAN{ch}"),
        "81150A": _dump(bias_res, BIAS),
        "33220A": _dump(led_res, LED),
    }
    relay = Shutter(cfg.dio_dll_path or None, module_id=cfg.dio_module_id,
                    module_nr=cfg.relay_module_nr).open()
    try:
        line = relay.read_line()
    finally:
        relay.close()
    state["relay_line"] = line
    state["relay_meaning"] = ("amplifier — the 81150A reaches the device"
                              if line == TO_AMPLIFIER else "NOT the amplifier side")

    print("inherited state — every line below was read, none of it was set\n")
    for inst in ("scope", "81150A", "33220A"):
        print(f"  {inst}")
        for label, value in state[inst]:
            print(f"    {label:<24} {value}")
    print(f"\n  relay line               {line}  ({state['relay_meaning']})")

    # -- what this program will write -------------------------------------
    if a.as_found:
        hi = _q(bias_res, ":VOLT1:HIGH?")
        lo = _q(bias_res, ":VOLT1:LOW?")
        print(f"\n--as-found: the levels are NOT written. Both traces run at "
              f"HIGH {hi} / LOW {lo} V,\n"
              f"            i.e. {float(hi)*cfg.pulse_amp:+.5f} / "
              f"{float(lo)*cfg.pulse_amp:+.5f} V at the device "
              f"(x{cfg.pulse_amp:g}).")
        print("            The shutter is the only thing that moves.")
        plan = [(float(hi), None, None)]
    else:
        vpres = [float(x) for x in a.vpre.split(",") if x.strip()]
        print(f"\nlevels this program writes  (amp {cfg.pulse_amp:g}, "
              f"invert {a.invert}, dark {a.dark})")
        plan = []
        for v in vpres:
            lv = pulse_levels(v, a.vcoll, cfg.pulse_amp, 0.0, 0.0,
                              invert=a.invert, trigger_offset_s=0.0)
            light = (lv.high_light, lv.low_light)
            dark = light if a.dark == "same" else (lv.high_dark, lv.low_dark)
            plan.append((v, light, dark))
            print(f"    vpre {v:.5f}  light {light[0]:+.5f}/{light[1]:+.5f}"
                  f"   dark {dark[0]:+.5f}/{dark[1]:+.5f}  (generator V)")

    print(f"\n  then: shutter, {a.settle:g} s settle, {a.acq} acquisition, "
          f"fetch {cfg.current_source}")
    print(f"  integration from {a.t0*1e9:g} ns of record time, baseline = mean "
          f"of the last 200 samples")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = a.out or os.path.join(here, "runs", f"bare_{stamp}")

    if a.dry:
        print("\n  --dry: nothing written, nothing moved")
        return 0

    if not a.yes:
        try:
            ans = input("\n  ?  this puts the inherited pulse on the device. "
                        "type yes to run\n     > ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("yes", "y", "ja"):
            print("  stopped, nothing written")
            return 1

    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"0_instrument_state{stamp}.json"), "w",
              encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    with open(os.path.join(folder, f"0_instrument_state{stamp}.txt"), "w",
              encoding="utf-8") as fh:
        fh.write(f"read at {state['read_at']}\n")
        fh.write("every line was read back, none of it was set by this program\n")
        for inst in ("scope", "81150A", "33220A"):
            fh.write(f"\n[{inst}]\n")
            for label, value in state[inst]:
                fh.write(f"{label:<24} {value}\n")
        fh.write(f"\nrelay_line               {line}  ({state['relay_meaning']})\n")
        fh.write(f"as_found                 {a.as_found}\n")
        fh.write(f"settle_s                 {a.settle}\n")
        fh.write(f"acquisition              {a.acq}\n")
    print(f"\n  wrote the instrument state into {folder}")

    scope = Infiniium(scope_res, sense_resistor_ohm=cfg.sense_resistor_ohm,
                      current_sign=cfg.current_sign,
                      probe_attenuation=cfg.probe_attenuation)
    shutter = Shutter(cfg.dio_dll_path or None, module_id=cfg.dio_module_id,
                      module_nr=cfg.shutter_module_nr).open()

    lights, darks, labels, t = [], [], [], None
    try:
        for v, light, dark in plan:
            print(f"\n  point {v:.5f}")
            if not a.as_found:
                bias_res.write(f":VOLT1:HIGH {light[0]:g};")
                bias_res.write(f":VOLT1:LOW {light[1]:g};")
                time.sleep(a.level_settle)
            shutter.unblock()
            time.sleep(a.settle)
            _acquire(scope_res, scope, a)
            tr = scope.fetch_volts(cfg.current_source)
            # This program does the digitiser chain by hand (volts / R), so
            # it applies the rig's sign here, where `Infiniium._fetch` would.
            # Without it the Q printed below has the opposite sign from every
            # other run on this bench and from the LabVIEW reference it is
            # printed next to -- which is how the sign question stayed open.
            L = cfg.current_sign * tr.y / cfg.sense_resistor_ohm
            print(f"    light  {L.min()*1e3:+8.3f} .. {L.max()*1e3:+8.3f} mA"
                  f"   {L.size} samples, dt {tr.dt*1e9:.3f} ns")

            if not a.as_found and a.dark != "same":
                bias_res.write(f":VOLT1:HIGH {dark[0]:g};")
                bias_res.write(f":VOLT1:LOW {dark[1]:g};")
                time.sleep(a.level_settle)
            shutter.shut()
            time.sleep(a.settle)
            _acquire(scope_res, scope, a)
            D = (cfg.current_sign * scope.fetch_volts(cfg.current_source).y
                 / cfg.sense_resistor_ohm)
            print(f"    dark   {D.min()*1e3:+8.3f} .. {D.max()*1e3:+8.3f} mA")
            if D.size != L.size:
                print("    the two traces are different lengths — stopping.")
                break
            lights.append(L); darks.append(D); labels.append(v)
            t = np.arange(L.size) * tr.dt
    except KeyboardInterrupt:
        print("\n  interrupted")
    finally:
        try:
            shutter.shut()
        finally:
            shutter.close()
        print("\n  shutter shut. Nothing else was restored, by design.")

    if not lights:
        return 2

    L = np.array(lights); D = np.array(darks)
    photo = L - D
    photo = photo - photo[:, -200:].mean(axis=1, keepdims=True)
    dt = float(t[1] - t[0])
    q = np.trapezoid(photo[:, t >= a.t0], dx=dt, axis=1)

    print("\n  results")
    for i, v in enumerate(labels):
        j = int(np.argmax(np.abs(photo[i])))
        raw = L[i] - D[i]
        print(f"    {v:.5f}   photo peak {photo[i][j]*1e3:+8.3f} mA @ "
              f"{t[j]*1e9:6.1f} ns   raw gap before the step "
              f"{raw[t < 300e-9].mean()*1e3:+.4f} mA   Q {q[i]:+.4g} C")
    print("\n  LabVIEW 参照: Q ≈ -9.2e-10 C, 光电流峰 ≈ -3.7 mA, 原始峰 ≈ -27 mA")

    from bace.storage.legacy_dat import LegacyWriter
    w = LegacyWriter(folder, stamp)
    vp = np.array(labels, dtype=float)
    for p in (w.trace_matrix("averages_light", t, vp, L),
              w.trace_matrix("averages_dark", t, vp, D),
              w.trace_matrix("averages_photocurrent", t, vp, photo),
              w.averages_q(vp, np.full_like(vp, a.vcoll), np.zeros_like(vp),
                           q, np.zeros_like(q))):
        print(f"  wrote {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
