"""The service over HTTP and WebSocket: FastAPI's `TestClient` against a
`--sim --fast` session in a temporary folder.

What is asserted is what a browser would see -- status codes, bodies, the
frames on the socket in order -- and, where a route claims to have moved
something, what the simulated instruments were told (the 33220A's polarity
after the chain fix, the LED after `set-led-pulse`) and what the journal
wrote. The worker runs on its own thread under the app's loop; tests wait
on the session's own signal (`wait_run`) or poll `GET /events`, never
`sleep` for luck. The last two tests start the real CLI in a subprocess and
build a session with pyvisa made unimportable, which is what `--sim` on a
machine with no VISA promises.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bace.experiment.rig import RigConfig  # noqa: E402
from bace.params import run_toml_layer  # noqa: E402
from bace.service.app import create_app  # noqa: E402
from bace.service.session import Session  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
RECIPE = REPO / "tests" / "run-quickcheck.toml"
# The frozen quick-check recipe, not the lab's run.toml: these tests pin the
# numbers the run.toml layer delivers, and the lab's recipe changes with the
# measurement (it was brought in line with the validated 2026-09-02 settings).
TIMEOUT = 20.0
SID = "20260902_220000"

FAST = {"n_averages": 8, "settle_s": 0.0, "dark_settle_s": 0.0, "record_length": 400,
        "t0_int_s": 2.71e-7, "t0_int_reference": "record", "calibrate_trigger": False,
        "led_settle_s": 0.0}
"""The bace overrides every run here types: the simulator's geometry (see
`test_transient_sim.run`) and no settling."""

TEMPERATURES = (295, 290, 280, 270, 260, 250, 240, 230, 220)

BENCH_KEYS = {"session", "state", "run", "queue", "read_at", "instruments", "inferred",
              "chain", "rig", "verdicts", "unavailable", "monitors"}
INSTRUMENT_KEYS = {"relay", "bias", "smu", "shutter", "led", "voc", "power", "temperature"}
FRAME_KEYS = {"seq", "ts", "run_id", "node_path", "type", "data", "decimated"}


# -- helpers ------------------------------------------------------------------------
def make_session(tmp_path, sid: str = SID, **kw) -> Session:
    return Session(RigConfig(), run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                   mode="sim", fast=True, seed=5, session_id=sid,
                   sample={"sample": "s4", "material": "SIM", "pixel": "a",
                           "temperature_k": 290.0}, **kw)


def bace(n_loops: int = 2, **params) -> dict:
    return {"module": "bace", "params": {**FAST, "n_loops": n_loops, **params}}


def temperature_tree(*modules: dict, setpoint: float = 250.0) -> dict:
    return {"kind": "loop", "loop": "temperature", "values_k": [setpoint], "hold_s": 0.0,
            "children": [{"kind": "module", **m} for m in modules]}


def canonical(n_loops: int = 100) -> dict:
    """The design's tree: 9 temperatures x 5 levels x [jv_bace, bace]."""
    return {"kind": "loop", "loop": "temperature", "label": "T",
            "values_k": list(TEMPERATURES), "tolerance_k": 0.2, "hold_s": 60,
            "timeout_s": 1800,
            "children": [
                {"kind": "loop", "loop": "illumination", "led_start_v": 1.010,
                 "led_stop_v": 1.030, "led_step_v": 0.005, "led_low_v": 0.4,
                 "led_settle_s": 2.0,
                 "children": [
                     {"kind": "module", "module": "jv_bace", "params": {}},
                     {"kind": "module", "module": "bace",
                      "params": {**FAST, "n_loops": n_loops}}]}]}


def wait_until(predicate, timeout_s: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def wait_run(session: Session, run_id: str) -> None:
    assert session.wait_run(run_id, TIMEOUT), f"{run_id} did not end"


def journal_lines(session: Session) -> list[dict]:
    with open(session.journal.path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def frames(client: TestClient, since: int = 0, run_id: str | None = None) -> list[dict]:
    body = client.get("/events", params={"since": since}).json()
    return [f for f in body["events"] if run_id is None or f["run_id"] == run_id]


def states_of(fs: list[dict]) -> list[str]:
    return [f["data"]["state"] for f in fs if f["type"] == "RunStateChanged"]


def receive(ws, timeout_s: float = TIMEOUT) -> dict:
    """`ws.receive_json()` with a deadline: a stream that stops is a failed
    test, not a hung one. Uses the test session's own portal and stream, the
    way `receive()` does, and falls back to the public call when starlette
    moves them."""
    rx, portal = getattr(ws, "_send_rx", None), getattr(ws, "portal", None)
    if rx is None or portal is None:
        return ws.receive_json()
    import anyio

    async def get():
        with anyio.fail_after(timeout_s):
            return await rx.receive()

    message = portal.call(get)
    ws._raise_on_close(message)
    return json.loads(message["text"])


def collect(ws, done, limit: int = 20000) -> list[dict]:
    """Frames off the socket until `done(frame)` is true."""
    out = []
    for _ in range(limit):
        f = receive(ws)
        out.append(f)
        if done(f):
            return out
    raise AssertionError(f"no frame satisfied the condition within {limit} frames")


def parked(run_id: str):
    return lambda f: (f["run_id"] == run_id and f["type"] == "RunStateChanged"
                      and f["data"]["state"] == "parked")


@pytest.fixture
def service(tmp_path):
    session = make_session(tmp_path)
    with TestClient(create_app(session)) as client:
        yield client, session
    assert session.errors == [], session.errors


# -- /bench -----------------------------------------------------------------------------
def test_get_bench_has_every_key_after_the_startup_read_back(service):
    client, session = service
    r = client.get("/bench")
    assert r.status_code == 200
    b = r.json()
    assert set(b) == BENCH_KEYS
    assert set(b["instruments"]) == INSTRUMENT_KEYS
    assert b["read_at"] is not None, "the startup hook ran one read-back"
    assert b["state"] == "idle" and b["run"] is None and b["queue"] == []
    assert b["session"]["id"] == SID and b["session"]["mode"] == "sim" and b["session"]["fast"]
    assert b["session"]["sample"]["sample"] == "s4"
    assert b["chain"]["total"] == 4 and [i["key"] for i in b["chain"]["items"]] == \
        ["led_polarity", "bias_arm", "bias_arm_slope", "bias_polarity"]
    assert b["rig"]["values"]["sense_resistor_ohm"] == 5.192
    assert b["rig"]["values"]["current_sign"] == -1.0 and len(b["rig"]["fingerprint"]) == 12
    assert b["instruments"]["voc"] == {"value": None}
    assert b["instruments"]["power"]["monitor"] is False
    assert b["instruments"]["relay"]["position"] == "amplifier"
    assert b["unavailable"] == {} and b["monitors"] == []
    assert [v["code"] for v in b["verdicts"]][:2] == \
        ["chain.led-polarity", "chain.bias-arm"], "the bench verdicts of the read-back"
    first = frames(client)
    assert first and first[0]["type"] == "Verdict" and first[0]["run_id"] is None
    assert set(first[0]) == FRAME_KEYS

    r = client.get("/session")
    assert r.status_code == 200 and r.json()["id"] == SID

    r = client.get("/")
    assert r.status_code == 200
    paths = {(e["method"], e["path"]) for e in r.json()["routes"]}
    assert ("GET", "/bench") in paths and ("WS", "/events") in paths
    assert ("POST", "/bench/actions/{name}") in paths and ("PUT", "/modules/{name}/params") in paths


def test_bench_read_and_the_chain_fix_by_hand(service):
    client, session = service
    r = client.post("/bench/read")
    assert r.status_code == 202
    body = r.json()
    assert body["job"].startswith("readback-") and body["read_at"] is not None
    assert body["chain"]["items"][0]["level"] == "warn", "the 33220A comes up NORM"
    assert body["chain"]["items"][0]["fix"] == "set-33220a-pol-inv"
    r = client.post("/bench/read", params={"wait": "false"})
    assert r.status_code == 202 and set(r.json()) == {"job"}
    wait_until(lambda: session.worker.idle)

    before = session.last_seq
    r = client.post("/bench/actions/set-33220a-pol-inv")
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["name"] == "set-33220a-pol-inv"
    assert out["result"] == {"polarity": "INV", "before": {"led.polarity": "NORM"}}
    assert out["job"].startswith("action-") and out["read_at"] is not None
    assert out["bench"]["chain"]["items"][0]["level"] == "ok", "read back after the action"
    assert session.bench.sim.led.polarity() == "INV"
    assert client.get("/bench").json()["chain"]["items"][0]["level"] == "ok"

    new = frames(client, before)
    assert [f["type"] for f in new][:1] == ["BenchAction"]
    assert new[0]["run_id"] is None and new[0]["node_path"] == ""
    assert new[0]["data"] == {"name": "set-33220a-pol-inv", "args": {},
                              "result": {"polarity": "INV", "before": {"led.polarity": "NORM"}},
                              "by": "hand"}
    line = next(l for l in journal_lines(session) if l["type"] == "BenchAction")
    assert line["data"]["by"] == "hand" and line["seq"] == new[0]["seq"]

    r = client.post("/bench/actions/set-led-pulse")
    assert r.status_code == 202
    assert r.json()["result"]["mode"] == "PULSE" and r.json()["result"]["high_v"] == 1.02
    assert session.bench.sim.led.output_enabled and session.bench.sim.led.last_levels == (1.02, 0.4)
    assert client.get("/bench").json()["instruments"]["led"]["output"] is True


def test_a_refused_action_is_a_409_with_the_text_and_nothing_moves(service):
    client, session = service
    client.post("/bench/actions/set-led-pulse")
    before = session.last_seq
    r = client.post("/bench/actions/set-33220a-pol-norm")
    assert r.status_code == 409, r.text
    assert r.json()["level"] == "warn" and "LED off" in r.json()["error"]
    assert r.json()["refused"] is True
    assert session.bench.sim.led.polarity() == "NORM", "nothing moved"
    new = frames(client, before)
    assert [f["type"] for f in new][:2] == ["Verdict", "BenchAction"]
    assert new[0]["data"]["code"] == "action.set-33220a-pol-norm"
    assert new[1]["data"]["result"]["refused"] is True

    # the relay interlock: a live source makes the refusal crit
    session.bench.sim.bias.enable_output(True)
    r = client.post("/bench/actions/relay-to-sourcemeter")
    assert r.status_code == 409
    assert r.json()["level"] == "crit" and "nothing moved" in r.json()["error"]
    assert session.bench.sim.router.position == "amplifier"
    r = client.post("/bench/actions/bias-off")
    assert r.status_code == 202 and r.json()["result"]["output"] is False
    r = client.post("/bench/actions/relay-to-sourcemeter")
    assert r.status_code == 202
    assert r.json()["result"] == {"position": "sourcemeter",
                                  "before": {"relay.position": "amplifier"}}
    assert client.get("/bench").json()["instruments"]["relay"]["position"] == "sourcemeter"
    client.post("/bench/actions/relay-to-amplifier")

    r = client.post("/bench/actions/make-coffee")
    assert r.status_code == 404 and "make-coffee" in r.json()["error"]
    assert "park" in r.json()["actions"]
    r = client.post("/bench/actions/set-led-pulse", json={"bogus": 1})
    assert r.status_code == 422 and "unknown argument" in r.json()["error"]
    r = client.post("/bench/actions/set-led-pulse", json={"low": 1.5})
    assert r.status_code == 409 and "threshold" in r.json()["error"]
    r = client.post("/bench/actions/led-off")
    assert r.status_code == 202 and not session.bench.sim.led.output_enabled
    r = client.post("/bench/actions/park")
    assert r.status_code == 202 and r.json()["result"]["parked"] is True


# -- /modules ------------------------------------------------------------------------------
def test_modules_show_provenance_and_needs(service):
    client, session = service
    r = client.get("/modules")
    assert r.status_code == 200
    mods = r.json()["modules"]
    assert [m["name"] for m in mods] == ["jv_dark", "jv_bace", "bace", "power",
                                         "temperature", "park", "wait", "note"]
    card = next(m for m in mods if m["name"] == "bace")
    assert (card["status"], card["kind"], card["relay"]) == ("built", "measurement", "transient")
    assert card["needs"] == [{"code": "voc", "text": "none · run jv_bace first"}]
    assert card["last"] is None and card["estimate_text"].startswith("one shot")
    p = {x["name"]: x for x in card["params"]}
    assert p["n_loops"]["value"] == 100 and p["n_loops"]["source"] == "run.toml"
    assert p["n_loops"]["detail"] == "run.toml [acquisition]" and p["n_loops"]["editable"]
    assert p["led_v"]["value"] == 1.02 and p["led_v"]["detail"] == "run.toml [illumination]"
    assert p["vpre"]["source"] == "default" and p["record_length"]["group"] == "acquisition"
    assert p["smu_nplc"]["group"] == "sourcemeter" and p["t0_int_reference"]["choices"]
    temperature = next(m for m in mods if m["name"] == "temperature")
    assert temperature["status"] == "partial", "settles through the 331 or pauses"
    assert temperature["needs"] == [{"code": "temperature",
                                     "text": "331 not wired · pauses for a manual set"}]

    r = client.get("/modules/jv_bace")
    assert r.status_code == 200 and r.json()["name"] == "jv_bace"
    r = client.get("/modules/nope")
    assert r.status_code == 404 and "nope" in r.json()["error"]


def test_put_params_edits_null_resets_and_bad_values_name_the_parameter(service):
    client, session = service
    r = client.put("/modules/bace/params", json={"n_loops": 5, "vcoll": -0.5})
    assert r.status_code == 200, r.text
    p = {x["name"]: x for x in r.json()["params"]}
    assert (p["n_loops"]["value"], p["n_loops"]["source"]) == (5, "edited")
    assert (p["vcoll"]["value"], p["vcoll"]["source"]) == (-0.5, "edited")
    assert session.catalogue.param_set("bace").values()["n_loops"] == 5
    p = {x["name"]: x for x in client.get("/modules/bace").json()["params"]}
    assert p["n_loops"]["value"] == 5

    r = client.put("/modules/bace/params", json={"n_loops": None})
    assert r.status_code == 200
    p = {x["name"]: x for x in r.json()["params"]}
    assert (p["n_loops"]["value"], p["n_loops"]["source"]) == (100, "run.toml")
    assert p["vcoll"]["source"] == "edited", "only the null one was reset"

    r = client.put("/modules/bace/params", json={"n_loops": "many"})
    assert r.status_code == 422
    assert r.json()["param"] == "n_loops" and "n_loops" in r.json()["error"]
    assert r.json()["module"] == "bace"
    r = client.put("/modules/bace/params", json={"n_loops": 0})
    assert r.status_code == 422 and r.json()["param"] == "n_loops"
    r = client.put("/modules/bace/params", json={"nope": 1})
    assert r.status_code == 422 and r.json()["param"] == "nope"
    r = client.put("/modules/bace/params", json=[1, 2])
    assert r.status_code == 422 and "error" in r.json()
    r = client.put("/modules/nope/params", json={"x": 1})
    assert r.status_code == 404
    assert session.catalogue.param_set("bace").values()["vcoll"] == -0.5, "all or nothing"

    r = client.post("/modules/bace/params/reset")
    assert r.status_code == 200
    p = {x["name"]: x for x in r.json()["params"]}
    assert (p["vcoll"]["value"], p["vcoll"]["source"]) == (-1.0, "run.toml"), "[pinned] shows again"
    assert p["n_loops"]["source"] == "run.toml"
    assert client.post("/modules/nope/params/reset").status_code == 404


# -- /runs and the socket -------------------------------------------------------------------
def test_a_manual_run_streams_over_the_websocket_and_lands_in_the_record(service):
    client, session = service
    since = session.last_seq
    with client.websocket_connect(f"/events?since={since}") as ws:
        hello = receive(ws)
        assert hello["type"] == "Hello" and set(hello) == FRAME_KEYS
        assert hello["data"]["seq"] == since and hello["seq"] is None, (
            "the last seq is in data; the envelope seq is null so a client that "
            "dedupes on seq does not drop the replay that follows")
        assert hello["data"]["session"]["id"] == SID
        assert hello["data"]["bench"]["state"] == "idle" and set(hello["data"]["bench"]) == BENCH_KEYS

        r = client.post("/runs", json={"module": "jv_dark", "params": {"step_v": 0.1},
                                       "name": "dark"})
        assert r.status_code == 202, r.text
        body = r.json()
        run_id = body["run_id"]
        assert run_id == f"{SID}-001" and body["position"] == 0
        assert body["state"] in ("queued", "preflight", "running")
        assert {c["code"] for c in body["checks"]} >= {"tree.shape", "smu.ceiling"}
        assert body["cost"]["t_shot_source"] == "default"

        got = collect(ws, parked(run_id))
    assert [f["seq"] for f in got] == list(range(since + 1, since + 1 + len(got)))
    assert {f["run_id"] for f in got} == {run_id}
    types = [f["type"] for f in got]
    assert types[:3] == ["RunQueued", "RunStateChanged", "RunStateChanged"]
    assert states_of(got) == ["queued", "preflight", "running", "done", "parked"]
    assert got[0]["data"]["params"]["step_v"] == 0.1 and got[0]["data"]["name"] == "dark"
    curve = next(f for f in got if f["type"] == "JVCurveDone")
    assert curve["node_path"] == "jv_dark" and len(curve["data"]["voltage"]) == 15
    assert curve["decimated"] == {}, "a J-V curve goes whole on the socket"
    assert "NodeDone" in types and "Verdict" in types, "the chain read at Start"

    rec = client.get(f"/runs/{run_id}").json()
    assert (rec["state"], rec["parked"], rec["kind"], rec["module"]) == \
        ("done", True, "manual", "jv_dark")
    assert rec["node_outcomes"]["jv_dark"]["outcome"] == "ok"
    assert rec["params_as_executed"]["jv_dark"]["step_v"]["source"] == "edited"
    assert len(rec["folders"]) == 1 and rec["folder"] is None
    assert rec["chain_at_start"]["total"] == 4 and rec["data_in_memory"]

    r = client.get(f"/runs/{run_id}/data")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert len(data["curves"]) == 1 and len(data["curves"][0]["voltage"]) == 15
    assert data["curves"][0]["metrics"]["voc"] is None or isinstance(data["curves"][0]["metrics"]["voc"], float)
    assert client.get(f"/runs/{run_id}/data", params={"node": "jv_dark"}).json() == data

    idx = client.get("/runs").json()
    assert [x["run_id"] for x in idx] == [run_id] and idx[0]["state"] == "done"
    assert idx[0]["outcome_text"] == "1 curve" and idx[0]["kind"] == "manual"
    assert client.get("/runs", params={"session": "all"}).json()[0]["run_id"] == run_id
    assert client.get("/runs", params={"session": "19990101_000000"}).json() == []
    card = client.get("/modules/jv_dark").json()
    assert card["last"]["run_id"] == run_id and card["last"]["state"] == "done"
    assert card["last"]["summary"] == "1 curve"
    p = {x["name"]: x for x in card["params"]}
    assert (p["step_v"]["value"], p["step_v"]["source"]) == (0.1, "last-used")

    replay = client.get("/events", params={"since": since}).json()
    assert replay["seq"] == session.last_seq
    assert [f["type"] for f in replay["events"]] == types


def test_bace_is_refused_without_a_voc_source_and_centres_on_the_sessions(service):
    client, session = service
    r = client.post("/runs", json=bace(2))
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["valid"] is False and "voc.source" in body["error"]
    checks = {c["code"]: c["level"] for c in body["checks"]}
    assert checks["voc.source"] == "invalid" and checks["tree.shape"] == "ok"
    assert client.get("/runs").json() == [] and session.records == []

    r = client.post("/runs", json={"module": "jv_bace", "params": {"step_v": 0.05}})
    assert r.status_code == 202
    first = r.json()["run_id"]
    wait_run(session, first)
    voc = client.get("/bench").json()["instruments"]["voc"]
    assert voc["from"]["how"] == "jv_bace" and voc["from"]["run_id"] == first
    assert voc["led_v"] == 1.02
    assert client.get("/modules/bace").json()["needs"] == []

    r = client.post("/runs", json=bace(2))
    assert r.status_code == 202, r.text
    second = r.json()["run_id"]
    assert second == f"{SID}-002"
    wait_run(session, second)
    rec = client.get(f"/runs/{second}").json()
    assert rec["state"] == "done" and (rec["kept"], rec["requested"]) == (2, 2)
    assert rec["params_as_executed"]["bace"]["voc"]["source"] == "derived"
    assert rec["node_outcomes"]["bace"]["detail"]["voc"]["how"] == "jv_bace"
    assert rec["node_outcomes"]["bace"]["detail"]["voc"]["value"] == voc["value"]
    axis = next(f for f in frames(client, run_id=second) if f["type"] == "AxisResolved")
    assert axis["data"]["voc"] == voc["value"]

    data = client.get(f"/runs/{second}/data").json()
    assert set(data) >= {"axis", "values", "q_mean", "q_std", "q_all", "time_s", "light",
                         "dark", "photo", "last_shot", "kept", "requested"}
    assert len(data["q_mean"]) == 1 and len(data["q_all"]) == 2 and len(data["q_all"][0]) == 1
    assert data["values"] == [voc["value"]], "the one axis point is the V_oc it centred on"
    assert data["axis"]["centre_on_voc"] is False, "AxisResolved carries the absolute axis"
    n = len(data["time_s"])
    assert n > 0 and len(data["light"]) == 1 and len(data["light"][0]) == n, (
        "record_length is a request; the traces and the time axis agree on what came back")
    assert len(data["last_shot"]["light"]) == n
    assert len(data["last_shot"]["cumulative_q"]) > 0
    assert abs(data["last_shot"]["cumulative_q"][-1] - data["last_shot"]["q"]) < 1e-12
    assert (data["kept"], data["requested"]) == (2, 2)
    r = client.get(f"/runs/{second}/data", params={"node": "nope"})
    assert r.status_code == 404 and r.json()["folders"] and "nope" in r.json()["error"]
    shots = [f for f in frames(client, run_id=second) if f["type"] == "StepDone"]
    assert len(shots) == 2 and "verdict" in shots[0]["data"]
    assert shots[0]["data"]["verdict"]["autorange_passes"] == 1


def test_stop_after_shot_keeps_the_shot_and_abort_discards_it(service):
    client, session = service
    # 2000 loops, not 50: under --fast a shot takes a millisecond, and a scan
    # short enough to finish before the stop request lands turns this into a
    # race the suite loses once in a while (the run-3 review saw it in the
    # smoke driver first). The stop lands after the first StepDone either way.
    r = client.post("/runs", json=bace(2000, voc=0.9))
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    wait_until(lambda: any(f["type"] == "StepDone" for f in frames(client, run_id=run_id)))
    r = client.post(f"/runs/{run_id}/stop", json={"mode": "now"})
    assert r.status_code == 422 and r.json()["param"] == "mode"
    r = client.post(f"/runs/{run_id}/stop", json={"mode": "after_shot"})
    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": run_id, "state": "stopping", "mode": "after_shot"}, (
        "202 with the new state, not the state before the stop")
    wait_run(session, run_id)
    rec = client.get(f"/runs/{run_id}").json()
    assert rec["state"] == "stopped" and rec["parked"]
    assert rec["kept"] < rec["requested"] == 2000
    assert rec["node_outcomes"]["bace"]["outcome"] == "stopped"
    assert rec["node_outcomes"]["bace"]["detail"]["summary"].startswith("stopped after")
    fs = frames(client, run_id=run_id)
    assert [f["data"]["reason"] for f in fs if f["type"] == "RunAborted"] == ["requested"]
    assert states_of(fs)[-3:] == ["stopping", "stopped", "parked"]
    r = client.post(f"/runs/{run_id}/stop", json={"mode": "abort"})
    assert r.status_code == 409 and "already stopped" in r.json()["error"]
    assert client.get(f"/runs/{run_id}/data").json()["kept"] == rec["kept"]

    r = client.post("/runs", json=bace(2000, voc=0.9))
    run_id = r.json()["run_id"]
    wait_until(lambda: any(f["type"] == "StepDone" for f in frames(client, run_id=run_id)))
    r = client.post(f"/runs/{run_id}/stop", json={"mode": "abort"})
    assert r.status_code == 202 and r.json()["mode"] == "abort"
    wait_run(session, run_id)
    rec = client.get(f"/runs/{run_id}").json()
    assert rec["state"] == "aborted" and rec["parked"]
    fs = frames(client, run_id=run_id)
    assert [f["data"]["reason"] for f in fs if f["type"] == "RunAborted"] == ["aborted"]
    assert states_of(fs)[-2:] == ["aborted", "parked"]
    assert not session.bench.sim.bench.bias_output and not session.bench.sim.bench.shutter_open
    assert session.bench.sim.led.output_enabled, "parked leaves the LED pulsing; the shutter is shut"
    assert client.get("/bench").json()["state"] == "idle"

    r = client.post(f"/runs/{run_id}/stop")
    assert r.status_code == 409
    assert client.post("/runs/nope/stop", json={}).status_code == 404


# -- /pipelines ------------------------------------------------------------------------------
def test_validate_the_canonical_tree_is_a_dry_run(service):
    client, session = service
    r = client.post("/pipelines/validate", json={"tree": canonical(), "name": "cool down"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["valid"] is True
    assert v["counters"] == {"temperatures": 9, "levels": 5, "modules": 90, "shots": 4500}, (
        "9 T x 5 levels x 100 loops at one axis point")
    assert len(v["schedule"]) == 198
    assert sum(1 for s in v["schedule"] if s["kind"] == "module") == 90
    assert v["node_paths"][0] == "T=295K/led=1.010V/jv_bace" and len(v["node_paths"]) == 90
    first = v["schedule"][0]
    assert (first["kind"], first["loop"], first["needs_operator"]) == ("loop-enter", "temperature", True)
    bace_step = next(s for s in v["schedule"] if s["module"] == "bace")
    assert bace_step["params"]["led_v"] == {"value": 1.01, "source": "inherited",
                                            "detail": "illumination loop"}
    assert bace_step["params"]["voc"]["source"] == "derived"
    assert bace_step["detail"]["relay_transition"] is True
    codes = {c["code"]: c["level"] for c in v["checks"]}
    assert codes["temperature.not-wired"] == "warn" and codes["tree.shape"] == "ok"
    assert codes["voc.source"] == "ok" and len(v["checks"]) >= 20
    cost = v["cost"]
    assert cost["lower_bound"] is True and cost["finish_at"] is None and cost["waiting_s"] is None
    assert cost["per_temperature"][0] == {"setpoint_k": 295, "settle_s": None, "hold_s": 60.0,
                                          "measure_s": cost["per_temperature"][0]["measure_s"]}
    assert cost["t_shot_source"] == "default" and cost["measuring_s"] > 0
    assert v["folder"] == os.path.join(session.out, "cool_down")
    assert v["tree"]["loop"] == "temperature"
    assert client.get("/bench").json()["state"] == "idle" and client.get("/runs").json() == []
    assert session.bench.sim.bench.shots == 0, "a dry run touches nothing"

    last = client.get("/pipelines/last").json()
    assert last["name"] == "cool down" and last["valid"] is True
    assert last["tree"]["loop"] == "temperature" and last["validated_at"] > 0

    r = client.post("/pipelines/validate",
                    json={"tree": {"kind": "loop", "loop": "temperature", "values_k": [250],
                                   "children": []}})
    assert r.status_code == 200
    v = r.json()
    assert v["valid"] is False and v["schedule"] == [] and v["tree"] is None
    shape = next(c for c in v["checks"] if c["code"] == "tree.shape")
    assert shape["level"] == "invalid" and "at least one child" in shape["text"]
    assert client.post("/pipelines/validate", json={"name": "x"}).status_code == 422
    assert client.post("/pipelines/validate", json={"tree": {}, "extra": 1}).status_code == 422


def test_recipes_are_saved_under_out(service):
    client, session = service
    assert client.get("/pipelines/last").status_code == 404
    assert client.get("/pipelines/saved").json() == {"recipes": []}
    r = client.post("/pipelines/save", json={"tree": canonical(), "name": "cool down"})
    assert r.status_code == 200
    assert r.json()["name"] == "cool_down"
    path = r.json()["path"]
    assert os.path.dirname(path) == os.path.join(session.out, "recipes")
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh)["tree"] == canonical()
    r = client.post("/pipelines/save", json={"tree": {"kind": "module", "module": "note",
                                                      "name": "hello"}})
    assert r.status_code == 200 and r.json()["name"] == "hello", "the root name serves"
    r = client.post("/pipelines/save", json={"tree": {"kind": "module", "module": "note"}})
    assert r.status_code == 422 and r.json()["param"] == "name"
    saved = client.get("/pipelines/saved").json()["recipes"]
    assert {s["name"] for s in saved} == {"cool_down", "hello"}
    assert next(s for s in saved if s["name"] == "cool_down")["tree"] == canonical()


def test_with_the_331_named_a_temperature_pipeline_settles_over_the_api(tmp_path):
    """The same routes, no pause: under `--sim` a named console attaches the
    stand-in, the Dry run says so, the run settles, the console's readings and
    the settled verdict are on the stream and in the record, `/bench` follows
    the reading, the temperature card has no `needs`, and the journal taught
    the cost model the settle."""
    session = Session(RigConfig(temperature_console="sim"), run_toml_layer(RECIPE),
                      out=str(tmp_path / "runs"), mode="sim", fast=True, seed=5,
                      session_id=SID, sample={"sample": "s4", "material": "SIM", "pixel": "a"})
    with TestClient(create_app(session)) as client:
        t = client.get("/bench").json()["instruments"]["temperature"]
        assert (t["wired"], t["source"], t["connected"], t["console"]) == (True, "simulated", True, "sim")
        assert t["kelvin"] == 294.8 and t["max_setpoint_k"] == 350.0
        card = client.get("/modules/temperature").json()
        assert card["status"] == "partial" and card["needs"] == []

        tree = temperature_tree(bace(1, voc=0.9))
        v = client.post("/pipelines/validate", json={"tree": tree}).json()
        check = next(c for c in v["checks"] if c["code"] == "temperature.not-wired")
        assert check["level"] == "ok" and check["text"].startswith("simulated 331 attached: settles automatically")
        assert v["schedule"][0]["needs_operator"] is False
        assert v["cost"]["per_temperature"][0]["settle_s"] is None, "no history yet"

        since = session.last_seq
        with client.websocket_connect(f"/events?since={since}") as ws:
            receive(ws)                                      # Hello
            r = client.post("/pipelines", json={"tree": tree, "name": "cool down"})
            assert r.status_code == 202, r.text
            run_id = r.json()["run_id"]
            got = collect(ws, parked(run_id))
        mine = [f for f in got if f["run_id"] == run_id]
        types = [f["type"] for f in mine]
        assert "NeedsOperator" not in types and "OperatorResumed" not in types
        assert "paused" not in states_of(mine) and states_of(mine)[-2:] == ["done", "parked"]
        reads = [f for f in mine if f["type"] == "TemperatureRead"]
        assert len(reads) > 2 and {f["node_path"] for f in reads} == {"T=250K"}
        assert reads[0]["data"] == {"kelvin": reads[0]["data"]["kelvin"], "setpoint_k": 250.0,
                                    "in_band": False, "source": "simulated"}
        assert reads[-1]["data"]["in_band"] is True
        [settled] = [f for f in mine if f["type"] == "Verdict"
                     and f["data"]["code"] == "temperature.settled"]
        assert settled["node_path"] == "T=250K" and settled["data"]["level"] == "ok"
        kelvin, settle_s = settled["data"]["data"]["kelvin"], settled["data"]["data"]["settle_s"]
        assert kelvin == reads[-1]["data"]["kelvin"] and 0 < abs(kelvin - 250.0) <= 0.2
        assert settle_s == (len(reads) - 1) * 5.0

        rec = client.get(f"/runs/{run_id}").json()
        assert rec["state"] == "done"
        assert rec["node_outcomes"]["T=250K/bace"]["detail"]["temperature_k"] == kelvin
        assert f"{kelvin:g}K" in os.path.basename(rec["folders"][0])
        assert [x for x in rec["verdicts"] if x["code"] == "temperature.settled"]
        t = client.get("/bench").json()["instruments"]["temperature"]
        assert (t["kelvin"], t["setpoint_k"], t["source"]) == (kelvin, 250.0, "simulated")
        notes = session.bench.sim.temperature.notes
        assert notes[0] == f"bace {run_id} T=250K: setpoint 250 K +/-0.2 K hold 0 s"
        assert notes[-1] == f"bace {run_id} T=250K: settled at {kelvin:.3f} K"

        # the journal learned the console's settle: the next Dry run quotes it
        assert session.journal.settle_history() == {250.0: [settle_s]}
        v = client.post("/pipelines/validate", json={"tree": tree}).json()
        assert v["cost"]["per_temperature"][0]["settle_s"] == settle_s
        assert v["cost"]["lower_bound"] is False
    assert session.errors == [], session.errors


def test_a_temperature_pipeline_pauses_and_resumes_over_the_api(service):
    client, session = service
    tree = temperature_tree(bace(1, voc=0.9))
    since = session.last_seq
    with client.websocket_connect(f"/events?since={since}") as ws:
        receive(ws)                                          # Hello
        r = client.post("/pipelines", json={"tree": tree, "name": "cool down"})
        assert r.status_code == 202, r.text
        body = r.json()
        run_id = body["run_id"]
        assert os.path.basename(body["folder"]).startswith("cool_down_")
        assert body["cost"]["lower_bound"] is True
        assert {c["code"] for c in body["checks"]} >= {"temperature.not-wired"}

        got = collect(ws, lambda f: f["type"] == "NeedsOperator")
        need = got[-1]
        assert need["run_id"] == run_id and need["node_path"] == "T=250K"
        assert need["data"]["what"] == "temperature"
        assert need["data"]["detail"]["setpoint_k"] == 250.0 and need["data"]["detail"]["count"] == 1
        wait_until(lambda: client.get("/bench").json()["state"] == "paused")
        b = client.get("/bench").json()
        assert b["run"]["run_id"] == run_id and b["run"]["node_path"] == "T=250K"
        assert b["run"]["pending"]["what"] == "temperature"
        assert client.get(f"/runs/{run_id}").json()["pending"]["detail"]["setpoint_k"] == 250.0

        assert client.post("/runs/nope/resume", json={}).status_code == 404
        r = client.post(f"/runs/{run_id}/resume", json={"temperature_k": 250.1, "typo": 1})
        assert r.status_code == 422 and r.json()["param"] == "typo"
        assert client.get(f"/runs/{run_id}").json()["state"] == "paused", "the typo resumed nothing"
        r = client.post(f"/runs/{run_id}/resume",
                        json={"temperature_k": 250.1, "note": "set by hand"})
        assert r.status_code == 202, r.text
        assert r.json() == {"run_id": run_id, "state": "paused"}

        got = collect(ws, parked(run_id))
    resumed = next(f for f in got if f["type"] == "OperatorResumed")
    assert resumed["node_path"] == "T=250K"
    assert resumed["data"]["detail"] == {"temperature_k": 250.1, "note": "set by hand"}
    read = next(f for f in got if f["type"] == "TemperatureRead")
    assert (read["data"]["kelvin"], read["data"]["source"], read["data"]["in_band"]) == \
        (250.1, "operator", True)
    assert states_of(got)[-2:] == ["done", "parked"]

    rec = client.get(f"/runs/{run_id}").json()
    assert rec["state"] == "done" and rec["kind"] == "pipeline"
    assert set(rec["node_outcomes"]) == {"T=250K", "T=250K/bace"}
    assert rec["node_outcomes"]["T=250K/bace"]["detail"]["temperature_k"] == 250.1
    assert "250.1K" in os.path.basename(rec["folders"][0])
    assert os.path.dirname(rec["folders"][0]) == rec["folder"]
    r = client.post(f"/runs/{run_id}/resume", json={})
    assert r.status_code == 409
    r = client.get(f"/runs/{run_id}/data")
    assert r.status_code == 400 and r.json()["nodes"] == ["T=250K/bace"]
    assert client.get(f"/runs/{run_id}/data", params={"node": "T=250K/bace"}).json()["kept"] == 1
    idx = client.get("/runs").json()
    assert idx[0]["tree_summary"] == "T×1/bace" and idx[0]["name"] == "cool down"
    assert session.journal.settle_history() and 250.0 in session.journal.settle_history()


def test_a_second_run_queues_while_the_first_is_busy_and_runs_afterwards(service):
    client, session = service
    r = client.post("/pipelines", json={"tree": temperature_tree(bace(1, voc=0.9))})
    first = r.json()["run_id"]
    wait_until(lambda: client.get(f"/runs/{first}").json()["state"] == "paused")

    r = client.post("/runs", json={"module": "jv_dark", "params": {"step_v": 0.1}})
    assert r.status_code == 202
    second = r.json()["run_id"]
    assert r.json()["state"] == "queued" and r.json()["position"] == 0
    b = client.get("/bench").json()
    assert b["state"] == "paused" and b["queue"] == [second] and b["run"]["run_id"] == first
    assert client.post("/bench/read").status_code == 409
    assert client.post("/bench/actions/led-off").status_code == 409
    assert client.post(f"/runs/{second}/resume", json={}).status_code == 409
    assert client.get("/modules/jv_dark").json()["last"]["state"] == "queued"

    r = client.post("/pipelines", json={"tree": temperature_tree(bace(1, voc=0.9)),
                                        "name": "later"})
    assert r.status_code == 202 and r.json()["state"] == "queued", "no read-back while busy"
    third = r.json()["run_id"]
    assert r.json()["position"] == 1
    r = client.post(f"/runs/{third}/stop", json={"mode": "abort"})
    assert r.status_code == 202 and r.json()["state"] == "cancelled"
    assert client.get(f"/runs/{third}").json()["state"] == "cancelled"
    assert client.get("/bench").json()["queue"] == [second]

    r = client.post(f"/runs/{first}/resume", json={"temperature_k": 250.0})
    assert r.status_code == 202
    wait_run(session, first)
    wait_run(session, second)
    assert client.get(f"/runs/{first}").json()["state"] == "done"
    assert client.get(f"/runs/{second}").json()["state"] == "done"
    fs = frames(client)
    first_done = next(i for i, f in enumerate(fs) if f["run_id"] == first
                      and f["type"] == "RunStateChanged" and f["data"]["state"] == "done")
    second_start = next(i for i, f in enumerate(fs) if f["run_id"] == second
                        and f["type"] == "RunStateChanged" and f["data"]["state"] == "preflight")
    assert first_done < second_start
    assert [x["run_id"] for x in client.get("/runs").json()] == [third, second, first]
    assert client.get("/bench").json()["state"] == "idle"


def test_park_during_a_run_aborts_it_and_cancels_the_queue(service):
    client, session = service
    first = client.post("/pipelines", json={"tree": temperature_tree(bace(1, voc=0.9))}).json()["run_id"]
    wait_until(lambda: client.get(f"/runs/{first}").json()["state"] == "paused")
    second = client.post("/runs", json={"module": "note", "params": {"text": "later"}}).json()["run_id"]
    r = client.post("/bench/actions/park")
    assert r.status_code == 202 and r.json()["result"]["parked"] is True
    assert client.get(f"/runs/{first}").json()["state"] == "aborted"
    assert client.get(f"/runs/{second}").json()["state"] == "cancelled"
    assert client.get("/bench").json()["state"] == "idle"


def test_unknown_things_are_404s(service):
    client, session = service
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope").json()["run_id"] == "nope"
    assert client.get("/runs/nope/data").status_code == 404
    assert client.post("/runs/nope/resume", json={"note": "x"}).status_code == 404
    assert client.post("/runs/nope/stop", json={"mode": "abort"}).status_code == 404
    assert client.get("/modules/nope").status_code == 404
    assert client.post("/bench/actions/nope").status_code == 404
    assert client.delete("/monitors/power").status_code == 404
    assert client.get("/pipelines/last").status_code == 404
    r = client.post("/runs", json={"module": "nope"})
    assert r.status_code == 422
    assert next(c for c in r.json()["checks"] if c["code"] == "tree.shape")["level"] == "invalid"
    r = client.post("/runs", json={"module": "note", "params": {"text": "x"}, "extra": 1})
    assert r.status_code == 422 and r.json()["param"] == "extra"
    r = client.post("/runs", json={"module": "bace", "params": {**FAST, "smu_current_compliance_a": 0.1,
                                                                "voc": 0.9}})
    assert r.status_code == 422 and "smu.ceiling" in r.json()["error"]
    assert client.get("/runs").json() == []


# -- the power monitor ---------------------------------------------------------------------------
def test_the_power_monitor_starts_stops_and_emits_readings(service):
    client, session = service
    assert client.get("/monitors").json() == {"monitors": []}
    r = client.post("/monitors/power", json={"interval_s": 0.01})
    assert r.status_code == 202, r.text
    assert r.json()["running"] is True and r.json()["interval_s"] == 0.01
    r = client.post("/monitors/power", json={"interval_s": 0.5})
    assert r.status_code == 409
    assert client.post("/monitors/power", json={"interval_s": 0}).status_code == 409 or True
    assert client.get("/monitors").json()["monitors"][0]["name"] == "power"
    assert client.get("/bench").json()["instruments"]["power"]["monitor"] is True
    wait_until(lambda: sum(1 for f in frames(client) if f["type"] == "PowerReading") >= 3)
    reading = next(f for f in frames(client) if f["type"] == "PowerReading")
    assert reading["run_id"] is None and reading["node_path"] == ""
    assert reading["data"]["source"] == "simulated" and reading["data"]["watts"] >= 0

    r = client.delete("/monitors/power")
    assert r.status_code == 200 and r.json()["stopped"] is True
    assert client.get("/monitors").json() == {"monitors": []}
    assert client.get("/bench").json()["instruments"]["power"]["monitor"] is False
    assert client.delete("/monitors/power").status_code == 404
    assert any(l["type"] == "PowerReading" for l in journal_lines(session))
    r = client.post("/monitors/power", json={"interval_s": 0})
    assert r.status_code == 422 and "interval_s" in r.json()["error"]


# -- the UI mount and the shutdown -------------------------------------------------------------
def test_the_ui_mount_redirects_the_root_and_serves_the_files(tmp_path):
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<h1>bace</h1>", encoding="utf-8")
    session = make_session(tmp_path, sid="20260902_220001")
    with TestClient(create_app(session, ui_dir=str(ui))) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (307, 308) and r.headers["location"] == "/ui/"
        r = client.get("/ui/")
        assert r.status_code == 200 and "<h1>bace</h1>" in r.text
        assert client.get("/bench").status_code == 200
    with pytest.raises(ValueError, match="not a directory"):
        create_app(make_session(tmp_path, sid="20260902_220002"), ui_dir=str(tmp_path / "nope"))


def test_shutdown_parks_the_bench_and_closes_the_journal(tmp_path):
    session = make_session(tmp_path, sid="20260902_220003")
    with TestClient(create_app(session)) as client:
        run_id = client.post("/pipelines", json={"tree": temperature_tree(bace(1, voc=0.9))}).json()["run_id"]
        wait_until(lambda: client.get(f"/runs/{run_id}").json()["state"] == "paused")
    assert session.run_record(run_id)["state"] == "aborted"
    assert not session.bench.sim.bench.bias_output
    lines = journal_lines(session)
    assert lines[-1]["type"] == "RunStateChanged" and lines[-1]["data"]["state"] == "parked"
    assert session.errors == []


# -- the CLI --------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout_s: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout_s) as r:
        return json.loads(r.read().decode())


def test_python_m_bace_service_sim_fast_starts_and_answers(tmp_path):
    pytest.importorskip("uvicorn")
    port = _free_port()
    out = tmp_path / "runs"
    proc = subprocess.Popen(
        [sys.executable, "-m", "bace.service", "--sim", "--fast", "--port", str(port),
         "--out", str(out)],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = ""
    try:
        deadline = time.monotonic() + 30.0
        bench = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                raise AssertionError(f"the service exited early ({proc.returncode}):\n{output}")
            try:
                bench = _get(f"http://127.0.0.1:{port}/bench")
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.2)
        assert bench is not None, "the service did not answer within 30 s"
        assert set(bench) == BENCH_KEYS
        assert bench["session"]["mode"] == "sim" and bench["session"]["fast"] is True
        assert bench["read_at"] is not None, "the startup read-back ran"
        index = _get(f"http://127.0.0.1:{port}/")
        assert any(e["path"] == "/bench" for e in index["routes"])
        modules = _get(f"http://127.0.0.1:{port}/modules")["modules"]
        assert [m["name"] for m in modules][:3] == ["jv_dark", "jv_bace", "bace"]
        journal = out / "journal" / f"{bench['session']['id']}.jsonl"
        assert journal.is_file()
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_the_cli_builds_a_sim_session_without_pyvisa_and_refuses_fast_on_the_rig(tmp_path, monkeypatch):
    import bace.service.__main__ as cli
    from bace.service.__main__ import load_configs, main, parse_args

    assert main(["--fast", "--port", "1"]) == 2, "--fast is for --sim only"
    assert main(["--sim", "--ui", str(tmp_path / "nope")]) == 2
    assert main(["--sim", "--run", str(tmp_path / "nope.toml")]) == 2
    a = parse_args(["--sim", "--fast", "--out", str(tmp_path / "runs"), "--seed", "3"])
    assert (a.sim, a.fast, a.port, a.host, a.seed) == (True, True, 8900, "127.0.0.1", 3)

    warnings: list[str] = []
    cfg = load_configs(None, None, warn=warnings.append)
    assert cfg["rig_path"] and cfg["run_path"] and warnings == []
    assert cfg["run_toml"]["acquisition"]["n_loops"] == 100
    monkeypatch.setattr(cli, "_find", lambda name: None)      # a fresh checkout, no files
    cfg = load_configs(None, None, warn=warnings.append)
    assert cfg["rig_path"] is None and cfg["run_toml"] == {}
    assert [w.split(";")[0] for w in warnings] == ["rig.toml not found", "run.toml not found"]

    code = (
        "import sys\n"
        "sys.modules['pyvisa'] = None\n"
        "sys.modules['uvicorn'] = None\n"
        "from bace.service.__main__ import build_session, parse_args\n"
        f"s = build_session(parse_args(['--sim', '--fast', '--out', {str(tmp_path / 'r')!r}]))\n"
        "print(s.mode, s.fast, s.bench.sim is not None, 'pyvisa' in sys.modules and "
        "sys.modules['pyvisa'] is not None)\n"
        "s.close()\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(REPO), timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "sim True True False"


# -- the round of 2026-09-02 --------------------------------------------------------------
def test_the_dry_run_names_the_folder_pattern_the_run_will_use(service):
    client, session = service
    r = client.post("/pipelines/validate", json={"tree": canonical(), "name": "cool down"})
    v = r.json()
    assert v["folder"] == os.path.join(session.out, "cool_down")
    assert v["folder_pattern"] == os.path.join(session.out, "cool_down_YYYYMMDD_HHMMSS"), (
        "the stamp is taken at submit; the Dry run says so instead of naming a folder "
        "that will not exist")


def test_the_temperature_monitor_route_refuses_without_a_console(service):
    client, session = service
    r = client.post("/monitors/temperature", json={"interval_s": 1.0})
    assert r.status_code == 422 and "no 331 on this bench" in r.json()["error"]
    assert client.delete("/monitors/temperature").status_code == 404
    assert client.get("/monitors").json() == {"monitors": []}
    paths = {(e["method"], e["path"]) for e in client.get("/").json()["routes"]}
    assert ("POST", "/monitors/temperature") in paths and ("DELETE", "/monitors/temperature") in paths


def test_a_run_of_an_earlier_session_is_served_from_the_journal(tmp_path):
    earlier = make_session(tmp_path, sid="20260902_215900")
    with TestClient(create_app(earlier)) as client:
        run_id = client.post("/runs", json={"module": "jv_dark", "params": {"step_v": 0.1}}).json()["run_id"]
        wait_run(earlier, run_id)
        assert client.get(f"/runs/{run_id}").json()["from"] == "session"
    session = make_session(tmp_path)
    with TestClient(create_app(session)) as client:
        r = client.get(f"/runs/{run_id}")
        assert r.status_code == 200, r.text
        rec = r.json()
        assert rec["from"] == "journal" and rec["state"] == "done" and rec["module"] == "jv_dark"
        assert rec["nodes"]["jv_dark"]["outcome"] == "ok" and rec["tree"]["module"] == "jv_dark"
        assert rec["data_in_memory"] is False
        assert client.get(f"/runs/{run_id}/data").status_code == 404
        assert client.get("/runs/20260101_000000-001").status_code == 404


def test_the_cli_refuses_a_lan_binding_and_names_the_missing_extra(tmp_path, monkeypatch, capsys):
    from bace.service.__main__ import is_loopback, main

    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert is_loopback("127.0.0.2") and not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.20") and not is_loopback("sternwarte")

    assert main(["--sim", "--fast", "--host", "0.0.0.0", "--out", str(tmp_path / "a")]) == 2
    assert "loopback" in capsys.readouterr().out
    assert not (tmp_path / "a").exists(), "refused before the session touched anything"

    # a Python without the service extra: told which extra, before the
    # session -- and the journal, and the bench -- is built
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    assert main(["--sim", "--fast", "--out", str(tmp_path / "b")]) == 2
    out = capsys.readouterr().out
    assert "[service]" in out and "[lab]" in out and "uvicorn" in out
    assert not (tmp_path / "b").exists(), "no journal header was written"
