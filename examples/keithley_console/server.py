"""The console's HTTP layer: `http.server` and nothing else.

Standard library on purpose. `bace.service` is FastAPI + uvicorn behind the
`service` extra, and a console that needed it would be one more reason not to
run on the machine in front of the instrument -- this one comes up on a bare
`pip install -e .`, and under `--sim` on a checkout with no VISA either.

Six routes, and the page. Every one of them answers the same object
(`KeithleyPanel.state`), because a panel is a *state* and a client that had to
merge four different answers into one screen would be four chances to draw a
bench that never existed:

    GET  /                  the page
    GET  /api/state         the panel, the last reading, the display's counters
    POST /api/source        set the source; body is any of `SOURCE_FIELDS`
    POST /api/output        {"on": true|false}
    POST /api/read          one reading now
    POST /api/poll          {"interval_s": 1.0}, or null to stop the display
    POST /api/errors        drain `:SYST:ERR?`

A refusal is a **409 with a sentence**: `{"error": …, "level": "warn"|"crit"}`,
which is the shape the rest of this project uses and the reason a client can
show what happened rather than "-> 409". A value the panel cannot read is a
422 naming the field; no instrument at all is a 503 carrying why.

`ThreadingHTTPServer`, so a browser's parallel requests do not queue behind
each other -- and every one of them reaches the instrument through the panel's
single worker thread, which is where the serialising belongs.
"""
from __future__ import annotations

import ipaddress
import json
import os
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from panel import (KeithleyPanel, PanelPending, PanelRefused, PanelUnavailable,
                    as_bool)

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

MAX_BODY = 64 * 1024
"""Every body here is a handful of numbers. A cap so a stray upload cannot
make the console read it into memory."""

CONTROL_CONTENT_TYPE = "application/json"
"""What a POST here must declare, and it is a **safety** check, not a parser's
convenience.

Binding to loopback does not keep a browser out. Any page the operator has
open can `fetch("http://127.0.0.1:8924/api/output", {method: "POST", mode:
"no-cors", body: '{"on": true}'})`, and the browser sends it -- to a fixed
port, on a machine whose console is a fixed program. The page cannot read the
answer, and does not need to: the *side effect* is a source driving a device.
That is cross-site request forgery against an instrument.

`no-cors` may only set a Content-Type from the "simple" set (`text/plain`,
`application/x-www-form-urlencoded`, `multipart/form-data`). Requiring
`application/json` therefore refuses it, and a cross-origin fetch that sets
the header properly triggers a CORS preflight this console does not answer.
`Origin` is checked as well (below), so neither defence stands alone."""


def is_loopback_origin(origin: str) -> bool:
    """Whether an `Origin` header is this machine talking to itself.

    `null` -- a sandboxed iframe or a `file://` page -- has no host and is not
    loopback, which is the answer that matters. `localhost` is allowed
    alongside the addresses because the operator may have opened the page by
    either name and the server binds nothing but loopback anyway."""
    try:
        host = urllib.parse.urlsplit(origin).hostname or ""
    except ValueError:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(panel: KeithleyPanel, *, host: str, port: int) -> ThreadingHTTPServer:
    """The server, not started. `server.server_port` is the port actually
    bound, which is what `port=0` is for in the tests."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "bace-keithley-console"
        protocol_version = "HTTP/1.1"

        # -- routing --------------------------------------------------------
        def do_GET(self) -> None:                            # noqa: N802
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                return self._page()
            if path == "/api/state":
                # Queued, not waited for: this GET answers with the flag as of
                # the last ask and the next one carries the new value. Without
                # it, a display switched off left the page drawing a source
                # that somebody had switched on at the instrument as off.
                panel.refresh_output_soon()
                return self._json(200, panel.state())
            return self._json(404, {"error": f"no such route: {path}"})

        def do_POST(self) -> None:                           # noqa: N802
            path = self.path.split("?")[0]
            refusal = self._cross_site()
            if refusal is not None:
                # 403 and no state: a page that is not allowed to ask is not
                # told what the bench is doing either.
                return self._json(403, {"error": refusal, "level": "crit"})
            try:
                body = self._body()
                if path == "/api/source":
                    return self._json(200, panel.set_source(body))
                if path == "/api/output":
                    if "on" not in body:
                        raise ValueError("on: the body needs {\"on\": true} or false")
                    # `as_bool`, never `bool()`. Python's truthiness makes the
                    # string "false" true, so `{"on": "false"}` -- a request
                    # that plainly means off -- would have **energised the
                    # source**. This is the one route on this console that
                    # puts current into somebody's device, so a value it does
                    # not recognise is a 422 and never a guess.
                    return self._json(200, panel.set_output(as_bool("on", body["on"])))
                if path == "/api/read":
                    return self._json(200, panel.read())
                if path == "/api/poll":
                    return self._json(200, panel.set_poll(body.get("interval_s")))
                if path == "/api/errors":
                    return self._json(200, {"errors": panel.errors(),
                                            **panel.state()})
                return self._json(404, {"error": f"no such route: {path}"})
            except PanelPending as exc:
                # 202, not 409: the job is queued and will run. The only
                # operation that gets here is switching the output off behind
                # a read that has not finished, and calling that a failure
                # would be exactly backwards -- the state comes with it, so
                # the caller can see the source is still on for now.
                return self._json(202, {"error": str(exc), "level": "warn",
                                        "pending": True, **panel.state()})
            except PanelRefused as exc:
                # What the bench will not do *now*, with the reason and the
                # remedy in the sentence -- never a bare status code.
                return self._json(409, {"error": exc.text, "level": exc.level,
                                        "refused": True, **panel.state()})
            except PanelUnavailable as exc:
                return self._json(503, {"error": str(exc), "level": "crit",
                                        **panel.state()})
            except ValueError as exc:
                return self._json(422, {"error": str(exc), **panel.state()})
            except Exception as exc:                         # noqa: BLE001
                # The state goes with every answer, this one included: a
                # failure that left the caller unable to see whether the
                # source is live is worse than the failure. `state()` touches
                # no instrument, but if even that is broken the error is still
                # reported rather than swallowed by a second one.
                body: dict[str, Any] = {"error": f"{type(exc).__name__}: {exc}"}
                try:
                    body.update(panel.state())
                except Exception:                            # noqa: BLE001
                    body["state_unavailable"] = True
                return self._json(500, body)

        # -- plumbing -------------------------------------------------------
        def _cross_site(self) -> str | None:
            """Why this POST is refused, or None. See `CONTROL_CONTENT_TYPE`.

            Both checks, because neither is enough alone: a non-browser client
            sends no `Origin` (so that check must not be the only one), and a
            browser can be talked into some content types (so that check must
            not be either)."""
            origin = self.headers.get("Origin")
            if origin is not None and not is_loopback_origin(origin):
                return (f"refused: this request says it came from {origin}. The "
                        "console answers its own page, and nothing else may drive "
                        "an instrument through it.")
            declared = (self.headers.get("Content-Type") or "").split(";")[0]
            if declared.strip().lower() != CONTROL_CONTENT_TYPE:
                return (f"refused: a control request needs `Content-Type: "
                        f"{CONTROL_CONTENT_TYPE}` (this one said "
                        f"{declared.strip() or 'nothing'}). curl: add "
                        f"-H 'content-type: {CONTROL_CONTENT_TYPE}'.")
            return None

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError(f"body is larger than {MAX_BODY} bytes")
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"the body is not JSON: {exc}") from None
            if not isinstance(parsed, dict):
                raise ValueError("the body is a JSON object of fields")
            return parsed

        def _json(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            # The panel is live state; a cached `/api/state` would be a screen
            # showing a source that has since been switched off.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _page(self) -> None:
            try:
                with open(PAGE, "rb") as fh:
                    data = fh.read()
            except OSError as exc:
                return self._json(500, {"error": f"the page is missing: {exc}"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:
            """Quiet. The display polls, so the default access log is a line a
            second for as long as a tab is open, which buries the two lines
            that matter (start-up, and a traceback)."""

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


STOP_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
"""Every way of ending this program that software can catch.

`SIGTERM` is what a service manager, a container, `kill` and Task Manager's
"End task" send, and `SIGHUP` is a closed terminal -- both end Python through
their *default* handlers, which do not unwind, so the `finally` below never
runs and the SourceMeter is left driving. `SIGBREAK` is Ctrl-Break on Windows,
where there is no SIGHUP. Each is installed only if this platform has it and
this is the main thread.

**`SIGINT` is in here for the second Ctrl-C.** Python's own handler raises
`KeyboardInterrupt`, which the `try` below catches once -- but the output-off
happens in the `finally`, and an interrupt arriving *there* escapes it. That
is not a hypothetical keypress: the first Ctrl-C can land while the worker is
inside a read that legally takes 80 s, so the operator sees nothing happen and
presses again, and the program they are trying to stop dies with the source
still driving. Caught here instead, a second one sets an event that is already
set and is absorbed.

`SIGKILL` and Windows' `TerminateProcess` cannot be caught by anything, so the
output survives them. Nothing in software fixes that; the instrument's own
OUTPUT key does."""


def serve_forever(panel: KeithleyPanel, *, host: str, port: int,
                  on_ready: Any = None) -> None:
    """Run until interrupted, then put the output off (`panel.close`).

    The `finally` is the point: whatever ends this program -- Ctrl-C, a
    `kill`, a closed terminal, an exception out of the server -- the source is
    switched off on the way out.
    """
    # The worker starts **before** anything that can fail, and everything after
    # it is inside the `try`. `make_server` raises when the port is taken --
    # the ordinary way of starting the console twice -- and by then the
    # instrument is open and `_open_real` may already have found its output
    # live, left there by whoever was using it. Constructed outside the `try`,
    # that raised straight out of the program: no output-off, no warning, a
    # source driving and a traceback about a socket.
    panel.start()
    server = None
    thread = None
    stop = threading.Event()
    restore = _catch_stop_signals(stop)
    try:
        server = make_server(panel, host=host, port=port)
        thread = threading.Thread(target=server.serve_forever, name="keithley-http",
                                  daemon=True)
        thread.start()
        if on_ready is not None:
            on_ready(server)
        # Not `thread.join()`: a signal handler can only ask, and this is what
        # notices that it did.
        while thread.is_alive() and not stop.wait(0.25):
            pass
    except KeyboardInterrupt:
        # Only reachable when the handler above could not be installed -- not
        # the main thread, which is how the tests run this.
        pass
    finally:
        # `restore()` **after** the output is off, not before. Put back first,
        # the default handlers were live for exactly the window that matters:
        # a second Ctrl-C or `kill` during `panel.close()` -- the ordinary
        # response to a shutdown that appears to be doing nothing -- killed
        # the process outright, with the source still driving. Held until
        # here, that second signal only re-sets an event nobody is waiting on.
        try:
            if server is not None:
                server.shutdown()
                server.server_close()
            if not panel.close():
                print("\nWARNING: the SourceMeter output could not be confirmed off — the "
                      "instrument did not answer in time. Check it, and switch the output "
                      "off at the 2400's own OUTPUT key if it is still on.", file=sys.stderr)
        finally:
            restore()


def _catch_stop_signals(stop: threading.Event) -> Any:
    """Ask every catchable termination signal to set `stop`. Returns a callable
    that puts the previous handlers back."""
    previous: list[tuple[Any, Any]] = []

    def handler(signum: int, frame: Any) -> None:
        stop.set()

    for name in STOP_SIGNALS:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous.append((number, signal.signal(number, handler)))
        except (ValueError, OSError):
            # Not the main thread, or the platform will not have it. The
            # console still runs; it just cannot promise this signal.
            continue

    def restore() -> None:
        for number, old in previous:
            try:
                signal.signal(number, old)
            except (ValueError, OSError):
                pass

    return restore


__all__ = ["make_server", "serve_forever", "PAGE", "MAX_BODY"]
