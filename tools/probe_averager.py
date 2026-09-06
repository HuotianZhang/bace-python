#!/usr/bin/env python3
"""What the DSO9054H's averager actually does -- measured, not assumed.

Two rig runs on 2026-09-06 (sessions 024017-001 and -002) died on the driver's
stale-buffer check: `:WAV:COUN?` read right after `:RUN` answered the
*configured* count (16, then 200) inside one command round trip. The old
driver only ever read the count after `:STOP`, where it reported partial,
cumulative numbers. So before the driver is changed a third time, this asks
the scope directly, one question per stage, and prints every answer:

  A  what the count reads stopped and running, and whether it climbs
  B  whether :STOP / :RUN continues the average          (the 2026-09-06 defect)
  C  whether :CDIS empties it
  D  whether AVER OFF -> a single shot -> AVER ON empties it, or seeds it
  E  whether the LabVIEW double configure (COUN 64, :RUN, COUN 200) empties it
  F  whether a channel range write empties it            (the one reset seen so far)
  G  whether :DIGitize with averaging on completes the whole count

It touches only acquisition settings on the scope -- averaging, count, trigger
sweep (AUTO, so it acquires with nothing driving the sync), and CHAN2's range,
which it restores. It does not touch the generators, the shutter or the relay,
and the next run reconfigures everything it changed. Run it on the lab PC
from the repo root, with no run in progress:

    py -3 tools/probe_averager.py

The answers go to stdout and to runs/probe_averager_<timestamp>.txt.
"""
from __future__ import annotations

import datetime
import os
import sys
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    import pyvisa

    with open(os.path.join(ROOT, "rig.toml"), "rb") as f:
        rig = tomllib.load(f)
    address = rig["scope"]["address"]
    channel = int(rig["scope"].get("channel", 2))
    source = f"CHAN{channel}"

    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs(os.path.join(ROOT, "runs"), exist_ok=True)
    out_path = os.path.join(ROOT, "runs", f"probe_averager_{stamp}.txt")
    out = open(out_path, "w", encoding="utf-8")

    def say(text: str = "") -> None:
        print(text)
        out.write(text + "\n")
        out.flush()

    rm = pyvisa.ResourceManager()
    io = rm.open_resource(address)
    io.timeout = 20000
    t_start = time.monotonic()

    def w(cmd: str) -> None:
        io.write(cmd)
        say(f"  {time.monotonic() - t_start:7.3f}  > {cmd}")

    def q(cmd: str) -> str:
        t0 = time.monotonic()
        try:
            reply = io.query(cmd).strip()
        except Exception as exc:                            # noqa: BLE001
            reply = f"<{type(exc).__name__}: {exc}>"
        say(f"  {time.monotonic() - t_start:7.3f}  ? {cmd:<22} -> {reply}   ({(time.monotonic() - t0) * 1e3:.0f} ms)")
        return reply

    def count() -> str:
        return q(":WAV:COUN?")

    def run_for(seconds: float) -> None:
        w(":RUN;")
        time.sleep(seconds)
        w(":STOP;")

    say(f"probe_averager {stamp} on {address}, source {source}")
    say(f"IDN: {q('*IDN?')}")
    w(":SYST:HEAD OFF;")
    w(f":WAV:SOUR {source};")
    say(f"as found: AVER={q(':ACQ:AVER?')} COUN={q(':ACQ:AVER:COUN?')} SWE={q(':TRIG:SWE?')} "
        f"RANG={q(f':{source}:RANG?')}")
    original_range = q(f":{source}:RANG?")
    w(":TRIG:SWE AUTO;")            # acquire whether or not anything drives the sync

    say("\nA. running versus stopped reads. AVER ON, COUN 200, then :RUN and read the count as it goes")
    w(":STOP;:ACQ:AVER ON;:ACQ:AVER:COUN 200;")
    say(f"   stopped, before :RUN: {count()}")
    w(":RUN;")
    for _ in range(6):
        count()
        time.sleep(0.3)
    w(":STOP;")
    say(f"   stopped, after ~1.8 s: {count()}")
    say("   -> if the running reads all say 200 and the stopped read is smaller, :WAV:COUN? reports the")
    say("      configured count while running and the real one stopped; if they climb, it reports the real one")

    say("\nB. does :STOP / :RUN continue the average? (no reset in between)")
    before = count()
    run_for(0.5)
    say(f"   {before} -> {count()}   (larger = continued, ~same small number = restarted)")

    say("\nC. does :CDIS empty it? (scope stopped)")
    before = count()
    w(":CDIS;")
    say(f"   right after :CDIS, stopped: {count()}")
    run_for(0.5)
    say(f"   {before} -> {count()} after 0.5 s running   (small = emptied, larger than before = not)")

    say("\nD. AVER OFF, one single shot, then AVER ON again: emptied, or seeded as complete?")
    before = count()
    w(":STOP;:ACQ:AVER OFF;")
    run_for(0.3)
    say(f"   after the single shot, AVER OFF: {count()}")
    w(":ACQ:AVER ON;:ACQ:AVER:COUN 200;")
    say(f"   AVER ON again, still stopped: {count()}")
    run_for(0.5)
    say(f"   {before} -> {count()} after 0.5 s running   (~50-250 = restarted from the shot; 200 = seeded complete)")

    say("\nE. the LabVIEW double configure: COUN 64, :RUN, COUN 200")
    before = count()
    w(":ACQ:AVER ON;:ACQ:AVER:COUN 64;")
    w(":RUN;")
    w(":ACQ:AVER ON;:ACQ:AVER:COUN 200;")
    time.sleep(0.5)
    w(":STOP;")
    say(f"   {before} -> {count()} after 0.5 s running")

    say("\nF. a channel range write")
    before = count()
    w(f":{source}:RANG {original_range};")
    run_for(0.5)
    say(f"   same range rewritten: {before} -> {count()}")
    before = count()
    try:
        grown = float(original_range) * 1.2
    except ValueError:
        grown = None
    if grown is not None:
        w(f":{source}:RANG {grown:g};")
        run_for(0.5)
        say(f"   range changed: {before} -> {count()}")
        w(f":{source}:RANG {original_range};")
        run_for(0.5)
        say(f"   range restored: {count()}")

    say("\nG. :DIGitize with averaging on: does it complete the whole count?")
    w(":STOP;:ACQ:AVER ON;:ACQ:AVER:COUN 32;:CDIS;")
    t0 = time.monotonic()
    w(f":DIG {source};")
    opc = q("*OPC?")
    say(f"   *OPC? -> {opc} after {time.monotonic() - t0:.2f} s; count now {count()}  (32 = it waits for the average)")
    say(f"   ADER after that: {q(':ADER?')}")

    w(":ACQ:AVER OFF;:RUN;")
    say(f"\nleft AVER OFF, running, range {q(f':{source}:RANG?')}. Written to {out_path}")
    io.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
