"""The session end to end, on the simulator, with no asyncio loop attached.

What is asserted is what a client of the service would see: the frames in
seq order with the right `run_id` and `node_path`, the journal file's lines,
the run record, the arrays behind `/runs/{id}/data`, the module card's
`last` and `needs`, the bench snapshot's V_oc rail, and the queue holding a
second run while the first is paused. The worker runs on its own thread, so
the tests wait on the session's own signals (`wait_run`) or poll the record.
"""
from __future__ import annotations

import json
import os
import pathlib
import queue
import time

import numpy as np
import pytest

from bace.experiment.rig import RigConfig
from bace.params import ParamValue, Source, run_toml_layer
from bace.service.rigs import BenchActionRefused
from bace.service.session import (RING_TRACES_KEPT, Busy, Conflict, DataUnavailable,
                                  NodeRequired, NotPaused, Session, SubmitRefused,
                                  UnknownRun, tree_for_module)

REPO = pathlib.Path(__file__).resolve().parents[1]
RECIPE = REPO / "tests" / "run-quickcheck.toml"
# The frozen quick-check recipe, not the lab's run.toml: these tests pin the
# numbers the run.toml layer delivers, and the lab's recipe changes with the
# measurement (it was brought in line with the validated 2026-09-02 settings).
TIMEOUT = 20.0
SID = "20260902_210000"

FAST = {"n_averages": 8, "settle_s": 0.0, "dark_settle_s": 0.0, "record_length": 400,
        "t0_int_s": 2.71e-7, "t0_int_reference": "record", "calibrate_trigger": False,
        "led_settle_s": 0.0}


def make_session(tmp_path, **kw) -> Session:
    kw.setdefault("session_id", SID)
    return Session(RigConfig(), run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                   mode="sim", fast=True, seed=5,
                   sample={"sample": "s4", "material": "SIM", "pixel": "a",
                           "temperature_k": 290.0}, **kw)


def bace(n_loops: int = 2, **params) -> dict:
    return tree_for_module("bace", {**FAST, "n_loops": n_loops, **params})


def temperature_pipeline(*modules: dict, setpoint: float = 250.0) -> dict:
    return {"kind": "loop", "loop": "temperature", "values_k": [setpoint], "hold_s": 0.0,
            "children": list(modules)}


def wait_until(predicate, timeout_s: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.01)


def journal_lines(s: Session) -> list[dict]:
    with open(s.journal.path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def frames_of(s: Session, run_id: str) -> list[dict]:
    return [f for f in s.events_since(0) if f["run_id"] == run_id]


def states_of(frames: list[dict]) -> list[str]:
    return [f["data"]["state"] for f in frames if f["type"] == "RunStateChanged"]


# -- a manual run, end to end ---------------------------------------------------------
def test_a_manual_jv_dark_goes_through_the_worker_the_journal_and_the_registry(tmp_path):
    with make_session(tmp_path) as s:
        assert s.session_id == SID and s.last_seq == 0
        run_id, v = s.submit(tree_for_module("jv_dark", {"step_v": 0.1}), name="dark",
                             kind="manual")
        assert run_id == f"{SID}-001" and v.valid
        assert s.wait_run(run_id, TIMEOUT)

        frames = s.events_since(0)
        assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
        assert {f["run_id"] for f in frames} == {run_id}
        assert [f["type"] for f in frames[:3]] == ["RunQueued", "RunStateChanged",
                                                   "RunStateChanged"]
        assert frames[0]["data"] == {"kind": "manual", "module": "jv_dark", "name": "dark",
                                     "tree": {"kind": "module", "module": "jv_dark",
                                              "params": {"step_v": 0.1}},
                                     "params": frames[0]["data"]["params"],
                                     "resolved": frames[0]["data"]["resolved"],
                                     "folder": None}
        # params is what last_used_params hands back: what the operator
        # chose, not every resolved default. step_v was typed; smu_nplc came
        # from run.toml and keeps its own provenance next time.
        assert frames[0]["data"]["params"] == {"step_v": 0.1}
        assert "smu_nplc" not in frames[0]["data"]["params"]
        assert frames[0]["data"]["resolved"]["smu_nplc"] == 1.0, "resolved keeps the whole record"
        assert frames[0]["data"]["resolved"]["step_v"] == 0.1
        assert states_of(frames) == ["queued", "preflight", "running", "done", "parked"]
        by_type = {}
        for f in frames:
            by_type.setdefault(f["type"], []).append(f)
        assert {f["node_path"] for f in by_type["JVCurveDone"]} == {"jv_dark"}
        assert {f["node_path"] for f in by_type["RunStateChanged"]} == {""}
        assert {f["node_path"] for f in by_type["NodeStarted"]} == {"jv_dark"}
        assert by_type["Verdict"], "the chain was read at Start and its verdicts attached"
        assert [f["node_path"] for f in by_type["Verdict"]] == [""] * len(by_type["Verdict"])
        assert by_type["JVCurveDone"][0]["data"]["voltage"], "arrays whole on the wire"
        done = by_type["NodeDone"][0]["data"]
        assert done["outcome"] == "ok" and done["detail"]["summary"] == "1 curve"

        lines = journal_lines(s)
        assert lines[0]["type"] == "SessionStarted" and lines[0]["seq"] == 0
        assert lines[0]["data"]["mode"] == "sim" and len(lines[0]["data"]["fingerprint"]) == 12
        assert lines[0]["data"]["session_id"] == SID and lines[0]["data"]["fast"] is True
        assert [l["type"] for l in lines[1:]] == [f["type"] for f in frames]
        assert [l["seq"] for l in lines[1:]] == [f["seq"] for f in frames]
        curve = next(l for l in lines if l["type"] == "JVCurveDone")
        assert "voltage" not in curve["data"] and curve["data"]["n_points"] == 15
        assert curve["decimated"]["voltage"] == {"omitted": True}

        rec = s.run_record(run_id)
        assert (rec["state"], rec["parked"], rec["kind"], rec["module"]) == \
            ("done", True, "manual", "jv_dark")
        assert rec["node_outcomes"]["jv_dark"]["outcome"] == "ok"
        assert rec["node_outcomes"]["jv_dark"]["kind"] == "jv_dark"
        assert (rec["kept"], rec["requested"]) == (1, 1)
        assert rec["params_as_executed"]["jv_dark"]["step_v"] == \
            {"value": 0.1, "source": "edited", "detail": "pipeline node"}
        assert rec["params_as_executed"]["jv_dark"]["smu_nplc"]["source"] == "run.toml"
        assert rec["chain_at_start"]["total"] == 4 and rec["error"] is None
        assert rec["folder"] is None, "a manual run has no pipeline folder"
        (folder,) = rec["folders"]
        assert os.path.dirname(folder) == str(tmp_path / "runs"), "recorded straight under out"
        assert "290K" in os.path.basename(folder)
        assert rec["started_at"] >= rec["queued_at"] and rec["finished_at"] >= rec["started_at"]
        json.dumps(rec)

        data = s.run_data(run_id)
        assert isinstance(data["curves"][0]["voltage"], np.ndarray)
        assert data["curves"][0]["voltage"].size == 15
        assert s.run_data(run_id, "jv_dark") is data

        (summary,) = s.runs_index()
        assert (summary["run_id"], summary["state"], summary["module"], summary["folder"]) == \
            (run_id, "done", "jv_dark", folder)
        assert summary["outcome_text"] == "1 curve"

        card = s.module_wire("jv_dark")
        assert card["last"] == {"run_id": run_id, "state": "done", "node_path": "jv_dark",
                                "ts": card["last"]["ts"], "summary": "1 curve",
                                "folder": folder}
        assert s.module_wire("bace")["last"] is None
        assert s.catalogue.param_set("jv_dark").get("step_v") == \
            ParamValue(0.1, Source.LAST_USED, "previous run"), "the journal feeds last-used"

        snap = s.bench_snapshot()
        assert snap["state"] == "idle" and snap["run"] is None and snap["queue"] == []
        assert snap["read_at"] is not None and snap["chain"]["total"] == 4
        assert snap["session"]["id"] == SID and snap["instruments"]["voc"] == {"value": None}
        assert s.errors == []


def test_a_jv_bace_gives_the_session_its_voc_and_a_manual_bace_centres_on_it(tmp_path):
    with make_session(tmp_path) as s:
        assert s.session_voc is None
        assert s.module_wire("bace")["needs"] == [{"code": "voc", "text": "none · run jv_bace first"}]
        first, _ = s.submit(tree_for_module("jv_bace", {"step_v": 0.05}))
        assert s.wait_run(first, TIMEOUT)
        voc = s.session_voc
        assert (voc.how, voc.led_v, voc.run_id, voc.node_path) == ("jv_bace", 1.02, first, "jv_bace")
        assert voc.value == pytest.approx(s.bench.sim.bench.device.voc(1.02), abs=0.02)
        rail = s.bench_snapshot()["instruments"]["voc"]
        assert rail["value"] == voc.value and rail["from"]["how"] == "jv_bace"
        assert rail["from"]["run_id"] == first
        assert s.module_wire("bace")["needs"] == []

        second, v = s.submit(bace(2))
        assert v.valid
        step = v.schedule.modules[0]
        assert step.params["voc"].source is Source.DERIVED
        assert step.params["voc"].detail == "jv_bace jv_bace (this session)"
        assert s.wait_run(second, TIMEOUT)
        frames = frames_of(s, second)
        axis = next(f for f in frames if f["type"] == "AxisResolved")
        assert axis["data"]["voc"] == voc.value and axis["node_path"] == "bace"
        shots = [f for f in frames if f["type"] == "StepDone"]
        assert len(shots) == 2
        verdict = shots[0]["data"]["verdict"]
        assert verdict["level"] in ("ok", "warn") and "rail_light" in verdict
        assert verdict["autorange_passes"] == 1, "the simulated digitiser's pass count"
        assert shots[0]["decimated"] == {}, "400-sample records need no decimation"
        line = next(l for l in journal_lines(s) if l["type"] == "StepDone")
        assert "light" not in line["data"] and line["data"]["verdict"] == verdict

        rec = s.run_record(second)
        assert rec["state"] == "done" and (rec["kept"], rec["requested"]) == (2, 2)
        name = os.path.basename(rec["folders"][0])
        assert f"{round(voc.value * 1000)}mVVOC" in name and "1020mVLED" in name
        assert rec["node_outcomes"]["bace"]["detail"]["voc"]["how"] == "jv_bace"
        assert s.run_data(second)["q_all"].shape == (2, 1)
        card = s.module_wire("bace")
        assert card["last"]["run_id"] == second and card["last"]["summary"].startswith("Q ")
        queued = next(l for l in journal_lines(s)
                      if l["type"] == "RunQueued" and l["run_id"] == second)
        assert "voc" not in queued["data"]["params"], (
            "a derived V_oc is not written as a last-used value")
        assert s.catalogue.param_set("bace").get("voc").value is None
        assert s.session_voc is voc, "a bace that centred on the session's V_oc does not replace it"


def test_submit_refuses_crit_and_invalid_trees_with_the_checks_and_queues_nothing(tmp_path):
    with make_session(tmp_path) as s:
        with pytest.raises(SubmitRefused) as refused:
            s.submit(bace(1, voc=0.9, smu_current_compliance_a=0.1))
        checks = {(c.code, c.level) for c in refused.value.validation.checks}
        assert ("smu.ceiling", "crit") in checks
        assert "smu.ceiling" in str(refused.value)
        with pytest.raises(SubmitRefused) as refused:
            s.submit(bace(1))                          # centre_on_voc from run.toml, no source
        assert ("voc.source", "invalid") in {(c.code, c.level)
                                              for c in refused.value.validation.checks}
        with pytest.raises(SubmitRefused) as refused:
            s.submit(tree_for_module("nope"))
        assert refused.value.validation.by_code("tree.shape")[0].level == "invalid"
        assert s.last_validated["tree"]["module"] == "bace" and not s.last_validated["valid"], (
            "a tree that did not resolve is not kept; the last one that did is")
        with pytest.raises(ValueError, match="single module node"):
            s.submit(temperature_pipeline(bace(1, voc=0.9)), kind="manual")
        assert s.last_validated["valid"] is True and s.last_validated["tree"]["loop"] == "temperature"
        assert s.records == [] and s.events_since(0) == [] and s.runs_index() == []
        assert len(journal_lines(s)) == 1
        with pytest.raises(UnknownRun):
            s.stop("nope")
        with pytest.raises(UnknownRun):
            s.run_record("nope")


# -- the queue and the pause ------------------------------------------------------------
def test_the_queue_holds_a_second_run_while_the_first_is_paused(tmp_path):
    with make_session(tmp_path) as s:
        first, v = s.submit(temperature_pipeline(bace(1, voc=0.9)), name="cool down")
        assert v.valid and s.run_record(first)["kind"] == "pipeline"
        wait_until(lambda: s.run_record(first)["state"] == "paused")
        second, _ = s.submit(tree_for_module("jv_dark", {"step_v": 0.1}))
        rec2 = s.run_record(second)
        assert rec2["state"] == "queued"
        assert rec2["position"] == 0, "nothing queued ahead of it: it starts when the first ends"

        snap = s.bench_snapshot()
        assert snap["state"] == "paused" and snap["queue"] == [second]
        assert snap["run"]["run_id"] == first and snap["run"]["node_path"] == "T=250K"
        assert snap["run"]["pending"]["what"] == "temperature"
        assert snap["run"]["pending"]["detail"]["setpoint_k"] == 250.0
        assert s.run_active() == first
        with pytest.raises(Busy):
            s.bench_read()
        with pytest.raises(Busy):
            s.bench_action("led-off")
        with pytest.raises(NotPaused):
            s.resume(second, {})
        with pytest.raises(UnknownRun):
            s.resume("nope", {})
        assert s.module_wire("jv_dark")["last"] == {
            "run_id": second, "state": "queued", "node_path": "jv_dark",
            "ts": rec2["queued_at"], "summary": None}

        assert s.resume(first, {"temperature_k": 250.0, "note": "set by hand"}) == \
            {"run_id": first, "state": "paused"}
        assert s.wait_run(first, TIMEOUT) and s.wait_run(second, TIMEOUT)
        assert s.run_record(first)["state"] == "done"
        assert s.run_record(second)["state"] == "done"
        with pytest.raises(NotPaused):
            s.resume(first, {})

        frames = s.events_since(0)
        first_done = next(i for i, f in enumerate(frames)
                          if f["run_id"] == first and f["type"] == "RunStateChanged"
                          and f["data"]["state"] == "done")
        second_start = next(i for i, f in enumerate(frames)
                            if f["run_id"] == second and f["type"] == "RunStateChanged"
                            and f["data"]["state"] == "preflight")
        assert first_done < second_start, "the second run waited for the first"
        resumed = next(f for f in frames if f["type"] == "OperatorResumed")
        assert resumed["run_id"] == first and resumed["node_path"] == "T=250K"
        assert resumed["data"]["detail"] == {"temperature_k": 250.0, "note": "set by hand"}

        rec1 = s.run_record(first)
        parent = rec1["folder"]
        assert os.path.dirname(parent) == str(tmp_path / "runs")
        assert os.path.basename(parent).startswith("cool_down_")
        assert rec1["folders"] and all(os.path.dirname(f) == parent for f in rec1["folders"])
        assert "250K" in os.path.basename(rec1["folders"][0])
        assert set(rec1["node_outcomes"]) == {"T=250K", "T=250K/bace"}
        assert rec1["node_outcomes"]["T=250K"]["outcome"] == "ok"
        assert rec1["node_outcomes"]["T=250K/bace"]["detail"]["temperature_k"] == 250.0
        assert rec1["params_as_executed"]["T=250K/bace"]["voc"]["source"] == "edited"
        idx = s.runs_index()
        assert [r["run_id"] for r in idx] == [second, first]
        assert idx[1]["tree_summary"] == "T×1/bace" and idx[1]["kind"] == "pipeline"
        assert idx[1]["name"] == "cool down" and idx[1]["folder"] == parent
        assert s.journal.settle_history() == {250.0: [pytest.approx(
            s.journal.settle_history()[250.0][0])]}

        with pytest.raises(NodeRequired) as need:
            s.run_data(first)
        assert need.value.nodes == ["T=250K/bace"]
        assert s.run_data(first, "T=250K/bace")["kept"] == 1
        with pytest.raises(DataUnavailable):
            s.run_data(first, "T=250K/nope")


def test_stop_abort_and_cancel_through_the_session(tmp_path):
    with make_session(tmp_path) as s:
        run_id, _ = s.submit(bace(200, voc=0.9))
        wait_until(lambda: any(f["type"] == "StepDone" for f in frames_of(s, run_id)))
        out = s.stop(run_id, "abort")
        assert out["mode"] == "abort" and out["run_id"] == run_id
        assert s.wait_run(run_id, TIMEOUT)
        rec = s.run_record(run_id)
        assert rec["state"] == "aborted" and rec["parked"]
        node = rec["node_outcomes"]["bace"]
        assert node["outcome"] == "aborted", "closed by the terminal state"
        # The generator was closed before it could report, but the folder
        # its recorder opened and the shots it took are on the record, so
        # GET /runs/{id} does not say "nothing written" about a run whose
        # files are on disk.
        assert node["detail"]["folder"] and os.path.isdir(node["detail"]["folder"])
        assert node["detail"]["kept"] >= 1 and node["detail"]["requested"] == 200
        assert rec["folders"] == [node["detail"]["folder"]]
        assert rec["kept"] >= 1 and rec["requested"] == 200
        frames = frames_of(s, run_id)
        assert frames[-1]["data"]["state"] == "parked"
        assert [f["data"]["reason"] for f in frames if f["type"] == "RunAborted"] == ["aborted"]
        assert states_of(frames)[-2:] == ["aborted", "parked"]
        with pytest.raises(Conflict):
            s.stop(run_id, "abort")
        assert s.run_data(run_id)["kept"] >= 1, "what was measured is still served"
        assert not s.bench.sim.bench.bias_output and not s.bench.sim.bench.shutter_open
        assert s.bench.sim.led.output_enabled, "the LED is left pulsing; the shutter is the light switch"

        run_id, _ = s.submit(bace(200, voc=0.9))
        wait_until(lambda: any(f["type"] == "StepDone" for f in frames_of(s, run_id)))
        assert s.stop(run_id, "after_shot")["mode"] == "after_shot"
        assert s.wait_run(run_id, TIMEOUT)
        rec = s.run_record(run_id)
        assert rec["state"] == "stopped" and rec["kept"] < rec["requested"] == 200
        assert rec["node_outcomes"]["bace"]["outcome"] == "stopped"
        assert rec["node_outcomes"]["bace"]["detail"]["summary"].startswith("stopped after")
        assert s.runs_index()[0]["outcome_text"].startswith("stopped after")

        # a queued run is cancelled whichever mode is asked, and never parked
        paused, _ = s.submit(temperature_pipeline(bace(1, voc=0.9)))
        with pytest.raises(ValueError, match="stop mode"):
            s.stop(paused, "now")
        wait_until(lambda: s.run_record(paused)["state"] == "paused")
        queued, _ = s.submit(tree_for_module("note", {"text": "later"}))
        assert s.stop(queued) == {"run_id": queued, "state": "cancelled", "mode": "after_shot"}
        assert s.run_record(queued)["state"] == "cancelled"
        assert states_of(frames_of(s, queued)) == ["queued", "cancelled"]
        assert s.bench_snapshot()["queue"] == []
        assert s.cancel(queued) is False
        s.stop(paused, "abort")
        assert s.wait_run(paused, TIMEOUT) and s.run_record(paused)["state"] == "aborted"
        assert s.run_record(paused)["node_outcomes"]["T=250K"]["outcome"] == "aborted"


def test_a_crit_on_the_read_back_at_start_blocks_the_run_without_touching_the_bench(tmp_path):
    with make_session(tmp_path) as s:
        s.bench_read()
        s.bench.sim.bias.enable_output(True)          # the operator left the 81150A on
        run_id, v = s.submit(bace(1, voc=0.9))
        assert v.valid, "the cached read-back saw the output off"
        assert s.wait_run(run_id, TIMEOUT)
        rec = s.run_record(run_id)
        # A crit at Start is its own terminal state (contract section 2): a
        # blocked run is not one that crashed, and a client can tell them
        # apart without parsing the error.
        assert rec["state"] == "blocked" and rec["error"].startswith("BlockedAtStart: blocked at start")
        assert "bench.live-at-start" in rec["error"]
        assert [c["code"] for c in rec["verdicts"] if c["level"] == "crit"] == \
            ["bench.live-at-start", "relay.interlock"] or "bench.live-at-start" in rec["error"]
        frames = frames_of(s, run_id)
        assert [f["data"]["where"] for f in frames if f["type"] == "RunFailed"] == ["preflight"]
        assert states_of(frames) == ["queued", "preflight", "blocked", "parked"]
        assert s.bench.sim.bench.shots == 0 and rec["folders"] == []
        assert not s.bench.sim.bench.bias_output, "parked on the way out"


# -- the bench outside a run ----------------------------------------------------------------
def test_bench_actions_are_journaled_by_hand_and_refusals_are_verdicts(tmp_path):
    with make_session(tmp_path) as s:
        assert s.bench_snapshot()["read_at"] is None
        snap = s.bench_read()
        assert snap["read_at"] is not None and snap["instruments"]["led"]["polarity"] == "NORM"
        assert [i["level"] for i in snap["chain"]["items"]] == ["warn", "warn", "ok", "info"]
        verdicts = [f for f in s.events_since(0) if f["type"] == "Verdict"]
        assert [v["data"]["code"] for v in verdicts][:2] == ["chain.led-polarity", "chain.bias-arm"]
        assert {v["run_id"] for v in verdicts} == {None}

        out = s.bench_action("set-33220a-pol-inv")
        assert out["result"] == {"polarity": "INV", "before": {"led.polarity": "NORM"}}, (
            "the value before the action rides with the result, so the log can say NORM -> INV")
        assert out["job"].startswith("action-")
        action = next(f for f in s.events_since(0) if f["type"] == "BenchAction")
        assert action["run_id"] is None and action["node_path"] == ""
        assert action["data"] == {"name": "set-33220a-pol-inv", "args": {},
                                  "result": {"polarity": "INV", "before": {"led.polarity": "NORM"}},
                                  "by": "hand"}
        assert any(l["type"] == "BenchAction" for l in journal_lines(s))
        assert s.bench_snapshot()["chain"]["items"][0]["level"] == "ok", "read back after it"

        s.bench_action("set-led-pulse")
        assert s.bench.sim.led.output_enabled and s.bench.sim.led.last_levels == (1.02, 0.4)
        before = s.last_seq
        with pytest.raises(BenchActionRefused) as refused:
            s.bench_action("set-33220a-pol-norm")
        assert refused.value.level == "warn" and "LED off" in refused.value.text
        assert s.bench.sim.led.polarity() == "INV", "nothing moved"
        new = [f for f in s.events_since(before)]
        assert [f["type"] for f in new][:2] == ["Verdict", "BenchAction"]
        assert new[0]["data"]["code"] == "action.set-33220a-pol-norm"
        assert new[1]["data"]["result"]["refused"] is True

        with pytest.raises(KeyError):
            s.bench_action("make-coffee")
        with pytest.raises(ValueError, match="unknown argument"):
            s.bench_action("set-led-pulse", {"bogus": 1})
        s.bench_action("led-off")
        assert not s.bench.sim.led.output_enabled
        with pytest.raises(BenchActionRefused) as refused:
            s.bench_action("set-led-pulse", {"low": 1.5})
        assert "threshold" in refused.value.text


def test_park_during_a_run_aborts_it_cancels_the_queue_and_parks(tmp_path):
    with make_session(tmp_path) as s:
        first, _ = s.submit(temperature_pipeline(bace(1, voc=0.9)))
        wait_until(lambda: s.run_record(first)["state"] == "paused")
        second, _ = s.submit(tree_for_module("note", {"text": "later"}))
        out = s.bench_action("park")
        assert out["result"]["parked"] is True
        assert set(out["result"]["before"]) == {"bias.output", "smu.output", "led.output",
                                                "shutter.open"}
        assert s.run_record(first)["state"] == "aborted"
        assert s.run_record(second)["state"] == "cancelled"
        assert s.bench_snapshot()["state"] == "idle"


# -- events, subscribers, the monitor ---------------------------------------------------
def test_events_since_replays_and_subscribers_are_fed_or_dropped(tmp_path):
    with make_session(tmp_path) as s:
        run_id, _ = s.submit(tree_for_module("note", {"text": "hello"}))
        assert s.wait_run(run_id, TIMEOUT)
        n = s.last_seq
        tail = s.events_since(n - 3)
        assert [f["seq"] for f in tail] == [n - 2, n - 1, n]
        assert s.events_since(n) == []
        hello = s.hello()
        assert hello["seq"] == n and hello["bench"]["state"] == "idle"
        json.dumps(hello)

        live = s.subscribe(queue.SimpleQueue())
        assert s.subscribers == 1
        run_id, _ = s.submit(tree_for_module("note", {"text": "again"}))
        assert s.wait_run(run_id, TIMEOUT)
        got = []
        while not live.empty():
            got.append(live.get_nowait())
        assert [f["type"] for f in got][:2] == ["RunQueued", "RunStateChanged"]
        assert any(f["type"] == "Notice" and f["data"]["text"] == "again" for f in got)
        assert got[-1]["data"]["state"] == "parked"
        s.unsubscribe(live)
        assert s.subscribers == 0

        class Behind:
            """A sink that never drains."""
            def __init__(self):
                self.items = []

            def put_nowait(self, frame):
                self.items.append(frame)

            def qsize(self):
                return 5000

        slow = s.subscribe(Behind())
        s.bench_read()
        assert s.subscribers == 0, "dropped at the first frame it could not keep up with"
        assert slow.items[-1] is None and slow.items[-2]["type"] == "Notice"
        assert "dropped" in slow.items[-2]["data"]["text"]
        assert slow.items[-2]["data"]["since"] == slow.items[-3]["seq"], (
            "reconnect from the last numbered frame it was given")
        assert s.errors == []

        # an ephemeral frame is not even queued for a sink that is behind:
        # "where inside the shot the run is" is worthless fifty frames late
        class Lagging(Behind):
            def qsize(self):
                return 60

        lag, fresh = s.subscribe(Lagging()), s.subscribe(queue.SimpleQueue())
        from bace.experiment import events as E
        from bace.service.wire import ephemeral_frame
        s._fan_out(ephemeral_frame("r", "bace", E.StepPhase(0, "levels", 1, 7)), ephemeral=True)
        assert lag.items == [] and fresh.get_nowait()["type"] == "StepPhase"
        s._fan_out({"seq": 1, "type": "Notice", "data": {}})
        assert len(lag.items) == 1, "a numbered frame is queued whatever the backlog"
        assert s.subscribers == 2
        s.unsubscribe(lag)
        s.unsubscribe(fresh)


def test_the_power_monitor_reads_beside_a_run_and_says_so_on_the_snapshot(tmp_path):
    with make_session(tmp_path) as s:
        assert s.monitors() == [] and s.stop_power_monitor() is False
        info = s.start_power_monitor(0.01)
        assert info["running"] and info["interval_s"] == 0.01
        with pytest.raises(Conflict):
            s.start_power_monitor(0.5)
        with pytest.raises(ValueError):
            s.start_power_monitor(0.0)
        assert s.bench_snapshot()["instruments"]["power"]["monitor"] is True
        wait_until(lambda: sum(1 for f in s.events_since(0) if f["type"] == "PowerReading") >= 3)
        readings = [f for f in s.events_since(0) if f["type"] == "PowerReading"]
        assert {f["run_id"] for f in readings} == {None}
        assert readings[0]["data"]["source"] == "simulated" and readings[0]["data"]["watts"] >= 0
        assert s.monitors()[0]["readings"] >= 3

        run_id, _ = s.submit(bace(60, voc=0.9))
        assert s.wait_run(run_id, TIMEOUT)
        frames = s.events_since(0)
        first_shot = next(i for i, f in enumerate(frames) if f["type"] == "StepDone")
        last_shot = max(i for i, f in enumerate(frames) if f["type"] == "StepDone")
        between = [f["type"] for f in frames[first_shot:last_shot]]
        assert "PowerReading" in between, "the observer kept reading while the scan ran"
        assert s.stop_power_monitor() is True and s.monitors() == []
        assert s.bench_snapshot()["instruments"]["power"]["monitor"] is False
        assert any(l["type"] == "PowerReading" for l in journal_lines(s))

        s.bench.rig.power = None
        with pytest.raises(ValueError, match="no power meter"):
            s.start_power_monitor(0.1)


def test_the_ring_keeps_every_scalar_and_the_traces_of_the_last_shots_only(tmp_path):
    with make_session(tmp_path) as s:
        run_id, _ = s.submit(bace(RING_TRACES_KEPT + 5, voc=0.9))
        assert s.wait_run(run_id, TIMEOUT)
        shots = [f for f in s.events_since(0) if f["type"] == "StepDone"]
        assert len(shots) == RING_TRACES_KEPT + 5
        old, recent = shots[0], shots[-1]
        assert old["data"]["light"] is None and old["decimated"]["light"] == \
            {"omitted": True, "replay": True}
        assert old["data"]["q"] is not None and old["data"]["verdict"]["level"] in ("ok", "warn")
        assert recent["data"]["light"]["y"] and recent["decimated"] == {}
        assert sum(1 for f in shots if f["data"]["light"] is not None) == RING_TRACES_KEPT
        assert [f["seq"] for f in s.events_since(0)] == \
            list(range(1, s.last_seq + 1)), "the slimming changes no seq"


def test_under_an_asyncio_loop_frames_reach_an_awaiting_subscriber(tmp_path):
    """The app's path: the worker's callbacks are posted to the loop with
    call_soon_threadsafe, and an asyncio.Queue subscriber is woken by them."""
    import asyncio

    async def main():
        s = make_session(tmp_path)
        s.start(asyncio.get_running_loop())
        try:
            live = s.subscribe()
            assert isinstance(live, asyncio.Queue)
            run_id, _ = s.submit(tree_for_module("jv_dark", {"step_v": 0.1}))
            seen = []
            while True:
                frame = await asyncio.wait_for(live.get(), TIMEOUT)
                seen.append(frame)
                if frame["type"] == "RunStateChanged" and frame["data"]["state"] == "parked":
                    break
            assert [f["type"] for f in seen[:3]] == ["RunQueued", "RunStateChanged",
                                                     "RunStateChanged"]
            assert [f["seq"] for f in seen] == list(range(1, len(seen) + 1))
            assert any(f["type"] == "JVCurveDone" for f in seen)
            assert s.run_record(run_id)["state"] == "done"
            snap = await asyncio.get_running_loop().run_in_executor(None, s.bench_read)
            assert snap["read_at"] is not None
            assert s.errors == []
        finally:
            s.close()

    asyncio.run(main())


def test_close_parks_the_bench_and_closes_the_journal(tmp_path):
    s = make_session(tmp_path).start()
    run_id, _ = s.submit(temperature_pipeline(bace(1, voc=0.9)))
    wait_until(lambda: s.run_record(run_id)["state"] == "paused")
    s.close()
    assert s.run_record(run_id)["state"] == "aborted"
    assert not s.bench.sim.bench.bias_output
    with pytest.raises(Conflict):
        s.submit(tree_for_module("note", {"text": "late"}))
    s.close()                                              # idempotent
    lines = journal_lines(s)
    assert lines[-1]["type"] == "RunStateChanged" and lines[-1]["data"]["state"] == "parked"


def test_a_second_session_on_the_same_id_continues_the_journal_and_the_numbering(tmp_path):
    with make_session(tmp_path) as s:
        run_id, _ = s.submit(tree_for_module("note", {"text": "one"}))
        assert s.wait_run(run_id, TIMEOUT)
        seq = s.last_seq
    with make_session(tmp_path) as again:
        assert again.journal.resumed and again.last_seq == seq
        run_id, _ = again.submit(tree_for_module("note", {"text": "two"}))
        assert run_id == f"{SID}-002"
        assert again.wait_run(run_id, TIMEOUT)
        assert [r["run_id"] for r in again.runs_index()] == [f"{SID}-002", f"{SID}-001"]
        assert [l["type"] for l in journal_lines(again)].count("SessionStarted") == 1


# -- state reporting at the edges -------------------------------------------------------
def test_a_stop_accepted_during_preflight_is_not_displaced_by_the_workers_running(tmp_path):
    """On the real rig preflight is a chain read-back of several VISA
    queries; a Stop within a second of Start lands there, and the worker's
    `running` that follows must not put itself over the `stopping` the
    session already said."""
    import threading

    with make_session(tmp_path) as s:
        gate, entered = threading.Event(), threading.Event()
        real = s.bench.read_back

        def held(*args, **kw):
            entered.set()
            assert gate.wait(TIMEOUT)
            return real(*args, **kw)

        s.bench.read_back = held
        run_id, _ = s.submit(bace(2, voc=0.9))
        assert entered.wait(TIMEOUT)
        wait_until(lambda: s.run_record(run_id)["state"] == "preflight")
        assert s.stop(run_id, "after_shot") == {"run_id": run_id, "state": "stopping",
                                                "mode": "after_shot"}
        assert s.run_record(run_id)["state"] == "stopping"
        assert s.bench_snapshot()["state"] == "stopping"
        gate.set()
        assert s.wait_run(run_id, TIMEOUT)
        rec = s.run_record(run_id)
        assert rec["state"] == "stopped" and rec["parked"]
        # the record never said `running` after the stop was accepted
        states = states_of(frames_of(s, run_id))
        assert states.index("stopping") < states.index("stopped")
        assert states[-2:] == ["stopped", "parked"]


def test_park_during_a_long_instrument_call_stays_queued_rather_than_timing_out(tmp_path):
    """A park's own wait must not cancel the park: the run it aborted may
    be inside a VISA call that outlasts the wait, and a park that vanished
    would leave no by-hand line. It answers `pending` and runs when the
    worker frees."""
    import threading

    with make_session(tmp_path) as s:
        gate, entered = threading.Event(), threading.Event()
        real = s.bench.sim.scope.acquire

        def held(*args, **kw):
            entered.set()
            assert gate.wait(TIMEOUT)
            return real(*args, **kw)

        s.bench.sim.scope.acquire = held
        run_id, _ = s.submit(bace(5, voc=0.9))
        assert entered.wait(TIMEOUT)
        out = s.bench_action("park", timeout_s=0.3)
        assert out["pending"] is True and out["name"] == "park" and out["result"] is None
        assert [j.id for j in s.worker.queued] == [out["job"]], "still queued, not cancelled"
        gate.set()
        assert s.wait_run(run_id, TIMEOUT)
        wait_until(lambda: any(f["type"] == "BenchAction" and f["data"]["name"] == "park"
                               for f in s.events_since(0)))
        assert s.run_record(run_id)["state"] == "aborted"
        park = next(f for f in s.events_since(0) if f["type"] == "BenchAction")
        assert park["data"]["result"]["parked"] is True and park["data"]["by"] == "hand"
        wait_until(lambda: s.bench_snapshot()["state"] == "idle")

        # an ordinary action's timeout still cancels it
        gate2, entered2 = threading.Event(), threading.Event()

        def held2(*args, **kw):
            entered2.set()
            assert gate2.wait(TIMEOUT)
            return real(*args, **kw)

        s.bench.sim.scope.acquire = held2
        run_id, _ = s.submit(bace(5, voc=0.9))
        assert entered2.wait(TIMEOUT)
        with pytest.raises(Busy):
            s.bench_action("led-off", timeout_s=0.2)
        s.stop(run_id, "abort")
        gate2.set()
        assert s.wait_run(run_id, TIMEOUT)


def test_the_bench_state_is_always_one_of_the_contracts_five():
    from bace.service.session import BENCH_STATES, _bench_state

    for state in BENCH_STATES:
        assert _bench_state(state) == state
    assert _bench_state("queued") == "preflight"
    for state in ("done", "stopped", "aborted", "failed", "blocked"):
        assert _bench_state(state) == "stopping"
    assert set(BENCH_STATES) == {"idle", "preflight", "running", "paused", "stopping"}


def test_a_pipeline_record_carries_a_measured_eta_and_no_duplicate_verdicts(tmp_path):
    with make_session(tmp_path) as s:
        s.bench_read()
        run_id, v = s.submit(temperature_pipeline(bace(1, voc=0.9)), name="eta")
        cost0 = s.run_record(run_id)["cost"]
        assert cost0.get("finish_source") is None and "finish_at_submit" not in cost0
        wait_until(lambda: s.run_record(run_id)["state"] == "paused")
        time.sleep(0.25)                                      # the operator's settle
        s.resume(run_id, {"temperature_k": 250.0})
        assert s.wait_run(run_id, TIMEOUT)
        rec = s.run_record(run_id)
        assert rec["eta"] is not None and rec["eta"]["node_path"] == "T=250K"
        assert rec["eta"]["finish_at"] == pytest.approx(rec["eta"]["at"] + rec["eta"]["eta_s"])
        assert rec["cost"]["finish_source"] == "measured"
        assert rec["cost"]["finish_at"] == rec["eta"]["finish_at"]
        assert "finish_at_submit" in rec["cost"]
        loops = [f for f in frames_of(s, run_id) if f["type"] == "Progress" and f["data"]["node_path"]]
        assert all(f["data"]["eta_s"] is not None for f in loops)
        assert loops[-1]["data"]["eta_s"] == 0.0, "nothing left after the last exit"

        keys = [(c["code"], c.get("node_path", "")) for c in rec["verdicts"]]
        assert len(keys) == len(set(keys)), "one entry per (code, node): the Start re-read " \
                                             "replaces the submit-time copy"
        assert "chain.led-polarity" in {k for k, _ in keys}


def test_run_record_answers_from_the_journal_for_a_run_of_an_earlier_session(tmp_path):
    with make_session(tmp_path) as s:
        jv, _ = s.submit(tree_for_module("jv_bace", {"step_v": 0.05}))
        assert s.wait_run(jv, TIMEOUT)
        pipe, _ = s.submit(temperature_pipeline(
            tree_for_module("jv_bace", {"step_v": 0.05}), bace(1, centre_on_voc=True)),
            name="grid")
        wait_until(lambda: s.run_record(pipe)["state"] == "paused")
        s.resume(pipe, {"temperature_k": 250.1})
        assert s.wait_run(pipe, TIMEOUT)
        rec0 = s.run_record(pipe)
        voc = rec0["node_outcomes"]["T=250K/bace"]["detail"]["voc"]["value"]
        assert rec0["from"] == "session" and voc is not None

    with make_session(tmp_path, session_id="20260902_213000") as later:
        rec = later.run_record(pipe)
        assert rec["from"] == "journal" and rec["data_in_memory"] is False
        assert rec["state"] == "done" and rec["kind"] == "pipeline" and rec["name"] == "grid"
        assert rec["tree"]["loop"] == "temperature"
        node = rec["nodes"]["T=250K/bace"]
        assert node["module"] == "bace" and node["outcome"] == "ok"
        assert node["voc"] == voc and node["voc_how"] == "jv_bace"
        assert node["temperature_k"] == 250.1 and node["led_v"] == 1.02
        assert (node["kept"], node["requested"]) == (1, 1) and node["folder"]
        assert later.run_record(jv)["module"] == "jv_bace"
        with pytest.raises(UnknownRun):
            later.run_record("20260101_000000-001")
        assert later.runs_index("all")[0]["run_id"] == pipe
        assert later.runs_index("all")[-1]["light_curves"] == 1


def test_the_temperature_monitor_reads_the_331_beside_the_bench(tmp_path):
    """Naming a console under `--sim` attaches the simulated 331, and the
    monitor reads the very controller a settle drives -- so its readings say
    `simulated`, and on the real bench the console's URL."""
    with make_session(tmp_path) as s:
        with pytest.raises(ValueError, match="not wired"):
            s.start_temperature_monitor(0.05)
    wired = Session(RigConfig(temperature_console="http://127.0.0.1:8331"),
                    run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                    mode="sim", fast=True, seed=5, session_id="20260902_214500")
    with wired as s:
        assert s.bench.rig.temperature is s.bench.sim.temperature
        s.bench.sim.temperature.set_setpoint(250.0)
        assert s.monitors() == [] and s.stop_temperature_monitor() is False
        info = s.start_temperature_monitor(0.02)
        assert info["name"] == "temperature" and info["running"]
        assert info["console"] == "http://127.0.0.1:8331"
        with pytest.raises(Conflict):
            s.start_temperature_monitor(0.5)
        wait_until(lambda: sum(1 for f in s.events_since(0)
                               if f["type"] == "TemperatureRead") >= 3)
        reads = [f for f in s.events_since(0) if f["type"] == "TemperatureRead"]
        assert {f["run_id"] for f in reads} == {None}
        first = reads[0]["data"]
        assert (first["setpoint_k"], first["in_band"], first["source"]) == (250.0, None, "simulated")
        assert 250.0 < first["kelvin"] < 294.8, "the stand-in is walking toward the setpoint"
        assert reads[1]["data"]["kelvin"] < first["kelvin"]
        t = s.bench_snapshot()["instruments"]["temperature"]
        assert t["wired"] is True and t["monitor"] is True and t["reads"] >= 3
        assert t["kelvin"] < first["kelvin"] and t["source"] == "simulated"
        assert t["read_at"] is not None
        assert [m["name"] for m in s.monitors()] == ["temperature"]
        s.start_power_monitor(0.05)
        assert [m["name"] for m in s.monitors()] == ["power", "temperature"]
        assert s.stop_temperature_monitor() is True
        assert s.bench_snapshot()["instruments"]["temperature"]["monitor"] is False
        assert any(l["type"] == "TemperatureRead" for l in journal_lines(s))
