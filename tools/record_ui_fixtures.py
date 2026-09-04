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
* a **J-V HDF5** (`bace-jv/4`) and its curves, because the one HDF5 in the repo is a
  transient run and the journal payload policy reduces `JVCurveDone` to
  `metrics + label + n_points` with no arrays. The J-V chart would otherwise
  have nothing to draw.

And two shapes exist only while a service is running: a **live** `StepDone`,
its traces decimated with `stride` and `n_full` -- the journal drops the traces
and the HDF5 keeps them undecimated, so neither is that frame -- and a
**`/bench` taken mid-scan**, where the four instruments the running step
implies carry `how: "inferred"`. A snapshot at rest has not one of them, and it
is the half of the rail the operator looks at for hours.

So this records all of it against whatever service it is pointed at:

    python3 tools/record_ui_fixtures.py                        # --sim, port 8900
    py -3 tools/record_ui_fixtures.py --tag rig --url http://127.0.0.1:8900

`--tag` names the bench the recording came off (`sim` by default) and is part
of every file name, because a simulated J-V is a plausible-looking curve and
must never be mistaken for a measured one. Nothing here writes to the bench
beyond the short runs it asks for, and on the rig those are real: a J-V, a
one-point scan, a two-node pipeline, a scan stopped after three shots, and one
more scan for the mid-run snapshot — run it when the sample can take them.

**The temperature tree is not one of them.** `--only tree` records it and
nothing else does, and it refuses to run against anything but `--sim`. It
drives a cryostat to 250 K and 280 K on a 60-second timeout, where a real
settle is 14 minutes to 2 hours (`docs/ui-rules.md` §5), and it answers the
`NeedsOperator` that follows *itself*, with the setpoint plus a tenth of a
kelvin. On a bench that would measure at room temperature and write 250.1 K
into the folder names and the metadata with `temperature_how = "operator"` —
a number that looks like somebody read it. Nobody did.
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


OPT_IN = frozenset({"tree"})
"""Steps that never run unless `--only` names them. See `_record_tree`."""


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
                           *, timeout_s: float, path: str = "/runs",
                           answer_pauses: bool = False,
                           stop_after_shots: int | None = None,
                           expect: str = "done") -> tuple[list[dict], dict]:
    """Post one run with the socket already open, and keep every frame of it.

    The socket is opened first and the run posted from inside it, so the
    capture starts before `RunQueued` rather than after — which is also the
    only way to see the `StepPhase` frames, live-only and never replayed.

    `answer_pauses` stands in for the operator: a `NeedsOperator` at a
    temperature node (`--sim` with no console is the pause path, on purpose)
    is answered with `POST /runs/{id}/resume` carrying a typed
    `temperature_k` a tenth of a kelvin off the setpoint and a note — so the
    recording holds the whole pair, the pause and the answer, and the
    `TemperatureRead(source="operator")` the answer produces. **It is a
    simulator device**: the number it types is invented, and on a bench that
    is a temperature nobody read written into the record. Its one caller
    (`_record_tree`) refuses to run against anything but `--sim`.
    """
    import websockets

    frames: list[dict] = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        first = json.loads(await ws.recv())
        if first.get("type") != "Hello":
            raise SystemExit(f"first frame was {first.get('type')!r}, not Hello")

        posted = _request(f"{base}{path}", "POST", body)
        run_id = posted["run_id"]
        shots = 0
        deadline = time.monotonic() + timeout_s
        state = posted["state"]
        # Read to `parked`, not to the terminal state: every run reaches its
        # outcome *through* `parked`, and a recording that stopped at `done`
        # would be a run the bench never made safe -- the one transition a
        # console keys on to say the worker is free again.
        while time.monotonic() < deadline:
            try:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            frames.append(frame)
            if (stop_after_shots is not None and frame.get("type") == "StepDone"
                    and frame.get("run_id") == run_id):
                shots += 1
                if shots == stop_after_shots:
                    # `after_shot`: the shot in flight completes and is kept,
                    # then the generator unwinds -- the honest verb, and the
                    # one whose `RunAborted(reason="requested")` a console
                    # has to render as a run that was stopped, not one that
                    # failed.
                    _request(f"{base}/runs/{run_id}/stop", "POST", {"mode": "after_shot"})
            if (answer_pauses and frame.get("type") == "NeedsOperator"
                    and frame.get("run_id") == run_id):
                detail = frame.get("data", {}).get("detail") or {}
                setpoint = float(detail.get("setpoint_k", 0.0))
                # A beat, so the recording shows a run *paused* rather than a
                # pause answered inside the same millisecond.
                await asyncio.sleep(0.3)
                _request(f"{base}/runs/{run_id}/resume", "POST",
                         {"temperature_k": round(setpoint + 0.1, 3),
                          "note": f"set to {setpoint:g} K by hand, reads {setpoint + 0.1:g} K"})
            if frame.get("type") == "RunStateChanged" and frame.get("run_id") == run_id:
                new_state = frame["data"].get("state", state)
                if new_state == "parked":
                    break
                state = new_state
                if state == "cancelled":          # never touched the bench
                    break
        else:
            raise SystemExit(f"{body.get('module', 'the pipeline')} did not finish within {timeout_s} s")
    if state != expect:
        raise SystemExit(f"{body.get('module', 'the pipeline')} ended {state}, not {expect} — "
                         f"see the service log")
    return frames, posted


def _bench_running(base: str, *, timeout_s: float = 180.0) -> dict:
    """`GET /bench` taken while a run is inside its acquisition.

    The rail's hardest rule is that `how: "inferred"` is not a read-back, and
    nothing recorded at rest carries a single inferred field: the overlay
    exists only while a run holds the worker (`service/live.py`). A snapshot at
    rest therefore develops and tests exactly the half of the rail that cannot
    be got wrong -- relay on the SourceMeter, bias off, shutter shut -- and
    none of the half the operator looks at for hours.

    So this posts a short scan and reads the snapshot while it is running, at
    a moment the shutter is open: relay amplifier, bias LIVE, LED pulsing,
    shutter open, every one of them inferred. Then it waits for the bench to
    come back to rest, so the recording leaves nothing running.
    """
    posted = _request(f"{base}/runs", "POST",
                      {"module": "bace", "name": "ui-fixture-rail",
                       "params": {"axis_name": "delay_ns", "axis_start": 0.0,
                                  "axis_stop": 100.0, "axis_step": 100.0,
                                  "centre_on_voc": False, "n_loops": 2, "vpre": 1.0,
                                  "vcoll": -2.0, "store_shots": False,
                                  "record_length": 500}})
    run_id = posted["run_id"]
    wanted = {"relay", "bias", "led", "shutter"}
    deadline = time.monotonic() + timeout_s
    snapshot = None
    while time.monotonic() < deadline:
        bench = _request(f"{base}/bench")
        instruments = bench.get("instruments") or {}
        if (wanted <= set(bench.get("inferred") or [])
                and (instruments.get("shutter") or {}).get("open")):
            snapshot = bench
            break
        if bench.get("state") == "idle" and (bench.get("run") or {}) == {}:
            break
        time.sleep(0.15)
    if snapshot is None:
        raise SystemExit(f"{run_id} never showed the four inferred instruments with the "
                         f"shutter open within {timeout_s} s")
    while time.monotonic() < deadline:            # leave the bench at rest
        if _request(f"{base}/bench").get("state") == "idle":
            break
        time.sleep(0.2)
    return snapshot


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
    ap.add_argument("--only", default=None,
                    help="record a subset: a comma-separated list of jv, bace, pipeline, "
                         "stopped, bench, hello, and tree (default: all but tree)")
    a = ap.parse_args(argv)
    only = set(a.only.split(",")) if a.only else None
    # `tree` is named or it does not happen. It is the one step that answers
    # its own operator pause, and a default that drove a real cryostat and
    # then fabricated the temperature it reached is not a default.
    wanted = lambda step: (step in only) if only else (step not in OPT_IN)  # noqa: E731

    base = a.url.rstrip("/")
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/events"
    # `/session`, not `/`: with `--ui` mounted the index redirects to `/ui/`,
    # and urllib follows it, so the recorder would try to parse the console's
    # HTML as JSON before it had recorded anything. The service the README
    # tells you to develop against is exactly the one with `--ui`.
    who = _request(f"{base}/session")
    tag = a.tag or who["mode"]
    print(f"recording off {base} — session {who['id']}, "
          f"mode {who['mode']}{' fast' if who.get('fast') else ''}, tag {tag!r}")

    # 1 · a J-V with a light curve and a dark one: the curves, and the HDF5.
    if wanted("jv"):
        _record_jv(a, base, ws_url, tag)

    # 2 · a short transient scan: live StepDone frames, decimated as the wire
    #     sends them, and the StepPhase frames that exist nowhere else.
    if wanted("bace"):
        _record_bace(a, base, ws_url, tag)

    # 3 · a pipeline: two module nodes under one `run_id`, each numbering its
    #     own shots from one. Nothing else in the fixture set has a `node_path`
    #     that is not the module's own name, and node identity is exactly what
    #     a store gets wrong quietly — the second node's shots landing on the
    #     first's, the loop's progress counter overwritten by the shot's.
    #     `record_length` is small on purpose: this fixture is about paths, and
    #     a full-length trace per shot would be half a megabyte of them.
    if wanted("pipeline"):
        _record_pipeline(a, base, ws_url, tag)

    # 3b · the tree `docs/ui-plan.md` M4 has to prove: two temperatures by two
    #      levels, with the pauses a `--sim` service takes at each temperature
    #      node and the operator's answers to them. It is the only recording
    #      with a `NeedsOperator`, an `OperatorResumed`, a loop `Progress` at
    #      three scales and a `TemperatureRead` typed by a person.
    if wanted("tree"):
        _record_tree(a, base, ws_url, tag, who["mode"])

    # 3c · a scan stopped after its third shot. `docs/ui-rules.md` §9: a
    #      truncated run is normal, not exceptional — the archive declares 100
    #      loops and holds 20 — and the console has to show kept of requested
    #      and the loops that were never acquired. Nothing else in the set
    #      carries a `RunAborted`, a `stopped` state, or a node that ended
    #      `stopped`; the acceptance journals have two `RunFailed`, which is a
    #      different ending.
    if wanted("stopped"):
        _record_stopped(a, base, ws_url, tag)

    # 4 · the bench mid-run: the `inferred` overlay, which exists nowhere at
    #     rest. Recorded before the read-back below, because it needs a run.
    if wanted("bench"):
        print("bench, mid-run …")
        _dump(os.path.join(a.out, f"bench_running_{tag}.json"),
              _bench_running(base, timeout_s=a.timeout_s))

    # 5 · the bench snapshot, as the console gets it on connect — recorded
    #     last, so it carries the V_oc the J-V measured and both runs on the
    #     modules' `last`, which is what the rail and the cards develop against.
    if wanted("hello"):
        _request(f"{base}/bench/read", "POST")
        _dump(os.path.join(a.out, f"hello_{tag}.json"), asyncio.run(_hello(ws_url)))

    print("done. `python3 tools/make_ui_fixtures.py` builds the derived ones "
          "(the acceptance HDF5 as JSON) beside these.")
    return 0


def _record_jv(a, base: str, ws_url: str, tag: str) -> None:
    print("jv_bace …")
    jv_frames, jv_posted = asyncio.run(_run_and_capture(
        ws_url, base,
        {"module": "jv_bace", "name": "ui-fixture-jv",
         # `pixel_area_cm2` is not in `run.toml`, so a J-V left to the recipe
         # reports amps and `density: null` -- and the first fixture recorded
         # here did, which left the console's mA/cm² path with nothing to draw
         # against. 0.04 cm² is a plausible pixel and it is simulated anyway:
         # what the fixture is for is the *shape* of a curve that has a
         # density, in the unit the wire actually carries it in.
         "params": {"start_v": -0.2, "stop_v": 1.2, "step_v": 0.02, "dark": True,
                    "led_v": 1.020, "both_directions": False,
                    "pixel_area_cm2": 0.04}},
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


def _record_bace(a, base: str, ws_url: str, tag: str) -> None:
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


def _record_pipeline(a, base: str, ws_url: str, tag: str) -> None:
    print("pipeline …")
    tree = {"kind": "loop", "loop": "repeat", "count": 2, "children": [
        {"kind": "module", "module": "bace",
         "params": {"axis_name": "delay_ns", "axis_start": 0.0, "axis_stop": 100.0,
                    "axis_step": 100.0, "centre_on_voc": False, "n_loops": 1,
                    "vpre": 1.0, "vcoll": -2.0, "store_shots": False,
                    "record_length": 500}}]}
    pipe_frames, _ = asyncio.run(_run_and_capture(
        ws_url, base, {"tree": tree, "name": "ui-fixture-pipeline"},
        timeout_s=a.timeout_s, path="/pipelines"))
    _dump(os.path.join(a.out, f"stream_pipeline_{tag}.jsonl"), pipe_frames, jsonl=True)


def _record_tree(a, base: str, ws_url: str, tag: str, mode: str) -> None:
    """The M4 tree: `T [250, 280] x led [1.010, 1.020] x bace`.

    Three points by two loops per leaf, so every leaf has a Q(axis) with a
    σ that exists (one loop leaves `q_std = 0`, which is "not recorded"), and
    `record_length` small for the reason the pipeline fixture's is. `hold_s`
    is one second: `--fast` makes it a no-op, and on a bench it is the dwell
    after the operator's answer, which a fixture has no use for.

    **Simulator only, and named explicitly.** This is the one step that
    answers its own `NeedsOperator`, and the answer is invented -- the
    setpoint plus a tenth of a kelvin, typed by nothing. Against a real
    service it would command the cryostat to 250 K, give up 60 seconds later
    (a real settle is 14 minutes to 2 hours), answer its own timeout, measure
    at whatever the sample is actually at, and write 250.1 K into every folder
    name and `/metadata` group with `temperature_how = "operator"`. The whole
    point of that field is to say who read the number; here nobody did. So the
    step refuses rather than trusting `--only` to be typed carefully.
    """
    if mode != "sim":
        raise SystemExit(
            f"the tree fixture is simulator-only and this service is mode {mode!r}.\n"
            "It answers its own temperature pause with a number nobody read, which on a\n"
            "bench writes a temperature the sample never reached into the run folders.\n"
            "Record it against `python -m bace.service --sim --fast`, and record the rest\n"
            "here with `--only jv,bace,pipeline,stopped,bench,hello`.")
    print("tree, with the pauses answered …")
    tree = {"kind": "loop", "loop": "temperature", "label": "T", "values_k": [250.0, 280.0],
            "tolerance_k": 0.5, "hold_s": 1.0, "timeout_s": 60.0, "children": [
                {"kind": "loop", "loop": "illumination", "levels_v": [1.010, 1.020],
                 "led_low_v": 0.4, "led_settle_s": 0.1, "children": [
                     {"kind": "module", "module": "bace",
                      "params": {"axis_name": "delay_ns", "axis_start": 0.0, "axis_stop": 100.0,
                                 "axis_step": 50.0, "centre_on_voc": False, "n_loops": 2,
                                 "vpre": 1.0, "vcoll": -2.0, "store_shots": False,
                                 "record_length": 500}}]}]}
    frames, _ = asyncio.run(_run_and_capture(
        ws_url, base, {"tree": tree, "name": "ui-fixture-tree"},
        timeout_s=a.timeout_s, path="/pipelines", answer_pauses=True))
    _dump(os.path.join(a.out, f"stream_tree_{tag}.jsonl"), frames, jsonl=True)


def _record_stopped(a, base: str, ws_url: str, tag: str) -> None:
    """A 3-point scan of 20 loops, stopped `after_shot` once three shots are
    in: 60 requested, 3 kept, and the first loop never completed -- so the
    Q(axis) has a point with one shot behind it and two with none, which is
    what the chart has to say rather than draw a curve through."""
    print("bace, stopped after three shots …")
    frames, _ = asyncio.run(_run_and_capture(
        ws_url, base,
        {"module": "bace", "name": "ui-fixture-stopped",
         "params": {"axis_name": "delay_ns", "axis_start": 0.0, "axis_stop": 100.0,
                    "axis_step": 50.0, "centre_on_voc": False, "n_loops": 20,
                    "vpre": 1.0, "vcoll": -2.0, "store_shots": False,
                    "record_length": 500}},
        timeout_s=a.timeout_s, stop_after_shots=3, expect="stopped"))
    _dump(os.path.join(a.out, f"stream_stopped_{tag}.jsonl"), frames, jsonl=True)


if __name__ == "__main__":
    sys.exit(main())
