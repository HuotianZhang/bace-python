"""The MCP endpoint: the bench to an external agent, within an approved plan.

`POST /mcp` speaks the Model Context Protocol's JSON-RPC over HTTP, answering
each request with one JSON response (the protocol's Streamable HTTP
transport, without a server-sent stream). It serves `initialize`, `ping`,
`tools/list` and `tools/call`. An agent such as Claude Code connects with

    claude mcp add --transport http bace http://127.0.0.1:8900/mcp

The tools are the console assistant's read-only tools (`service.assistant`),
plus the ones an agent needs to run an experiment: list, propose and close
plans, start a run or a pipeline under a plan, stop a run it started. There
is no tool that approves a plan: a person does that in the console
(`service.plans`), and every run an agent starts must name an approved plan
whose envelope it fits. Every check, interlock and park of an ordinary run
applies.

Standard library only, like the rest of the service core; `app.py` routes
the HTTP. The contract is `docs/service-contract.md`, section 8c.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

from . import assistant
from .modules import jsonable
from .plans import PlanConflict, PlanError
from .session import Conflict, SubmitRefused, UnknownRun, tree_for_module
from .worker import StopMode

PROTOCOL_VERSIONS: tuple[str, ...] = ("2025-06-18", "2025-03-26", "2024-11-05")
"""Protocol revisions this endpoint answers, newest first. A client asking for
another one is offered the newest."""

SERVER_INFO = {"name": "bace", "title": "BACE bench", "version": "1"}

INSTRUCTIONS = """\
This server is a laboratory bench that measures bias-assisted charge extraction \
(BACE) on thin-film solar cells. Read before acting: `read_doc` serves the bench \
manual (manual-1.1 to manual-4.4); `get_bench` and `list_modules` give the bench \
and every parameter with the layer its value comes from.

To run anything, propose a plan with `propose_plan`: a title, a goal, and an \
envelope listing the modules you will run, the parameters you will set with \
their bounds, the loops and their values, and caps on runs, time and the \
approval's lifetime. A person approves or rejects it in the console; you cannot. \
Check its state with `list_plans`. Once it is approved, `start_run` or \
`start_pipeline` with its id. A submission outside the envelope is refused with \
every reason; the ordinary checks still apply. Parameters you do not set keep the \
values the operator chose. Follow a run with `get_run` and `get_events`, stop one \
you started with `stop_run`, and close the plan with `close_plan` when done. \
Report what you measured with its uncertainty and the chapter that explains it."""


def _tool(name: str, description: str, properties: dict | None = None,
          required: tuple[str, ...] = ()) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties or {},
                             "required": list(required), "additionalProperties": False}}


ENVELOPE_DOC = ("The envelope: {\"modules\": {module: {parameter: bound}}, \"loops\": "
                "{loop: {value_or_parameter: bound}}, \"max_runs\", \"max_total_s\", "
                "\"expires_s\"}. A bound is {\"min\": a, \"max\": b} or {\"choices\": [...]}. "
                "Loop values are values_k (temperature), levels_v (illumination), count "
                "(repeat). See docs/service-contract.md section 8b.")

AGENT_TOOLS = [
    _tool("list_plans", "Every plan of the session with its state (proposed, approved, "
          "rejected, closed), whether it accepts runs now (open), its runs and time spent."),
    _tool("propose_plan", "Propose an experiment plan for a person to approve in the "
          "console. Nothing runs until it is approved. " + ENVELOPE_DOC,
          {"title": {"type": "string"}, "goal": {"type": "string",
                                                 "description": "what the plan measures and why"},
           "envelope": {"type": "object"}}, ("title", "goal", "envelope")),
    _tool("close_plan", "Close an approved plan you have finished with; nothing more is "
          "accepted under it.",
          {"plan_id": {"type": "string"}, "reason": {"type": "string"}}, ("plan_id",)),
    _tool("start_run", "Start one module under an approved plan. `params` are the "
          "parameters to set, each within the plan's bounds; the rest keep their values.",
          {"plan": {"type": "string"}, "module": {"type": "string"},
           "params": {"type": "object"}, "name": {"type": "string"}}, ("plan", "module")),
    _tool("start_pipeline", "Start a pipeline tree under an approved plan "
          "(docs/service-contract.md section 7 for the tree).",
          {"plan": {"type": "string"}, "tree": {"type": "object"},
           "name": {"type": "string"}}, ("plan", "tree")),
    _tool("stop_run", "Stop a run started under a plan: after_shot keeps the shot in "
          "progress, abort discards it. Either way the bench ends parked.",
          {"run_id": {"type": "string"},
           "mode": {"type": "string", "enum": ["after_shot", "abort"]}}, ("run_id",)),
]

TOOLS = assistant.TOOLS + AGENT_TOOLS
"""Every tool the endpoint offers. None approves a plan."""


def _text(obj: Any) -> str:
    return json.dumps(assistant.compact(jsonable(obj)), default=str,
                      ensure_ascii=False)


def _refusal(exc: SubmitRefused) -> str:
    w = exc.validation.as_wire()
    lines = [f"{c['level']} {c['code']}: {c['text']}" for c in w["checks"]
             if c["level"] in ("invalid", "crit")]
    return "refused:\n" + "\n".join(lines)


async def _call(session, name: str, args: Mapping[str, Any],
                read_bench: Callable[[], Awaitable[Any]]) -> tuple[str, bool]:
    """`(text, is_error)` for one tool call."""
    if name in {t["name"] for t in assistant.TOOLS}:
        return assistant.run_tool(session, name, args), False
    try:
        if name == "list_plans":
            return _text({"plans": session.plans_wire()}), False
        if name == "propose_plan":
            return _text(session.propose_plan(dict(args))), False
        if name == "close_plan":
            return _text(session.decide_plan(args["plan_id"], "close",
                                             reason=args.get("reason", ""))), False
        if name == "start_run":
            tree = tree_for_module(args["module"], dict(args.get("params") or {}),
                                   args.get("name", ""))
            run_id, v = session.submit(tree, name=args.get("name", ""), kind="manual",
                                       plan=args["plan"])
            return _text({"run_id": run_id, "cost": v.as_wire()["cost"],
                          "warnings": [c["text"] for c in v.as_wire()["checks"]
                                       if c["level"] == "warn"]}), False
        if name == "start_pipeline":
            if session.run_active() is None:
                await read_bench()
            run_id, v = session.submit(args["tree"], name=args.get("name", ""),
                                       plan=args["plan"])
            return _text({"run_id": run_id, "cost": v.as_wire()["cost"],
                          "warnings": [c["text"] for c in v.as_wire()["checks"]
                                       if c["level"] == "warn"]}), False
        if name == "stop_run":
            record = session.run_record(args["run_id"])
            if not record.get("plan"):
                return (f"{args['run_id']} was not started under a plan; only the "
                        "operator stops it"), True
            mode = args.get("mode", StopMode.AFTER_SHOT)
            return _text(session.stop(args["run_id"], mode)), False
    except SubmitRefused as exc:
        return _refusal(exc), True
    except (PlanError, PlanConflict, Conflict, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}", True
    except (KeyError, UnknownRun) as exc:
        return f"not found: {exc}", True
    return f"no tool {name}", True


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle(session, message: Any, *,
                 read_bench: Callable[[], Awaitable[Any]]) -> dict | None:
    """The JSON-RPC response to one message, or None for a notification.

    Runs on the event loop's thread, as every route does; `read_bench` is the
    app's way of reading the bench off it before a pipeline starts.
    """
    if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
        return _error(None, -32600, "a JSON-RPC 2.0 request object")
    method, msg_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if "id" not in message:
        return None
    if method == "initialize":
        asked = params.get("protocolVersion") if isinstance(params, Mapping) else None
        version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["input_schema"]} for t in TOOLS]}}
    if method == "tools/call":
        if not isinstance(params, Mapping):
            return _error(msg_id, -32602, "params: {name, arguments}")
        name, args = params.get("name"), params.get("arguments") or {}
        tool = next((t for t in TOOLS if t["name"] == name), None)
        if tool is None:
            return _error(msg_id, -32602, f"no tool {name!r}")
        problem = assistant.check_input(tool, args)
        if problem:
            text, is_error = problem, True
        else:
            text, is_error = await _call(session, name, args, read_bench)
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text}], "isError": bool(is_error)}}
    return _error(msg_id, -32601, f"method {method!r} is not served here")
