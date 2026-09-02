#!/usr/bin/env python3
"""Render the acceptance HDF5 as the fixture the charts can actually read.

`acceptance/20260902_service-vs-labview/service_153722/run20260902_153722.h5`
is the rig day's transient run at full precision, and it is the best chart
material in the repo — but a browser cannot open HDF5, and vendoring a reader
to read one file would be a strange trade. So this writes the same numbers in
the shape `GET /runs/{id}/data` answers, which is the shape the console reads
from a live service anyway:

    python3 tools/make_ui_fixtures.py        # -> ui/fixtures/transient_*.json

What it cannot take from the file it says so about, rather than inventing:

* **the per-shot traces.** This run had `store_shots` off, so the HDF5 keeps
  the loop-averaged traces and nothing per shot. `last_shot` therefore carries
  the running integral and the window, and names the averaged traces as its
  source instead of repeating them;
* **`t0_int_record_s`**, the start of the integration window in record time.
  It needs the record's own origin (`:WAV:XOR?`), which the recorder does not
  store. The service says it in as many words in a `Notice` — "integration
  starts 120.5 ns after the trigger; this record puts the trigger 199.5 ns in,
  so that is 320.0 ns of record time" — so the session's journal is read for
  it, and the geometry of the timebase is the fallback. The fixture records
  which of the two it used.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCEPTANCE = os.path.join(REPO, "acceptance", "20260902_service-vs-labview")
DEFAULT_H5 = os.path.join(ACCEPTANCE, "service_153722", "run20260902_153722.h5")
DEFAULT_OUT = os.path.join(REPO, "ui", "fixtures")

NOTICE = re.compile(r"integration starts ([\d.]+) ns after the trigger; this record puts the "
                    r"trigger ([\d.]+) ns in, so that is ([\d.]+) ns of record time")

SIGNIFICANT = 9
"""Digits kept per trace sample. The scope digitises to eight bits and the
traces are averages of 200 of them, so nine digits is already far past what
the instrument resolves — and it halves a 400 KB fixture."""


def _round(value: float, digits: int = SIGNIFICANT) -> float:
    if value == 0 or value != value:
        return float(value)
    from math import floor, log10
    return round(float(value), digits - 1 - int(floor(log10(abs(float(value))))))


def _list(array, digits: int = SIGNIFICANT) -> list:
    return [_round(float(v), digits) for v in array]


def _t0_from_journals(started: str) -> tuple[float | None, str]:
    """The window's start in record time, from the service's own sentence.

    `started` is the run's own timestamp; the journal wanted is the session
    that was open when it ran, which is the newest session file stamped no
    later than the run. The sentence is identical in every session of that
    day, but reading it out of the wrong one would be luck, not a method.
    """
    stamp = started.replace("-", "").replace("T", "_").replace(":", "")[:15]
    files = sorted(glob.glob(os.path.join(ACCEPTANCE, "journals", "*.jsonl")))
    earlier = [p for p in files if os.path.basename(p)[:15] <= stamp]
    for path in reversed(earlier or files):
        for line in open(path, encoding="utf-8"):
            match = NOTICE.search(line)
            if match:
                return float(match.group(3)) * 1e-9, f"journal · {os.path.basename(path)}"
    return None, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--h5", default=DEFAULT_H5)
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    import h5py
    import numpy as np

    f = h5py.File(a.h5, "r")
    schema = f.attrs.get("schema", "")
    if not str(schema).startswith("bace-run/"):
        raise SystemExit(f"{a.h5}: schema {schema!r}, not a transient run")

    axis_attrs = dict(f["axis"].attrs)
    run_attrs = dict(f["config/run"].attrs)
    metadata = dict(f["metadata"].attrs)

    time = np.asarray(f["traces/time"])
    light = np.asarray(f["traces/light"])
    dark = np.asarray(f["traces/dark"])
    photo = np.asarray(f["traces/photocurrent"])
    dt = float(time[1] - time[0])

    q_all = np.asarray(f["charge/q"])                  # (loops, steps)
    q_mean = np.asarray(f["charge/mean"])
    q_std = np.asarray(f["charge/std"])

    t0_record, source = _t0_from_journals(str(metadata.get("started", "")))
    if t0_record is None:
        # The geometry the driver sets: `:TIM:POS` is four divisions in, and
        # the record is centred on it, so the record starts half a record
        # before that. Half a sample out from the instrument's own `:WAV:XOR?`.
        per_div = float(run_attrs.get("timebase_ns_per_div", 200.0)) * 1e-9
        trace_t0 = per_div * 4.0 - (time.size * dt) / 2.0
        t0_record = float(run_attrs.get("t0_int_s", 0.0)) - trace_t0
        source = "timebase geometry (the record's own origin is not stored)"

    after = (np.arange(photo.shape[1]) * dt) > t0_record
    cumulative = np.cumsum(photo[-1][after]) * dt

    payload = {
        "axis": {"name": str(axis_attrs.get("name")), "unit": str(axis_attrs.get("unit", "")),
                 "start": float(axis_attrs.get("start")), "stop": float(axis_attrs.get("stop")),
                 "step": float(axis_attrs.get("step")),
                 "centre_on_voc": bool(axis_attrs.get("centre_on_voc", False))},
        "values": _list(f["axis/values"], 9),
        "q_mean": _list(q_mean, 9),
        "q_std": _list(q_std, 9),
        "q_all": [_list(row, 9) for row in q_all],
        "time_s": _list(time - time[0], 12),
        "light": [_list(row) for row in light],
        "dark": [_list(row) for row in dark],
        "photo": [_list(row) for row in photo],
        "last_shot": {
            "traces": "the loop-averaged ones above — this run had store_shots off",
            "cumulative_q": _list(cumulative),
            "t0_int_record_s": _round(t0_record, 6),
            "t0_int_record_source": source,
            # `StepDone.index` counts shots flat across every loop and step
            # ((loop-1)*n_steps + step-1), not steps: two loops of one step end
            # at index 1, not 0.
            "index": int(q_all.shape[0] * photo.shape[0] - 1),
            "loop": int(q_all.shape[0]),
            "step": int(photo.shape[0]),
            "axis_value": float(np.asarray(f["axis/values"])[-1]),
            "q": _round(float(q_all[-1][-1]), 9),
        },
        "kept": int(f.attrs.get("loops_completed", q_all.shape[0]) * q_all.shape[1]),
        "requested": int(f.attrs.get("loops_planned", q_all.shape[0]) * q_all.shape[1]),
        "voc": float(axis_attrs["voc"]) if "voc" in axis_attrs else None,
        "dt": dt,
        "_fixture": {
            "from": os.path.relpath(a.h5, REPO).replace("\\", "/"),
            "schema": str(schema),
            "shape": "GET /runs/{id}/data, for a bace run",
            "written_by": "tools/make_ui_fixtures.py",
            "significant_digits": SIGNIFICANT,
            "metadata": {k: (v.item() if hasattr(v, "item") else str(v)) for k, v in metadata.items()},
            "run": {k: (v.item() if hasattr(v, "item") else str(v)) for k, v in run_attrs.items()},
        },
    }

    stamp = os.path.basename(a.h5).replace("run", "").replace(".h5", "")
    path = os.path.join(a.out, f"transient_{stamp}.json")
    os.makedirs(a.out, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh)
        fh.write("\n")
    print(f"  {os.path.relpath(path, REPO)}  {os.path.getsize(path)/1024:.0f} KB  "
          f"· t0_int in record time from {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
