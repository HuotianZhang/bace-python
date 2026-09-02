"""The observers: threads that read a console beside a run and never touch VISA.

The 1918-C sits on a beam splitter and is owned by its own console
(`:8918`); the Lake Shore 331, when it is wired, by its own on `:8331`. So
reading either is an HTTP request and not a bus transaction, and that is
what lets a reading arrive *during* a bace scan (the design's R3·2 power
sparkline, and the temperature card's "294.8 K · 331 reads" while a scan
runs): the worker thread holds the instruments, these threads hold nothing,
and the only place they meet is the session's event stream. They are the
concurrent things the bench-lock rule allows, and they stay the only ones: a
monitor that reached for a VISA resource would be a second thread on the bus.

Threads rather than asyncio tasks so the same objects work under the FastAPI
loop and in a test with no loop at all; the interval is a `threading.Event`
wait, so `stop()` returns at once rather than after the interval, and a test
can run one at 10 ms. A console that stops answering is reported once, as a
`Verdict(warn, "<name>.console")`, and the monitor keeps trying -- the
console being restarted is the ordinary case, and a monitor that gave up
would have to be restarted by hand afterwards, which nobody remembers to do.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..experiment import events as E
from .rigs import power_reading, read_temperature_console

MAX_INTERVAL_S = 3600.0
MIN_INTERVAL_S = 0.01


class Monitor:
    """Read something every `interval_s` on a thread of its own and hand
    each reading to `emit`. Subclasses say what (`read`) and how the last
    reading is summarised (`last_info`)."""

    name = "monitor"

    def __init__(self, *, emit: Callable[[E.Event], None], interval_s: float = 1.0,
                 console: str = ""):
        interval_s = float(interval_s)
        if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
            raise ValueError(f"interval_s must be between {MIN_INTERVAL_S:g} and "
                             f"{MAX_INTERVAL_S:g} s, not {interval_s:g}")
        self.emit = emit
        self.interval_s = interval_s
        self.console = console
        self.started_at: float | None = None
        self.readings = 0
        self.failures = 0
        self.last: E.Event | None = None
        self.last_at: float | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned = False

    # -- what a subclass supplies ---------------------------------------
    def read(self) -> E.Event:
        """One reading, or raise. Run on the monitor's own thread."""
        raise NotImplementedError

    def last_info(self) -> dict | None:
        """The last reading as the `/monitors` entry shows it."""
        return None

    # -- lifecycle ------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name=f"bace-{self.name}-monitor",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Ask the thread to end and wait for it. Returns whether it ended."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_s)
            return not thread.is_alive()
        return True

    def info(self) -> dict:
        return {"name": self.name, "running": self.running, "interval_s": self.interval_s,
                "started_at": self.started_at, "readings": self.readings,
                "failures": self.failures, "console": self.console or None,
                "last": self.last_info(), "last_at": self.last_at,
                "last_error": self.last_error}

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._read_once()
            if self._stop.wait(self.interval_s):
                return

    def _read_once(self) -> None:
        try:
            ev = self.read()
        except Exception as exc:                            # noqa: BLE001 -- keep trying
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            if not self._warned:
                self._warned = True
                self._emit(E.Verdict(
                    level="warn", code=f"{self.name}.console",
                    text=f"the {self.what} stopped answering; the monitor keeps trying "
                         f"every {self.interval_s:g} s ({self.last_error})",
                    data={"console": self.console, "error": self.last_error}))
            return
        self._warned = False
        self.last_error = None
        self.readings += 1
        self.last = ev
        self.last_at = time.time()
        self._emit(ev)

    what = "console"

    def _emit(self, ev: E.Event) -> None:
        try:
            self.emit(ev)
        except Exception as exc:                            # noqa: BLE001 -- a broken sink is not a broken meter
            self.last_error = f"emit: {type(exc).__name__}: {exc}"


class PowerMonitor(Monitor):
    """Read the power meter every `interval_s` and emit `PowerReading`s."""

    name = "power"
    what = "1918-C console"

    def __init__(self, meter: Any, *, emit: Callable[[E.Event], None],
                 interval_s: float = 1.0, console: str = "", samples: int = 1):
        if meter is None:
            raise ValueError("no power meter to monitor: the 1918-C console is not "
                             "answering, or this bench has none")
        super().__init__(emit=emit, interval_s=interval_s, console=console)
        self.meter = meter
        self.samples = max(1, int(samples))

    def read(self) -> E.Event:
        return power_reading(self.meter, samples=self.samples)

    def last_info(self) -> dict | None:
        last = self.last
        if not isinstance(last, E.PowerReading):
            return None
        return {"watts": last.watts, "trustworthy": last.trustworthy,
                "wavelength_nm": last.wavelength_nm}


class TemperatureMonitor(Monitor):
    """Read the 331 every `interval_s` and emit `TemperatureRead`s
    (`source = "console"`, or `"simulated"` under `--sim`). The console is
    `RigConfig.temperature_console`; with it empty there is nothing to
    monitor and the constructor says so -- naming the console in rig.toml is
    the lab's step, not this one's. `controller` is the rig's attached
    driver when it has one, so a `--sim` bench monitors its own stand-in and
    the real bench the very object a settle drives; without one (the console
    was silent at start-up) the URL is read, so a console started later is
    seen without a restart."""

    name = "temperature"
    what = "331 console"

    def __init__(self, console: str, *, emit: Callable[[E.Event], None],
                 interval_s: float = 5.0, controller: Any = None):
        if not console:
            raise ValueError("no temperature console: [temperature] console in rig.toml "
                             "is empty, so the 331 is not wired and there is nothing "
                             "to monitor")
        super().__init__(emit=emit, interval_s=interval_s, console=console)
        self.controller = controller

    def read(self) -> E.Event:
        t = read_temperature_console(self.controller or self.console)
        if t.get("kelvin") is None:
            raise RuntimeError(t.get("error") or t.get("status_text")
                               or "the console answered with no temperature")
        return E.TemperatureRead(kelvin=float(t["kelvin"]), setpoint_k=t.get("setpoint_k"),
                                 in_band=None, source=str(t.get("source") or "console"))

    def last_info(self) -> dict | None:
        last = self.last
        if not isinstance(last, E.TemperatureRead):
            return None
        return {"kelvin": last.kelvin, "setpoint_k": last.setpoint_k, "source": last.source}


__all__ = ["Monitor", "PowerMonitor", "TemperatureMonitor", "MAX_INTERVAL_S", "MIN_INTERVAL_S"]
