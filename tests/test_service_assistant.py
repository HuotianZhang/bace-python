"""The console's assistant against a scripted model.

No request leaves the machine: `assistant.client` is replaced by a fake whose
`beta.messages.stream` plays back prepared turns and records what it was
sent. What is asserted is the loop around the model -- the tools run against
a real `--sim --fast` session, their results go back in the same request
shape, a failed turn leaves the history as it was -- and the wire the console
reads.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("anthropic", minversion="2.3")
from fastapi.testclient import TestClient  # noqa: E402

from bace.service import assistant  # noqa: E402
from bace.service.app import create_app  # noqa: E402

from test_service_api import make_session  # noqa: E402


# -- a scripted model -------------------------------------------------------------------
def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(name, args, tid="toolu_1"):
    return SimpleNamespace(type="tool_use", name=name, input=args, id=tid)


def turn(*blocks, stop=None):
    stop = stop or ("tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn")
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5,
                                                 cache_read_input_tokens=0))


class _Stream:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def events():
            for b in self.response.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)
        return events()

    async def get_final_message(self):
        return self.response


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})
        return _Stream(self.responses.pop(0))


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def use(*responses):
        holder["client"] = FakeClient(*responses)
        monkeypatch.setattr(assistant, "client", lambda key: holder["client"])
        return holder["client"]
    return use


@pytest.fixture
def service(tmp_path):
    session = make_session(tmp_path)
    with TestClient(create_app(session)) as client:
        yield client, session


def ask(client, text, conversation=None, key="sk-test"):
    body = {"text": text, **({"conversation": conversation} if conversation else {})}
    r = client.post("/assistant/chat", json=body, headers={"X-Anthropic-Key": key})
    assert r.status_code == 200, r.text
    return [json.loads(line) for line in r.text.splitlines() if line]


# -- the loop -----------------------------------------------------------------------------
def test_a_tool_call_reads_the_session_and_the_answer_streams(service, fake):
    client, session = service
    model = fake(turn(text("Reading the bench."), tool_use("get_bench", {})),
                 turn(text("The shutter is shut.")))
    frames = ask(client, "is the shutter open?")
    assert [f["type"] for f in frames] == ["conversation", "text", "tool", "tool_result",
                                           "text", "done"]
    assert frames[3] == {"type": "tool_result", "name": "get_bench", "ok": True}
    # The second request carries the first turn back as it came, then the result.
    second = model.sent[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    result = second[2]["content"][0]
    assert result["tool_use_id"] == "toolu_1" and "is_error" not in result
    assert "instruments" in json.loads(result["content"])
    # What every request is made with.
    sent = model.sent[0]
    assert sent["model"] == assistant.MODEL and sent["fallbacks"] == "default"
    assert all(t["eager_input_streaming"] for t in sent["tools"])
    assert "thinking" not in sent and "temperature" not in sent


def test_the_conversation_carries_on_under_its_id(service, fake):
    client, _ = service
    model = fake(turn(text("one")), turn(text("two")))
    cid = ask(client, "first")[0]["id"]
    assert ask(client, "second", conversation=cid)[0]["id"] == cid
    assert [m["role"] for m in model.sent[1]["messages"]] == ["user", "assistant", "user"]
    assert model.sent[1]["messages"][2]["content"] == "second"


def test_a_failed_turn_leaves_the_history_as_it_was(service, fake):
    client, _ = service
    fake(turn(text("one")), RuntimeError("the line dropped"))
    cid = ask(client, "first")[0]["id"]
    frames = ask(client, "second", conversation=cid)
    assert frames[-1]["type"] == "error" and "the line dropped" in frames[-1]["message"]
    model = fake(turn(text("three")))
    ask(client, "again", conversation=cid)
    assert [m["role"] for m in model.sent[0]["messages"]] == ["user", "assistant", "user"]


def test_an_input_that_does_not_fit_the_schema_is_not_run(service, fake):
    client, _ = service
    model = fake(turn(tool_use("get_run", {"run": "x"})), turn(text("sorry")))
    frames = ask(client, "the last run?")
    assert {"type": "tool_result", "name": "get_run", "ok": False,
            "text": "missing run_id"} in frames
    result = model.sent[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "INVALID_JSON" in result["content"]


def test_a_refusal_ends_the_turn_without_running_its_tools(service, fake):
    client, _ = service
    model = fake(turn(tool_use("get_bench", {}), stop="refusal"))
    frames = ask(client, "?")
    assert frames[-1]["type"] == "done" and frames[-1]["stop_reason"] == "refusal"
    assert not any(f["type"] == "tool" for f in frames) and len(model.sent) == 1


def test_a_text_answer_cut_off_at_max_tokens_is_an_error_and_not_kept(service, fake):
    client, _ = service
    fake(turn(text("The first half of"), stop="max_tokens"))
    frames = ask(client, "explain everything")
    assert frames[-1]["type"] == "error" and "ran out of room" in frames[-1]["message"]
    model = fake(turn(text("short")))
    ask(client, "again", conversation=frames[0]["id"])
    assert [m["role"] for m in model.sent[0]["messages"]] == ["user"]


def test_the_run_index_comes_in_pages_newest_first(service, monkeypatch):
    _, session = service
    runs = [{"run_id": f"r{i:03d}"} for i in range(100)]
    monkeypatch.setattr(session, "runs_index", lambda scope="this": runs)
    out = json.loads(assistant.run_tool(session, "list_runs", {}))
    assert out["total"] == 100 and len(out["runs"]) == 20
    assert out["runs"][0]["run_id"] == "r099"
    out = json.loads(assistant.run_tool(session, "list_runs", {"limit": 1000, "offset": 90}))
    assert [r["run_id"] for r in out["runs"]] == [f"r{i:03d}" for i in range(9, -1, -1)]


def test_the_dry_run_does_not_replace_the_tree_the_pipeline_tab_reopens(service):
    _, session = service
    tree = {"kind": "module", "module": "voc"}
    out = json.loads(assistant.run_tool(session, "dry_run", {"tree": tree}))
    assert "checks" in out and session.last_validated is None


# -- the parts ----------------------------------------------------------------------------
def test_long_lists_are_replaced_by_their_length():
    assert assistant.compact({"t": list(range(100)), "s": [1, 2]}) == \
        {"t": {"omitted_list_of": 100}, "s": [1, 2]}


def test_every_listed_document_is_there_and_long_ones_come_in_pages():
    for name in assistant.DOCS:
        assert "could not be read" not in assistant.read_doc(name)[:200], name
    page = assistant.read_doc("service-contract")
    assert "read on with offset=" in page


def test_the_schema_check():
    get_run = next(t for t in assistant.TOOLS if t["name"] == "get_run")
    list_runs = next(t for t in assistant.TOOLS if t["name"] == "list_runs")
    assert assistant.check_input(get_run, {"run_id": "a"}) is None
    assert assistant.check_input(get_run, {"run_id": 3}) == "run_id should be string"
    assert assistant.check_input(list_runs, {"scope": "mine"}).startswith("scope should be one of")
    assert assistant.check_input(get_run, "x") == "input is not an object"


# -- the routes ---------------------------------------------------------------------------
def test_status_and_the_key_is_required(service):
    client, _ = service
    assert client.get("/assistant").json()["available"] is True
    r = client.post("/assistant/chat", json={"text": "hi"})
    assert r.status_code == 401 and "API key" in r.json()["error"]


def test_forgetting_a_conversation(service, fake):
    client, _ = service
    fake(turn(text("one")))
    cid = ask(client, "first")[0]["id"]
    assert client.delete(f"/assistant/conversations/{cid}").json() == {"forgotten": cid}
    assert client.delete(f"/assistant/conversations/{cid}").status_code == 404
