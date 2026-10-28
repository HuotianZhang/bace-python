"""Drive the Lake Shore 331 from the service's GPIB session.

The 331 answers only the last query received. `_LOCK` serialises monitor
polls, setpoint writes and read-back to prevent callers reading each other's
replies. The separate 331 console must not hold a second session on the
same address. Liveness checks cannot detect that competing owner.

The safety layer refuses setpoints above 350 K without clamping. `_watch`
counts consecutive control-sensor or heater faults and sends `RANGE 0` at
`max_consecutive_faults`. A failed sensor can otherwise leave the heater
energised for hours during a run. Heater range, PID, ramp and wiring are
read from the instrument; normal runs change only the setpoint. Raising
heater range requires confirmation that this driver never supplies.
The watchdog can lower range to zero.

`note` records through the service's `audit` hook and log. The session
journal holds the record without a second audit file."""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Callable

from . import protocol as p
from .config import Connection, Limits, Settings
from .console import TemperatureError
from .instrument import Lakeshore331, SafetyError
from .transport import TransportError, open_transport
from ..protocols import TemperatureReading

log = logging.getLogger(__name__)

_LOCK = threading.RLock()
"""Serialise all instrument exchanges under one owner.

The lock is reentrant because a read performs several exchanges and its
caller can also hold the lock."""

__all__ = ["DirectTemperatureController", "open_temperature_controller"]


class DirectTemperatureController:
    """Implement temperature control through this process's GPIB session.

    Construction opens the session and calls `Lakeshore331.discover`.
    Failure raises so the bench can report an unavailable instrument while
    keeping the service running."""

    def __init__(self, resource: str = Connection.resource, *,
                 timeout_ms: int = Connection.timeout_ms,
                 max_setpoint_k: float = 350.0,
                 control_loop: int = 1,
                 simulate: bool = False,
                 watchdog: bool = True,
                 audit: Callable[[str], None] | None = None):
        settings = Settings(limits=Limits(max_setpoint_k=max_setpoint_k),
                            connection=Connection(resource=resource,
                                                  timeout_ms=timeout_ms),
                            control_loop=control_loop)
        self.settings = settings
        self.resource = resource
        self.audit = audit
        self.watchdog = watchdog
        self.last: TemperatureReading | None = None
        self.heater_cut: str | None = None
        """The reason the watchdog last cut the heater, once it has."""
        self._faults = 0
        self._setpoint_written_at: float | None = None
        try:
            self._transport = open_transport(settings.connection, simulated=simulate,
                                             simulate_loop=control_loop)
        except TransportError as exc:
            raise TemperatureError(str(exc)) from exc
        self.device = Lakeshore331(self._transport, settings,
                                   audit=self._on_write)
        try:
            with _LOCK:
                self.discovery = self.device.discover(control_loop)
        except Exception as exc:                            # noqa: BLE001
            self.close()
            raise TemperatureError(
                f"the 331 at {resource} did not answer its configuration "
                f"({type(exc).__name__}: {exc}); check the instrument, the GPIB "
                "cable and that nothing else holds the bus") from exc

    # -- plumbing ---------------------------------------------------------
    def _on_write(self, message: str, reason: str) -> None:
        """Send an instrument write and its reason to the audit hook; return `None`."""
        text = f"331 <- {message}" + (f" ({reason})" if reason else "")
        log.info("%s", text)
        if self.audit is not None:
            try:
                self.audit(text)
            except Exception:                               # noqa: BLE001
                pass            # an audit hook must never break a write

    def close(self) -> None:
        try:
            self._transport.close()
        except Exception:                                   # noqa: BLE001
            pass

    # -- liveness ---------------------------------------------------------
    def probe(self) -> dict | None:
        """Poll the instrument and return a console-shaped status object or `None`.

        The matching interface lets bench checks use direct and HTTP adapters
        without a branch for the transport."""
        try:
            r = self.read()
        except TemperatureError:
            return None
        return {"connected": r.connected, "max_setpoint_k": r.max_setpoint_k,
                "control_temperature": r.kelvin, "setpoint": r.setpoint_k,
                "ramping": r.ramping, "heater_range": r.heater_range}

    def available(self) -> bool:
        state = self.probe()
        return state is not None and bool(state.get("connected"))

    # -- reading ----------------------------------------------------------
    def read(self) -> TemperatureReading:
        """Poll temperature and setpoint in K, ramp state and heater range.

        Return `TemperatureReading`. A bus error sets `connected=False` and
        `status_text`, matching the console adapter. Only an unavailable
        controller object raises; callers can share one status interface."""
        try:
            with _LOCK:
                channel = self.discovery.control_input if self.discovery else "A"
                kelvin = self.device.kelvin(channel)
                setpoint = self.device.setpoint()
                ramping = self.device.is_ramping()
                heater = int(self.device.heater_range())
                status = self.device.reading_status(channel)
        except (TransportError, p.ProtocolError, ValueError) as exc:
            r = TemperatureReading(
                kelvin=float("nan"), setpoint_k=None, ramping=None,
                heater_range=None, connected=False,
                status_text=f"{type(exc).__name__}: {exc}",
                elapsed_s=self._elapsed(),
                max_setpoint_k=self.settings.limits.max_setpoint_k)
            self.last = r
            return r
        self._watch(self._fault_text(status))
        r = TemperatureReading(
            kelvin=float(kelvin),
            setpoint_k=None if setpoint is None else float(setpoint),
            ramping=None if ramping is None else bool(ramping),
            heater_range=heater,
            connected=not math.isnan(float(kelvin)),
            status_text="" if status.ok else str(status),
            elapsed_s=self._elapsed(),
            max_setpoint_k=self.settings.limits.max_setpoint_k)
        self.last = r
        return r

    # -- the watchdog -----------------------------------------------------
    def _fault_text(self, status: Any) -> str | None:
        """Return a control-sensor or heater fault reason, or `None`.

        Both `RDGST?` and `HTRST?` are required. A working sensor alone does not
        rule out an open or shorted heater load, and must not reset the watchdog
        counter while that fault persists."""
        if not getattr(status, "ok", True):
            return f"control sensor reports {status}"
        try:
            fault = self.device.heater_fault()
        except (TransportError, p.ProtocolError, ValueError):
            return None     # a bus that will not answer is the read's problem
        if fault is not p.HeaterFault.OK:
            return "heater reports " + fault.name.replace("_", " ").lower()
        return None

    def poll_faults(self) -> str | None:
        """Query sensor and heater faults without a full state read; return the reason.

        `bace.service.rigs.Bench.sleeper` calls this between operations during
        a long job. The monitor cannot poll while the worker holds the bus.
        Without worker polls, a fault after settling can leave a heater
        energised and unobserved for hours. An unavailable poll returns `None`
        without raising through the run."""
        try:
            with _LOCK:
                fault = self.device.check_faults()
        except Exception:                                   # noqa: BLE001
            return None     # never raise into a run for want of a poll
        self._watch(fault)
        return fault

    def _watch(self, fault: str | None) -> None:
        """Cut heater output after `max_consecutive_faults` consecutive bad polls.

        A clean poll resets the counter. Monitor and settle reads feed the
        watchdog; `poll_faults` feeds it while a run owns the bus. A sensor or
        heater fault must not leave the heater energised through hours of
        measurement. `RANGE 0` disables output in every configuration. A failed
        stop write is logged without raising through the read or aborting a run."""
        if not self.watchdog:
            return
        if fault is None:
            self._faults = 0
            return
        self._faults += 1
        if self._faults < self.settings.limits.max_consecutive_faults:
            return
        reason = f"{fault} on {self._faults} consecutive reads"
        self._faults = 0
        try:
            with _LOCK:
                self.device.emergency_stop(reason)
            self.heater_cut = reason
            log.error("heater cut: %s", reason)
            if self.audit is not None:
                self.audit(f"331 heater cut by the watchdog: {reason}")
        except Exception as exc:                            # noqa: BLE001
            self.heater_cut = f"{reason} (RANGE 0 failed: {exc})"
            log.error("heater cut FAILED: %s", self.heater_cut)

    def _elapsed(self) -> float | None:
        """Return elapsed s after this controller's last setpoint write.

        Temperature nodes use it for display, not to decide settling."""
        if self._setpoint_written_at is None:
            return None
        return time.monotonic() - self._setpoint_written_at

    # -- writing ----------------------------------------------------------
    def set_setpoint(self, kelvin: float) -> float:
        """Send the setpoint in K and return the instrument's read-back in K.

        An out-of-limit request raises `TemperatureError` with `refused=True`
        and the safety layer's message. This matches the console adapter's
        refusal interface; no value is clamped."""
        try:
            with _LOCK:
                self.device.set_setpoint(float(kelvin),
                                         reason="set by the BACE service")
                self._setpoint_written_at = time.monotonic()
                confirmed = self.device.setpoint()
        except SafetyError as exc:
            raise TemperatureError(
                f"{exc} (refused by the 331 driver's safety limits, not a "
                "connection problem)",
                refused=True, status=403, console_message=str(exc)) from exc
        except (TransportError, p.ProtocolError, ValueError) as exc:
            raise TemperatureError(
                f"writing the setpoint to the 331 at {self.resource} failed "
                f"({type(exc).__name__}: {exc})") from exc
        return float(confirmed)

    def note(self, text: str) -> bool:
        """Record a journal note and return whether an audit sink accepts it.

        An audit failure never raises because logging must not stop a run."""
        try:
            log.info("331 note: %s", text)
            if self.audit is not None:
                self.audit(str(text))
            return True
        except Exception:                                   # noqa: BLE001
            return False


def open_temperature_controller(rig_config: Any, *, simulate: bool = False):
    """Build the temperature driver selected by `[temperature]` in `rig.toml`.

    By default this process owns `address`. A nonempty `console` selects the
    HTTP adapter when the separate console owns GPIB. Both empty means no
    cryostat is configured; the caller handles the refusal."""
    console = getattr(rig_config, "temperature_console", "") or ""
    address = getattr(rig_config, "temperature_address", Connection.resource) or ""
    if console:
        from .console import ConsoleTemperatureController
        return ConsoleTemperatureController(console)
    if not address:
        raise TemperatureError(
            "no 331 on this bench: [temperature] address and [temperature] "
            "console are both empty in rig.toml")
    return DirectTemperatureController(
        address,
        max_setpoint_k=getattr(rig_config, "temperature_max_setpoint_k", 350.0),
        control_loop=getattr(rig_config, "temperature_control_loop", 1),
        simulate=simulate)
