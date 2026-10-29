"""The application exposes session operations through HTTP and WebSocket routes.

Only this module and `__main__` import FastAPI in `bace.service`. Routes
translate request bodies into `Session` calls and return their results.
`_HANDLERS` maps `SubmitRefused` to 422 with all checks, `Busy` and other
`Conflict` exceptions to 409, unknown runs, modules and actions to 404, and
refused parameters to 422 with the parameter name
(`docs/service-contract.md`, sections 4 and 6).

Routes use `async def` because session event handling belongs to the attached
asyncio loop's thread. A synchronous route runs in Starlette's thread pool;
feeding a subscriber's `asyncio.Queue` there can fail to wake the consumer.
Blocking `Session.bench_read` and `Session.bench_action` calls use the thread
pool explicitly. They submit worker jobs and wait; worker callbacks return
through `call_soon_threadsafe`.

WebSocket subscriptions precede replay. The socket sends `Hello`, replays
frames after `since` when requested, then streams session events. Sequence
numbers deduplicate frames queued during replay and preserve order. A
reconnecting client receives every frame still in the ring. A subscriber
that falls behind receives `Notice` before closure.

Startup attaches the loop, starts the worker and reads the bench. Shutdown
waits for the worker in the thread pool, keeping the loop available for
`parked` frames, then closes the session and journal
(`docs/service-contract.md`, section 9)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from typing import Any, Callable, Literal

from fastapi import Body, FastAPI, Header, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.routing import Mount

from ..params import ParamError
from .modules import SAMPLE_KEYS, ModuleError, jsonable
from . import assistant, mcp, pipeline, providers
from .plans import PlanConflict, PlanError
from .pipeline import TreeError
from .rigs import ACTIONS, BenchActionRefused
from .session import (Busy, Conflict, DataUnavailable, NodeRequired, NotPaused, Session,
                      SubmitRefused, UnknownRun, tree_for_module)
from .worker import StopMode

SHUTDOWN_TIMEOUT_S = 5.0
"""The shutdown timeout bounds the initial worker abort wait in seconds.

A blocking VISA call cannot be interrupted. Session closure then waits for
the worker before releasing bench resources. If it cannot finish, resources
remain open (`docs/service-contract.md`, section 9)."""

SHUTDOWN_DRAIN_S = 0.05
"""The shutdown drain lets final worker frames reach the journal.

This pause in seconds follows worker completion. It lets `aborted` and
`parked` callbacks run before journal closure instead of becoming
`session.errors`."""

_RECIPE_STEM = re.compile(r"[^A-Za-z0-9_.-]+")

log = logging.getLogger("bace.service")


# -- request bodies -------------------------------------------------------------
class RunRequest(BaseModel):
    """The run request selects one module with parameter overrides.

    The contract is `docs/service-contract.md`, section 6."""

    model_config = ConfigDict(extra="forbid")
    module: str
    params: dict[str, Any] = Field(default_factory=dict)
    name: str = ""
    plan: str | None = None


class StopRequest(BaseModel):
    """The stop request selects `after_shot` or `abort`.

    The modes are defined in `docs/service-contract.md`, section 2."""

    model_config = ConfigDict(extra="forbid")
    mode: Literal["after_shot", "abort"] = StopMode.AFTER_SHOT


class ShutdownRequest(BaseModel):
    """The shutdown request uses `force` to allow ending an active run.

    The contract is `docs/service-contract.md`, section 9."""

    model_config = ConfigDict(extra="forbid")
    force: bool = False


class ResumeRequest(BaseModel):
    """The resume request records the operator's answer to a pause.

    `temperature_k` becomes the subtree temperature in K. It and the note are
    journaled (`docs/service-contract.md`, section 6)."""

    model_config = ConfigDict(extra="forbid")
    note: str = ""
    temperature_k: float | None = None


class TreeRequest(BaseModel):
    """The tree request supplies a pipeline for execution, validation or saving.

    The contract is `docs/service-contract.md`, section 7."""

    model_config = ConfigDict(extra="forbid")
    tree: dict[str, Any]
    name: str = ""
    plan: str | None = None


class PlanRequest(BaseModel):
    """A proposed plan: title, goal and envelope (`docs/service-contract.md`, 8b)."""

    model_config = ConfigDict(extra="forbid")
    title: str
    goal: str
    envelope: dict[str, Any]


class DecisionRequest(BaseModel):
    """A decision on a plan. Approval names the fingerprint the person saw."""

    model_config = ConfigDict(extra="forbid")
    fingerprint: str = ""
    reason: str = ""


class BenchSaveRequest(BaseModel):
    """The bench-save request names the file for the whole bench.

    The contract is `docs/service-contract.md`, section 4."""

    model_config = ConfigDict(extra="forbid")
    name: str = ""


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_s: float = 1.0


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1)
    conversation: str | None = None
    provider: Literal["anthropic", "openai"] = "anthropic"
    model: str | None = None
    base_url: str | None = None


# -- errors to status codes --------------------------------------------------------
def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


class _ConsoleFiles(StaticFiles):
    """The console's static files, revalidated on every load.

    Without `no-cache` a browser may keep `style.css` and `app.js` from before an
    update and run them against the new service. The ETag keeps an unchanged
    file to a 304."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def _param_of(message: str, module: str | None = None) -> str | None:
    """The error parser extracts the parameter named by a catalogue refusal.

    `ModuleError` and `ParamError` put it before the first colon, after any module
    prefix, so the console can attach the error to a field."""
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
    # Starlette does not log handled exceptions. Keep the traceback so a
    # 500 response remains diagnosable beyond the browser's error line.
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
"""The handler table maps session exceptions to HTTP responses.

Starlette walks the exception's method resolution order. The most specific
registered class wins: `Busy` before `Conflict` before `RuntimeError`, and
`ModuleError` before `ValueError`."""


# -- frames ------------------------------------------------------------------------
def _dumps(frame: Any) -> str:
    """The frame encoder returns valid JSON without NaN tokens.

    The wire policy normally replaces NaN with null. `jsonable` provides a
    fallback so a nonfinite value cannot break the client's parser."""
    try:
        return json.dumps(frame, allow_nan=False, separators=(",", ":"))
    except ValueError:
        return json.dumps(jsonable(frame), allow_nan=False, separators=(",", ":"))


def _hello(session: Session) -> dict:
    """The first WebSocket frame carries `{session, seq, bench}` in `data`.

    The envelope's `seq` is null. Using the last sequence number there would
    make clients discard lower-numbered replay frames during deduplication.
    `data.seq` carries that number instead (`docs/service-contract.md`, section 6)."""
    data = session.hello()
    return {"seq": None, "ts": time.time(), "run_id": None, "node_path": "",
            "type": "Hello", "data": data, "decimated": {}}


def _bench_values(session: Session, names: list[str]) -> dict[str, dict[str, dict]]:
    """The bench snapshot records resolved values and layers for `names`.

    Its shape is `{module: {param: {value, source}}}`. These values underlie
    node overrides. Saving them records a recipe's parameters independently
    of the session lifetime."""
    out: dict[str, dict[str, dict]] = {}
    for module in names:
        resolved = session.catalogue.param_set(module).resolve()
        out[module] = {n: {"value": jsonable(pv.value), "source": pv.source.value}
                       for n, pv in resolved.items()}
    return out


def _bench_snapshot(session: Session, tree_obj: dict) -> dict[str, dict[str, dict]]:
    """The recipe snapshot includes every module named in a valid tree.

    An invalid tree yields an empty snapshot. Saving still succeeds; an empty
    `bench` block records the lack of resolved bench values."""
    try:
        root = pipeline.parse_tree(tree_obj)
    except Exception:
        return {}
    names: list[str] = []
    for node, _ancestors in pipeline.walk(root):
        module = getattr(node, "module", None)
        if module and module not in names and module in session.catalogue.names():
            names.append(module)
    return _bench_values(session, names)


def _saved_records(folder: str, keys: tuple[str, ...]) -> list[dict]:
    """The saved-record index lists JSON files with selected fields, latest first.

    `keys` selects fields beside each record's `name` and `path`. Unparseable
    files remain listed with `error`; silently omitting one would hide a saved
    file. Both save folders use this rule (`docs/service-contract.md`, section 4)."""
    out: list[dict] = []
    if not os.path.isdir(folder):
        return out
    for entry in os.scandir(folder):
        if not entry.name.endswith(".json"):
            continue
        try:
            with open(entry.path, encoding="utf-8") as fh:
                record = json.load(fh)
            if not isinstance(record, dict):
                raise ValueError(f"not an object: {type(record).__name__}")
        except (OSError, ValueError) as exc:
            record = {"error": f"{type(exc).__name__}: {exc}"}
        out.append({"name": entry.name[:-5], "path": entry.path,
                    "saved_at": record.get("saved_at"),
                    **{k: record.get(k) for k in keys},
                    **({"error": record["error"]} if "error" in record else {})})
    out.sort(key=lambda r: r.get("saved_at") or 0, reverse=True)
    return out


def _write_record(folder: str, stem: str, record: dict) -> str:
    """The record writer creates or overwrites `<folder>/<stem>.json`.

    The console arms a second click for overwrite; the route writes without
    another confirmation (`docs/service-contract.md`, section 4)."""
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{stem}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=1, allow_nan=False)
    return path


def _stem(name: str) -> str:
    return _RECIPE_STEM.sub("_", name.strip()).strip("_")


# -- the app ---------------------------------------------------------------------
def create_app(session: Session, *, ui_dir: str | None = None,
               read_back_at_start: bool = True,
               request_exit: Callable[[], None] | None = None) -> FastAPI:
    """The app factory exposes HTTP and WebSocket routes over `session`.

    `ui_dir` mounts the console at `/ui` and redirects `/` there. Without it,
    `/` returns a JSON route index. `read_back_at_start` runs a startup read-back
    so the bench card has state before an operator acts. Tests can disable it
    to inspect an untouched event stream. `request_exit` ends the hosting server.
    Without it, `/service/shutdown` is refused because the app cannot stop its
    host process (`docs/service-contract.md`, section 9)."""
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
        """The index returns route descriptions or redirects to the mounted console."""
        if ui_dir is not None:
            return RedirectResponse("/ui/")
        return {"service": "bace", "session": session.session_info(),
                "routes": _route_index(app)}

    @app.get("/session")
    async def session_info():
        """The session response identifies the process, sample, paths and fingerprint.

        The contract is `docs/service-contract.md`, section 3a."""
        return session.session_info()

    @app.put("/session/sample")
    async def set_sample(values: dict[str, Any] = Body(...)):
        """The sample edit merges `[sample]` and resets null keys to `run.toml`.

        It touches no instrument and takes no worker job, so it is allowed during
        a run. Only runs queued afterward use the edited identity. `run_active`
        lets the console explain that scope (`docs/service-contract.md`, section 3a)."""
        try:
            return session.set_sample(values)
        except ValueError as exc:
            return _error(422, str(exc), param="sample", accepted=sorted(SAMPLE_KEYS))

    @app.post("/service/shutdown", status_code=202)
    async def shutdown(body: ShutdownRequest | None = Body(default=None)):
        """The shutdown route ends the service, parks the bench and closes the journal.

        A run holding or waiting for the bench causes refusal unless `force` is
        set, protecting a run the client may not see. Exit follows the response.
        The server stops accepting requests and the lifespan parks the bench as on
        Ctrl-C (`docs/service-contract.md`, section 9)."""
        if request_exit is None:
            return _error(409, "this service was not started by `python -m bace.service`; "
                               "stop it where it was started")
        active = session.run_active()
        if active is not None and not (body and body.force):
            return _error(409, f"run {active} holds the bench; shutting down aborts it and "
                               "cancels the queue. Repeat with force to do so.", run=active)
        asyncio.get_running_loop().call_soon(request_exit)
        return {"shutting_down": True, "aborts": active}

    # -- /bench --------------------------------------------------------------
    @app.get("/bench")
    async def bench():
        """The bench route returns cached read-back, active run and queue without I/O.

        The contract is `docs/service-contract.md`, section 4."""
        return session.bench_snapshot()

    @app.post("/bench/read", status_code=202)
    async def bench_read(wait: bool = Query(True)):
        """The read-back route submits a job and returns 409 during an active run.

        Start reads the chain itself.

        The contract is `docs/service-contract.md`, section 4."""
        if not wait:
            return session.bench_read(wait=False)
        return await run_in_threadpool(session.bench_read)

    @app.post("/bench/actions/{name}", status_code=202)
    async def bench_action(name: str, args: dict[str, Any] | None = Body(default=None),
                           wait: bool = Query(True)):
        """The action route journals one explicit bench change and reads back the
        result.

        The contract is `docs/service-contract.md`, section 4."""
        if name not in ACTIONS:
            return _error(404, f"no such bench action: {name}", actions=list(ACTIONS))
        if not wait:
            return session.bench_action(name, args, wait=False)
        out = await run_in_threadpool(session.bench_action, name, args)
        # A park queued behind a blocking VISA call stays queued and reports
        # `pending: true`. Cancelling it with a 504 would leave the bench unsafe.
        return {**out, "bench": session.bench_snapshot()}

    @app.post("/bench/save")
    async def save_bench(body: BenchSaveRequest):
        """The bench-save route writes all settings to `<out>/bench/<name>.json`.

        The edited layer lives only in the session. Saving the whole catalogue
        preserves settings beyond the modules named in one recipe. The record maps
        `{module: {param: {value, source}}}`, preserving resolved values and layers.

        Restoring uses `PUT /modules/{m}/params` one module at a time. A refused value
        gets the same field error as an edit, while other modules can accept their
        values. There is no load route with a catalogue-wide all-or-nothing rule:
        such a refusal would hide which value prevents the whole-bench restore
        (`docs/service-contract.md`, section 4)."""
        stem = _stem(body.name)
        if not stem:
            return _error(422, "name: saved bench settings need a name", param="name")
        record = {"name": stem, "saved_at": time.time(),
                  "bench": _bench_values(session, session.catalogue.names())}
        path = _write_record(os.path.join(session.out, "bench"), stem, record)
        return {"name": stem, "path": path}

    @app.get("/bench/saved")
    async def saved_bench():
        """The saved-bench index returns values from `<out>/bench`, latest first.

        Its key is `benches`, matching the tab's noun, as `recipes` does for
        `/pipelines/saved` (`docs/service-contract.md`, section 4)."""
        return {"benches": _saved_records(os.path.join(session.out, "bench"), ("bench",))}

    @app.delete("/bench/saved/{name}")
    async def delete_bench(name: str):
        """The delete route removes one saved bench and returns 404 if it is absent.

        Only saved benches can be removed from disk through the console. Recipes
        have no delete route; run folders are experiment records. The console arms
        a second click, as for overwrite (`docs/service-contract.md`, section 4)."""
        stem = _stem(name)
        path = os.path.join(session.out, "bench", f"{stem}.json")
        if not stem or not os.path.isfile(path):
            return _error(404, f"no saved bench called {stem or name!r}", name=stem)
        os.remove(path)
        return {"name": stem, "deleted": True}

    # -- /modules --------------------------------------------------------------
    @app.get("/modules")
    async def modules():
        """The catalogue response includes layers, estimates, needs and last runs.

        The contract is `docs/service-contract.md`, section 5."""
        return session.modules_wire()

    @app.get("/modules/{name}")
    async def module(name: str):
        """The module route returns one catalogue entry.

        The contract is `docs/service-contract.md`, section 5."""
        try:
            return session.module_wire(name)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())

    @app.put("/modules/{name}/params")
    async def edit_params(name: str, values: dict[str, Any] = Body(...)):
        """The parameter edit coerces values into the edited layer atomically.

        Null resets a parameter to the layer below.

        The contract is `docs/service-contract.md`, section 5."""
        try:
            session.catalogue.edit(name, values)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())
        return session.module_wire(name)

    @app.post("/modules/{name}/params/reset")
    async def reset_params(name: str):
        """The parameter reset removes the edited layer to expose lower layers.

        The contract is `docs/service-contract.md`, section 5."""
        try:
            session.catalogue.reset(name)
        except KeyError:
            return _error(404, f"no such module: {name}", modules=session.catalogue.names())
        return session.module_wire(name)

    # -- /runs ---------------------------------------------------------------------
    @app.post("/runs", status_code=202)
    async def start_run(body: RunRequest):
        """The run route validates and queues one module as a pipeline node.

        The contract is `docs/service-contract.md`, section 6."""
        tree = tree_for_module(body.module, body.params, body.name)
        run_id, v = session.submit(tree, name=body.name, kind="manual", plan=body.plan)
        rec = session.run_record(run_id)
        w = v.as_wire()
        return {"run_id": run_id, "state": rec["state"], "position": rec["position"],
                "checks": w["checks"], "cost": w["cost"]}

    @app.get("/runs")
    async def runs(which: str = Query("this", alias="session")):
        """The run index reads the journal for this session, `all`, or a session ID.

        The contract is `docs/service-contract.md`, section 6."""
        return session.runs_index(which)

    @app.get("/runs/{run_id}")
    async def run(run_id: str):
        """The run record includes its tree, executed parameters, outcomes and folders.

        The contract is `docs/service-contract.md`, section 6."""
        return session.run_record(run_id)

    @app.get("/runs/{run_id}/data")
    async def run_data(run_id: str, node: str | None = Query(None)):
        """The data route returns full-precision arrays for one node.

        A pipeline request without `node` returns 400.

        The contract is `docs/service-contract.md`, section 6."""
        def render() -> str:
            return json.dumps(jsonable(session.run_data(run_id, node)), allow_nan=False)
        return Response(content=await run_in_threadpool(render),
                        media_type="application/json")

    @app.post("/runs/{run_id}/stop", status_code=202)
    async def stop_run(run_id: str, body: StopRequest | None = Body(default=None)):
        """The stop route keeps the shot with `after_shot` or discards it with `abort`.

        A queued run is cancelled.

        The contract is `docs/service-contract.md`, section 2."""
        mode = body.mode if body is not None else StopMode.AFTER_SHOT
        return session.stop(run_id, mode)

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, body: ResumeRequest | None = Body(default=None)):
        """The resume route answers `NeedsOperator` and returns 409 unless paused.

        The contract is `docs/service-contract.md`, section 6."""
        detail = body.model_dump(exclude_unset=True) if body is not None else {}
        return session.resume(run_id, detail)

    # -- /events ---------------------------------------------------------------------
    @app.get("/events")
    async def events_since(since: int = Query(0)):
        """The event route returns ring frames with `seq` above `since`.

        It supports clients without a socket (`docs/service-contract.md`, section 6)."""
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
                    await asyncio.sleep(0)                  # see `pump` below
                    last = frame["seq"]

            async def pump() -> None:
                nonlocal last
                while True:
                    frame = await queue.get()
                    # Yield before processing each queued frame, including duplicates.
                    # `Queue.get` can return without suspending for a nonempty queue;
                    # `send_text` can return when the transport accepts the bytes.
                    # A backlog then encodes roughly 20 kB frames without yielding and
                    # starves other requests. Measured on this bench with a 21 x 60
                    # fast scan and one subscriber, `GET /bench` latency is 6 ms without
                    # this backlog and a median 3.0 s with it. The console's bench state
                    # can become stale despite continuous polling.
                    #
                    # Yield before duplicate rejection: frames can queue while `Hello`
                    # is sent. Replay covers those sequence numbers, but a burst of
                    # duplicates must also give the event loop time.
                    await asyncio.sleep(0)
                    if frame is None:                       # dropped by the session
                        with contextlib.suppress(Exception):
                            await ws.close(code=1008, reason="fell behind; reconnect with since=")
                        return
                    seq = frame.get("seq")
                    if seq is None:
                        # A client-only drop `Notice` has no `seq` and is sent once.
                        # It does not participate in ring-frame deduplication.
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
        """The validation route returns checks, schedule and cost for Dry run.

        It touches no instrument (`docs/service-contract.md`, section 7)."""
        v = session.validate_tree(body.tree, name=body.name)
        w = v.as_wire()
        schedule = w["schedule"] or {}
        return {**w, "schedule": schedule.get("steps", []), "tree": schedule.get("tree"),
                "folder": session.pipeline_folder(body.tree, body.name),
                "folder_pattern": session.pipeline_folder_pattern(body.tree, body.name)}

    @app.post("/pipelines", status_code=202)
    async def start_pipeline(body: TreeRequest):
        """The pipeline start reads the idle bench, validates, then refuses or queues.

        A refusal includes the checks (`docs/service-contract.md`, section 7)."""
        if session.run_active() is None:
            try:
                await run_in_threadpool(session.bench_read)
            except Busy:
                pass
        run_id, v = session.submit(body.tree, name=body.name, plan=body.plan)
        rec = session.run_record(run_id)
        w = v.as_wire()
        return {"run_id": run_id, "state": rec["state"], "position": rec["position"],
                "checks": w["checks"], "cost": w["cost"], "folder": rec["folder"]}

    @app.get("/pipelines/last")
    async def last_pipeline():
        """The last-pipeline route returns the session's last validated tree for
        reopening.

        The route is defined in `docs/service-contract.md`, section 7."""
        if session.last_validated is None:
            return _error(404, "nothing has been validated in this session yet")
        return session.last_validated

    @app.post("/pipelines/save")
    async def save_pipeline(body: TreeRequest):
        """The recipe-save route writes the tree to `<out>/recipes/<name>.json`.

        The route is defined in `docs/service-contract.md`, section 7."""
        stem = _stem(body.name or str(body.tree.get("name") or ""))
        if not stem:
            return _error(422, "name: a saved recipe needs a name", param="name")
        # A tree stores only node overrides. Other values come from the
        # bench at run start, so a bench edit can change the same recipe's
        # behavior without changing its tree. Save resolved values and
        # layers for every module it names. The console compares this
        # snapshot with the bench when reopening the recipe.
        record = {"name": stem, "saved_at": time.time(), "tree": body.tree,
                  "bench": _bench_snapshot(session, body.tree)}
        path = _write_record(os.path.join(session.out, "recipes"), stem, record)
        return {"name": stem, "path": path}

    @app.get("/pipelines/saved")
    async def saved_pipelines():
        """The recipe index returns trees from `<out>/recipes`, latest first.

        A file without recorded bench values returns null for `bench`."""
        return {"recipes": _saved_records(os.path.join(session.out, "recipes"),
                                          ("tree", "bench"))}

    # -- /monitors -----------------------------------------------------------------------
    @app.post("/monitors/power", status_code=202)
    async def start_monitor(body: MonitorRequest | None = Body(default=None)):
        """The power-monitor route starts at most one observer beside a run.

        The contract is `docs/service-contract.md`, section 8."""
        return session.start_power_monitor(body.interval_s if body is not None else 1.0)

    @app.delete("/monitors/power")
    async def stop_monitor():
        """The power-monitor stop returns 404 when none is running.

        The contract is `docs/service-contract.md`, section 8."""
        if not session.stop_power_monitor():
            return _error(404, "no power monitor is running")
        return {"stopped": True, "monitors": session.monitors()}

    @app.get("/monitors/power/history")
    async def power_history(since: float | None = Query(None), limit: int | None = Query(None)):
        """The power-history route returns held readings, earliest first.

        Rows contain `[ts, watts, trustworthy]`.

        The contract is `docs/service-contract.md`, section 8."""
        return session.power_history(since=since, limit=limit)

    @app.get("/monitors/power/history.csv")
    async def power_history_csv():
        """The power CSV route downloads the held readings for a spreadsheet.

        The contract is `docs/service-contract.md`, section 8."""
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return Response(session.power_history_csv(), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="power_{session.session_id}_{stamp}.csv"'})

    @app.delete("/monitors/power/history")
    async def clear_power_history():
        """The power-history clear removes readings from memory and keeps the journal.

        The contract is `docs/service-contract.md`, section 8."""
        return {"cleared": session.clear_power_history()}

    @app.post("/monitors/temperature", status_code=202)
    async def start_temperature_monitor(body: MonitorRequest | None = Body(default=None)):
        """The temperature-monitor route starts or restarts one 331 observer.

        At most one runs. The session starts it on a bench with a 331, so the route
        also supports interval changes (`docs/service-contract.md`, section 8)."""
        if body is None:
            return session.start_temperature_monitor()
        return session.start_temperature_monitor(body.interval_s)

    @app.delete("/monitors/temperature")
    async def stop_temperature_monitor():
        """The temperature-monitor stop returns 404 when none is running.

        The contract is `docs/service-contract.md`, section 8."""
        if not session.stop_temperature_monitor():
            return _error(404, "no temperature monitor is running")
        return {"stopped": True, "monitors": session.monitors()}

    @app.get("/monitors")
    async def monitors():
        """The monitor index lists observers running beside the bench.

        The contract is `docs/service-contract.md`, section 8."""
        return {"monitors": session.monitors()}

    # -- /plans -------------------------------------------------------------------------
    @app.get("/plans")
    async def plans():
        """Every plan of the session, with its state and what it has spent."""
        return {"plans": session.plans_wire()}

    @app.post("/plans", status_code=201)
    async def propose_plan(body: PlanRequest):
        """Records a proposed plan; nothing runs under it until a person approves it."""
        try:
            return session.propose_plan(body.model_dump())
        except PlanError as exc:
            return _error(422, str(exc))

    @app.get("/plans/{plan_id}")
    async def plan(plan_id: str):
        """One plan."""
        for p in session.plans_wire():
            if p["plan_id"] == plan_id:
                return p
        return _error(404, f"no plan {plan_id}")

    async def _decide(plan_id: str, decision: str, body: DecisionRequest | None):
        body = body or DecisionRequest()
        try:
            return session.decide_plan(plan_id, decision, fingerprint=body.fingerprint,
                                       reason=body.reason)
        except KeyError:
            return _error(404, f"no plan {plan_id}")
        except PlanConflict as exc:
            return _error(409, str(exc))
        except PlanError as exc:
            return _error(422, str(exc))

    @app.post("/plans/{plan_id}/approve")
    async def approve_plan(plan_id: str, body: DecisionRequest | None = Body(default=None)):
        """A person's approval of the plan with the fingerprint shown. The console's
        plan card is the place for it; the MCP endpoint does not offer it."""
        return await _decide(plan_id, "approve", body)

    @app.post("/plans/{plan_id}/reject")
    async def reject_plan(plan_id: str, body: DecisionRequest | None = Body(default=None)):
        """Rejects a proposed plan."""
        return await _decide(plan_id, "reject", body)

    @app.post("/plans/{plan_id}/close")
    async def close_plan(plan_id: str, body: DecisionRequest | None = Body(default=None)):
        """Closes an approved plan: nothing more is accepted under it."""
        return await _decide(plan_id, "close", body)

    # -- /mcp ---------------------------------------------------------------------------
    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        """The Model Context Protocol for an external agent: one JSON-RPC message in,
        one JSON response out (`service.mcp`, docs/service-contract.md section 8c)."""
        origin = request.headers.get("origin")
        if origin and not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$", origin):
            # A page on another site must not drive the bench through the
            # browser of whoever is at the console.
            return _error(403, f"origin {origin} may not use /mcp")
        try:
            message = json.loads(await request.body())
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "not JSON"}})

        async def read_bench():
            try:
                await run_in_threadpool(session.bench_read)
            except Busy:
                pass

        answer = await mcp.handle(session, message, read_bench=read_bench)
        if answer is None:
            return Response(status_code=202)
        return JSONResponse(answer)

    @app.get("/mcp")
    async def mcp_stream():
        """No server-sent stream is offered; every answer comes with its request."""
        return Response(status_code=405, headers={"Allow": "POST"})

    # -- /assistant --------------------------------------------------------------------
    conversations = assistant.Conversations()

    @app.get("/assistant")
    async def assistant_status():
        """The assistant status reports availability, model and readable documents.

        The contract is `docs/service-contract.md`, section 8a."""
        return assistant.status()

    @app.post("/assistant/chat")
    async def assistant_chat(body: ChatRequest,
                             key: str | None = Header(default=None, alias="X-API-Key")):
        """The chat route streams one answer as NDJSON frames.

        The request supplies its own key, which is not kept.

        The contract is `docs/service-contract.md`, section 8a."""
        try:
            provider = providers.make(body.provider, key=key or "", model=body.model,
                                      base_url=body.base_url)
        except providers.NoKey as exc:
            return _error(401, str(exc), param="X-API-Key")
        except providers.BadSetting as exc:
            return _error(422, str(exc))
        except providers.Unavailable as exc:
            return _error(503, str(exc))
        frames = assistant.chat(session, conversations, provider, text=body.text,
                                conversation_id=body.conversation)
        try:
            first = await frames.__anext__()
        except assistant.Busy as exc:
            return _error(409, str(exc))

        async def lines():
            yield json.dumps(first) + "\n"
            async for frame in frames:
                yield json.dumps(jsonable(frame), ensure_ascii=False, default=str) + "\n"
        return StreamingResponse(lines(), media_type="application/x-ndjson")

    @app.delete("/assistant/conversations/{conversation_id}")
    async def assistant_forget(conversation_id: str):
        """The conversation delete forgets one history and returns 404 if it is absent.

        The contract is `docs/service-contract.md`, section 8a."""
        if not conversations.forget(conversation_id):
            return _error(404, f"no conversation {conversation_id}")
        return {"forgotten": conversation_id}

    if ui_dir is not None:
        app.mount("/ui", _ConsoleFiles(directory=ui_dir, html=True), name="ui")
    return app


def _route_index(app: FastAPI) -> list[dict]:
    """The route index lists endpoints and their docstring opening lines.

    It serves `GET /` without a UI. Reading the app's routes prevents entries
    for endpoints the app does not expose."""
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
           "MonitorRequest", "ChatRequest"]
