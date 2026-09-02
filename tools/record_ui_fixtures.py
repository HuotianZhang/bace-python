#!/usr/bin/env python3
"""Record the fixtures `ui/` develops against, off a running service.

The console has to be workable with no bench and no service running, and
`docs/ui-plan.md` M0 names four fixtures for that. Two of them already exist in
the repo — the acceptance journals of 2026-09-02 and the `bace-run/2` HDF5
beside them — and two do not, because nothing writes them:

* the **`Hello` frame**, which carries `data.bench`: the whole `GET /bench`
  snapshot, the rail and the chain strip. A journal contains none; `Hello` is
  never journalled, and `InstrumentState` says what one step changed rather
  than what the bench is;
* a **`bace-jv/2` HDF5** and its curves, because the one HDF5 in the repo is a
  transient run and the journal payload policy reduces `JVCurveDone` to
  `metrics + label + n_points` with no arrays. The J-V chart would otherwise
  have nothing to draw.

And one shape exists only while a service is running: a **live** `StepDone`,
its traces decimated with `stride` and `n_full`. The journal drops the traces
and the HDF5 keeps them undecimated, so neither is that frame.

So this records all of it against whatever service it is pointed at:

    python3 tools/record_ui_fixtures.py                        # --sim, port 8900
    py -3 tools/record_ui_fixtures.py --tag rig --url http://127.0.0.1:8900

`--tag` names the bench the recording came off (`sim` by default) and is part
of every file name, because a simulated J-V is a plausible-looking curve and
must never be mistaken for a measured one. Nothing here writes to the bench
beyond the two short runs it asks for, and on the rig those are a real J-V and
a real one-point scan — run it when the sample can take them.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "ui", "fixtures")


# --- the service, over plain HTTP -------------------------------------------

def _request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        raise SystemExit(f"{method} {url} -> {e.code}\n{detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"{method} {url}: {e.reason}\n"
                         f"start one with:  python3 -m bace.service --sim --fast --port 8900")


def _dump(path: str, obj, *, jsonl: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        if jsonl:
            for line in obj:
                fh.write(json.dumps(line) + "\n")
        else:
            json.dump(obj, fh, indent=1)
            fh.write("\n")
    size = os.path.getsize(path)
    print(f"  {os.path.relpath(path, REPO)}  {size/1024:.0f} KB")


# --- one run, with every frame it produced ----------------------------------

async def _hello(ws_url: str) -> dict:
    """The first frame of a fresh connection, and nothing else."""
    import websockets

    async with websockets.connect(ws_url, max_size=None) as ws:
        frame = json.loads(await ws.recv())
    if frame.get("type") != "Hello":
        raise SystemExit(f"first frame was {frame.get('type')!r}, not Hello")
    return frame


async def _run_and_capture(ws_url: str, base: str, body: dict,
                           *, timeout_s: float) -> tuple[list[dict], dict]:
    """Post one run with the socket already open, and keep every frame of it.

    The socket is opened first and the run posted from inside it, so the
    capture starts before `RunQueued` rather than after — which is also the
    only way to see the `StepPhase` frames, live-only and never replayed.
    """
    import websockets

    frames: list[dict] = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        first = json.loads(await ws.recv())
        if first.get("type") != "Hello":
            raise SystemExit(f"first frame was {first.get('type')!r}, not Hello")

        posted = _request(f"{base}/runs", "POST", body)
        run_id = posted["run_id"]
        deadline = time.monotonic() + timeout_s
        state = posted["state"]
        while time.monotonic() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            frames.append(frame)
            if frame.get("type") == "RunStateChanged" and frame.get("run_id") == run_id:
                state = frame["data"].get("state", state)
                if state in ("done", "stopped", "aborted", "failed", "blocked", "cancelled"):
                    break
        else:
            raise SystemExit(f"{body['module']} did not finish within {timeout_s} s")
    if state != "done":
        raise SystemExit(f"{body['module']} ended {state}, not done — see the service log")
    return frames, posted


def _folder_h5(record: dict) -> str | None:
    """The HDF5 a finished run wrote, from the folders on its record."""
    folders = record.get("folders") or ([record["folder"]] if record.get("folder") else [])
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".h5"):
                return os.path.join(folder, name)
    return None


# --- the recording ----------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8900", help="a running service")
    ap.add_argument("--out", default=DEFAULT_OUT, help="where the fixtures land")
    ap.add_argument("--tag", default=None, help="the bench it came off; default: the session's mode")
    ap.add_argument("--timeout-s", type=float, default=600.0, help="per run")
    ap.add_argument("--with-full-data", action="store_true",
                    help="also dump the transient's undecimated /runs/{id}/data (megabytes)")
    a = ap.parse_args(argv)

    base = a.url.rstrip("/")
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/events"
    index = _request(f"{base}/")
    tag = a.tag or index["session"]["mode"]
    fast = index["session"].get("fast")
    print(f"recording off {base} — session {index['session']['id']}, "
          f"mode {index['session']['mode']}{' fast' if fast else ''}, tag {tag!r}")

    # 1 · a J-V with a light curve and a dark one: the curves, and the HDF5.
    print("jv_bace …")
    jv_frames, jv_posted = asyncio.run(_run_and_capture(
        ws_url, base,
        {"module": "jv_bace", "name": "ui-fixture-jv",
         "params": {"start_v": -0.2, "stop_v": 1.2, "step_v": 0.02, "dark": True,
                    "led_v": 1.020, "both_directions": False}},
        timeout_s=a.timeout_s))
    jv_id = jv_posted["run_id"]
    _dump(os.path.join(a.out, f"stream_jv_{tag}.jsonl"), jv_frames, jsonl=True)
    _dump(os.path.join(a.out, f"jv_{tag}.json"), _request(f"{base}/runs/{jv_id}/data"))
    jv_record = _request(f"{base}/runs/{jv_id}")
    h5 = _folder_h5(jv_record)
    if h5 is None:
        print("  ! no HDF5 on the J-V's record; the curves JSON is the whole fixture")
    else:
        dest = os.path.join(a.out, f"jv_{tag}.h5")
        os.makedirs(a.out, exist_ok=True)
        shutil.copyfile(h5, dest)
        print(f"  {os.path.relpath(dest, REPO)}  {os.path.getsize(dest)/1024:.0f} KB  "
              f"(from {h5})")

    # 2 · a short transient scan: live StepDone frames, decimated as the wire
    #     sends them, and the StepPhase frames that exist nowhere else.
    print("bace …")
    bace_frames, bace_posted = asyncio.run(_run_and_capture(
        ws_url, base,
        {"module": "bace", "name": "ui-fixture-bace",
         "params": {"axis_name": "delay_ns", "axis_start": 0.0, "axis_stop": 200.0,
                    "axis_step": 100.0, "centre_on_voc": False, "n_loops": 2,
                    "vpre": 1.0, "vcoll": -2.0, "store_shots": False}},
        timeout_s=a.timeout_s))
    bace_id = bace_posted["run_id"]
    _dump(os.path.join(a.out, f"stream_bace_{tag}.jsonl"), bace_frames, jsonl=True)
    if a.with_full_data:
        # Megabytes of undecimated trace, and the repo already has better: the
        # acceptance `bace-run/2` is a real one. `make_ui_fixtures.py` renders
        # it in exactly this endpoint's shape.
        _dump(os.path.join(a.out, f"bace_{tag}.json"), _request(f"{base}/runs/{bace_id}/data"))

    # 3 · the bench snapshot, as the console gets it on connect — recorded
    #     last, so it carries the V_oc the J-V measured and both runs on the
    #     modules' `last`, which is what the rail and the cards develop against.
    _request(f"{base}/bench/read", "POST")
    _dump(os.path.join(a.out, f"hello_{tag}.json"), asyncio.run(_hello(ws_url)))

    print("done. `python3 tools/make_ui_fixtures.py` builds the derived ones "
          "(the acceptance HDF5 as JSON) beside these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
