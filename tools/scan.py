#!/usr/bin/env python3
"""Run one real transient scan on the rig, from `run.toml`, into a run folder.

Everything before this was either a simulator or a bench check on a single axis
point. This is the measurement: it reads the recipe, drives the sample along the
configured axis, and writes the ordinary output set — the legacy `.dat` files and
the HDF5 — through the same recorder any other run uses.

    py -3 tools/scan.py                    read run.toml, ask, then run
    py -3 tools/scan.py --dry              print the plan and stop
    py -3 tools/scan.py --out D:/data      somewhere other than ./runs

It is deliberately the *standalone* measurement, the shape `TDCF-BACE.vi` had:
no relay, no SourceMeter, no V_oc measurement. The relay must already be on the
amplifier path and `[pinned] vpre` must already hold the V_oc you measured. That
keeps this program to one job — sweep an axis and record — and leaves the outer
loops to `experiment.intensity_series`, which is where they belong.

What it refuses to do:

* run while it cannot prove the LED is pulsing at the frequency the 81150A is
  armed at. The two generators must agree on the period or every second arm is
  missing, and nothing downstream would show it;
* run without a typed confirmation, because it puts a voltage on the device for
  as long as the scan takes.

It always parks the bench on the way out — `run_transient_scan`'s `finally`
disables the bias output and shuts the shutter on completion, abort, exception
and Ctrl-C alike, and the recorder flushes whatever loops finished.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime


def _ask(prompt: str) -> str:
    try:
        return input(f"  ?  {prompt}\n     > ").strip()
    except EOFError:
        return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rig", default=None)
    ap.add_argument("--run", default=None)
    ap.add_argument("--out", default="runs", help="where run folders are created")
    ap.add_argument("--dry", action="store_true",
                    help="resolve the plan and print it; touch no instrument")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation")
    ap.add_argument("--set-led", action="store_true",
                    help="put the 33220A into pulse mode from run.toml first, "
                         "instead of requiring it to be there already")
    a = ap.parse_args(argv)

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, here)

    from bace.bench import checks
    from bace.config import check_smu_limits, load_rig, load_run
    from bace.experiment import events as E
    from bace.experiment.rig import Rig
    from bace.experiment.transient import resolve_t0_int, run_transient_scan
    from bace.storage.recorder import RunRecorder, record

    p_rig = a.rig or checks._find("rig.toml")
    p_run = a.run or checks._find("run.toml")
    if not (p_rig and p_run):
        print("need both rig.toml and run.toml")
        return 2
    rig_cfg = load_rig(p_rig)
    spec, run_cfg, drive, smu_cfg, meta, extras = load_run(p_run)
    check_smu_limits(smu_cfg, rig_cfg)

    if spec.axis.centre_on_voc:
        print("this axis is centred on V_oc, which needs a SourceMeter reading "
              "under the illumination in force. That is the intensity series' "
              "job. For a standalone scan, pin an absolute axis and put the "
              "V_oc you measured into [pinned] vpre.")
        return 2
    plan = spec.plan()

    # -- the plan, before anything is touched -----------------------------
    print(f"rig.toml           {p_rig}")
    print(f"run.toml           {p_run}")
    print(f"axis               {plan.axis}")
    print(f"                   {plan.describe()}")
    print(f"pinned             vpre {spec.vpre:g} V  vcoll {spec.vcoll:g} V  "
          f"delay {spec.delay_ns:g} ns")
    print(f"LED                {drive.level:g} / {drive.low_level:g} V  "
          f"{run_cfg.pulse_frequency_hz:g} Hz  {run_cfg.duty_percent:g} %")
    print(f"acquisition        {run_cfg.n_averages} averages, "
          f"{run_cfg.timebase_ns_per_div:g} ns/div, {run_cfg.record_length} pts")
    print(f"integration        t0_int {run_cfg.t0_int_s * 1e9:g} ns "
          f"({run_cfg.t0_int_reference})")
    instr = run_cfg.polarity_instruction()
    print(f"polarity           :OUTP1:POL "
          + ("left as found, not written" if instr is None
             else ("INV" if instr else "NORM"))
          + f"   invert_polarity {run_cfg.invert_polarity}")
    print(f"dark reference     {run_cfg.dark_reference}"
          + ("   — only the shutter changes between the two traces"
             if run_cfg.dark_reference == "same"
             else "   — the dark trace repeats the swing from 0 V"))
    print(f"settling           {run_cfg.settle_s:g} s levels + "
          f"{run_cfg.shutter_settle_s:g} s after the shutter moves, both traces")

    if plan.axis.name == "delay_ns":
        lo, hi = float(plan.values.min()), float(plan.values.max())
        from bace.core.pulses import TRIGGER_OFFSET_S
        print(f":PULS:DEL1 range   {lo + TRIGGER_OFFSET_S * 1e9:g} .. "
              f"{hi + TRIGGER_OFFSET_S * 1e9:g} ns "
              f"(delay_ns + {TRIGGER_OFFSET_S * 1e9:g})")

    per_shot = 2 * (run_cfg.settle_s + 0.35) + run_cfg.dark_settle_s
    print(f"shots              {plan.n_shots}  (~{plan.n_shots * per_shot / 60:.1f} min "
          f"at {per_shot:.1f} s a shot)")
    if a.dry:
        return 0

    # -- instruments ------------------------------------------------------
    # Everything below is read back from the instrument, never assumed. Five of
    # these were inherited state until 2026-09-01 -- the relay position, the
    # 33220A polarity and frequency, and the 81150A arming source and slope --
    # and a run that inherits the wrong one still produces a full set of files.
    import pyvisa

    from bace.drivers.agilent33220a import Agilent33220A
    from bace.drivers.agilent81150 import Agilent81150, TriggerConfig
    from bace.drivers.infiniium import Infiniium
    from bace.drivers.routing import TO_AMPLIFIER
    from bace.drivers.shutter import Shutter

    rm = pyvisa.ResourceManager()
    scope_res = rm.open_resource(rig_cfg.scope_address)
    bias_res = rm.open_resource(rig_cfg.bias_address)
    led_res = rm.open_resource(rig_cfg.led_address)
    smu_res = rm.open_resource(rig_cfg.sourcemeter_address)
    for r in (scope_res, bias_res, led_res, smu_res):
        r.timeout = 20000

    rows: list[tuple[str, str, str]] = []          # (what, value, "" | why it stops)
    def check(what, value, ok, why=""):
        rows.append((what, str(value), "" if ok else why))

    # -- the path to the device -------------------------------------------
    relay_pos = None
    try:
        with Shutter(rig_cfg.dio_dll_path or None, module_id=rig_cfg.dio_module_id,
                     module_nr=rig_cfg.relay_module_nr).open() as r:
            relay_pos = r.read_line()
    except Exception as exc:
        check("relay", f"unreadable ({type(exc).__name__})", False,
              "no proof the amplifier reaches the device")
    else:
        check("relay", {0: "amplifier", 1: "sourcemeter"}.get(relay_pos, relay_pos),
              relay_pos == TO_AMPLIFIER,
              "on the SourceMeter side: the amplifier is disconnected, so every "
              "trace would be the amplifier driving nothing")

    smu_on = str(smu_res.query(":OUTP?")).strip().startswith("1")
    check("Keithley output", "ON" if smu_on else "OFF", not smu_on,
          "a second source into the device node")

    # -- the LED, and the polarity the whole experiment hangs on -----------
    led = Agilent33220A(led_res)
    if a.set_led:
        led.set_pulse(drive.level, drive.low_level,
                      frequency_hz=run_cfg.pulse_frequency_hz,
                      duty_percent=run_cfg.duty_percent)
        led.set_polarity(True)
        led.enable_output(True)
    shape = str(led_res.query("FUNC:SHAP?")).strip().upper()
    freq = float(str(led_res.query(":FREQ?")).strip())
    led_on = str(led_res.query(":OUTP?")).strip().startswith("1")
    pol = led.polarity()
    check("33220A shape", shape, shape.startswith("PULS"),
          "the 81150A is armed by this generator's Sync; a DC output has no edges")
    check("33220A output", "ON" if led_on else "OFF", led_on, "no light, no arm")
    check("33220A frequency", f"{freq:g} Hz  (run.toml {run_cfg.pulse_frequency_hz:g})",
          abs(freq - run_cfg.pulse_frequency_hz) <= 1.0,
          "the two generators disagree on the period, so arms go missing")
    # The levels were checked by nobody until 2026-09-01. `--set-led` writes them,
    # but without it the generator keeps whatever the previous run left, and the
    # extracted charge scales with the intensity -- so a recipe that changes the
    # LED level and is run without `--set-led` silently measures the old one.
    hi_led = float(str(led_res.query(":VOLT:HIGH?")).strip())
    lo_led = float(str(led_res.query(":VOLT:LOW?")).strip())
    check("33220A levels", f"{hi_led:g} / {lo_led:g} V  "
                           f"(run.toml {drive.level:g} / {drive.low_level:g})",
          abs(hi_led - drive.level) <= 1e-3 and abs(lo_led - drive.low_level) <= 1e-3,
          "the LED is not being driven at the level this recipe asks for, and the "
          "charge scales with intensity. Pass --set-led")
    check("33220A :OUTP:POL", pol, pol.startswith("INV"),
          "NORM makes the Sync's rising edge mean light *on*, so extraction "
          "would happen during generation. Pass --set-led")

    # -- the 81150A: set the arming, do not inherit it ---------------------
    bias = Agilent81150(bias_res)
    bias.configure_trigger(TriggerConfig(external=True, positive_slope=True))
    arm_src = str(bias_res.query(":ARM:SOUR1?")).strip().upper()
    arm_slp = str(bias_res.query(":ARM:SLOP?")).strip().upper()
    check("81150A arm", f"{arm_src} / {arm_slp}", arm_src.startswith("EXT"),
          "not externally armed: the pulse would free-run against the light")

    # -- can the generator take the delays this axis asks for? ------------
    if plan.axis.name == "delay_ns":
        from bace.core.pulses import pulse_levels
        asked = []
        for v in (float(plan.values.min()), float(plan.values.max())):
            lv = pulse_levels(spec.vpre, spec.vcoll, rig_cfg.pulse_amp, v,
                              run_cfg.pulse_width_ns,
                              trigger_offset_s=rig_cfg.trigger_offset_s)
            bias_res.write(f":PULS:DEL1 {lv.delay_s:g};")
            got = float(str(bias_res.query(":PULS:DEL1?")).strip())
            err = str(bias_res.query(":SYST:ERR?")).strip()
            asked.append((lv.delay_s, got, err))
        worst = max(abs(g - w) for w, g, _ in asked)
        clean = all(e.startswith(("0,", "+0,")) for _, _, e in asked)
        check(":PULS:DEL1 extremes",
              " / ".join(f"{w*1e9:g}->{g*1e9:g} ns" for w, g, _ in asked),
              clean and worst < 1e-9,
              "the generator did not take the delays at the ends of this axis: "
              + "; ".join(e for _, _, e in asked if not e.startswith(("0,", "+0,"))))

    # -- report and decide -------------------------------------------------
    print()
    width = max(len(w) for w, _, _ in rows)
    blocking = []
    for what, value, why in rows:
        mark = "  " if not why else "!!"
        print(f"{mark} {what:<{width}}  {value}")
        if why:
            blocking.append(f"{what}: {why}")
    if blocking:
        print("\nREFUSING to run:")
        for b in blocking:
            print(f"  - {b}")
        print("\nNothing was driven. Fix these and rerun; `tools/relay.py` moves "
              "the relay, `--set-led` sets the 33220A.")
        return 2

    shutter = Shutter(rig_cfg.dio_dll_path or None,
                      module_id=rig_cfg.dio_module_id,
                      module_nr=rig_cfg.shutter_module_nr).open()
    # The power meter. `run.toml` has asked for `read_intensity = true` all
    # along, but this program never handed the Rig a meter, so `rig.power` was
    # None and every run wrote an empty `intensity` dataset. The one quantity
    # that would settle "is the charge small because the light is weak" was the
    # one quantity nobody recorded. Absent console = no intensity, not a refusal.
    power = None
    try:
        from bace.drivers.newport1918c import ConsolePowerMeter
        candidate = ConsolePowerMeter(rig_cfg.power_meter_console, timeout_s=5.0)
        if candidate.available():
            candidate.set_units_watts()
            candidate.set_wavelength(rig_cfg.power_meter_wavelength_nm)
            power = candidate
            print(f"power meter        {rig_cfg.power_meter_console}  "
                  f"{power.read_power():.4e} W at "
                  f"{rig_cfg.power_meter_wavelength_nm:g} nm")
        else:
            print(f"power meter        not answering at "
                  f"{rig_cfg.power_meter_console} — no intensity will be "
                  f"recorded (start it with Start Console.bat)")
    except Exception as exc:                                    # noqa: BLE001
        print(f"power meter        unusable: {exc}")

    rig = Rig(bias=bias,
              scope=Infiniium(scope_res,
                              sense_resistor_ohm=rig_cfg.sense_resistor_ohm,
                              current_sign=rig_cfg.current_sign,
                              probe_attenuation=rig_cfg.probe_attenuation),
              shutter=shutter, config=rig_cfg, led=led, power=power)
    rig.scope.default_setup()

    import dataclasses
    meta = dataclasses.replace(meta, started=datetime.now())
    # What this runner verified but the run itself never reads. Without these
    # the file cannot say which Sync edge armed the pulse -- and that is the
    # whole question a delay scan is trying to answer.
    rec = RunRecorder(a.out, meta,
                      store_shots=bool(extras.get("store_shots")),
                      resolved={"led_output_polarity": pol,
                                "led_frequency_hz": f"{freq:g}",
                                "led_levels_v": f"{drive.level:g}/{drive.low_level:g}",
                                "bias_arm_source": arm_src,
                                "bias_arm_slope": arm_slp})

    started = time.monotonic()
    clipped_steps: list[float] = []
    try:
        for ev in record(run_transient_scan(rig, spec, run_cfg), rec):
            if isinstance(ev, E.Notice):
                print(f"  [{ev.level}] {ev.text}")
            elif isinstance(ev, E.StepDone):
                if ev.index == 0:
                    # The scope gives window x sample rate, not what :ACQ:POIN
                    # asked for. A coarse record is how a narrow feature gets
                    # dropped outright rather than attenuated.
                    print(f"  record             {ev.light.n} pts, "
                          f"{ev.light.dt * 1e9:g} ns/sample, "
                          f"t0 {ev.light.t0 * 1e9:g} ns")
                    if ev.light.dt > 2e-9:
                        print("  [warning] coarser than 2 ns a sample — "
                              "raise record_length in run.toml")
                flag = "  CLIPPED" if ev.clipped else ""
                if ev.clipped:
                    clipped_steps.append(ev.axis_value)
                print(f"  loop {ev.loop:>3}  {plan.axis.name} "
                      f"{ev.axis_value:>8.3f} {plan.axis.unit}   "
                      f"Q {ev.q:+.4e}   mean {ev.q_mean:+.4e} "
                      f"+- {ev.q_std:.2e}{flag}")
            elif isinstance(ev, E.Progress) and ev.eta_s:
                if ev.done % 10 == 0:
                    print(f"       {ev.done}/{ev.total}, "
                          f"{ev.eta_s / 60:.1f} min left")
            elif isinstance(ev, E.RunFinished):
                print(f"\nfinished in {ev.elapsed_s / 60:.1f} min, "
                      f"dt {ev.dt * 1e9:g} ns")
    except KeyboardInterrupt:
        print("\ninterrupted — the bias output is off and the shutter is shut; "
              "the loops that finished are being written")
    finally:
        try:
            shutter.close()
        except Exception:
            pass
        for r in (scope_res, bias_res, led_res, smu_res):
            try:
                r.close()
            except Exception:
                pass

    print(f"\nrun folder         {rec.folder}")
    for p in rec.written:
        print(f"                   {os.path.basename(p)}")
    if rec.hdf5_error:
        print(f"HDF5 failed        {rec.hdf5_error}  (the .dat set is written)")
    if clipped_steps:
        print(f"\nCLIPPED at {len(clipped_steps)} shots, "
              f"{plan.axis.name} = {sorted(set(clipped_steps))}")
        print("those charges are underestimates — do not read a plateau there "
              "as physics")
    print(f"total              {(time.monotonic() - started) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
