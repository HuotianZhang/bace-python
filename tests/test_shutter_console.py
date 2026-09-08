"""`examples/shutter_console/`: the shutter as one switch in a browser.

That folder is a standalone example -- it imports nothing from `bace/`, so it
can be copied to a machine with the Deditec module and a Python and nothing
else. Being standalone is the property most easily lost by accident, so it is
the first thing asserted here, in a subprocess.

The rest is what the page depends on: the HTTP surface, the honesty of the
state it reports (measured, assumed, or unknown), and the two refusals that
keep a switch on a web page from being the thing that throws the relay or
binds to the network. The line under it is the example's own `SimulatedLine`,
so the requests below are the ones the page makes, against no hardware.
"""
from __future__ import annotations

import errno
import importlib.util
import json
import pathlib
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

FOLDER = pathlib.Path(__file__).resolve().parents[1] / "examples" / "shutter_console"
MODULE = FOLDER / "shutter_console.py"


def _load():
    spec = importlib.util.spec_from_file_location("shutter_console_example", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load()


class Line(sc.SimulatedLine):
    """The example's simulated line, counting its writes."""

    def __init__(self, value: int | None = 0, **kw):
        super().__init__(**kw)
        self._value = value
        self.writes: list[int] = []
        self.released = False

    def set_line(self, value: int) -> None:
        self.writes.append(int(value))
        super().set_line(value)

    def release(self) -> None:
        self.released = True
        super().release()


class Blind(Line):
    """A module with no `DapiDOReadback32`: it never says where the line is."""

    def read_line(self):
        return None


class Broken(Line):
    """A handle that is gone -- unplugged, or taken by another process."""

    def read_line(self):
        raise OSError("the module went away")

    def set_line(self, value: int) -> None:
        raise OSError("the module went away")


@pytest.fixture
def api():
    """A real server on a free port, and a client for it."""
    served = {}

    def start(line):
        line.open()
        server = sc.serve(sc.Control(line), "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        served["server"], served["thread"] = server, thread
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(path, method="GET"):
            req = urllib.request.Request(base + path, method=method)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read().decode("utf-8"), r.headers
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read().decode("utf-8"), exc.headers

        return call

    yield start
    server = served.get("server")
    if server is not None:
        server.shutdown()
        server.server_close()
        served["thread"].join(timeout=5)


def _json(result):
    status, body, _ = result
    return status, json.loads(body)


# -- standalone -----------------------------------------------------------
def test_it_imports_nothing_from_the_package_or_off_the_shelf() -> None:
    """The property the folder exists for. It has to start on a machine that
    has the Deditec module and a Python and nothing else -- and under the
    32-bit interpreter the rig's DELIB needs, which has no numpy."""
    code = (
        "import importlib.util, sys;"
        f"spec = importlib.util.spec_from_file_location('t', {str(MODULE)!r});"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(sorted(n for n in sys.modules if n.split('.')[0] in "
        "('bace', 'numpy', 'scipy', 'pyvisa', 'h5py', 'fastapi', 'uvicorn')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        "examples/shutter_console pulled in " + out.stdout.strip() +
        ". The folder must stay copy-and-run: standard library only, and "
        "nothing from bace/.")


def test_the_folder_carries_everything_it_needs() -> None:
    for name in ("shutter_console.py", "page.html", "README.md",
                 "Run Shutter Console.bat"):
        assert (FOLDER / name).is_file(), f"examples/shutter_console/{name} is missing"


# -- the page and the API -------------------------------------------------
def test_the_page_is_served_from_the_folder(api) -> None:
    status, body, headers = api(Line(0))("/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    # `read_bytes`, not `read_text`: the console sends the file's bytes and
    # text mode translates newlines, so on a Windows checkout (CRLF in the
    # working tree, nothing in `.gitattributes` pinning `.html`) this compared
    # every line against itself minus a `\r`.
    assert body == (FOLDER / "page.html").read_bytes().decode("utf-8")
    assert "/api/open" in body and "does not move the shutter" in body


def test_state_open_and_shut(api) -> None:
    line = Line(0)
    call = api(line)

    status, state = _json(call("/api/state"))
    assert status == 200 and state["open"] is False and state["how"] == "readback"
    assert line.writes == [], "reading must not drive the line"

    status, state = _json(call("/api/open", "POST"))
    assert status == 200 and state["open"] is True and state["line"] == 1
    assert line.writes == [1]

    status, state = _json(call("/api/shut", "POST"))
    assert status == 200 and state["open"] is False
    assert line.writes == [1, 0]


def test_it_says_when_the_state_is_only_assumed(api) -> None:
    """A DELIB build with no readback. 'shut' and 'cannot say' are different
    answers: a shutter believed shut and actually open is a dark measurement
    taken in the light, so the API must not flatten them."""
    call = api(Blind(None))

    _, state = _json(call("/api/state"))
    assert state["open"] is None and state["how"] == "unknown"

    _, state = _json(call("/api/open", "POST"))
    assert state["open"] is True and state["how"] == "assumed"


def test_a_driver_error_is_a_503_with_the_reason(api) -> None:
    """A switch that silently does nothing is the failure to avoid: whatever
    the driver raises reaches the page as text."""
    call = api(Broken(1))
    for result in (call("/api/state"), call("/api/open", "POST")):
        status, body = _json(result)
        assert status == 503
        assert "the module went away" in body["error"]


def test_unknown_paths_are_404(api) -> None:
    call = api(Line(0))
    assert _json(call("/nope"))[0] == 404
    assert _json(call("/api/nope", "POST"))[0] == 404


# -- the refusals ---------------------------------------------------------
@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.5", True),
    ("0.0.0.0", False), ("192.168.1.10", False), ("example.org", False),
])
def test_only_loopback(host, ok) -> None:
    assert sc.is_loopback(host) is ok


def test_serve_refuses_a_host_the_network_can_reach() -> None:
    with pytest.raises(ValueError):
        sc.serve(sc.Control(Line(0)), "0.0.0.0", 0)


def test_main_refuses_that_host_before_it_opens_the_module(capsys) -> None:
    line = Line(0)
    assert sc.main(["--host", "0.0.0.0"], make=lambda **kw: line) == 2
    assert line._handle is None
    assert "loopback" in capsys.readouterr().out


def test_main_refuses_to_serve_the_relays_module(capsys) -> None:
    """A switch on a page is the last place that line should be moved from."""
    line = Line(0)
    assert sc.main(["--module-nr", str(sc.RELAY_MODULE_NR)],
                   make=lambda **kw: line) == 2
    assert line.writes == [] and line._handle is None
    assert "RELAY" in capsys.readouterr().out


def test_a_port_already_in_use_is_named_and_frees_the_module(capsys) -> None:
    """Double-clicking the .bat twice. A traceback about a socket would send
    someone looking at the DIO -- and the second console must not walk away
    holding the module it just opened."""
    import socket

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    line = Line(0)
    try:
        assert sc.main(["--port", str(held.getsockname()[1])],
                       make=lambda **kw: line) == 2
    finally:
        held.close()
    assert line.released and line._handle is None
    # The hint, not just the socket error. It was `EADDRINUSE`-only, which
    # Windows never raises for a busy port (`WINDOWS_PORT_TAKEN`), so the one
    # platform this console is double-clicked on got the raw socket message
    # that this code exists to translate.
    assert "already running" in capsys.readouterr().out


def test_a_busy_port_is_recognised_on_windows_too() -> None:
    """Windows answers a second bind with `WSAEACCES` (10013), not
    `EADDRINUSE` — and Python maps that to `EACCES`, which on POSIX means a
    privileged port and not a busy one. So the match is on `winerror`."""
    posix = OSError(errno.EADDRINUSE, "Address already in use")
    assert sc._port_is_taken(posix)

    windows = OSError(errno.EACCES, "forbidden by its access permissions")
    windows.winerror = 10013
    assert sc._port_is_taken(windows)
    windows.winerror = 10048
    assert sc._port_is_taken(windows)

    # A privileged port on POSIX is `EACCES` with no `winerror`, and is not
    # a console that is already running.
    privileged = OSError(errno.EACCES, "Permission denied")
    assert not sc._port_is_taken(privileged)
    assert not sc._port_is_taken(OSError(errno.ENOENT, "nope"))
