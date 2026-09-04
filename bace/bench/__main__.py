"""Bench check — run this on the lab PC and send back the report.

Nothing here has ever spoken to an instrument: every SCPI string in this port
was recovered from a LabVIEW binary. This session sends each one and reads the
instrument's error queue immediately afterwards, so the report comes back with
an exact list of what worked, what was rejected, and where the recovered
behaviour differs from the instrument's own.

Stages, in the order they run. Each is opt-in past the first two, and nothing
that can put a voltage on the device node happens without an explicit flag and
a typed confirmation:

    (always)      offline    the package itself, on this machine. No instruments
    (always)      discover   enumerate VISA and *IDN? each configured address
    (always)      local      the DELIB library, the 1918-C and 331 consoles
    --read        read       read back what each instrument is set to
    --configure   configure  send the configuration commands. Outputs stay OFF
    --acquire     acquire    one scope acquisition (the trigger must be running)
    --sync        sync       where the pulse sits in the LED cycle. Scope only
    --dio         dio        toggle the shutter and relay lines. Audible
    --outputs     outputs    enable outputs, NO SAMPLE CONNECTED
    --measure     measure    one real transient. Needs a sample

Typical use, in three passes:

    python -m bace.bench --archive "D:\\DATA\\TDCF-BACE\\data\\<a run folder>"
    python -m bace.bench --read --configure
    python -m bace.bench --read --configure --acquire

then, when those look right:

    python -m bace.bench --outputs                 # nothing connected
    python -m bace.bench --measure --manual-shutter

Three files land in the output folder: a `.txt` to read now, a `.html` to look
at, and a `.json` with the full transcript — that is the one to send back.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from . import checks, sync
from .report import FAILED, WARNED, Report, render_text


def _confirm(prompt: str) -> str:
    try:
        return input(f"  ?  {prompt}\n     > ").strip()
    except EOFError:
        return "<no answer: not an interactive session>"


def _ask(prompt: str) -> str:
    try:
        input(f"  >> {prompt} ")
    except EOFError:
        pass
    return "done"


def _default_archive() -> str | None:
    """`bench-archive/` beside the package, if it is there."""
    import bace
    root = os.path.dirname(os.path.dirname(os.path.abspath(bace.__file__)))
    folder = os.path.join(root, "bench-archive")
    return folder if os.path.isdir(folder) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m bace.bench",
        description="Exercise the BACE port against the real rig and write a report.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--archive", default=None,
                    help="a measurement folder, to re-run the numerical "
                         "regression on this machine (default: the "
                         "bench-archive/ folder beside the package, if present)")
    ap.add_argument("--out", default="bench-reports",
                    help="where to write the report (default: bench-reports)")
    ap.add_argument("--rig", default=None, help="path to rig.toml")
    ap.add_argument("--run", default=None, help="path to run.toml")

    ap.add_argument("--read", action="store_true", help="read instrument state")
    ap.add_argument("--configure", action="store_true",
                    help="send configuration commands (outputs stay off)")
    ap.add_argument("--listen", action="store_true",
                    help="with --dio, also ask what you heard at each state. "
                         "The measurement settles it either way; this is extra "
                         "evidence when someone is in the room")
    ap.add_argument("--leave-outputs-off", action="store_true",
                    help="after --dio, leave the instrument outputs off "
                         "instead of restoring the state they were found in")
    ap.add_argument("--force-configure", action="store_true",
                    help="configure even if an instrument's output is already "
                         "ON. Only with nothing connected")
    ap.add_argument("--acquire", action="store_true",
                    help="take one acquisition (the trigger must be running)")
    ap.add_argument("--averages", type=int, default=16,
                    help="averages for --acquire (default 16, keep it small)")
    ap.add_argument("--sync", action="store_true",
                    help="one wide single-shot record of a whole LED cycle: is "
                         "the 81150A armed when the light goes off, or when it "
                         "comes on? Touches the scope only, and restores it")
    ap.add_argument("--sync-drive", action="store_true",
                    help="with --sync: put the 33220A into pulse mode at the "
                         "run's frequency and levels and enable both "
                         "generators, then restore them. Needs the relay on "
                         "the SourceMeter side, or --yes")
    ap.add_argument("--sync-points", type=int, default=500_000,
                    help="memory depth for --sync (default 500000 = 5 ns a "
                         "point over 2.5 ms). The sample rate follows from "
                         "this, and a sync pulse narrower than one point is "
                         "dropped, not attenuated")
    ap.add_argument("--dio", action="store_true",
                    help="toggle the shutter and relay lines and ask what moved")
    ap.add_argument("--outputs", action="store_true",
                    help="enable instrument outputs. NO SAMPLE CONNECTED")
    ap.add_argument("--measure", action="store_true",
                    help="one real transient scan. Needs a sample")
    ap.add_argument("--manual-shutter", action="store_true",
                    help="with --measure, prompt a person to work the shutter "
                         "(needed on 64-bit Python, where delib.dll cannot load)")
    ap.add_argument("--invert-output", action="store_true",
                    help="with --measure, send `:OUTP1:POL INV` so the device "
                         "is held at Vpre between pulses and stepped to Vcoll, "
                         "instead of the other way round")
    ap.add_argument("--vpre", type=float, default=0.0)
    ap.add_argument("--vcoll", type=float, default=-1.0)
    ap.add_argument("--delay-ns", type=float, default=88.0)
    ap.add_argument("--loops", type=int, default=2)
    ap.add_argument("--yes", action="store_true",
                    help="skip the confirmation prompts")
    a = ap.parse_args(argv)

    stages = ["offline", "discover", "local"]
    for name in ("read", "configure", "acquire", "sync", "dio", "outputs",
                 "measure"):
        if getattr(a, name):
            stages.append(name)

    report = Report(title="BACE bench check")
    report.stages_requested = stages

    print(__doc__.split("Typical use")[0].rstrip())
    print(f"\nstages this run: {', '.join(stages)}")
    if a.dio or a.outputs or a.measure:
        print("\n  !! A stage in this run MOVES HARDWARE.")
        if a.dio:
            print("     --dio walks all four states of DIO modules 0 and 1 and")
            print("     measures the Keithley in each: V at I=0, then I at V=0.")
            print("     Photocurrent means the device is on the Keithley and the")
            print("     light is reaching it; the voltage compliance rail means")
            print("     an open circuit. That says which line is the shutter and")
            print("     which switches the electrical path -- by measurement,")
            print("     with nobody in the room.")
            print()
            print("     LEAVE THE SAMPLE CONNECTED and the LED on: with no")
            print("     device there is nothing to tell the states apart.")
            print("     Nothing is forced into it -- 0 A for one reading, 0 V")
            print("     for the other. Both generator outputs are OFF for the")
            print("     whole stage and put back at the end, and each DIO line")
            print("     is returned to the position it was found in.")
        if a.outputs:
            print("     --outputs drives the 81150A and the Keithley at 0 V.")
            print("     Disconnect the sample before continuing.")
        if a.measure:
            print("     --measure runs a real transient into a connected sample.")
        if not a.yes:
            answer = input("\n     Type 'yes' to continue: ").strip().lower()
            if answer != "yes":
                print("     stopped.")
                return 2
    print()

    # -- offline ---------------------------------------------------------
    # The regression is the strongest evidence the harness can produce -- the
    # maths core reproducing a real 2026-08-07 run to 0.6 ulp -- and it was
    # skipping on the lab PC because the archive lives on the other machine.
    # A copy of one run folder now ships beside the package so every session
    # re-proves the *deployed* code, not only mine.
    checks.stage_offline(report, a.archive or _default_archive())

    rig_config, run_config, smu_config, led_drive, pinned = _load_configs(
        report, a.rig, a.run)

    # -- instruments -----------------------------------------------------
    rm = checks.open_visa(report)
    checks.stage_discover(report, rm, {
        "scope": rig_config.scope_address,
        "bias": rig_config.bias_address,
        "sourcemeter": rig_config.sourcemeter_address,
        "led": rig_config.led_address,
    })
    checks.stage_local(report, rig_config)

    if a.read:
        checks.stage_read(report, rm, rig_config)
    if a.configure:
        checks.stage_configure(report, rm, rig_config, run_config,
                               led_drive=led_drive, pinned=pinned,
                               force=a.force_configure)
    if a.acquire:
        checks.stage_acquire(report, rm, rig_config, run_config, a.averages)
    if a.sync:
        # Before --measure, not after. A charge measured with the extraction
        # pulse in the wrong half of the LED cycle is a charge of something
        # else, and it looks exactly like a good one.
        sync.stage_sync(report, rm, rig_config, run_config,
                        led_drive=led_drive, drive=a.sync_drive,
                        confirm=(lambda q: "yes") if a.yes else _confirm,
                        points=a.sync_points)
    if a.dio:
        checks.stage_dio(report, rig_config, _confirm, rm,
                         smu_config=smu_config,
                         restore_outputs=not a.leave_outputs_off,
                         listen=a.listen)
    if a.outputs:
        checks.stage_outputs(report, rm, rig_config, run_config,
                             led_drive=led_drive)
    if a.measure:
        checks.stage_measure(report, rm, rig_config, run_config,
                             averages=a.averages, loops=a.loops, vpre=a.vpre,
                             vcoll=a.vcoll, delay_ns=a.delay_ns,
                             invert_output=a.invert_output,
                             manual_shutter=a.manual_shutter, ask=_ask,
                             folder=os.path.join(a.out, "first-light"))

    # -- report ----------------------------------------------------------
    os.makedirs(a.out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(a.out, f"bench_{stamp}")
    paths = [report.write_text(base + ".txt"),
             report.write_html(base + ".html"),
             report.write_json(base + ".json")]

    print(render_text(report))
    print("written:")
    for p in paths:
        print("   ", os.path.abspath(p))
    print("\nSend the .json back — it carries the full command transcript.")

    c = report.counts
    return 1 if c[FAILED] else 0


def _load_configs(report: Report, rig_path, run_path):
    """Load rig.toml / run.toml, falling back to the built-in defaults."""
    from bace.experiment.rig import RigConfig
    from bace.experiment.transient import RunConfig

    from bace.drivers.keithley2400 import SourceMeterConfig

    rig_config, run_config = RigConfig(), RunConfig()
    smu_config = SourceMeterConfig()
    led_drive = (1.020, 0.4)
    """(high, low) at the 33220A output. The 81150A is armed by this
    generator's Sync, so anything that sets one has to know the other."""
    pinned = (0.9, -1.0, 88.0)
    """(vpre, vcoll, delay_ns) at the device: the archive's point until
    run.toml says otherwise."""

    def load(c):
        nonlocal rig_config, run_config, smu_config, led_drive, pinned
        from bace.config import load_rig, load_run
        p_rig = rig_path or checks._find("rig.toml")
        p_run = run_path or checks._find("run.toml")
        if p_rig:
            rig_config = load_rig(p_rig)
            c.data["rig.toml"] = p_rig
        else:
            c.warn("rig.toml not found; using built-in defaults")
        if p_run:
            spec, run_config, drive, smu_config, _, _ = load_run(p_run)
            led_drive = (drive.level, drive.low_level)
            pinned = (spec.vpre, spec.vcoll, spec.delay_ns)
            c.data["pinned (vpre / vcoll / delay_ns)"] = (
                f"{spec.vpre:g} V / {spec.vcoll:g} V / {spec.delay_ns:g} ns")
            c.data["run.toml"] = p_run
            c.data["LED high / low (V)"] = f"{drive.level:g} / {drive.low_level:g}"
        else:
            c.warn("run.toml not found; using built-in defaults")

    report.run("load configuration", "offline", load)
    return rig_config, run_config, smu_config, led_drive, pinned


if __name__ == "__main__":
    sys.exit(main())
