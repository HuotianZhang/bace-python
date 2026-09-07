"""The shutter's own console: one switch, in a browser, owning the DIO module.

    py -3 -m bace.drivers.shutter_console          then open http://127.0.0.1:8910
    py -3.11-32 -m bace.drivers.shutter_console    where the rig's DELIB is 32-bit

**Why a console at all.** Only one process can hold a Deditec module -- exactly
as only one can hold the 1918-C over USB or the 331 over GPIB. The answer on
this rig for those two is a small program that owns the instrument and answers
over localhost, with everything else asking it (`[power_meter] console` and
`[temperature] console` in rig.toml; the clients are
`drivers/newport1918c/console.py` and `drivers/lakeshore331/console.py`). This
is the same shape for the third instrument, from the other side: the server.

The BACE service does not know about it **yet**. It opens the shutter line
itself at startup and holds it, so the two cannot run at once: stop the service
before starting this, and this before that. Teaching `Bench.build_real` to
reach a `[dio] console` the way it already reaches the other two is the change
that would remove the conflict, and it is deliberately not in this file.

Standard library only -- `http.server`, `json`, and `ctypes` under the driver --
so it runs on the 32-bit interpreter the rig's DELIB needs, from a checkout,
with nothing installed. The page is one string in this file for that reason,
not for want of `ui/`.

Loopback only, and that is not overridable: there is no authentication and the
thing on the other end moves hardware.

Closing the window does not move the shutter. The handle is released, not
closed -- `Shutter.close()` shuts the line first, and a console that dropped
the beam every time a tab closed would be its own kind of surprise.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .delib import DioError, dio_settings, find_rig, make_line
from .shutter import DEFAULT_CHANNEL, ShutterError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8910
"""Beside the service's 8900, clear of the two instrument consoles this rig
already runs (1918-C on 8918, 331 on 8331)."""

SETTLE_S = 0.4
"""After the line moves, before it is read back -- what `_dio_backend` gives
the same driver. It is also what the browser waits for, so the state the page
paints is the state after the shutter has finished moving."""


def is_loopback(host: str) -> bool:
    """`127.0.0.0/8`, `::1` or the name `localhost`; nothing else.

    The service's own check (`bace/service/__main__.py`), repeated rather than
    imported: importing it would drag in the whole service, which is the one
    thing this program must not need.
    """
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


class Control:
    """The line, a lock, and an honest answer about where it is.

    `read_line()` asks the module; older DELIB builds have no
    `DapiDOReadback32` and answer None. Rather than let the page paint "shut"
    for "cannot say", the state carries `how`: `readback` when the module
    said so, `assumed` when this console is only repeating what it last sent,
    `unknown` before it has sent anything. The distinction is the same one
    `/bench` makes in the service, and it exists because a shutter believed
    shut and actually open is a dark trace taken in the light.
    """

    def __init__(self, line, facts: dict):
        self.line = line
        self.facts = dict(facts)
        self._last: int | None = None
        self._lock = threading.Lock()

    def _state_locked(self) -> dict:
        value = self.line.read_line()
        how = "readback"
        if value is None:
            value, how = self._last, ("assumed" if self._last is not None
                                      else "unknown")
        return {**self.facts,
                "open": None if value is None else bool(value),
                "line": None if value is None else int(value),
                "how": how}

    def state(self) -> dict:
        with self._lock:
            return self._state_locked()

    def set(self, value: int) -> dict:
        with self._lock:
            self.line.set_line(value)
            self._last = int(value)
            return self._state_locked()

    def release(self) -> None:
        with self._lock:
            self.line.release()


def make_handler(control: Control) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "bace-shutter-console"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict) -> None:
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _act(self, value: int | None) -> None:
            try:
                self._json(200, control.state() if value is None
                           else control.set(value))
            except Exception as exc:                          # noqa: BLE001
                # A DELIB call that failed mid-session: the module was
                # unplugged, or another process took it. Say so on the page
                # rather than leaving a switch that silently does nothing.
                self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:                             # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._act(None)
            else:
                self._json(404, {"error": f"no such path: {self.path}"})

        def do_POST(self) -> None:                            # noqa: N802
            if self.path == "/api/open":
                self._act(1)
            elif self.path == "/api/shut":
                self._act(0)
            else:
                self._json(404, {"error": f"no such path: {self.path}"})

        def log_message(self, fmt: str, *args) -> None:
            # One line per action, not per request: the page polls.
            if self.command == "POST":
                sys.stdout.write("  %s %s\n" % (self.command, self.path))
                sys.stdout.flush()

    return Handler


def serve(control: Control, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A bound, not yet serving, server. The caller runs `serve_forever()`."""
    if not is_loopback(host):
        raise ValueError(f"{host} is not a loopback address. This console has no "
                         "authentication and moves hardware; it binds to "
                         "127.0.0.1 only.")
    server = ThreadingHTTPServer((host, port), make_handler(control))
    server.daemon_threads = True
    return server


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BACE shutter</title>
<style>
  :root {
    --bg: #f4f4f2; --card: #fff; --ink: #16161a; --dim: #6b6b73;
    --line: #dcdcd6; --open: #d98324; --shut: #4a5568; --bad: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16161a; --card: #1e1e24; --ink: #f2f2ef; --dim: #9a9aa4;
      --line: #32323a; --open: #f0a04b; --shut: #8b98ac; --bad: #f2856f;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: var(--bg); color: var(--ink);
    font: 15px/1.5 "IBM Plex Sans", system-ui, -apple-system, sans-serif;
  }
  main { width: min(30rem, 92vw); }
  h1 {
    font: 600 12px/1 "IBM Plex Mono", ui-monospace, monospace;
    letter-spacing: .18em; text-transform: uppercase; color: var(--dim);
    margin: 0 0 .75rem .1rem;
  }
  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 1.5rem;
  }
  .state { display: flex; align-items: center; gap: .8rem; }
  .dot {
    width: 1.1rem; height: 1.1rem; border-radius: 50%;
    background: var(--shut); box-shadow: 0 0 0 4px color-mix(in srgb, var(--shut) 22%, transparent);
    transition: background .2s;
  }
  .is-open .dot { background: var(--open); box-shadow: 0 0 0 4px color-mix(in srgb, var(--open) 26%, transparent); }
  .word { font: 600 2rem/1 "IBM Plex Mono", ui-monospace, monospace; letter-spacing: .04em; }
  .said { color: var(--dim); font-size: 13px; margin-top: .15rem; }
  .buttons { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; margin: 1.4rem 0 0; }
  button {
    font: 600 15px/1 inherit; letter-spacing: .06em; text-transform: uppercase;
    padding: 1.05rem 0; border-radius: 8px; border: 1px solid var(--line);
    background: transparent; color: var(--ink); cursor: pointer;
  }
  button:hover:not(:disabled) { border-color: var(--ink); }
  button:disabled { opacity: .45; cursor: default; }
  button.on { background: var(--ink); color: var(--card); border-color: var(--ink); }
  .facts {
    margin: 1.3rem 0 0; padding-top: 1rem; border-top: 1px solid var(--line);
    font: 12px/1.7 "IBM Plex Mono", ui-monospace, monospace; color: var(--dim);
  }
  .facts b { color: var(--ink); font-weight: 500; }
  .note { color: var(--dim); font-size: 12.5px; margin: .9rem .2rem 0; }
  .err {
    display: none; margin-top: 1rem; padding: .7rem .85rem; border-radius: 8px;
    background: color-mix(in srgb, var(--bad) 12%, transparent);
    border: 1px solid color-mix(in srgb, var(--bad) 45%, transparent);
    color: var(--bad); font: 12.5px/1.5 "IBM Plex Mono", ui-monospace, monospace;
  }
</style>
</head>
<body>
<main>
  <h1>BACE &middot; shutter</h1>
  <div class="card" id="card">
    <div class="state">
      <span class="dot"></span>
      <div>
        <div class="word" id="word">&hellip;</div>
        <div class="said" id="said">asking the module</div>
      </div>
    </div>
    <div class="buttons">
      <button id="open" disabled>open</button>
      <button id="shut" disabled>shut</button>
    </div>
    <div class="err" id="err"></div>
    <div class="facts" id="facts"></div>
  </div>
  <p class="note">
    Closing this window does not move the shutter &mdash; the line is left where
    you put it. While this console runs it owns the Deditec module, so the BACE
    service and <code>tools/shutter.py</code> cannot open it.
  </p>
</main>
<script>
  const el = (id) => document.getElementById(id);
  const HOW = {
    readback: "the module says so",
    assumed: "assumed \\u2014 this console set it, the module cannot be read back",
    unknown: "unknown \\u2014 this DELIB build has no readback and nothing has been set",
  };
  let busy = false;

  function paint(s) {
    const open = s.open === true, known = s.open !== null;
    el("card").classList.toggle("is-open", open);
    el("word").textContent = known ? (open ? "OPEN" : "SHUT") : "UNKNOWN";
    el("said").textContent = known && open
      ? "light reaches the sample \\u00b7 " + (HOW[s.how] || s.how)
      : known ? "the dark state \\u00b7 " + (HOW[s.how] || s.how) : HOW.unknown;
    el("open").classList.toggle("on", open);
    el("shut").classList.toggle("on", known && !open);
    el("open").disabled = el("shut").disabled = busy;
    el("facts").innerHTML =
      "DIO module <b>" + s.module_nr + "</b>, id <b>" + s.module_id +
      "</b>, channel <b>" + s.channel + "</b><br>" +
      "DELIB <b>" + (s.dll_path || "found by search") + "</b> \\u00b7 " +
      s.bits + "-bit Python";
  }

  function fail(text) {
    const box = el("err");
    box.style.display = text ? "block" : "none";
    box.textContent = text || "";
  }

  async function call(path, method) {
    busy = true;
    el("open").disabled = el("shut").disabled = true;
    try {
      const r = await fetch(path, { method });
      const body = await r.json();
      if (!r.ok) { fail(body.error || ("HTTP " + r.status)); return; }
      fail("");
      paint(body);
    } catch (e) {
      fail("the console is not answering: " + e);
    } finally {
      busy = false;
      el("open").disabled = el("shut").disabled = false;
    }
  }

  el("open").onclick = () => call("/api/open", "POST");
  el("shut").onclick = () => call("/api/shut", "POST");
  const poll = () => { if (!busy) call("/api/state", "GET"); };
  poll();
  setInterval(poll, 2000);
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None, *, make=None) -> int:
    """`make(cfg, module_nr=…, channel=…, dll_path=…, settle_s=…)` builds the
    line; it defaults to the real driver, as `tools/shutter.py` does."""
    ap = argparse.ArgumentParser(
        prog="python -m bace.drivers.shutter_console",
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rig", default=None, help="path to rig.toml")
    ap.add_argument("--delib", default=None,
                    help="path to the DELIB dll. Overrides [dio] dll_path")
    ap.add_argument("--module-nr", type=int, default=None,
                    help="DIO module number (default: [dio] shutter_module_nr)")
    ap.add_argument("--channel", type=int, default=None,
                    help=f"digital output channel (default: {DEFAULT_CHANNEL})")
    ap.add_argument("--settle", type=float, default=SETTLE_S, metavar="SECONDS")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", action="store_true",
                    help="open the page in the default browser once the server "
                         "is bound (what the .bat passes)")
    ap.add_argument("--force", action="store_true",
                    help="serve the relay's module. Only with the device "
                         "disconnected")
    a = ap.parse_args(argv)

    try:
        rig_path = find_rig(a.rig)
        cfg = dio_settings(rig_path)
    except DioError as exc:
        print(f"rig.toml: {exc}")
        return 2

    module_nr = cfg["shutter_module_nr"] if a.module_nr is None else a.module_nr
    channel = DEFAULT_CHANNEL if a.channel is None else a.channel
    dll_path = a.delib or cfg["dll_path"]
    bits = struct.calcsize("P") * 8

    if module_nr == cfg["relay_module_nr"] and not a.force:
        print(f"module {module_nr} is the RELAY, not the shutter. A switch on a "
              "page is the last place that line should be moved from: it carries "
              "the device between the amplifier and the Keithley, and moving it "
              "under a live source damages hardware. Use tools/relay.py.")
        return 2
    if not is_loopback(a.host):
        print(f"--host {a.host} is not a loopback address. This console has no "
              "authentication and moves hardware.")
        return 2

    try:
        line = (make or make_line)(cfg, module_nr=module_nr, channel=channel,
                                   dll_path=dll_path, settle_s=a.settle)
        line.open()
    except (ShutterError, OSError) as exc:
        print(f"no DIO on this machine: {exc}")
        return 2

    control = Control(line, {"module_id": cfg["module_id"], "module_nr": module_nr,
                             "channel": channel, "dll_path": dll_path,
                             "bits": bits})
    try:
        server = serve(control, a.host, a.port)
    except OSError as exc:
        # Double-clicking the .bat twice is the way this happens, and a
        # traceback about a socket would send someone looking at the DIO.
        control.release()
        print(f"cannot listen on {a.host}:{a.port}: {exc}")
        if getattr(exc, "errno", None) == errno.EADDRINUSE:
            print("  a shutter console is already running — its page is at "
                  f"http://{a.host}:{a.port}/")
        return 2
    print(f"BACE shutter console — {bits}-bit Python {sys.version.split()[0]}")
    print(f"  rig.toml: {rig_path or '(built-in defaults)'}")
    print(f"  shutter:  DIO module {module_nr}, id {cfg['module_id']}, "
          f"channel {channel}")
    url = f"http://{a.host}:{server.server_address[1]}/"
    print(f"  open      {url}   (Ctrl-C to stop; the line is left where "
          "you put it)")
    if a.browser:
        # After the bind, never before: a page opened first lands on a refused
        # connection and has to be reloaded by hand.
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        control.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
