#!/usr/bin/env python3
"""Work out which Deditec line is the shutter and which is the relay.

The block diagrams do not settle it. In `BACE_Mehrdad.vi` both event frames
begin by driving module 0 channel 0 HIGH and module 1 channel 0 HIGH, and only
module 1 is ever driven LOW again (between the light and dark acquisitions).
So module 1 behaves like the shutter there. But the newer VI's front panel
labels its shutter control `ShutterModule Nr = 0`. Both cannot be right, and
getting it wrong means the software opens the shutter when it means to move the
bias path.

Thirty seconds on the bench settles it. Run this with **nothing connected to
the device terminals** -- neither the amplifier nor the SourceMeter -- so a
relay throw is harmless, and with the LED able to run so a shutter movement is
visible.

    py -3.11-32 tools/identify_dio.py --delib "D:/BACE/shutter/builds/data/delib.dll"

You will hear the relay click and see the shutter move. Note which module does
which, then set `MODULE_NR` in bace/drivers/routing.py and the shutter's module
number in your config.
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time

MODULE_ID = 9
CHANNEL = 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--delib", required=True, help="path to delib.dll (32-bit)")
    ap.add_argument("--modules", default="0,1", help="module numbers to test")
    ap.add_argument("--dwell", type=float, default=2.0, help="seconds in each state")
    a = ap.parse_args(argv)

    lib = ctypes.WinDLL(a.delib)
    lib.DapiOpenModule.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    lib.DapiOpenModule.restype = ctypes.c_ulong
    lib.DapiDOSet1.argtypes = [ctypes.c_ulong] * 3
    lib.DapiDOSet1.restype = None
    lib.DapiCloseModule.argtypes = [ctypes.c_ulong]
    lib.DapiCloseModule.restype = ctypes.c_ulong

    print("\n  Nothing should be connected to the device terminals.")
    print("  Watch the shutter and listen for the relay.\n")
    if input("  Ready? [y/N] ").strip().lower() != "y":
        print("  aborted"); return 1

    findings = {}
    for nr in [int(x) for x in a.modules.split(",")]:
        handle = int(lib.DapiOpenModule(MODULE_ID, nr))
        if handle == 0:
            print(f"\n  module {nr}: DapiOpenModule returned 0 — not present")
            findings[nr] = "absent"
            continue
        print(f"\n  module {nr} (ID {MODULE_ID}, channel {CHANNEL})")
        try:
            for _ in range(3):
                print("     -> 1"); lib.DapiDOSet1(handle, CHANNEL, 1); time.sleep(a.dwell)
                print("     -> 0"); lib.DapiDOSet1(handle, CHANNEL, 0); time.sleep(a.dwell)
        finally:
            lib.DapiDOSet1(handle, CHANNEL, 0)
            lib.DapiCloseModule(handle)
        ans = input("     what moved?  [s]hutter / [r]elay / [n]othing : ").strip().lower()
        findings[nr] = {"s": "shutter", "r": "relay", "n": "nothing"}.get(ans, "unclear")

    print("\n  ── result ──")
    for nr, what in findings.items():
        print(f"     module {nr}  ->  {what}")
    relay = [nr for nr, w in findings.items() if w == "relay"]
    if relay:
        print(f"\n  Set MODULE_NR = {relay[0]} in bace/drivers/routing.py")
        print("  Then check the polarity: which value connects the amplifier?")
    else:
        print("\n  No relay identified — it may not be on this DIO at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
