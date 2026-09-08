#!/usr/bin/env python3
"""Move the shutter, and touch nothing else on the bench.

    py -3 tools/shutter.py                 read the line, change nothing
    py -3 tools/shutter.py --open          light reaches the sample
    py -3 tools/shutter.py --shut          the dark state

The counterpart of `tools/relay.py`, for the other Deditec line -- and
deliberately without its interlock. The relay carries the device terminals,
and throwing it under a live source is the one software mistake on this rig
that costs hardware rather than a dataset; the shutter carries light, `shut`
is its safe state, and nothing it can do is worse than a trace taken in the
wrong illumination. So this asks no other instrument for permission. The one
thing it refuses is the one move that is not a shutter move: a `--module-nr`
that names the relay.

Nothing else is opened, on purpose -- no pyvisa, no scope, no generators, no
service. `[dio]` comes from `drivers.delib.dio_settings`, which reads the
block with `tomllib` rather than through `bace.config.load_rig`: that reaches
`core.axis` and so imports numpy, and this program has to run under whichever
interpreter can load the DELIB build on the machine, which may have nothing
installed at all. Outside the standard library it imports two driver modules
and nothing more.

For a switch instead of a command line, `examples/shutter_console/` serves the
same line as one page in a browser -- a standalone folder that shares nothing
with this file but the hardware. Only one process can hold the module, so run
one or the other.

BITNESS
    `ctypes` loads only the DELIB build matching the interpreter. The copy on
    the rig -- `D:\\BACE\\shutter\\builds\\data\\delib.dll` -- is 32-bit, so
    there this is

        py -3.11-32 tools/shutter.py --open

    A machine with the 64-bit DELIB (`delib64.dll` in System32) runs it under
    `py -3`. `drivers/delib.py` picks whichever matches the interpreter;
    `--delib` overrides both it and `[dio] dll_path`.

The line is left where the command put it. That is why this releases the
module handle instead of closing it: `Shutter.close()` shuts the line first,
which is right at the end of a measurement and wrong here -- a plain read has
no business closing a shutter somebody opened.

One process at a time owns the DELIB module. Stop the service (and the
LabVIEW VI) before running this, or `DapiOpenModule` answers 0 and the reason
will look like missing hardware.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Run from a checkout without installing anything, as `tools/relay.py` does:
# the 32-bit interpreter this may need is rarely the one `pip install -e .`
# was run under.
sys.path.insert(0, REPO_ROOT)

from bace.drivers.delib import (DioError, dio_settings,  # noqa: E402
                                find_rig, make_line)
from bace.drivers.shutter import DEFAULT_CHANNEL, ShutterError  # noqa: E402

SETTLE_S = 0.4
"""After the line moves, before it is read back. What `bench.checks._dio_backend`
gives the same driver: `shutter_lv2012.vi` waits 0.1 s, a mechanical shutter
finishing its travel wants more, and here it costs only the exit."""

STATE = {1: "open  — light reaches the sample",
         0: "shut  — the dark state"}

UNREADABLE = "unreadable — this DELIB build has no DapiDOReadback32"


def main(argv=None, *, make=None) -> int:
    """`make(cfg, module_nr=…, channel=…, dll_path=…, settle_s=…)` builds the
    line. It defaults to the real driver; the tests pass a simulated one."""
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="Reading is always safe and changes nothing. Do that first.")
    move = ap.add_mutually_exclusive_group()
    move.add_argument("--open", dest="open_", action="store_true",
                      help="open the shutter — light reaches the sample")
    move.add_argument("--shut", action="store_true",
                      help="shut the shutter — the dark state")
    ap.add_argument("--rig", default=None, help="path to rig.toml")
    ap.add_argument("--delib", default=None,
                    help="path to the DELIB dll. Overrides [dio] dll_path and "
                         "the search in drivers/delib.py")
    ap.add_argument("--module-nr", type=int, default=None,
                    help="DIO module number (default: [dio] shutter_module_nr)")
    ap.add_argument("--channel", type=int, default=None,
                    help=f"digital output channel (default: {DEFAULT_CHANNEL})")
    ap.add_argument("--settle", type=float, default=SETTLE_S, metavar="SECONDS",
                    help="after the line moves, before it is read back "
                         f"(default: {SETTLE_S:g})")
    ap.add_argument("--force", action="store_true",
                    help="drive the line even where it is the relay's module. "
                         "Only with the device disconnected")
    a = ap.parse_args(argv)

    try:
        rig_path = find_rig(a.rig)
        cfg = dio_settings(rig_path)
    except DioError as exc:
        print(f"rig.toml: {exc}")
        return 2

    module_nr = cfg["shutter_module_nr"] if a.module_nr is None else a.module_nr
    # `[dio] channel` is in rig.toml but `RigConfig` has no field for it, so
    # the service and the bench harness both drive `DEFAULT_CHANNEL`. Follow
    # them rather than the file -- a tool that moved a different line from the
    # service would be worse than a stale key -- and say so when they differ.
    channel = DEFAULT_CHANNEL if a.channel is None else a.channel
    dll_path = a.delib or cfg["dll_path"]
    want = 1 if a.open_ else 0 if a.shut else None
    found = f"(whichever build this {struct.calcsize('P') * 8}-bit interpreter can load)"

    print(f"rig.toml           = {rig_path or '(built-in defaults)'}")
    print(f"shutter            = DIO module {module_nr}, id {cfg['module_id']}, "
          f"channel {channel}")
    print(f"DELIB              = {dll_path or found}")
    if a.channel is None and cfg["channel"] != DEFAULT_CHANNEL:
        print(f"  note: [dio] channel = {cfg['channel']} is read by nothing — "
              f"RigConfig has no such field and the service drives channel "
              f"{DEFAULT_CHANNEL}. Pass --channel to mean it.")
    if module_nr == cfg["relay_module_nr"]:
        print(f"  module {module_nr} is the RELAY, not the shutter.")
        if want is not None and not a.force:
            print("\nREFUSING to drive it: this line moves the device between "
                  "the amplifier and the Keithley, and doing that under a live "
                  "source is what damages hardware. Use tools/relay.py, which "
                  "proves both outputs off first.")
            return 2

    try:
        line = (make or make_line)(cfg, module_nr=module_nr, channel=channel,
                                   dll_path=dll_path, settle_s=a.settle)
        line.open()
    except (ShutterError, OSError) as exc:
        print(f"\nno DIO on this machine: {exc}")
        return 2

    try:
        before = line.read_line()
        print(f"line               = {before}  {STATE.get(before, UNREADABLE)}")
        if want is None:
            return 0
        if before == want:
            print(f"already {'open' if want else 'shut'}; nothing to do")
            return 0
        line.set_line(want)
        after = line.read_line()
        shown = after if after is not None else want
        print(f"moved              = {before} -> {shown}  {STATE[want]}")
        if after is not None and after != want:
            print("  the line did not take the value it was given")
            return 2
        return 0
    finally:
        # Not close(): that shuts the line first, and this program's whole
        # contract is that it leaves the shutter where the command put it.
        line.release()


if __name__ == "__main__":
    raise SystemExit(main())
