#!/usr/bin/env python3
"""A shutter switch in a browser. One folder, standard library, nothing else.

    py -3 shutter_console.py --sim        try it with no hardware at all
    py -3 shutter_console.py              the real Deditec line
    py -3.11-32 shutter_console.py        where the DELIB build is 32-bit

Then open http://127.0.0.1:8910/ (`--browser` opens it for you).

**Standalone on purpose.** This folder imports nothing from `bace/`: not the
drivers, not the config, and above all not the service. Copy the folder to a
machine that has the Deditec module and a Python, and it runs. The ~60 lines
of `ctypes` below are therefore a deliberate second copy of what
`bace/drivers/shutter.py` and `bace/drivers/delib.py` do properly -- go there
for the full story (the DLL search, the readback, the bridge for a 64-bit
host). Nothing in a measurement depends on this file.

**It is not part of the BACE service, and the service knows nothing about it.**
What they do share is the hardware: only one process can hold a Deditec
module, so while this runs the service (and every other program that opens the
module) cannot, and vice versa. Stop one before starting the other.

**The relay is not a shutter.** The same module type on module number 1 moves
the device between the amplifier and the SourceMeter, and throwing that under a
live source damages hardware. This refuses to serve it without `--force`.

**Loopback only**, and that is not overridable: there is no authentication and
the thing on the other end moves hardware.

**The line is left where you put it.** Closing the window releases the module
handle without driving the line, so a shutter you opened stays open.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import ipaddress
import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_FILE = os.path.join(HERE, "page.html")

# -- the bench's numbers, measured on the rig 2026-09-01 --------------------
MODULE_ID = 9
SHUTTER_MODULE_NR = 0     # 1 opens the shutter, 0 shuts it
RELAY_MODULE_NR = 1       # NOT a shutter. See the refusal in `main`
CHANNEL = 0
SETTLE_S = 0.4            # after the line moves, before it is read back

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8910

#: Where Deditec's installers put each build. `ctypes` can load only the one
#: matching the interpreter: 32-bit is `delib.dll`, 64-bit is `delib64.dll`.
DELIB_PATHS = (
    r"C:\Windows\System32\delib64.dll",
    r"C:\Windows\SysWOW64\delib.dll",
    r"D:\BACE\DELIB\delib64.dll",
    r"D:\BACE\shutter\builds\data\delib.dll",
)

_PE_MACHINE = {0x014C: 32, 0x8664: 64, 0xAA64: 64}


class ShutterError(RuntimeError):
    pass


# --------------------------------------------------------------- the DIO line
def interpreter_bits() -> int:
    return struct.calcsize("P") * 8


def pe_bitness(path: str) -> int | None:
    """32 or 64 from a Windows PE header, or None if it cannot be read as one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(0x400)
        offset = int.from_bytes(head[0x3C:0x40], "little")
        return _PE_MACHINE.get(int.from_bytes(head[offset + 4:offset + 6], "little"))
    except Exception:
        return None


def find_delib(explicit: str | None = None) -> list[str]:
    """Everything worth handing `ctypes`, best first.

    A file whose PE header matches this interpreter beats one we could not read
    a header from, and the bare name is the last resort -- Windows searches its
    own DLL path, and System32/SysWOW64 are each redirected to the build that
    matches the calling process.
    """
    bits = interpreter_bits()
    found = [(p, pe_bitness(p)) for p in ((explicit,) if explicit else DELIB_PATHS)
             if p and os.path.isfile(p)]
    out = [p for p, m in found if m == bits]
    out += [p for p, m in found if m is None]
    out.append("delib64.dll" if bits == 64 else "delib.dll")
    return out


class DelibLine:
    """One digital output on a Deditec module, through `delib.dll`.

        ULONG DapiOpenModule(ULONG moduleID, ULONG nr);
        void  DapiDOSet1(ULONG handle, ULONG ch, ULONG data);
        ULONG DapiDOReadback32(ULONG handle, ULONG ch);
        ULONG DapiCloseModule(ULONG handle);
    """

    simulated = False

    def __init__(self, dll_path: str | None = None, *, module_id: int = MODULE_ID,
                 module_nr: int = SHUTTER_MODULE_NR, channel: int = CHANNEL,
                 settle_s: float = SETTLE_S):
        opener = ctypes.WinDLL if hasattr(ctypes, "WinDLL") else ctypes.CDLL
        tried: list[str] = []
        lib = None
        for path in find_delib(dll_path):
            try:
                lib = opener(path)
            except OSError as exc:
                tried.append(f"{path} ({exc})")
                continue
            self.dll_path = path
            break
        if lib is None:
            raise ShutterError(
                f"no DELIB library this {interpreter_bits()}-bit interpreter can "
                "load. Install the matching DELIB (64-bit installs as delib64.dll "
                "in System32, 32-bit as delib.dll in SysWOW64), or pass --delib. "
                "Tried: " + "; ".join(tried))
        self._lib = lib
        lib.DapiOpenModule.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        lib.DapiOpenModule.restype = ctypes.c_ulong
        lib.DapiDOSet1.argtypes = [ctypes.c_ulong] * 3
        lib.DapiDOSet1.restype = None
        lib.DapiCloseModule.argtypes = [ctypes.c_ulong]
        lib.DapiCloseModule.restype = ctypes.c_ulong
        try:
            lib.DapiDOReadback32.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
            lib.DapiDOReadback32.restype = ctypes.c_ulong
            self._readback = lib.DapiDOReadback32
        except AttributeError:            # older DELIB without the export
            self._readback = None
        self.module_id, self.module_nr = module_id, module_nr
        self.channel, self.settle_s = channel, settle_s
        self._handle: int | None = None

    def open(self) -> "DelibLine":
        handle = int(self._lib.DapiOpenModule(self.module_id, self.module_nr))
        if handle == 0:
            raise ShutterError(
                f"DapiOpenModule(id={self.module_id}, nr={self.module_nr}) "
                "returned 0 — the module is not there, another program holds it, "
                "or the DELIB bitness does not match this interpreter")
        self._handle = handle
        return self

    def read_line(self) -> int | None:
        """The line as the module reports it, or None if it cannot say.

        `DapiDOReadback32(handle, ch)` returns a word of 32 outputs starting at
        `ch`, so bit `channel` of the word read from 0 is this line.
        """
        if self._handle is None or self._readback is None:
            return None
        try:
            return (int(self._readback(self._handle, 0)) >> self.channel) & 1
        except Exception:
            return None

    def set_line(self, value: int) -> None:
        if self._handle is None:
            raise ShutterError("the module is not open")
        self._lib.DapiDOSet1(self._handle, self.channel, 1 if value else 0)
        if self.settle_s:
            time.sleep(self.settle_s)

    def release(self) -> None:
        """Drop the handle and leave the line exactly where it is."""
        if self._handle is not None:
            self._lib.DapiCloseModule(self._handle)
            self._handle = None


class SimulatedLine:
    """`--sim`: the page, the API and every refusal, with no module at all."""

    simulated = True
    dll_path = ""

    def __init__(self, *, module_id: int = MODULE_ID,
                 module_nr: int = SHUTTER_MODULE_NR, channel: int = CHANNEL,
                 settle_s: float = 0.0):
        self.module_id, self.module_nr = module_id, module_nr
        self.channel, self.settle_s = channel, settle_s
        self._handle: int | None = None
        self._value = 0

    def open(self) -> "SimulatedLine":
        self._handle = 1
        return self

    def read_line(self) -> int | None:
        return None if self._handle is None else self._value

    def set_line(self, value: int) -> None:
        if self._handle is None:
            raise ShutterError("the module is not open")
        self._value = 1 if value else 0

    def release(self) -> None:
        self._handle = None


# ----------------------------------------------------------------- the server
def is_loopback(host: str) -> bool:
    """`127.0.0.0/8`, `::1` or the name `localhost`; nothing else."""
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return False


class Control:
    """The line, a lock, and an honest answer about where it is.

    `read_line()` asks the module; DELIB builds without `DapiDOReadback32`
    answer None. Rather than let the page paint "shut" for "cannot say", the
    state carries `how`: `readback` when the module said so, `assumed` when
    this console is repeating what it last sent, `unknown` before it has sent
    anything. A shutter believed shut and actually open is a dark measurement
    taken in the light, so the three are not flattened into a boolean.
    """

    def __init__(self, line):
        self.line = line
        self._last: int | None = None
        self._lock = threading.Lock()

    def _state_locked(self) -> dict:
        value = self.line.read_line()
        how = "readback"
        if value is None:
            value, how = self._last, ("assumed" if self._last is not None
                                      else "unknown")
        return {"open": None if value is None else bool(value),
                "line": None if value is None else int(value),
                "how": how,
                "module_id": self.line.module_id,
                "module_nr": self.line.module_nr,
                "channel": self.line.channel,
                "dll_path": getattr(self.line, "dll_path", ""),
                "simulated": bool(getattr(self.line, "simulated", False)),
                "bits": interpreter_bits()}

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
        server_version = "shutter-console"
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
                # The module was unplugged, or another process took it. Say so
                # on the page rather than leaving a switch that does nothing.
                self._json(503, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:                             # noqa: N802
            if self.path in ("/", "/index.html"):
                try:
                    with open(PAGE_FILE, "rb") as fh:
                        page = fh.read()
                except OSError as exc:
                    self._json(500, {"error": f"page.html: {exc}"})
                    return
                self._send(200, page, "text/html; charset=utf-8")
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
            if self.command == "POST":       # one line per action; the page polls
                sys.stdout.write("  %s %s\n" % (self.command, self.path))
                sys.stdout.flush()

    return Handler


def serve(control: Control, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """A bound, not yet serving, server. The caller runs `serve_forever()`."""
    if not is_loopback(host):
        raise ValueError(f"{host} is not a loopback address. This console has "
                         "no authentication and moves hardware; it binds to "
                         "127.0.0.1 only.")
    server = ThreadingHTTPServer((host, port), make_handler(control))
    server.daemon_threads = True
    return server


# ------------------------------------------------------------------- the main
WINDOWS_PORT_TAKEN = (10013, 10048)
"""`WSAEACCES` and `WSAEADDRINUSE`.

Windows does not answer a bound port with `EADDRINUSE` the way POSIX does: a
second `bind` to a port somebody is already listening on raises **10013**,
"an attempt was made to access a socket in a way forbidden by its access
permissions" -- that is the code when the holder did not ask for
`SO_REUSEADDR`, which is exactly the second console. **10048**
(`WSAEADDRINUSE`) is the other way it comes back. So the `EADDRINUSE` check
below found nothing on the one platform this console is actually
double-clicked on, and the operator who started it twice got the socket error
this code exists to translate (measured 2026-09-08).

Matched on `winerror` rather than on `errno`, which Python maps 10013 to
`EACCES` -- and `EACCES` on POSIX is a privileged port, not a busy one, so
matching it there would explain `--port 80` as a console that is already
running.
"""


def _port_is_taken(exc: OSError) -> bool:
    """Whether `exc` from `bind`/`listen` means somebody already has the port."""
    return (getattr(exc, "errno", None) == errno.EADDRINUSE
            or getattr(exc, "winerror", None) in WINDOWS_PORT_TAKEN)


def main(argv: list[str] | None = None, *, make=None) -> int:
    """`make(**kw)` builds the line; it defaults to the real one, or the
    simulated one under `--sim`, and the tests pass their own."""
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="The line is left where you put it: closing this releases the "
               "module handle without driving anything.")
    ap.add_argument("--sim", action="store_true",
                    help="no hardware: the page and the API against a fake line")
    ap.add_argument("--delib", default=None,
                    help="path to the DELIB dll, if it is not where the "
                         "installers put it")
    ap.add_argument("--module-nr", type=int, default=SHUTTER_MODULE_NR,
                    help=f"DIO module number (default: {SHUTTER_MODULE_NR})")
    ap.add_argument("--module-id", type=int, default=MODULE_ID,
                    help=f"DIO module id (default: {MODULE_ID})")
    ap.add_argument("--channel", type=int, default=CHANNEL,
                    help=f"digital output channel (default: {CHANNEL})")
    ap.add_argument("--settle", type=float, default=SETTLE_S, metavar="SECONDS",
                    help=f"after the line moves (default: {SETTLE_S:g})")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--browser", action="store_true",
                    help="open the page once the server is bound")
    ap.add_argument("--force", action="store_true",
                    help="serve the relay's module. Only with the device "
                         "disconnected")
    a = ap.parse_args(argv)

    if a.module_nr == RELAY_MODULE_NR and not a.force:
        print(f"module {a.module_nr} is the RELAY, not the shutter. A switch on "
              "a page is the last place that line should be moved from: it "
              "carries the device between the amplifier and the SourceMeter, "
              "and moving it under a live source damages hardware.")
        return 2
    if not is_loopback(a.host):
        print(f"--host {a.host} is not a loopback address. This console has no "
              "authentication and moves hardware.")
        return 2

    def build(**kw):
        if make is not None:
            return make(**kw)
        if a.sim:
            return SimulatedLine(**kw)
        return DelibLine(a.delib, **kw)

    try:
        line = build(module_id=a.module_id, module_nr=a.module_nr,
                     channel=a.channel, settle_s=0.0 if a.sim else a.settle)
        line.open()
    except (ShutterError, OSError) as exc:
        print(f"no DIO on this machine: {exc}")
        return 2

    control = Control(line)
    try:
        server = serve(control, a.host, a.port)
    except OSError as exc:
        # Double-clicking the .bat twice is how this happens, and a traceback
        # about a socket would send someone looking at the DIO.
        control.release()
        print(f"cannot listen on {a.host}:{a.port}: {exc}")
        if _port_is_taken(exc):
            print("  a shutter console is already running — its page is at "
                  f"http://{a.host}:{a.port}/")
        return 2

    url = f"http://{a.host}:{server.server_address[1]}/"
    print(f"shutter console — {interpreter_bits()}-bit Python "
          f"{sys.version.split()[0]}{'  (simulated)' if a.sim else ''}")
    print(f"  DIO module {a.module_nr}, id {a.module_id}, channel {a.channel}")
    print(f"  open      {url}   (Ctrl-C to stop; the line is left where you "
          "put it)")
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
