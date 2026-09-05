#!/usr/bin/env python3
"""Recompute Q from a stored run with a different integration window.

    python3 tools/reintegrate.py runs/<folder>/run<stamp>.h5
    python3 tools/reintegrate.py run.h5 --t0-int -5e-9 --width 1.2e-6
    python3 tools/reintegrate.py run.h5 --sweep=-40:40:2          # t0_int in ns (the = keeps the minus sign from argparse)
    python3 tools/reintegrate.py run.h5 --sweep=-40:40:2 --csv q_vs_t0.csv

Without `--t0-int` / `--width` the file's own window is applied, which
reproduces the stored `charge/mean` -- the check that the arithmetic here is
the run's. `--sweep a:b:step` (nanoseconds, from the field's arrival) prints
Q(t0_int) for every step; the integration zero is where Q stops changing as
the window starts earlier. `--latency` overrides the rig's `trigger_offset_s`
for a file taken before it was measured.

The window is measured from the field, as the run measured it: `t0_int_s` +
`axis/delay_ns` + `trigger_offset_s` after the trigger, in each step's own
record (`traces/trace_t0`). A file written before schema 3 has no `trace_t0`
and no field-referenced `t0_int_s` in its config; `--t0-int` is then required
and the record's origin comes from the timebase geometry, which the output
says.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys


def _parse_sweep(text: str):
    a, b, step = (float(x) for x in text.split(":"))
    if step <= 0 or b < a:
        raise SystemExit(f"--sweep {text}: want start:stop:step in ns with a positive step")
    import numpy as np
    return np.arange(a, b + step / 2, step) * 1e-9


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("h5")
    ap.add_argument("--t0-int", type=float, default=None,
                    help="window start in s from the field's arrival (default: the file's)")
    ap.add_argument("--width", type=float, default=None,
                    help="window length in s (default: the file's t_int_width_s)")
    ap.add_argument("--latency", type=float, default=None,
                    help="sync-to-field latency in s (default: the file's rig trigger_offset_s)")
    ap.add_argument("--sweep", default=None, help="t0_int values to try, ns: start:stop:step")
    ap.add_argument("--csv", default=None, help="write the table here as well")
    a = ap.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import numpy as np

    from bace.core.reintegrate import reintegrate, sweep
    from bace.storage.hdf5 import read_run

    run = read_run(a.h5)
    cfg = run["run_config"]
    old_file = "t0_int_reference" in cfg or "t_int_width_s" not in cfg
    if a.t0_int is None:
        if old_file:
            raise SystemExit(
                f"{a.h5} predates the pulse-referenced window (its t0_int_s is in "
                f"{cfg.get('t0_int_reference', 'record')!r} time); say where the window "
                "starts from the field with --t0-int, e.g. --t0-int -2e-9")
        a.t0_int = float(cfg["t0_int_s"])
    width = a.width if a.width is not None else float(cfg.get("t_int_width_s", 1.5e-6))
    values = np.asarray(run["values"], dtype=float)
    delays = np.asarray(run["delay_ns"], dtype=float)
    axis = str(run["axis"].get("name", "vpre"))
    print(f"{a.h5}")
    print(f"axis {axis}: {values.size} steps; loops completed "
          f"{run['attrs'].get('loops_completed', '?')}; shots stored: "
          f"{'yes' if run.get('shots') is not None else 'no'}")

    if a.sweep:
        t0s, q = sweep(run, _parse_sweep(a.sweep), width, a.latency)
        first = reintegrate(run, float(t0s[0]), width, a.latency)
        print(f"record origin from {first.trace_t0_source}; latency "
              f"{first.trigger_offset_s * 1e9:g} ns; width {width * 1e9:g} ns")
        head = ["t0_int_ns"] + [f"{axis}={v:g}" for v in values]
        rows = [[f"{t0 * 1e9:+.2f}"] + [f"{x:.5e}" for x in q[k]] for k, t0 in enumerate(t0s)]
        _table(head, rows)
        if a.csv:
            _csv(a.csv, head, rows)
        return 0

    r = reintegrate(run, a.t0_int, width, a.latency)
    stored = np.asarray(run["q_mean"], dtype=float)
    print(f"window {r.t0_int_s * 1e9:+g} ns from the field for {width * 1e9:g} ns; latency "
          f"{r.trigger_offset_s * 1e9:g} ns; record origin from {r.trace_t0_source}")
    head = [axis, "delay_ns", "window_start_ns", "window_end_ns", "Q_stored", "Q_here"]
    if r.q_std is not None:
        head += ["Q_std_here"]
    rows = []
    for i in range(values.size):
        row = [f"{values[i]:g}", f"{delays[i]:g}", f"{r.window_s[i, 0] * 1e9:.1f}",
               f"{r.window_s[i, 1] * 1e9:.1f}" + (" (past the record)" if r.clipped[i] else ""),
               f"{stored[i]:.5e}", f"{r.q_mean[i]:.5e}"]
        if r.q_std is not None:
            row.append(f"{r.q_std[i]:.3e}")
        rows.append(row)
    _table(head, rows)
    if a.csv:
        _csv(a.csv, head, rows)
    return 0


def _table(head, rows) -> None:
    widths = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
    for row in (head, *rows):
        print("  ".join(str(x).rjust(w) for x, w in zip(row, widths)))


def _csv(path, head, rows) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(head)
        w.writerows(rows)
    print(f"wrote {path}")


if __name__ == "__main__":
    raise SystemExit(main())
