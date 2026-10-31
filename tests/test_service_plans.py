"""Experiment plans: the envelope, the approval, and the refusal of what lies outside.

The envelope logic is tested on parsed trees; the gate end to end over HTTP
on a `--sim --fast` session, as `test_service_api` does.
"""
from __future__ import annotations

import json

import pytest

from bace.experiment.rig import RigConfig
from bace.service import pipeline
from bace.service.modules import Catalogue
from bace.service.plans import AUTO_APPROVED, Envelope, PlanBook, PlanConflict, PlanError

CAT = Catalogue(rig_config=RigConfig())

ENVELOPE = {
    "modules": {"bace": {"vcoll": {"min": -4.0, "max": -0.5},
                         "delay_ns": {"min": 0, "max": 400},
                         "n_loops": {"max": 4}},
                "jv_bace": {}},
    "loops": {"illumination": {"levels_v": {"min": 1.0, "max": 1.03}},
              "repeat": {"count": {"max": 3}}},
    "max_runs": 3, "max_total_s": 3600, "expires_s": 3600,
}
PLAN = {"title": "V_coll saturation", "goal": "Q against V_coll at V_oc", "envelope": ENVELOPE}


def parsed(obj):
    v = pipeline.validate(obj, CAT, rig_config=RigConfig())
    assert v.schedule is not None, [c.text for c in v.checks if c.level == "invalid"]
    return v.schedule.tree


def bace(**params):
    return {"kind": "module", "module": "bace", "params": params}


# -- the envelope ------------------------------------------------------------------
def test_an_envelope_names_known_modules_and_parameters_with_sound_bounds():
    Envelope.from_wire(ENVELOPE, catalogue=CAT)
    for bad, says in (
            ({**ENVELOPE, "modules": {"bake": {}}}, "no module 'bake'"),
            ({**ENVELOPE, "modules": {"bace": {"vcol": {"max": 0}}}}, "no parameter 'vcol'"),
            ({**ENVELOPE, "modules": {"bace": {"vcoll": {"min": 1, "max": 0}}}}, "above max"),
            ({**ENVELOPE, "modules": {"bace": {"vcoll": [-4, 0]}}}, "a bound is"),
            ({**ENVELOPE, "loops": {"spiral": {}}}, "not one of"),
            ({**ENVELOPE, "max_runs": 0}, "max_runs"),
            ({**ENVELOPE, "max_total_s": "NaN"}, "finite"),
            ({**ENVELOPE, "max_total_s": float("inf")}, "finite"),
            ({**ENVELOPE, "modules": {"bace": {"vcoll": {"max": float("nan")}}}}, "finite"),
            ({**ENVELOPE, "surprise": 1}, "unknown key")):
        with pytest.raises(PlanError, match=says):
            Envelope.from_wire(bad, catalogue=CAT)


def test_what_a_tree_sets_must_lie_inside_the_envelope():
    env = Envelope.from_wire(ENVELOPE, catalogue=CAT)
    assert env.violations(parsed(bace(vcoll=-2.0, n_loops=2))) == []
    assert env.violations(parsed(bace())) == [], "unset parameters keep their layers"
    [v] = env.violations(parsed(bace(vcoll=-6.0)))
    assert "vcoll = -6.0" in v and "between -4 and -0.5" in v
    [v] = env.violations(parsed(bace(n_averages=50)))
    assert "n_averages is set" in v
    [v] = env.violations(parsed({"kind": "module", "module": "jv", "params": {}}))
    assert "module jv is not in the plan" in v
    loop = {"kind": "loop", "loop": "illumination", "levels_v": [1.01, 1.05],
            "children": [bace(vcoll=-1.0)]}
    [v] = env.violations(parsed(loop))
    assert "levels_v" in v and "1.05" in v
    rep = {"kind": "loop", "loop": "repeat", "count": 5, "children": [bace()]}
    assert any("count" in v for v in env.violations(parsed(rep)))
    temp = {"kind": "loop", "loop": "temperature", "values_k": [295], "children": [bace()]}
    assert env.violations(parsed(temp)) == ["a temperature loop is not in the plan"]


def test_a_scan_is_held_to_the_bound_of_the_parameter_it_sweeps():
    """Loose bounds on axis_start and axis_stop must not let a scan of V_coll
    reach -10 V when the plan holds V_coll to -4 V."""
    loose = {**ENVELOPE, "modules": {"bace": {
        **ENVELOPE["modules"]["bace"],
        "axis_name": {"choices": ["vpre", "vcoll", "delay_ns"]},
        "axis_start": {"min": -10, "max": 400}, "axis_stop": {"min": -10, "max": 400},
        "axis_step": {"min": 0, "max": 50}, "centre_on_voc": {"choices": [True, False]}}}}
    env = Envelope.from_wire(loose, catalogue=CAT)

    def scan(name, start, stop, step, **more):
        return bace(axis_name=name, axis_start=start, axis_stop=stop, axis_step=step, **more)

    assert env.violations(parsed(scan("delay_ns", 30, 150, 20))) == []
    assert env.violations(parsed(scan("vcoll", -0.5, -2.0, 0.5))) == []
    found = env.violations(parsed(scan("vcoll", -0.5, -10.0, 0.5)))
    assert found == ["bace.axis_stop = -10.0 sweeps vcoll, the plan allows between -4 and -0.5"]
    # the plan does not let vpre be set, so it may not be swept either ...
    [v] = env.violations(parsed(scan("vpre", 0.2, 0.6, 0.1, centre_on_voc=False)))
    assert "sweeps vpre, and the plan does not let it be set" in v
    # ... except as offsets around V_oc, which only the axis bounds hold
    assert env.violations(parsed(scan("vpre", -0.05, 0.05, 0.05, centre_on_voc=True))) == []
    # whether a vpre scan is centred cannot be left to the layers (run.toml centres it)
    [v] = env.violations(parsed(scan("vpre", -0.05, 0.05, 0.05)))
    assert "without setting centre_on_voc" in v
    # an end without its axis cannot be checked, nor one end without the other
    [v] = env.violations(parsed(bace(axis_start=30, axis_stop=150, axis_step=20)))
    assert "without axis_name" in v
    [v] = env.violations(parsed(bace(axis_name="vcoll", axis_start=-0.5, axis_step=0.5)))
    assert "without axis_stop" in v


def test_a_loop_list_keeps_to_choices_element_by_element():
    """A plan that fixes the LED level with choices [1.02] must accept the loop
    [1.02]; the whole list compared to the choices refused it."""
    env = Envelope.from_wire({**ENVELOPE, "loops": {
        "illumination": {"levels_v": {"choices": [1.02, 1.03]}}}}, catalogue=CAT)
    loop = lambda levels: {"kind": "loop", "loop": "illumination", "levels_v": levels,  # noqa: E731
                           "children": [bace(vcoll=-1.0)]}
    assert env.violations(parsed(loop([1.02]))) == []
    assert env.violations(parsed(loop([1.02, 1.03]))) == []
    [v] = env.violations(parsed(loop([1.02, 1.01])))
    assert "levels_v" in v and "one of 1.02, 1.03" in v


def test_a_scan_over_discrete_choices_applies_only_those_values():
    """With choices, the points between the ends matter: -4 to -0.5 in 0.5 V
    steps also runs -3.5, -3.0 ..., which a plan of [-4, -0.5] does not allow."""
    env = Envelope.from_wire({**ENVELOPE, "modules": {"bace": {
        "vcoll": {"choices": [-4.0, -0.5]},
        "axis_name": {"choices": ["vcoll"]},
        "axis_start": {"min": -4, "max": 0}, "axis_stop": {"min": -4, "max": 0},
        "axis_step": {"min": 0, "max": 4}}}}, catalogue=CAT)

    def scan(start, stop, step):
        return bace(axis_name="vcoll", axis_start=start, axis_stop=stop, axis_step=step)

    assert env.violations(parsed(scan(-4.0, -0.5, 3.5))) == []
    [v] = env.violations(parsed(scan(-4.0, -0.5, 0.5)))
    assert "through 8 points" in v
    # a tiny step is refused from its point count, without building the scan
    [v] = env.violations(parsed(scan(-4.0, -0.5, 1e-10)))
    assert "through 35000000001 points" in v
    # as many points as choices: each point is checked
    three = Envelope.from_wire({**ENVELOPE, "modules": {"bace": {
        **env.modules["bace"], "vcoll": {"choices": [-4.0, -2.0, -0.5]}}}}, catalogue=CAT)
    [v] = three.violations(parsed(scan(-4.0, -0.5, 1.75)))
    assert "through -2.25" in v
    assert env.violations(parsed(scan(-0.5, -0.5, 0.0))) == []


# -- the book ----------------------------------------------------------------------
def test_a_plan_is_approved_by_its_fingerprint_and_then_spent():
    book = PlanBook("S")
    plan = book.propose(PLAN, catalogue=CAT, now=0.0)
    assert plan.plan_id == "S-P01" and plan.state == "proposed" and len(plan.fingerprint) == 16
    t = parsed(bace(vcoll=-1.0))
    assert book.check(plan.plan_id, t, 10.0, now=1.0) == ["plan S-P01 is proposed, not approved"]
    with pytest.raises(PlanConflict, match="approve the plan as shown"):
        book.decide(plan.plan_id, "approve", fingerprint_seen="0" * 16, now=1.0)
    book.decide(plan.plan_id, "approve", fingerprint_seen=plan.fingerprint, now=1.0)
    assert book.check(plan.plan_id, t, 10.0, now=2.0) == []
    assert book.check(plan.plan_id, t, 4000.0, now=2.0)[-1].endswith("of its 3600 s")
    for i in range(3):
        book.charge(plan.plan_id, f"r{i}", 10.0)
    assert "used its 3 run(s)" in book.check(plan.plan_id, t, 10.0, now=2.0)[0]
    assert book.check("S-P09", t, 1.0) == ["no plan S-P09 in this session"]


def test_an_approval_expires_and_a_closed_plan_accepts_nothing():
    book = PlanBook("S")
    plan = book.propose(PLAN, catalogue=CAT, now=0.0)
    book.decide(plan.plan_id, "approve", fingerprint_seen=plan.fingerprint, now=100.0)
    t = parsed(bace())
    assert book.check(plan.plan_id, t, 1.0, now=100.0 + 3599) == []
    assert "expired" in book.check(plan.plan_id, t, 1.0, now=100.0 + 3601)[0]
    book.decide(plan.plan_id, "close", reason="done")
    assert "is closed" in book.check(plan.plan_id, t, 1.0, now=101.0)[0]
    with pytest.raises(PlanConflict):
        book.decide(plan.plan_id, "approve", fingerprint_seen=plan.fingerprint)
    other = book.propose(PLAN, catalogue=CAT)
    book.decide(other.plan_id, "reject", reason="not today")
    assert other.state == "rejected" and other.reason == "not today"


# -- over HTTP ---------------------------------------------------------------------
fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bace.params import run_toml_layer  # noqa: E402
from bace.service.app import create_app  # noqa: E402
from bace.service.session import Session  # noqa: E402
from tests.test_service_api import FAST, RECIPE, wait_run  # noqa: E402


@pytest.fixture
def service(tmp_path):
    session = Session(RigConfig(), run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                      mode="sim", fast=True, seed=5, session_id="20261028_210000",
                      sample={"sample": "s4", "material": "SIM", "pixel": "a"})
    with TestClient(create_app(session)) as client:
        yield client, session


def run_body(plan, **params):
    return {"module": "bace", "params": {**FAST, "n_loops": 1, "voc": 0.9, **params},
            "plan": plan}


def test_the_gate_end_to_end(service):
    client, session = service
    env = json.loads(json.dumps(ENVELOPE))
    env["modules"]["bace"].update({k: {"choices": [v]} for k, v in FAST.items()})
    env["modules"]["bace"]["voc"] = {"min": 0.0, "max": 2.0}
    r = client.post("/plans", json={**PLAN, "envelope": env})
    assert r.status_code == 201
    plan = r.json()
    pid, fp = plan["plan_id"], plan["fingerprint"]
    assert plan["state"] == "proposed" and plan["open"] is False

    r = client.post("/runs", json=run_body(pid, vcoll=-1.0))
    assert r.status_code == 422
    [c] = [c for c in r.json()["checks"] if c["code"] == "plan.envelope"]
    assert "not approved" in c["text"]

    assert client.post(f"/plans/{pid}/approve", json={"fingerprint": "x"}).status_code == 409
    r = client.post(f"/plans/{pid}/approve", json={"fingerprint": fp})
    assert r.status_code == 200 and r.json()["state"] == "approved"

    r = client.post("/runs", json=run_body(pid, vcoll=-1.0))
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    wait_run(session, run_id)
    assert client.get(f"/runs/{run_id}").json()["plan"] == pid

    r = client.post("/runs", json=run_body(pid, vcoll=-9.0))
    assert r.status_code == 422
    [c] = [c for c in r.json()["checks"] if c["code"] == "plan.envelope"]
    assert "vcoll = -9.0" in c["text"] and c["data"]["plan"] == pid

    r = client.post("/runs", json={"module": "bace", "params": {**FAST, "n_loops": 1,
                                                                "voc": 0.9, "vcoll": -9.0}})
    assert r.status_code == 202, "a run a person starts is not bound by any plan"
    wait_run(session, r.json()["run_id"])

    [p] = client.get("/plans").json()["plans"]
    assert p["runs"] == [run_id] and p["spent_s"] > 0
    assert client.post(f"/plans/{pid}/close", json={"reason": "enough"}).json()["state"] == "closed"
    assert client.get("/plans/nope").status_code == 404

    with open(session.journal.path, encoding="utf-8") as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    types = [(line["type"], line["data"].get("decision")) for line in lines
             if line["type"] in ("PlanProposed", "PlanDecided")]
    assert types == [("PlanProposed", None), ("PlanDecided", "approve"),
                     ("PlanDecided", "close")]
    decided = [line for line in lines if line["type"] == "PlanDecided"]
    assert decided[0]["data"]["fingerprint"] == fp
    queued = [line for line in lines if line["type"] == "RunQueued"]
    assert queued[0]["data"]["plan"] == pid and queued[1]["data"]["plan"] is None
    # After a restart the record is rebuilt from the journal, plan included.
    assert session.journal.run_record(run_id)["plan"] == pid


def test_a_proposal_the_service_cannot_accept_is_a_422(service):
    client, _ = service
    r = client.post("/plans", json={**PLAN, "envelope": {"modules": {"bake": {}}}})
    assert r.status_code == 422 and "no module 'bake'" in r.json()["error"]


def test_auto_approve_opens_a_plan_as_it_is_proposed_and_keeps_the_envelope(tmp_path):
    session = Session(RigConfig(plans_auto_approve=True), run_toml_layer(RECIPE),
                      out=str(tmp_path / "runs"), mode="sim", fast=True, seed=5,
                      session_id="20261031_100000",
                      sample={"sample": "s4", "material": "SIM", "pixel": "a"})
    with TestClient(create_app(session)) as client:
        env = json.loads(json.dumps(ENVELOPE))
        env["modules"]["bace"].update({k: {"choices": [v]} for k, v in FAST.items()})
        env["modules"]["bace"]["voc"] = {"min": 0.0, "max": 2.0}
        plan = client.post("/plans", json={**PLAN, "envelope": env}).json()
        assert plan["state"] == "approved" and plan["open"] and plan["expires_at"]
        assert plan["reason"] == AUTO_APPROVED

        r = client.post("/runs", json=run_body(plan["plan_id"], vcoll=-1.0))
        assert r.status_code == 202, r.text
        wait_run(session, r.json()["run_id"])

        r = client.post("/runs", json=run_body(plan["plan_id"], vcoll=-9.0))
        assert r.status_code == 422 and "plan.envelope" in r.text

        client.post(f"/plans/{plan['plan_id']}/close", json={"reason": "enough"})
        assert client.get(f"/plans/{plan['plan_id']}").json()["open"] is False

    with open(session.journal.path, encoding="utf-8") as fh:
        decided = [json.loads(line) for line in fh if '"PlanDecided"' in line]
    assert decided[0]["data"]["decision"] == "approve"
    assert decided[0]["data"]["reason"] == AUTO_APPROVED
    assert decided[0]["data"]["fingerprint"] == plan["fingerprint"]
