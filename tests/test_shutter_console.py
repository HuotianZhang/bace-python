"""`bace.drivers.shutter_console`: the shutter as one switch in a browser.

The console owns the Deditec module while it runs, so what is under test here
is everything around that: the HTTP surface the page drives, the honesty of
the state it reports (measured, assumed, or unknown), the two refusals that
keep a switch on a web page from being the thing that throws the relay or
binds to the network -- and the import weight, since this has to start under
the 32-bit interpreter the rig's DELIB needs.

The line is a `SimulatedShutter`, so a real server is run against a fake
module: the requests below are the ones the page makes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from bace.drivers import shutter_console as sc
from bace.drivers.shutter import SimulatedShutter


class Line(SimulatedShutter):
    """A simulated line that starts where it is put and counts its writes."""

    def __init__(self, state=None, **kw):
        super().__init__(**kw)
        self._state = state
        self.writes: list[int] = []
        self.released = False

    def _set(self, value: int) -> None:
        self.writes.append(value)
        super()._set(value)

    def release(self) -> None:
        self.released = True
        super().release()


class Blind(Line):
    """A module with no `DapiDOReadback32`: it never says where the line is."""

    def read_line(self):
        return None


class Broken(Line):
    """A line whose handle is gone. `Shutter.set_line` raises exactly this way
    when the module was never opened or was closed underneath; `read_line`
    swallows its own errors and answers None, so the raising read here stands
    in for anything unexpected the driver could throw at the handler."""

    def read_line(self):
        raise OSError("the module went away")

    def _set(self, value: int) -> None:
        raise OSError("the module went away")


FACTS = {"module_id": 9, "module_nr": 0, "channel": 0, "dll_path": "", "bits": 64}


@pytest.fixture
def api():
    """A real server on a free port, and a client for it."""
    served = {}

    def start(line):
        control = sc.Control(line, FACTS)
        server = sc.serve(control, "127.0.0.1", 0)
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


# -- what it depends on ---------------------------------------------------
def test_it_imports_nothing_the_rig_interpreter_may_not_have() -> None:
    """It has to start under the 32-bit Python that can load the rig's DELIB.
    `http.server` and `json` are in the standard library; numpy is not."""
    code = ("import sys, bace.drivers.shutter_console;"
            "print(sorted(n for n in sys.modules "
            "if n in ('numpy', 'scipy', 'pyvisa', 'h5py', 'fastapi', 'uvicorn')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        "the console pulled in " + out.stdout.strip())


# -- the page and the API -------------------------------------------------
def test_the_page_is_the_switch(api) -> None:
    status, body, headers = api(Line(state=0))("/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert "/api/open" in body and "/api/shut" in body
    assert "does not move the shutter" in body


def test_state_open_and_shut(api) -> None:
    line = Line(state=0)
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
    answers, and the page paints them differently, so the API must not
    flatten them: a shutter believed shut and actually open is a dark trace
    taken in the light."""
    call = api(Blind(state=None))

    _, state = _json(call("/api/state"))
    assert state["open"] is None and state["how"] == "unknown"

    _, state = _json(call("/api/open", "POST"))
    assert state["open"] is True and state["how"] == "assumed"


def test_a_driver_error_is_a_503_with_the_reason(api) -> None:
    """A switch that silently does nothing is the failure to avoid: whatever
    the driver raises reaches the page as text."""
    call = api(Broken(state=1))
    for result in (call("/api/state"), call("/api/open", "POST")):
        status, body = _json(result)
        assert status == 503
        assert "the module went away" in body["error"]


def test_unknown_paths_are_404(api) -> None:
    call = api(Line(state=0))
    assert _json(call("/nope"))[0] == 404
    assert _json(call("/api/nope", "POST"))[0] == 404


# -- the two refusals -----------------------------------------------------
@pytest.mark.parametrize("host, ok", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.5", True),
    ("0.0.0.0", False), ("192.168.1.10", False), ("example.org", False),
])
def test_only_loopback(host, ok) -> None:
    assert sc.is_loopback(host) is ok


def test_serve_refuses_a_host_the_network_can_reach() -> None:
    with pytest.raises(ValueError):
        sc.serve(sc.Control(Line(state=0), FACTS), "0.0.0.0", 0)


def test_main_refuses_that_host_before_it_opens_the_module(capsys) -> None:
    line = Line(state=0)
    assert sc.main(["--host", "0.0.0.0"], make=lambda cfg, **kw: line) == 2
    assert line._handle is None
    assert "loopback" in capsys.readouterr().out


def test_main_refuses_to_serve_the_relays_module(capsys) -> None:
    """A switch on a page is the last place that line should be moved from."""
    line = Line(state=0)
    assert sc.main(["--module-nr", "1"], make=lambda cfg, **kw: line) == 2
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
    line = Line(state=0)
    try:
        assert sc.main(["--port", str(held.getsockname()[1])],
                       make=lambda cfg, **kw: line) == 2
    finally:
        held.close()
    assert line.released and line._handle is None
    assert "already running" in capsys.readouterr().out
