"""The console's assistant against a scripted model.

No request leaves the machine: `providers.make` hands the route a real
provider built on a fake client, which plays back prepared turns and records
what it was sent -- `beta.messages.stream` for Claude, `chat.completions` for
an OpenAI-compatible endpoint. What is asserted is the loop around the model -- the tools run against
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
pytest.importorskip("openai", minversion="3.40")
from fastapi.testclient import TestClient  # noqa: E402

from bace.service import assistant, providers  # noqa: E402
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
    def use(*responses):
        client = FakeClient(*responses)
        monkeypatch.setattr(providers, "make", lambda kind, **kw: providers.Anthropic(
            kw["key"], kw.get("model"), client=client))
        return client
    return use


@pytest.fixture
def service(tmp_path):
    session = make_session(tmp_path)
    with TestClient(create_app(session)) as client:
        yield client, session


def ask(client, text, conversation=None, key="sk-test", **settings):
    body = {"text": text, **({"conversation": conversation} if conversation else {}), **settings}
    r = client.post("/assistant/chat", json=body, headers={"X-API-Key": key})
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
    assert sent["model"] == providers.CLAUDE_MODEL and sent["fallbacks"] == "default"
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


def test_a_question_with_another_model_starts_a_new_conversation(service, fake):
    client, _ = service
    fake(turn(text("one")), turn(text("two")))
    cid = ask(client, "first")[0]["id"]
    assert ask(client, "second", conversation=cid, model="claude-haiku-5-5")[0]["id"] != cid


def test_the_settings_a_provider_cannot_use_are_refused(service):
    client, _ = service
    post = lambda body, key="k": client.post("/assistant/chat", json={"text": "hi", **body},  # noqa: E731
                                             headers={"X-API-Key": key} if key else {})
    r = post({"provider": "openai", "model": "m", "base_url": "http://api.example.com/v1"})
    assert r.status_code == 422 and "https://" in r.json()["error"]
    assert post({"provider": "openai", "base_url": "https://api.example.com/v1"}).status_code == 422
    r = post({"provider": "openai", "model": "m", "base_url": "https://api.example.com/v1"}, key="")
    assert r.status_code == 401
    # A model on this machine needs no key.
    assert providers.make("openai", key="", model="qwen", base_url="http://127.0.0.1:11434/v1")


# -- an OpenAI-compatible endpoint ---------------------------------------------------------
def chunk(content=None, calls=(), finish=None):
    delta = SimpleNamespace(content=content, tool_calls=[
        SimpleNamespace(index=i, id=cid, function=SimpleNamespace(name=name, arguments=args))
        for i, cid, name, args in calls] or None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None)


class FakeOpenAI:
    def __init__(self, *streams):
        self.streams = list(streams)
        self.sent = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.sent.append({**kw, "messages": list(kw["messages"])})
        chunks = self.streams.pop(0)

        async def stream():
            for c in chunks:
                yield c
        return stream()


@pytest.fixture
def fake_openai(monkeypatch):
    def use(*streams):
        client = FakeOpenAI(*streams)
        monkeypatch.setattr(providers, "make", lambda kind, **kw: providers.OpenAICompatible(
            kw["key"], kw["model"], kw["base_url"], client=client))
        return client
    return use


OPENAI = {"provider": "openai", "model": "deepseek-chat", "base_url": "https://api.example.com/v1"}


def test_an_openai_compatible_tool_call_arrives_in_pieces_and_goes_back_as_tool_messages(service, fake_openai):
    client, _ = service
    model = fake_openai(
        [chunk("Reading "), chunk(calls=[(0, "call_a", "get_mod", '{"na')]),
         chunk(calls=[(0, None, "ule", 'me": "bace"}')]), chunk(finish="tool_calls")],
        [chunk("200 averages."), chunk(finish="stop")])
    frames = ask(client, "why 200?", **OPENAI)
    assert {"type": "tool", "name": "get_module", "input": {"name": "bace"}} in frames
    assert frames[-1]["type"] == "done" and frames[-1]["model"] == "deepseek-chat"
    first, second = model.sent
    assert first["messages"][0]["role"] == "system" and first["model"] == "deepseek-chat"
    assert first["tools"][0]["type"] == "function" and "parameters" in first["tools"][0]["function"]
    roles = [m["role"] for m in second["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    said = second["messages"][2]
    assert said["content"] == "Reading " and said["tool_calls"][0]["function"] == \
        {"name": "get_module", "arguments": '{"name": "bace"}'}
    assert second["messages"][3]["tool_call_id"] == "call_a"
    assert "instruments" not in second["messages"][3]["content"] and '"bace"' in second["messages"][3]["content"]


def test_arguments_that_are_not_json_are_not_run(service, fake_openai):
    client, _ = service
    model = fake_openai([chunk(calls=[(0, "call_a", "get_run", '{"run_id": ')]), chunk(finish="tool_calls")],
                        [chunk("sorry"), chunk(finish="stop")])
    frames = ask(client, "?", **OPENAI)
    assert {"type": "tool_result", "name": "get_run", "ok": False,
            "text": "the arguments were not JSON"} in frames
    assert "INVALID_JSON" in model.sent[1]["messages"][3]["content"]


def test_a_length_stop_from_an_openai_compatible_endpoint_is_an_error(service, fake_openai):
    client, _ = service
    fake_openai([chunk("half an ans"), chunk(finish="length")])
    assert "ran out of room" in ask(client, "?", **OPENAI)[-1]["message"]
