"""The routes: `create_app(session) -> FastAPI`, HTTP and one WebSocket over a `Session`.

This is the only module in `bace.service` besides `__main__` that imports
FastAPI, and it decides nothing. Every route is a thin translation between
the wire and one `Session` method: the body becomes the call's arguments,
the session's answer is the response, and the session's exceptions become
status codes -- `SubmitRefused` is a 422 that carries every check, `Busy`
and the other `Conflict`s are 409s, an unknown run, module or action is a
404, a parameter the catalogue refuses is a 422 that names the parameter.
The mapping lives in one table (`_HANDLERS`) so a new session error cannot
arrive at a client as a 500 with a traceback for a body.

**Threads.** Everything the session does on the event path assumes it runs
on the asyncio loop's thread once a loop is attached: `submit` journals and
fans out synchronously, and an `asyncio.Queue` may only be fed from its own
loop's thread. So every route here is `async def` -- a plain `def` route
would be run by Starlette on a threadpool thread, and the first frame put
on a subscriber's queue from there would wake nobody. The two calls that
block on the worker (`bench_read`, `bench_action`) are the exception, and
they are sent to the threadpool explicitly; what they do off the loop is
submit a job and wait for it, and the worker's callbacks come back to the
loop with `call_soon_threadsafe` as they do for a run.

**The WebSocket** sends the `Hello` frame, replays the ring from `since`
when asked, then streams what the session fans out. The subscription is
taken *before* the replay is computed and every frame carries `seq`, so a
frame that arrives during the replay is sent once, in order, and a client
that reconnects with the last `seq` it saw misses nothing the ring still
holds. A client the session drops for falling behind receives the `Notice`
and is closed.

**Lifespan.** Startup attaches the loop, starts the worker and runs one
read-back so `/bench` has a state to show before anyone clicks (contract
section 9); shutdown drains the worker on the threadpool -- the loop must
keep running while the last `parked` frames come back -- and then closes
the session, which parks the bench and closes the journal.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.routing import Mount

from ..params import ParamError
from .modules import ModuleError, jsonable
from .pipeline import TreeError
from .rigs import ACTIONS, BenchActionRefused
from .session import (Busy, Conflict, DataUnavailable, NodeRequired, NotPaused, Session,
                      SubmitRefused, UnknownRun, tree_for_module)
from .worker import StopMode

SHUTDOWN_TIMEOUT_S = 5.0
"""How long shutdown waits for the worker to abort the current job and end.
A job inside a VISA call cannot be interrupted; after this the bench is
parked and the journal closed regardless, and the worker thread is a daemon."""

SHUTDOWN_DRAIN_S = 0.05
"""A pause between the worker ending and the journal closing, so the frames
the worker posted to the loop in its last moments (`aborted`, `parked`) are
written before the file is closed rather than landing in `session.errors`."""

_RECIPE_STEM = re.compile(r"[^A-Za-z0-9_.-]+")

log = logging.getLogger("bace.service")


# -- request bodies -------------------------------------------------------------
class RunRequest(BaseModel):
    """`POST /runs`: one module with overrides on its resolved parameters."""

    model_config = ConfigDict(extra="forbid")
    module: str
    params: dict[str, Any] = Field(default_factory=dict)
    name: str = ""


class StopRequest(BaseModel):
    """`POST /runs/{id}/stop`: the two honest verbs and nothing else."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["after_shot", "abort"] = StopMode.AFTER_SHOT


class ResumeRequest(BaseModel):
    """`POST /runs/{id}/resume`: what the operator did. `temperature_k`
    becomes the subtree's temperature; both are journaled as the answer."""

    model_config = ConfigDict(extra="forbid")
    note: str = ""
    temperature_k: float | None = None


class TreeRequest(BaseModel):
    """`POST /pipelines`, `/pipelines/validate`, `/pipelines/save`."""

    model_config = ConfigDict(extra="forbid")
    tree: dict[str, Any]
    name: str = ""


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_s: float = 1.0


# -- errors to status codes --------------------------------------------------------
def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _param_of(message: str, module: str | None = None) -> str | None:
    """The parameter a `ModuleError`/`ParamError` message names: the first
    token before a colon, after the module's own prefix. Both classes start
    their messages that way so the console can attach them to a field."""
    rest = message
    if module and rest.startswith(module + ": "):
        rest = rest[len(module) + 2:]
    head = rest.split(":", 1)[0].strip()
    return head if head and " " not in head and head != rest.strip() else None


async def _submit_refused(request: Request, exc: SubmitRefused) -> JSONResponse:
    w = exc.validation.as_wire()
    return _error(422, str(exc), valid=False, checks=w["checks"], counters=w["counters"],
                  cost=w["cost"], node_paths=w["node_paths"])


async def _conflict(request: Request, exc: Conflict) -> JSONResponse:
    return _error(409, str(exc), kind=type(exc).__name__)


async def _unknown_run(request: Request, exc: UnknownRun) -> JSONResponse:
    run_id = exc.args[0] if exc.args else ""
    return _error(404, f"no such run: {run_id}", run_id=run_id)


async def _data_unavailable(request: Request, exc: DataUnavailable) -> JSONResponse:
    return _error(404, str(exc), run_id=exc.run_id, reason=exc.reason, folders=exc.folders)


async def _node_required(request: Request, exc: NodeRequired) -> JSONResponse:
    return _error(400, str(exc), nodes=exc.nodes)


async def _action_refused(request: Request, exc: BenchActionRefused) -> JSONResponse:
    return _error(409, exc.text, level=exc.level, refused=True)


async def _module_error(request: Request, exc: ModuleError) -> JSONResponse:
    message = str(exc)
    module = message.split(":", 1)[0] if ":" in message else None
    return _error(422, message, module=module, param=_param_of(message, module))


async def _param_error(request: Request, exc: ParamError) -> JSONResponse:
    message = str(exc)
    return _error(422, message, param=_param_of(message))


async def _tree_error(request: Request, exc: TreeError) -> JSONResponse:
    return _error(422, str(exc), node_path=exc.node_path)


async def _value_error(request: Request, exc: ValueError) -> JSONResponse:
    return _error(422, str(exc))


async def _timeout(request: Request, exc: TimeoutError) -> JSONResponse:
    return _error(504, str(exc))


async def _runtime_error(request: Request, exc: RuntimeError) -> JSONResponse:
    # A handled exception is not logged by Starlette, and a 500 whose only
    # trace is one line in a browser is a bug nobody can find later.
    log.exception("%s %s failed", request.method, request.url.path)
    return _error(500, f"{type(exc).__name__}: {exc}")


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    first = errors[0] if errors else {}
    loc = [str(x) for x in first.get("loc", ()) if x != "body"]
    param = loc[-1] if loc else None
    where = ".".join(loc) + ": " if loc else ""
    return _error(422, f"{where}{first.get('msg', 'invalid request')}", param=param,
                  detail=jsonable([{k: v for k, v in e.items() if k != "ctx"}
                                   for e in errors]))


_HANDLERS: tuple[tuple[type[Exception], Any], ...] = (
    (SubmitRefused, _submit_refused),
    (Busy, _conflict),
    (NotPaused, _conflict),
    (Conflict, _conflict),
    (UnknownRun, _unknown_run),
    (DataUnavailable, _data_unavailable),
    (NodeRequired, _node_required),
    (BenchActionRefused, _action_refused),
    (ModuleError, _module_error),
    (ParamError, _param_error),
    (TreeError, _tree_error),
    (ValueError, _value_error),
    (TimeoutError, _timeout),
    (RuntimeError, _runtime_error),
    (RequestValidationError, _validation_error),
)
"""Session exception -> handler. Starlette walks the exception's MRO, so the
most specific class registered wins: `Busy` before `Conflict` before
`RuntimeError`, `ModuleError` before `ValueError`."""


# -- frames ------------------------------------------------------------------------
def _dumps(frame: Any) -> str:
    """One frame as JSON text. Frames come from the wire policy with NaN
    already turned into null; if one ever did not, `jsonable` does it here
    rather than a bare `NaN` token closing the client's parser."""
    try:
        return json.dumps(frame, allow_nan=False, separators=(",", ":"))
    except ValueError:
        return json.dumps(jsonable(frame), allow_nan=False, separators=(",", ":"))


def _hello(session: Session) -> dict:
    """The first WebSocket frame, in envelope shape so a client parses it
    like any other: `data` is `{session, seq, bench}`.

    The envelope `seq` is null, not the last seq: the replay that follows
    carries frames with lower seqs, and a Hello stamped with the newest seq
    would make a client that dedupes on `seq > last seen` drop the whole
    replay. The last seq the client has is `data.seq`, where the contract
    puts it."""
    data = session.hello()
    return {"seq": None, "ts": time.time(), "run_id": None, "node_path": "",
            "type": "Hello", "data": data, "decimated": {}}


def _stem(name: str) -> str:
    return _RECIPE_STEM.sub("_", name.strip()).strip("_")


# -- the app ---------------------------------------------------------------------
def create_app(session: Session, *, ui_dir: str | None = None,
               read_back_at_start: bool = True) -> FastAPI:
    """Every route of contract sections 4-8 over `session`.

    `ui_dir` mounts a static front end at `/ui` and makes `/` redirect to
    it; without it `/` is a JSON index of the routes. `read_back_at_start`
    runs one read-back job in the startup hook so the bench card is not
    blank until someone clicks; a test that wants the untouched stream can
    turn it off.
    """
    if ui_dir is not None and not os.path.isdir(ui_dir):
        raise ValueError(f"--ui: {ui_dir!r} is not a directory")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        session.start(asyncio.get_running_loop())
        if read_back_at_start:
            try:
                await run_in_threadpool(session.bench_read)
            except Exception as exc:                        # noqa: BLE001 -- reported, not fatal
                session.errors.append(f"read-back at start: {type(exc).__name__}: {exc}")
        try:
            yield
        finally:
            await run_in_threadpool(session.worker.shutdown, SHUTDOWN_TIMEOUT_S)
            await asyncio.sleep(SHUTDOWN_DRAIN_S)
            await run_in_threadpool(session.close, SHUTDOWN_TIMEOUT_S)

    app = FastAPI(title="bace service", version="1", lifespan=lifespan,
                  docs_url="/docs", redoc_url=None)
    for exc_type, handler in _HANDLERS:
        app.add_exception_handler(exc_type, handler)

    # -- index -------------------------------------------------------------
    @app.get("/")
    async def index():
        """The routes, or a redirect to the mounted UI."""
        if ui_dir is not None:
            return RedirectResponse("/ui/")
        return {"service": "bace", "session": session.session_info(),
                "routes": _route_index(app)}

    @app.get("/session")
    async def session_info():
        """Who this process is: id, mode, sample, paths, fingerprint."""
        return session.session_info()

    # -- /bench --------------------------------------------------------------
    @app.get("/bench")
    async def bench():
        """The cached read-back, the run in progress and the queue; touches no instrument."""
        return session.bench_snapshot()

    @app.post("/bench/read", status_code=202)
    async def bench_read(wait: bool = Query(True)):
        """A read-back job; 409 while a run is active (its Start re-reads the chain)."""
        if not wait:
            return session.bench_read(wait=False)
        return await run_in_threadpool(session.bench_read)

    @app.post("/bench/actions/{name}", status_code=202)
    async def bench_action(name: str, args: dict[str, Any] | None = Body(default=None),
                           wait: bool = Query(True)):
        """One explicit bench action, journaled by hand and followed by a read-back."""
        if name not in ACTIONS:
            return _error(404, f"no such bench action: {name}", actions=list(ACTIONS))
        if not wait:
            return session.bench_action(name, args, wait=False)
        out = await run_in_threadpool(session.bench_action, name, args)
        # A park still queued behind a run inside a long VISA call answers
        # `pending: true` rather than a 504 that cancelled it.
        return {**out, "bench": session.bench_snapshot()}

    # -- /modules --------------------------------------------------------------
    @app.get("/modules")
    async def modules():
        """The catalogue with provenance, estimates, needs and each module's last run."""
        return session.modules_wire()

    @app.get("/modules/{name}")
    async def module(name: str):
        """One module entry."""
        try:
            return session.module_wire(name)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())

    @app.put("/modules/{name}/params")
    async def edit_params(name: str, values: dict[str, Any] = Body(...)):
        """Set the edited layer (coerced, all or nothing); a null value resets that parameter."""
        try:
            session.catalogue.edit(name, values)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())
        return session.module_wire(name)

    @app.post("/modules/{name}/params/reset")
    async def reset_params(name: str):
        """Drop the whole edited layer so the lower layers show again."""
        try:
            session.catalogue.reset(name)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())
        return session.module_wire(name)

    # -- /runs ---------------------------------------------------------------------
    @app.post("/runs", status_code=202)
    async def start_run(body: RunRequest):
        """Queue a manual run: one module node, validated like any pipeline."""
        tree = tree_for_module(body.module, body.params, body.name)
        run_id, v = session.submit(tree, name=body.name, kind="manual")
        rec = session.run_record(run_id)
        w = v.as_wire()
        return {"run_id": run_id, "state": rec["state"], "position": rec["position"],
                "checks": w["checks"], "cost": w["cost"]}

    @app.get("/runs")
    async def runs(which: str = Query("this", alias="session")):
        """The run index from the journal: this session, `all`, or a session id."""
        return session.runs_index(which)

    @app.get("/runs/{run_id}")
    async def run(run_id: str):
        """The full record: tree, schedule, params as executed, node outcomes, folders."""
        return session.run_record(run_id)

    @app.get("/runs/{run_id}/data")
    async def run_data(run_id: str, node: str | None = Query(None)):
        """Full-precision arrays of one node; 400 for a pipeline run without `node`."""
        def render() -> str:
            return json.dumps(jsonable(session.run_data(run_id, node)), allow_nan=False)
        return Response(content=await run_in_threadpool(render),
                        media_type="application/json")

    @app.post("/runs/{run_id}/stop", status_code=202)
    async def stop_run(run_id: str, body: StopRequest | None = Body(default=None)):
        """`after_shot` keeps the shot in flight, `abort` discards it; a queued run is cancelled."""
        mode = body.mode if body is not None else StopMode.AFTER_SHOT
        return session.stop(run_id, mode)

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, body: ResumeRequest | None = Body(default=None)):
        """Answer a `NeedsOperator`; 409 when the run is not paused."""
        detail = body.model_dump(exclude_unset=True) if body is not None else {}
        return session.resume(run_id, detail)

    # -- /events ---------------------------------------------------------------------
    @app.get("/events")
    async def events_since(since: int = Query(0)):
        """Frames with seq above `since` from the ring, for a client without a socket."""
        return {"seq": session.last_seq, "events": session.events_since(since)}

    @app.websocket("/events")
    async def events(ws: WebSocket, since: int | None = Query(default=None)):
        await ws.accept()
        queue = session.subscribe()
        last = -1
        try:
            await ws.send_text(_dumps(_hello(session)))
            if since is not None:
                for frame in session.events_since(since):
                    await ws.send_text(_dumps(frame))
                    last = frame["seq"]

            async def pump() -> None:
                nonlocal last
                while True:
                    frame = await queue.get()
                    if frame is None:                       # dropped by the session
                        with contextlib.suppress(Exception):
                            await ws.close(code=1008, reason="fell behind; reconnect with since=")
                        return
                    seq = frame.get("seq")
                    if seq is None:
                        # A frame for this client only, not from the ring
                        # (the drop Notice): no seq to dedupe on, sent once.
                        pass
                    elif seq <= last:
                        continue
                    else:
                        last = seq
                    try:
                        await ws.send_text(_dumps(frame))
                    except Exception:                       # noqa: BLE001 -- the socket is gone
                        return

            sender = asyncio.create_task(pump())
            try:
                while True:
                    message = await ws.receive()
                    if message.get("type") == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            finally:
                sender.cancel()
                with contextlib.suppress(BaseException):
                    await sender
        finally:
            session.unsubscribe(queue)

    # -- /pipelines ---------------------------------------------------------------------
    @app.post("/pipelines/validate")
    async def validate_pipeline(body: TreeRequest):
        """The Dry run: every check, the schedule in order, the cost. Touches nothing."""
        v = session.validate_tree(body.tree, name=body.name)
        w = v.as_wire()
        schedule = w["schedule"] or {}
        return {**w, "schedule": schedule.get("steps", []), "tree": schedule.get("tree"),
                "folder": session.pipeline_folder(body.tree, body.name),
                "folder_pattern": session.pipeline_folder_pattern(body.tree, body.name)}

    @app.post("/pipelines", status_code=202)
    async def start_pipeline(body: TreeRequest):
        """Start: a fresh read-back when idle, validate, refuse with the checks or queue."""
        if session.run_active() is None:
            try:
                await run_in_threadpool(session.bench_read)
            except Busy:
                pass
        run_id, v = session.submit(body.tree, name=body.name)
        rec = session.run_record(run_id)
        w = v.as_wire()
        return {"run_id": run_id, "state": rec["state"], "position": rec["position"],
                "checks": w["checks"], "cost": w["cost"], "folder": rec["folder"]}

    @app.get("/pipelines/last")
    async def last_pipeline():
        """The last tree validated in this session, so the UI can reopen it."""
        if session.last_validated is None:
            return _error(404, "nothing has been validated in this session yet")
        return session.last_validated

    @app.post("/pipelines/save")
    async def save_pipeline(body: TreeRequest):
        """Write the tree to `<out>/recipes/<name>.json`."""
        stem = _stem(body.name or str(body.tree.get("name") or ""))
        if not stem:
            return _error(422, "name: a saved recipe needs a name", param="name")
        folder = os.path.join(session.out, "recipes")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{stem}.json")
        record = {"name": stem, "saved_at": time.time(), "tree": body.tree}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=1, allow_nan=False)
        return {"name": stem, "path": path}

    @app.get("/pipelines/saved")
    async def saved_pipelines():
        """Every recipe under `<out>/recipes`, newest first, trees included."""
        folder = os.path.join(session.out, "recipes")
        out = []
        if os.path.isdir(folder):
            for entry in os.scandir(folder):
                if not entry.name.endswith(".json"):
                    continue
                try:
                    with open(entry.path, encoding="utf-8") as fh:
                        record = json.load(fh)
                except (OSError, ValueError) as exc:
                    record = {"error": f"{type(exc).__name__}: {exc}"}
                out.append({"name": entry.name[:-5], "path": entry.path,
                            "saved_at": record.get("saved_at"), "tree": record.get("tree"),
                            **({"error": record["error"]} if "error" in record else {})})
        out.sort(key=lambda r: r.get("saved_at") or 0, reverse=True)
        return {"recipes": out}

    # -- /monitors -----------------------------------------------------------------------
    @app.post("/monitors/power", status_code=202)
    async def start_monitor(body: MonitorRequest | None = Body(default=None)):
        """Start the power monitor (HTTP observer; runs beside a scan). One at most."""
        return session.start_power_monitor(body.interval_s if body is not None else 1.0)

    @app.delete("/monitors/power")
    async def stop_monitor():
        """Stop the power monitor; 404 when none is running."""
        if not session.stop_power_monitor():
            return _error(404, "no power monitor is running")
        return {"stopped": True, "monitors": session.monitors()}

    @app.post("/monitors/temperature", status_code=202)
    async def start_temperature_monitor(body: MonitorRequest | None = Body(default=None)):
        """Start the temperature monitor (the 331 console over HTTP, beside a run). One at most."""
        return session.start_temperature_monitor(body.interval_s if body is not None else 5.0)

    @app.delete("/monitors/temperature")
    async def stop_temperature_monitor():
        """Stop the temperature monitor; 404 when none is running."""
        if not session.stop_temperature_monitor():
            return _error(404, "no temperature monitor is running")
        return {"stopped": True, "monitors": session.monitors()}

    @app.get("/monitors")
    async def monitors():
        """The observers running beside the bench."""
        return {"monitors": session.monitors()}

    if ui_dir is not None:
        app.mount("/ui", StaticFiles(directory=ui_dir, html=True), name="ui")
    return app


def _route_index(app: FastAPI) -> list[dict]:
    """`GET /` without a UI: every route with the first line of its docstring,
    read off the app so the index cannot list a route that is not there."""
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            doc = (r.endpoint.__doc__ or "").strip().splitlines()
            out.append({"method": ",".join(sorted(r.methods or ())), "path": r.path,
                        "what": doc[0] if doc else ""})
        elif isinstance(r, APIWebSocketRoute):
            out.append({"method": "WS", "path": r.path,
                        "what": "Hello, replay from ?since=<seq>, then the live stream"})
        elif isinstance(r, Mount):
            out.append({"method": "GET", "path": r.path + "/", "what": "the front end"})
    return out


__all__ = ["create_app", "RunRequest", "StopRequest", "ResumeRequest", "TreeRequest",
           "MonitorRequest"]
