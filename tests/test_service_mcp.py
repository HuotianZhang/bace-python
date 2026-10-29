"""The MCP endpoint: an external agent reads the bench and runs inside an approved plan.

Each test speaks JSON-RPC to `POST /mcp` on a `--sim --fast` session, as an
MCP client would over the Streamable HTTP transport.
"""
from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from bace.experiment.rig import RigConfig  # noqa: E402
from bace.params import run_toml_layer  # noqa: E402
from bace.service import mcp  # noqa: E402
from bace.service.app import create_app  # noqa: E402
from bace.service.session import Session  # noqa: E402
from tests.test_service_api import FAST, RECIPE, wait_run  # noqa: E402


@pytest.fixture
def client(tmp_path):
    session = Session(RigConfig(), run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                      mode="sim", fast=True, seed=5, session_id="20261028_230000",
                      sample={"sample": "s4", "material": "SIM", "pixel": "a"})
    with TestClient(create_app(session)) as c:
        yield c, session


class Rpc:
    def __init__(self, client):
        self.client, self.n = client, 0

    def __call__(self, method, params=None, **headers):
        self.n += 1
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": self.n, "method": method,
                                           "params": params or {}}, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    def tool(self, name, **arguments):
        out = self("tools/call", {"name": name, "arguments": arguments})["result"]
        return out["content"][0]["text"], out["isError"]


def test_initialize_lists_tools_and_offers_no_approval(client):
    c, _ = client
    rpc = Rpc(c)
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "bace" and "approves" in init["instructions"]
    assert rpc("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] \
        == mcp.PROTOCOL_VERSIONS[0]
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    names = {t["name"] for t in rpc("tools/list")["result"]["tools"]}
    assert {"get_bench", "read_doc", "propose_plan", "start_run", "stop_run"} <= names
    assert not any("approve" in n for n in names)
    assert rpc("ping")["result"] == {}
    assert rpc("resources/list")["error"]["code"] == -32601


def test_a_run_needs_a_plan_a_person_approved_in_the_console(client):
    c, session = client
    rpc = Rpc(c)
    envelope = {"modules": {"bace": {**{k: {"choices": [v]} for k, v in FAST.items()},
                                     "n_loops": {"max": 2}, "voc": {"min": 0, "max": 2},
                                     "vcoll": {"min": -4, "max": -0.5}}},
                "max_runs": 2, "max_total_s": 3600, "expires_s": 3600}
    text, err = rpc.tool("propose_plan", title="V_coll", goal="Q against V_coll",
                         envelope=envelope)
    assert not err
    plan = json.loads(text)
    params = {**FAST, "n_loops": 1, "voc": 0.9, "vcoll": -1.0}

    text, err = rpc.tool("start_run", plan=plan["plan_id"], module="bace", params=params)
    assert err and "plan.envelope" in text and "not approved" in text

    # The approval is a person's, through the console's route.
    r = c.post(f"/plans/{plan['plan_id']}/approve", json={"fingerprint": plan["fingerprint"]})
    assert r.status_code == 200

    text, err = rpc.tool("start_run", plan=plan["plan_id"], module="bace",
                         params={**params, "vcoll": -9.0})
    assert err and "vcoll = -9.0" in text

    text, err = rpc.tool("start_run", plan=plan["plan_id"], module="bace", params=params)
    assert not err, text
    run_id = json.loads(text)["run_id"]
    wait_run(session, run_id)
    text, err = rpc.tool("get_run", run_id=run_id)
    assert not err and json.loads(text)["plan"] == plan["plan_id"]

    [p] = json.loads(rpc.tool("list_plans")[0])["plans"]
    assert p["runs"] == [run_id]
    text, err = rpc.tool("close_plan", plan_id=plan["plan_id"], reason="done")
    assert not err and json.loads(text)["state"] == "closed"


def test_an_agent_stops_only_its_own_runs(client):
    c, session = client
    rpc = Rpc(c)
    r = c.post("/runs", json={"module": "bace", "params": {**FAST, "n_loops": 1, "voc": 0.9}})
    run_id = r.json()["run_id"]
    text, err = rpc.tool("stop_run", run_id=run_id)
    assert err and "only the operator stops it" in text
    wait_run(session, run_id)
    assert rpc.tool("stop_run", run_id="nope")[1]


def test_malformed_calls_and_foreign_origins_are_refused(client):
    c, _ = client
    rpc = Rpc(c)
    text, err = rpc.tool("start_run", plan=3, module="bace")
    assert err and "plan" in text
    assert rpc("tools/call", {"name": "approve_plan", "arguments": {}})["error"]["code"] == -32602
    assert c.post("/mcp", content=b"{not json").json()["error"]["code"] == -32700
    assert c.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).json()["error"]["code"] == -32600
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               headers={"Origin": "http://127.0.0.1:8900"})
    assert r.status_code == 200
    assert c.get("/mcp").status_code == 405
