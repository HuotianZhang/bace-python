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

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .panel import KeithleyPanel, PanelRefused, PanelUnavailable

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page.html")

MAX_BODY = 64 * 1024
"""Every body here is a handful of numbers. A cap so a stray upload cannot
make the console read it into memory."""


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
                return self._json(200, panel.state())
            return self._json(404, {"error": f"no such route: {path}"})

        def do_POST(self) -> None:                           # noqa: N802
            path = self.path.split("?")[0]
            try:
                body = self._body()
                if path == "/api/source":
                    return self._json(200, panel.set_source(body))
                if path == "/api/output":
                    if "on" not in body:
                        raise ValueError("on: the body needs {\"on\": true} or false")
                    return self._json(200, panel.set_output(bool(body["on"])))
                if path == "/api/read":
                    return self._json(200, panel.read())
                if path == "/api/poll":
                    return self._json(200, panel.set_poll(body.get("interval_s")))
                if path == "/api/errors":
                    return self._json(200, {"errors": panel.errors(),
                                            **panel.state()})
                return self._json(404, {"error": f"no such route: {path}"})
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
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        # -- plumbing -------------------------------------------------------
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


def serve_forever(panel: KeithleyPanel, *, host: str, port: int,
                  on_ready: Any = None) -> None:
    """Run until interrupted, then put the output off (`panel.close`).

    The `finally` is the point: whatever ends this program -- Ctrl-C, a closed
    terminal, an exception out of the server -- the source is switched off on
    the way out.
    """
    server = make_server(panel, host=host, port=port)
    panel.start()
    thread = threading.Thread(target=server.serve_forever, name="keithley-http",
                              daemon=True)
    thread.start()
    try:
        if on_ready is not None:
            on_ready(server)
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        panel.close()


__all__ = ["make_server", "serve_forever", "PAGE", "MAX_BODY"]
