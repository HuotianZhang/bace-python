"""The operator's entry point: `BACE.pyw` at the repository root calls `main`.

    BACE.pyw                       # double-click: the service and the console
    py -3 -m bace.launcher --sim   # the same with the simulated rig

One service owns the instruments, so the launcher first asks the port. A BACE
service that answers is reused and only the console is opened; a port that
accepts and does not answer yet is a service still starting, and is waited
for. Otherwise the service is started without a window, its output written
to `runs/logs/`, and the console is opened once `GET /session` answers.

Closing the console leaves the service and any run in progress untouched.
The service ends from the console's `shut down`, which parks the bench.

Standard library only: this runs before anything has checked that the
`service` extra is installed, and it reports that failure from the log.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
"""The repository: `rig.toml`, `run.toml`, `ui/` and `runs/` are found here."""

HOST = "127.0.0.1"
PORT = 8900

START_TIMEOUT_S = 120.0
"""Building the real rig opens every VISA resource and resets the digitiser;
a slow GPIB bus takes tens of seconds."""

POLL_S = 0.5
PROBE_TIMEOUT_S = 1.0
LOG_TAIL_LINES = 12

EDGE = (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")


# -- the port ---------------------------------------------------------------------
def probe(host: str = HOST, port: int = PORT, *,
          timeout_s: float = PROBE_TIMEOUT_S) -> str:
    """What holds the port.

    `"free"`: nothing listens, known by binding the port; on Windows a
    connection to a closed local port is refused only after about 2 s of
    retries. `"bace"`: a service answered `GET /session`. `"starting"`: the
    port is held and nothing answered in time, which is a service that has
    bound its socket and is still building the bench. `"other"`: something
    answered that is not a BACE service.
    """
    if port_free(host, port):
        return "free"
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/session",
                                    timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return "other"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return "starting"
        return "other"
    except (TimeoutError, socket.timeout):
        return "starting"
    except (ValueError, OSError):
        return "other"
    return "bace" if isinstance(body, dict) and "id" in body and "mode" in body else "other"


def port_free(host: str, port: int) -> bool:
    """Whether the service could bind the port now, by the same rule it binds by."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


# -- the service ------------------------------------------------------------------
def console_python(executable: str = sys.executable) -> str:
    """`python.exe` beside `pythonw.exe`: the service writes to a log, and
    under `pythonw` its standard streams would not exist."""
    folder, name = os.path.split(executable)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.isfile(candidate):
            return candidate
    return executable


def service_command(*, sim: bool, port: int, python: str | None = None) -> list[str]:
    command = [python or console_python(), "-m", "bace.service", "--port", str(port),
               "--ui", "ui"]
    if sim:
        return command + ["--sim"]
    return command + ["--rig", "rig.toml", "--run", "run.toml"]


def log_path(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return os.path.join(ROOT, "runs", "logs", f"service_{stamp}.log")


def start_service(command: list[str], log: str, *, cwd: str = ROOT) -> subprocess.Popen:
    """The service as a process of its own with no window, outliving the
    launcher, its output appended to `log`."""
    os.makedirs(os.path.dirname(log), exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(log, "ab") as out:
        return subprocess.Popen(command, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=flags)


def tail(path: str, lines: int = LOG_TAIL_LINES) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return "".join(fh.readlines()[-lines:]).strip()
    except OSError:
        return ""


# -- the console ------------------------------------------------------------------
def edge() -> str | None:
    for candidate in EDGE:
        path = os.path.expandvars(candidate)
        if os.path.isfile(path):
            return path
    return shutil.which("msedge")


def open_console(url: str) -> None:
    """The console in a window of its own: Edge's app mode, with no address
    bar or tabs; the default browser where Edge is absent."""
    browser = edge()
    if browser:
        subprocess.Popen([browser, f"--app={url}"], close_fds=True)
    else:
        webbrowser.open(url)


# -- what the operator sees -------------------------------------------------------
def alert(title: str, text: str) -> None:
    """A message box; under `pythonw` there is nowhere else to say it."""
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text, parent=root)
        root.destroy()
        return
    except Exception:                                   # noqa: BLE001 -- fall back
        pass
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}", file=sys.stderr)


class Splash:
    """A small window that says the service is starting. Without Tk it is
    absent and the wait is silent."""

    def __init__(self, text: str):
        self.root = None
        self.label = None
        try:
            import tkinter
            root = tkinter.Tk()
        except Exception:                               # noqa: BLE001 -- no display, no Tk
            return
        root.title("BACE")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        frame = tkinter.Frame(root, padx=28, pady=20)
        frame.pack()
        tkinter.Label(frame, text="BACE", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        self.label = tkinter.Label(frame, text=text, font=("Segoe UI", 10), justify="left")
        self.label.pack(anchor="w", pady=(6, 0))
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")
        # The operator may close it while the service starts: that only hides
        # the wait, and `close` must then not destroy it a second time.
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.update()
        self.root = root

    def say(self, text: str) -> None:
        if self.root is not None:
            self.label.configure(text=text)
            self.root.update()

    def pump(self) -> None:
        if self.root is not None:
            self.root.update()

    def close(self) -> None:
        root, self.root = self.root, None
        if root is not None:
            try:
                root.destroy()
            except Exception:                           # noqa: BLE001 -- already gone
                pass


# -- the sequence -----------------------------------------------------------------
def wait_for_service(proc: subprocess.Popen | None, *, port: int, timeout_s: float,
                     splash: Splash, clock=time.monotonic, sleep=time.sleep) -> str:
    """Poll until the service answers. Returns `"ready"`, `"exited"` (the
    process ended first) or `"timeout"`."""
    deadline = clock() + timeout_s
    while clock() < deadline:
        if proc is not None and proc.poll() is not None:
            return "exited"
        if probe(port=port) == "bace":
            return "ready"
        splash.pump()
        sleep(POLL_S)
    return "timeout"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bace.launcher",
                                 description="Start the BACE service if it is not "
                                             "running, then open the console.")
    ap.add_argument("--sim", action="store_true", help="the simulated rig")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    url = f"http://{HOST}:{a.port}/ui/"

    state = probe(port=a.port)
    if state == "bace":
        open_console(url)
        return 0
    if state == "other":
        alert("BACE", f"Port {a.port} is held by a program that is not the BACE service. "
                      f"Close it and start BACE again.")
        return 3

    proc, log = None, None
    splash = Splash("Starting the service and reading the instruments …")
    try:
        if state == "free":
            log = log_path()
            try:
                proc = start_service(service_command(sim=a.sim, port=a.port), log)
            except OSError as exc:
                splash.close()
                alert("BACE", f"The service could not be started: {exc}")
                return 2
        outcome = wait_for_service(proc, port=a.port, timeout_s=START_TIMEOUT_S, splash=splash)
    finally:
        splash.close()

    if outcome == "ready":
        open_console(url)
        return 0
    where = f"\n\nLog: {log}" if log else ""
    if outcome == "exited":
        alert("BACE", "The service stopped while starting.\n\n"
                      f"{tail(log) if log else ''}{where}")
        return 2
    alert("BACE", f"The service has not answered after {START_TIMEOUT_S:g} s. It may still "
                  f"be opening the instruments; start BACE again in a minute.{where}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
