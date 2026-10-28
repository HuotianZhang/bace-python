"""`bace.launcher`: what it decides from the port, and what it starts.

The port is asked of real sockets on 127.0.0.1 -- a free port, a listener
that never answers, an HTTP server that is not BACE and one that is. The
sequence in `main` runs with the process, the window and the browser
replaced, so no test opens a window or starts a service.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
from datetime import datetime

import pytest

from bace import launcher


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """An HTTP server on a free port that answers `GET /session` with `body`."""

    def __init__(self, body: bytes, status: int = 200):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                           # noqa: N802 -- the stdlib's name
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# -- the port ------------------------------------------------------------------------
def test_probe_tells_a_free_port_a_starting_service_another_program_and_bace():
    assert launcher.probe(port=_free_port()) == "free"

    with socket.socket() as listener:                   # bound and listening, never serving
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        assert launcher.probe(port=listener.getsockname()[1], timeout_s=0.3) == "starting"

    with _Server(json.dumps({"id": "20261028_120000", "mode": "sim"}).encode()) as bace:
        assert launcher.probe(port=bace.port) == "bace"
    with _Server(b"<html>a printer</html>") as other:
        assert launcher.probe(port=other.port) == "other"
    with _Server(b"{}", status=404) as other:
        assert launcher.probe(port=other.port) == "other"


# -- the service ---------------------------------------------------------------------
def test_the_service_runs_under_the_console_python_with_the_lab_configs(tmp_path):
    folder = tmp_path / "py"
    folder.mkdir()
    (folder / "python.exe").write_text("")
    assert launcher.console_python(str(folder / "pythonw.exe")) == str(folder / "python.exe")
    assert launcher.console_python(str(folder / "python3")) == str(folder / "python3")

    rig = launcher.service_command(sim=False, port=8900, python="py")
    assert rig == ["py", "-m", "bace.service", "--port", "8900", "--ui", "ui",
                   "--rig", "rig.toml", "--run", "run.toml"]
    assert launcher.service_command(sim=True, port=8911, python="py")[-1] == "--sim"
    assert launcher.log_path(datetime(2026, 10, 28, 9, 5, 7)).endswith(
        "service_20261028_090507.log")


def test_a_log_tail_is_the_last_lines_and_nothing_for_a_missing_file(tmp_path):
    log = tmp_path / "service.log"
    log.write_text("".join(f"line {i}\n" for i in range(30)), encoding="utf-8")
    assert launcher.tail(str(log), 3) == "line 27\nline 28\nline 29"
    assert launcher.tail(str(tmp_path / "absent.log")) == ""


# -- the sequence --------------------------------------------------------------------
class _Proc:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class _Splash:
    def __init__(self, *a):
        self.closed = False

    def pump(self):
        pass

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_waiting_ends_when_the_service_answers_exits_or_never_answers(monkeypatch):
    answers = iter(["starting", "starting", "bace"])
    monkeypatch.setattr(launcher, "probe", lambda **kw: next(answers))
    clock = _Clock()
    assert launcher.wait_for_service(_Proc(), port=1, timeout_s=10, splash=_Splash(),
                                     clock=clock, sleep=clock.sleep) == "ready"

    assert launcher.wait_for_service(_Proc(2), port=1, timeout_s=10, splash=_Splash(),
                                     clock=_Clock(), sleep=lambda s: None) == "exited"

    monkeypatch.setattr(launcher, "probe", lambda **kw: "starting")
    clock = _Clock()
    assert launcher.wait_for_service(None, port=1, timeout_s=3, splash=_Splash(),
                                     clock=clock, sleep=clock.sleep) == "timeout"
    assert clock.t >= 3


@pytest.fixture
def sequence(monkeypatch, tmp_path):
    """`main` with the process, the window and the browser recorded."""
    seen = {"started": [], "opened": [], "alerts": []}
    monkeypatch.setattr(launcher, "Splash", _Splash)
    monkeypatch.setattr(launcher, "open_console", seen["opened"].append)
    monkeypatch.setattr(launcher, "alert", lambda title, text: seen["alerts"].append(text))
    monkeypatch.setattr(launcher, "log_path", lambda: str(tmp_path / "service.log"))

    def start(command, log):
        seen["started"].append(command)
        return _Proc(seen.get("exit_code"))
    monkeypatch.setattr(launcher, "start_service", start)
    return seen


def test_a_running_service_is_reused_and_nothing_is_started(monkeypatch, sequence):
    monkeypatch.setattr(launcher, "probe", lambda **kw: "bace")
    assert launcher.main([]) == 0
    assert sequence["started"] == [] and sequence["opened"] == ["http://127.0.0.1:8900/ui/"]


def test_a_free_port_starts_the_service_and_opens_the_console_once_it_answers(monkeypatch, sequence):
    answers = iter(["free", "starting", "bace"])
    monkeypatch.setattr(launcher, "probe", lambda **kw: next(answers))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher.main(["--sim", "--port", "8911"]) == 0
    assert len(sequence["started"]) == 1 and sequence["started"][0][-1] == "--sim"
    assert sequence["opened"] == ["http://127.0.0.1:8911/ui/"] and sequence["alerts"] == []


def test_a_service_still_starting_is_waited_for_and_not_started_twice(monkeypatch, sequence):
    answers = iter(["starting", "starting", "bace"])
    monkeypatch.setattr(launcher, "probe", lambda **kw: next(answers))
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher.main([]) == 0
    assert sequence["started"] == [] and len(sequence["opened"]) == 1


def test_a_failed_start_is_reported_with_the_log(monkeypatch, sequence, tmp_path):
    (tmp_path / "service.log").write_text("config: --run: no such file: run.toml\n",
                                          encoding="utf-8")
    sequence["exit_code"] = 2
    monkeypatch.setattr(launcher, "probe", lambda **kw: "free")
    assert launcher.main([]) == 2
    assert sequence["opened"] == []
    [text] = sequence["alerts"]
    assert "stopped while starting" in text and "no such file" in text
    assert str(tmp_path / "service.log") in text


def test_another_program_on_the_port_is_named_and_nothing_starts(monkeypatch, sequence):
    monkeypatch.setattr(launcher, "probe", lambda **kw: "other")
    assert launcher.main([]) == 3
    assert sequence["started"] == [] and "not the BACE service" in sequence["alerts"][0]


def test_a_splash_the_operator_closed_is_not_destroyed_twice(monkeypatch):
    import types

    class Widget:
        def __init__(self, *a, **kw):
            pass

        def pack(self, **kw):
            pass

        def configure(self, **kw):
            pass

    class Tk(Widget):
        destroyed = 0

        def title(self, t):
            pass

        def resizable(self, *a):
            pass

        def attributes(self, *a):
            pass

        def protocol(self, name, handler):
            self.handler = handler

        def update_idletasks(self):
            pass

        def update(self):
            pass

        def winfo_width(self):
            return 300

        winfo_height = winfo_screenwidth = winfo_screenheight = winfo_width

        def geometry(self, g):
            pass

        def destroy(self):
            Tk.destroyed += 1
            if Tk.destroyed > 1:
                raise RuntimeError("can't invoke \"destroy\" command: application has been destroyed")

    fake = types.SimpleNamespace(Tk=Tk, Frame=Widget, Label=Widget)
    monkeypatch.setitem(__import__("sys").modules, "tkinter", fake)
    splash = launcher.Splash("starting")
    root = splash.root
    root.handler()                                       # the window's close button
    splash.pump()
    splash.say("still starting")
    splash.close()
    assert Tk.destroyed == 1 and splash.root is None
