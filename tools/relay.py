#!/usr/bin/env python3
"""Read — and only deliberately move — the relay that owns the device terminals.

    py -3 tools/relay.py                       read the position, change nothing
    py -3 tools/relay.py --to sourcemeter      connect the Keithley
    py -3 tools/relay.py --to amplifier        connect the x4 amplifier

The device terminals are shared: the relay connects them either to the
amplifier fed by the 81150A, or to the Keithley 2400. Throwing it while either
source is driving is the one software mistake on this rig that costs hardware
rather than a dataset, so this refuses to move until both outputs read OFF *on
the instruments*, not on a driver's own flag.

`BiasRouter` deliberately exposes no bare "set the position" call — a caller
inside the measurement must not be able to express the unsafe state, only
`dc()` and `transient()`. This is the other case: a person at the bench who
knows what they are doing and needs the relay somewhere specific between runs.
It keeps the interlock and drops the context manager.

Reading is free and always safe. Do that first; the un-driven state is the
amplifier, so a relay nobody has driven since power-on is on the transient path.
"""
from __future__ import annotations

import argparse
import sys

POSITIONS = {"amplifier": 0, "sourcemeter": 1}


def _live(address: str, query: str) -> bool | None:
    """Is this instrument's output on? None when it cannot be asked."""
    try:
        import pyvisa
        rm = pyvisa.ResourceManager()
    except Exception:
        return None
    try:
        res = rm.open_resource(address)
        res.timeout = 5000
        try:
            return str(res.query(query)).strip().startswith("1")
        finally:
            res.close()
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", choices=sorted(POSITIONS),
                    help="move the relay. Omit to read and change nothing")
    ap.add_argument("--rig", default=None, help="path to rig.toml")
    ap.add_argument("--force", action="store_true",
                    help="move even if an output cannot be proven off. "
                         "Only with the device disconnected")
    a = ap.parse_args(argv)

    sys.path.insert(0, __file__.rsplit("tools", 1)[0] or ".")
    from bace.bench import checks
    from bace.config import load_rig
    from bace.drivers.shutter import Shutter

    path = a.rig or checks._find("rig.toml")
    rig = load_rig(path) if path else __import__(
        "bace.experiment.rig", fromlist=["RigConfig"]).RigConfig()
    print(f"rig.toml           = {path or '(built-in defaults)'}")
    print(f"relay              = DIO module {rig.relay_module_nr}, "
          f"id {rig.dio_module_id}")

    try:
        relay = Shutter(rig.dio_dll_path or None, module_id=rig.dio_module_id,
                        module_nr=rig.relay_module_nr)
    except Exception as exc:
        print(f"\nno DIO on this machine: {exc}")
        return 2
    with relay.open() as r:
        before = r.read_line()
        name = {0: "amplifier (81150A reaches the device)",
                1: "sourcemeter (Keithley reaches the device)"}.get(before, "?")
        print(f"position           = {before}  {name}")
        if before is None:
            print("  the line could not be read back — this DELIB build has no "
                  "DapiDOReadback32, so nothing here can prove where the relay is")

        if a.to is None:
            return 0

        want = POSITIONS[a.to]
        if want == before:
            print(f"already on the {a.to} side; nothing to do")
            return 0

        bias = _live(rig.bias_address, ":OUTP1?")
        smu = _live(rig.sourcemeter_address, ":OUTP?")
        print(f"81150A :OUTP1?     = {'ON' if bias else 'OFF' if bias is False else 'unreadable'}")
        print(f"Keithley :OUTP?    = {'ON' if smu else 'OFF' if smu is False else 'unreadable'}")

        blocked = [n for n, v in (("81150A", bias), ("Keithley", smu)) if v is not False]
        if blocked and not a.force:
            print()
            print("REFUSING to move: " + ", ".join(blocked) +
                  " is driving, or could not be asked. Throwing the relay under "
                  "load is what damages a device rather than a dataset. Turn the "
                  "outputs off and try again, or pass --force with the device "
                  "disconnected.")
            return 2

        r.set_line(want)
        after = r.read_line()
        print(f"moved              = {before} -> {after}")
        if after is not None and after != want:
            print("  the line did not take the value it was given")
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
