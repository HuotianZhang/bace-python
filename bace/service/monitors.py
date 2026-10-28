"""Reads power and temperature on observer threads beside worker jobs.

The power meter uses USB when this process owns it, or HTTP through its
console. Neither path uses VISA, so readings can arrive during a scan.
`rigs._METER_LOCK` and the driver's lock serialise reads with the worker
(docs/service-contract.md section 8).

The 331 uses GPIB when this process owns it. Its monitor acquires the
worker's `bus` lock without blocking and holds it for the complete read.
A busy lock skips the tick; a skip is not a failure. With an HTTP console,
no bus lock is needed. Testing `worker.idle` is unsafe: a job can start
between the test and a multi-query read. Holding the lock prevents overlap
in both directions. A job waits for any read in flight to complete, which
takes a few tens of milliseconds as measured on this bench.

A pipeline holds the bus throughout its job. During a long subtree the
temperature reading can become stale. `skipped` on `GET /monitors` exposes
why. The worker supplies readings at safe points during temperature settle
and operator pauses, through `Job.wait_for_operator`'s `on_poll`. Continuous
readings inside other modules require safe points in the measurement path.

Threads work with both the asyncio service and tests without an event loop.
The interval uses `threading.Event.wait`, so `stop` need not wait for the
interval to expire and tests can use a 10 ms interval. An unresponsive
instrument or console emits one `Verdict(warn, "<name>.console")` while the
monitor keeps trying. Recovery then requires no manual monitor restart.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..experiment import events as E
from .rigs import power_reading, read_temperature_console

MAX_INTERVAL_S = 3600.0
MIN_INTERVAL_S = 0.01

TEMPERATURE_MONITOR_S = 5.0
"""Sets the default 331 monitor interval in seconds.
It matches `temperature.TEMPERATURE_POLL_S` and the console's slow poll,
so idle and settle readings arrive at the same rate. Five seconds is
sufficient for a cryostat and limits bus contention; the power monitor's
1 s interval is unnecessary here (docs/service-contract.md section 8).
"""


class Monitor:
    """Reads on a dedicated thread every `interval_s` and calls `emit`.

    Subclasses implement `read` and summarise the result in `last_info`.
    """

    name = "monitor"

    def __init__(self, *, emit: Callable[[E.Event], None], interval_s: float = 1.0,
                 console: str = "", bus: Any = None):
        interval_s = float(interval_s)
        if not MIN_INTERVAL_S <= interval_s <= MAX_INTERVAL_S:
            raise ValueError(f"interval_s must be between {MIN_INTERVAL_S:g} and "
                             f"{MAX_INTERVAL_S:g} s, not {interval_s:g}")
        self.emit = emit
        self.interval_s = interval_s
        self.console = console
        self.bus = bus
        """Protects reads of an instrument on the worker's bus.
        `None` denotes an instrument off that bus. A nonblocking acquire spans
        the whole read, so a tick has exclusive bus access or is skipped.
        """
        self.skipped = 0
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
        """Returns one reading or raises on the monitor thread."""
        raise NotImplementedError

    def last_info(self) -> dict | None:
        """Returns the last reading for the `/monitors` entry."""
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
        """Requests thread shutdown, waits and returns whether it ends."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_s)
            return not thread.is_alive()
        return True

    def info(self) -> dict:
        return {"name": self.name, "running": self.running, "interval_s": self.interval_s,
                "started_at": self.started_at, "readings": self.readings,
                "failures": self.failures, "skipped": self.skipped,
                "console": self.console or None,
                "last": self.last_info(), "last_at": self.last_at,
                "last_error": self.last_error}

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.bus is None:
                self._read_once()
            elif self.bus.acquire(blocking=False):
                try:
                    self._read_once()
                finally:
                    self.bus.release()
            else:
                self.skipped += 1
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
    """Reads the power meter every `interval_s` and emits `PowerReading`."""

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
    """Reads the 331 every `interval_s` and emits `TemperatureRead`.

    `source` is `instrument`, `console` or `simulated`. `controller` is
    the attached driver, usually the GPIB owner also used by settle.
    `console` supplies an HTTP URL when no driver is attached, so a console
    can become available without a service restart. With neither, the
    constructor refuses to start because the bench has no 331.

    Every driver exchange uses `lakeshore331.controller._LOCK`.
    Direct GPIB reads also require the worker's bus lock; HTTP reads stay
    off the bus (docs/service-contract.md section 8).
    """

    name = "temperature"
    what = "331"

    def __init__(self, console: str, *, emit: Callable[[E.Event], None],
                 interval_s: float = 5.0, controller: Any = None,
                 bus: Any = None):
        if not console and controller is None:
            raise ValueError("no 331 on this bench: nothing is attached at "
                             "[temperature] address and [temperature] console is "
                             "empty, so there is nothing to monitor")
        super().__init__(emit=emit, interval_s=interval_s, console=console,
                         bus=bus)
        self.controller = controller

    def read(self) -> E.Event:
        t = read_temperature_console(self.controller or self.console)
        if t.get("kelvin") is None:
            raise RuntimeError(t.get("error") or t.get("status_text")
                               or "the console answered with no temperature")
        return E.TemperatureRead(kelvin=float(t["kelvin"]), setpoint_k=t.get("setpoint_k"),
                                 in_band=None,
                                 source=str(t.get("source") or "instrument"))

    def last_info(self) -> dict | None:
        last = self.last
        if not isinstance(last, E.TemperatureRead):
            return None
        return {"kelvin": last.kelvin, "setpoint_k": last.setpoint_k, "source": last.source}


__all__ = ["Monitor", "PowerMonitor", "TemperatureMonitor", "MAX_INTERVAL_S", "MIN_INTERVAL_S"]
