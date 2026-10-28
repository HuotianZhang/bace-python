"""The console's assistant: a conversation with a model that can read the bench.

It explains -- the physics, the chain, what a parameter does and why it has
the value it has -- and it reads what the service already shows: the session,
the read-back, the catalogue with each value's layer, the runs, the journal's
frames, a Dry run of a tree. In this version it changes nothing. Every tool is
a read, and the Dry run is `Session.validate_tree(remember=False)`, so it does
not even replace the tree the pipeline tab reopens.

**The model is the asker's choice, and so is the key.** Claude through the
Anthropic API, or any OpenAI-compatible endpoint (`providers.py`). The key
arrives on each request in `X-API-Key`, is used for that request's calls and
is never stored, journaled or logged here. The console keeps it in the browser
only if asked to.

**History lives here.** A conversation's messages are kept in memory under an
id the first answer carries, because an assistant turn has to be sent back
exactly as it came -- thinking blocks, tool calls and all -- and the browser
has no reason to hold those. A history is one vendor's and one model's: a
question asked with another starts a new conversation. A request either completes a turn or leaves the
history as it found it, so a failed or abandoned request can simply be asked
again.

**The wire** is one JSON object per line (`application/x-ndjson`), in order:
`conversation`, then any number of `text` and `tool`/`tool_result`, then
`done` or `error`.

`anthropic` and `openai` are in the `service` extra, but nothing else imports
them: without them the service runs as before and `GET /assistant` says what
is missing.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from . import providers
from .modules import jsonable

MAX_TOOL_ROUNDS = 16
"""A turn that has called tools this many times is stopped and says so; a read
loop that long is a model going round in circles, not a question answered."""

MAX_CONVERSATIONS = 32
TOOL_TEXT_LIMIT = 40000
"""Characters of one tool result. A run record or the catalogue is a few
thousand; the cut is for the one that is not, and it says it was cut."""
LIST_LIMIT = 64
"""A list longer than this inside a tool result is replaced by its length:
traces and loops are for charts, and a model reading 10000 samples learns
nothing a summary would not tell it."""

ROOT = Path(__file__).resolve().parents[2]

DOCS: dict[str, tuple[str, str]] = {
    "principle": ("docs/bace-principle.md", "what BACE measures, from the papers that introduced it, and which code does each step"),
    "readme": ("README.md", "install, run, the repository layout"),
    "notes": ("docs/notes.md", "the rig as measured, defects only the hardware found, overturned claims"),
    "bench-checkout": ("docs/bench-checkout.md", "bringing the real bench up in stages"),
    "integration-window": ("docs/integration-window.md", "where the charge integral starts and ends"),
    "scope-window": ("docs/analysis/2026-10-26/scope-window/README.md", "record length, baseline, window and settle times, measured"),
    "ui-rules": ("docs/ui-rules.md", "what each screen of the console says and how to read it"),
    "console": ("ui/README.md", "the console's parts, file by file"),
    "service-contract": ("docs/service-contract.md", "every endpoint, the event frames, the checks; long"),
    "changelog": ("CHANGELOG.md", "what changed, newest first"),
    "manual-1.1": ("docs/manual/1.1-overview.md", "manual: the measurement in one page"),
    "manual-1.2": ("docs/manual/1.2-bench.md", "manual: the instruments, signal paths and timing"),
    "manual-1.3": ("docs/manual/1.3-first-measurement.md", "manual: starting the service and a first measurement"),
    "manual-1.4": ("docs/manual/1.4-results.md", "manual: reading a result and its checks"),
    "manual-1.5": ("docs/manual/1.5-troubleshooting.md", "manual: symptoms, causes and remedies"),
    "manual-2.1": ("docs/manual/2.1-extraction.md", "manual: what the extracted charge is, and the dark reference"),
    "manual-2.2": ("docs/manual/2.2-transient.md", "manual: the transient, the timing and the integration window"),
    "manual-2.3": ("docs/manual/2.3-signal.md", "manual: the measurement chain and the uncertainty budget"),
    "manual-2.4": ("docs/manual/2.4-validity.md", "manual: tests for a complete Q and a bulk density"),
    "manual-3.1": ("docs/manual/3.1-architecture.md", "manual: the packages and one run end to end"),
    "manual-3.2": ("docs/manual/3.2-correspondence.md", "manual: each step from physics to SCPI to code"),
    "manual-3.3": ("docs/manual/3.3-data.md", "manual: the files the bench writes"),
    "manual-3.4": ("docs/manual/3.4-extending.md", "manual: adding a module, instrument, check or tool"),
    "manual-3.5": ("docs/manual/3.5-testing.md", "manual: the test suite and the bench check-out"),
    "manual-4.4": ("docs/manual/4.4-glossary.md", "manual: one name, symbol and unit per term"),
}

SYSTEM = """\
You are the assistant inside the BACE console, a web console for a laboratory \
bench that measures bias-assisted charge extraction (BACE) on thin-film solar \
cells. The people who ask are physicists and students who may never have been \
trained on this console. Help them understand the physics, the instrument \
chain, what each parameter means and why it has its value, and how to operate \
the console.

The bench: an Agilent 33220A drives the LED (the light); an Agilent 81150A \
switches the device bias between the prebias V_pre (normally V_oc) and the \
collection voltage, and triggers the shot; an Agilent/Keysight DSO9054H \
records the current across a 5.192 ohm sense resistor; a Keithley 2400 measures V_oc, J_sc and \
J-V; a Lake Shore 331 holds the cryostat temperature; a Newport 1918-C reads \
the light power; a shutter and a relay sit in the light and signal paths. Each \
shot takes a light trace and a dark reference trace; Q is the integral of \
light minus dark over the integration window.

The console has four tabs: bench (one card per module, every parameter with \
the layer its value came from), pipeline (a tree of modules and loops, with a \
Dry run), results (past runs), and rig (the chain and one shot drawn in time). \
A rail across the top shows the live read-back; a strip at the foot carries \
warnings and the fix for each.

Parameter values come in layers, later ones winning: default < run.toml < \
last used (from the journal) < edited on the bench. When someone asks why a \
value is what it is, read the module and name the layer.

How to work:
- Read before you answer about the bench: use the tools rather than \
assuming. Name what you read ("the read-back says ...", "run 20261027_080417-001 \
...").
- Read the manual first: Part I (`manual-1.x`) for operation, Part II \
(`manual-2.x`) for the physics and the error budget, Part III (`manual-3.x`) \
for the code, `manual-4.4` for terms. `principle` gives the literature; \
`ui-rules` and `console` the console's behaviour; `notes` the bench as \
measured. Name the chapter you read, and say when something is not in them.
- You cannot change anything in this version: no parameter edit, no run, no \
instrument action. When an action is needed, say exactly where to click and \
what to type, and what the bench will do.
- Never advise exceeding a limit in rig.toml, and say so when a request would.
- Answer in the language the person writes in. Be short; use a table when \
comparing values. Give units.
"""


def status() -> dict:
    """`GET /assistant`: which vendors can answer here, and the documents read."""
    vendors = providers.status()
    return {"available": any(v["available"] for v in vendors.values()), "providers": vendors,
            "model": providers.CLAUDE_MODEL, "docs": {k: v[1] for k, v in DOCS.items()}}


# -- tools ---------------------------------------------------------------------------
def _tool(name: str, description: str, properties: dict | None = None,
          required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties or {},
                             "required": list(required), "additionalProperties": False}}


TOOLS = [
    _tool("get_session", "Who this service is: session id, real or simulated bench, "
          "the sample block, where runs are written, rig.toml and run.toml paths."),
    _tool("get_bench", "The cached read-back of every instrument, the run in progress "
          "and the queue. Touches no instrument."),
    _tool("list_modules", "Every module on the bench with its parameters: value, the "
          "layer it came from, unit, limits, and a short doc for each."),
    _tool("get_module", "One module's full entry, including each parameter's long doc.",
          {"name": {"type": "string", "description": "module name, e.g. bace, jv, voc"}},
          ("name",)),
    _tool("list_runs", "The run index from the journal, a page at a time, newest first.",
          {"scope": {"type": "string", "enum": ["this", "all"],
                     "description": "this session (default) or every session"},
           "limit": {"type": "integer", "description": f"how many, default 20, at most {LIST_LIMIT}"},
           "offset": {"type": "integer", "description": "how many of the newest to skip, default 0"}}),
    _tool("get_run", "One run's record: tree, schedule, parameters as executed, node "
          "outcomes and verdicts, folders.",
          {"run_id": {"type": "string"}}, ("run_id",)),
    _tool("get_events", "Recent event frames from the service's ring (warnings, step "
          "results, bench changes), arrays replaced by their lengths.",
          {"limit": {"type": "integer", "description": "how many of the newest, default 30"}}),
    _tool("get_monitors", "The power and temperature monitors running beside the bench."),
    _tool("dry_run", "Validate a pipeline tree exactly as the pipeline tab's Dry run "
          "does: every check, the schedule, the cost. Touches nothing and is not "
          "remembered as the last validated tree.",
          {"tree": {"type": "object", "description": "the tree, as docs/service-contract.md "
                    "section 8 and the pipeline tab write it"},
           "name": {"type": "string"}}, ("tree",)),
    _tool("read_doc", "Read one of the project's documents. Long ones come in pages.",
          {"name": {"type": "string", "enum": [*DOCS, "run.toml", "rig.toml"]},
           "offset": {"type": "integer", "description": "character to start at, default 0"}},
          ("name",)),
]

_TYPES = {"string": str, "integer": int, "object": dict}


def check_input(tool: Mapping[str, Any], args: Any) -> str | None:
    """Why `args` does not fit the tool's schema, or None.

    The tools stream their input eagerly, so nothing upstream has validated
    it: a truncated or malformed input reaches here as whatever the SDK's
    tolerant parser made of it."""
    schema = tool["input_schema"]
    if not isinstance(args, dict):
        return "input is not an object"
    props = schema["properties"]
    for key in schema["required"]:
        if key not in args:
            return f"missing {key}"
    for key, value in args.items():
        if key not in props:
            return f"unknown field {key}"
        want = _TYPES[props[key]["type"]]
        if not isinstance(value, want) or (want is int and isinstance(value, bool)):
            return f"{key} should be {props[key]['type']}"
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"{key} should be one of {props[key]['enum']}"
    return None


def compact(obj: Any) -> Any:
    """`obj` with every list longer than `LIST_LIMIT` replaced by its length."""
    if isinstance(obj, dict):
        return {k: compact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        if len(obj) > LIST_LIMIT:
            return {"omitted_list_of": len(obj)}
        return [compact(v) for v in obj]
    return obj


def _render(obj: Any) -> str:
    text = json.dumps(compact(jsonable(obj)), default=str, ensure_ascii=False)
    if len(text) > TOOL_TEXT_LIMIT:
        text = text[:TOOL_TEXT_LIMIT] + f" ... [cut at {TOOL_TEXT_LIMIT} of {len(text)} characters]"
    return text


def read_doc(name: str, offset: int = 0, *, session=None) -> str:
    if name in ("run.toml", "rig.toml"):
        path = Path(getattr(session, "run_path" if name == "run.toml" else "rig_path", "") or ROOT / name)
    else:
        path = ROOT / DOCS[name][0]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"{name} could not be read here ({exc.strerror}): {path}"
    offset = max(0, offset)
    page = text[offset:offset + TOOL_TEXT_LIMIT]
    end = offset + len(page)
    more = f"\n[characters {offset}-{end} of {len(text)}; read on with offset={end}]" \
        if end < len(text) else ""
    return page + more


def run_tool(session, name: str, args: Mapping[str, Any]) -> str:
    """One tool call against `session`, on the event loop's thread."""
    if name == "get_session":
        return _render(session.session_info())
    if name == "get_bench":
        return _render(session.bench_snapshot())
    if name == "list_modules":
        return _render(session.modules_wire())
    if name == "get_module":
        return _render(session.module_wire(args["name"]))
    if name == "list_runs":
        # Paged before `compact` sees it: a journal of more than LIST_LIMIT
        # runs would otherwise come back as its length and nothing else.
        runs = session.runs_index(args.get("scope", "this"))
        limit = max(1, min(int(args.get("limit", 20)), LIST_LIMIT))
        offset = max(0, int(args.get("offset", 0)))
        page = list(reversed(runs))[offset:offset + limit]
        return _render({"total": len(runs), "offset": offset, "runs": page})
    if name == "get_run":
        return _render(session.run_record(args["run_id"]))
    if name == "get_events":
        limit = max(1, min(int(args.get("limit", 30)), 200))
        return _render(session.events_since(max(0, session.last_seq - limit)))
    if name == "get_monitors":
        return _render({"monitors": session.monitors()})
    if name == "dry_run":
        v = session.validate_tree(args["tree"], name=args.get("name", ""), remember=False)
        return _render(v.as_wire())
    if name == "read_doc":
        return read_doc(args["name"], args.get("offset", 0), session=session)
    raise KeyError(name)


# -- conversations -------------------------------------------------------------------
class Busy(RuntimeError):
    """A turn is already being answered in this conversation."""


class Conversations:
    """Message histories by id, the newest `MAX_CONVERSATIONS` of them, each
    under the provider signature it was written in."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[tuple, list]] = OrderedDict()
        self._busy: set[str] = set()

    def open(self, conversation_id: str | None, signature: tuple = ()) -> tuple[str, list]:
        """The history under `conversation_id`, or a new one when there is
        none or it was written by another vendor or model."""
        held = self._items.get(conversation_id) if conversation_id else None
        if held is not None and held[0] == signature:
            self._items.move_to_end(conversation_id)
            return conversation_id, held[1]
        cid = uuid.uuid4().hex[:12]
        self._items[cid] = (signature, [])
        while len(self._items) > MAX_CONVERSATIONS:
            old, _ = self._items.popitem(last=False)
            self._busy.discard(old)
        return cid, self._items[cid][1]

    def forget(self, conversation_id: str) -> bool:
        self._busy.discard(conversation_id)
        return self._items.pop(conversation_id, None) is not None

    def claim(self, conversation_id: str) -> None:
        if conversation_id in self._busy:
            raise Busy("this conversation is still answering the last question")
        self._busy.add(conversation_id)

    def release(self, conversation_id: str) -> None:
        self._busy.discard(conversation_id)


async def chat(session, conversations: Conversations, provider, *, text: str,
               conversation_id: str | None = None) -> AsyncIterator[dict]:
    """Answer one question with `provider` (`providers.make`), yielding the
    wire's frames.

    The history is extended only when the turn completes. `Busy` is raised
    before the first frame, so a route can turn it into a 409."""
    cid, messages = conversations.open(conversation_id, provider.signature)
    conversations.claim(cid)
    start = len(messages)
    done = False
    try:
        yield {"type": "conversation", "id": cid}
        turn = [provider.user(text)]
        usage = {"input": 0, "output": 0, "cache_read": 0}
        by_name = {t["name"]: t for t in TOOLS}
        rounds = 0
        while True:
            answer = None
            async for frame in provider.turn(messages + turn, SYSTEM, TOOLS):
                if frame["type"] == "turn":
                    answer = frame["turn"]
                else:
                    yield frame
            for k in usage:
                usage[k] += answer.usage.get(k, 0)
            if answer.stop == "max_tokens":
                # Before the text-only exit: a cut-off answer is not kept as if
                # it were whole, and a cut-off tool input is not run.
                yield {"type": "error", "message": "the answer ran out of room mid-way; ask a narrower question"}
                return
            turn.append(answer.message)
            if answer.stop == "refusal" or not answer.calls:
                break
            rounds += 1
            if rounds > MAX_TOOL_ROUNDS:
                yield {"type": "error", "message": f"stopped after {MAX_TOOL_ROUNDS} rounds of reading"}
                return
            results = []
            for call in answer.calls:
                tool = by_name.get(call.name)
                if call.input is None:
                    problem = "the arguments were not JSON"
                else:
                    problem = "no such tool" if tool is None else check_input(tool, call.input)
                yield {"type": "tool", "name": call.name, "input": call.input}
                if problem:
                    bad = call.raw or json.dumps(call.input)
                    results.append((call, json.dumps({"INVALID_JSON": bad, "problem": problem}), False))
                    yield {"type": "tool_result", "name": call.name, "ok": False, "text": problem}
                    continue
                try:
                    out, ok = run_tool(session, call.name, call.input), True
                except Exception as exc:                   # noqa: BLE001 -- told to the model
                    out, ok = f"{type(exc).__name__}: {exc}", False
                results.append((call, out, ok))
                yield {"type": "tool_result", "name": call.name, "ok": ok}
                await asyncio.sleep(0)
            turn.extend(provider.results(results))
        messages.extend(turn)
        done = True
        yield {"type": "done", "stop_reason": answer.stop, "usage": usage, "model": provider.model}
    except Exception as exc:                               # noqa: BLE001 -- a sentence, not a 500
        message, status_code = providers.error_text(provider.sdk, exc)
        yield {"type": "error", "message": message, "status": status_code}
    finally:
        if not done:
            del messages[start:]
        conversations.release(cid)


__all__ = ["Busy", "Conversations", "DOCS", "SYSTEM", "TOOLS",
           "chat", "check_input", "compact", "read_doc", "run_tool", "status"]
