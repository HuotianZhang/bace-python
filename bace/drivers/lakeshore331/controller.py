r"""The 331 driven from this process: the service is the owner.

The console in `D:\TemperatureController` was written first, and this package's
`console.py` talks to it. That arrangement assumed the console was running.
It is not, on this rig -- nobody starts it -- and a driver whose precondition
is a program nobody starts is a driver that never works. So the instrument
code moved in here (`instrument.py`, `protocol.py`, `transport.py`,
`simulator.py`, vendored from that project) and this class opens the GPIB
session itself.

**The one-owner rule did not go away; it moved inside.** The 331 answers only
the last query it received and cannot arbitrate between callers, so two
callers sharing a session eventually read each other's replies. In the console
that was solved by being a separate process with a bus-owner thread. Here it
is solved by `_LOCK`: every exchange with the instrument -- the monitor's
poll, a temperature node's setpoint, the bench read-back -- passes through it,
the same way `service.rigs._POWER_LOCK` serialises the meter. What must not
happen now is a *second* owner appearing: if the console is ever started
beside the service, both hold `GPIB0::7` and both read the other's replies.
`available()` cannot see that, so `rig.toml` documents it and
`bace.bench.checks` says it out loud.

What the console kept and this keeps:

* the **350 K ceiling** (`config.Limits`), refused with `TemperatureError`
  and never clamped -- quietly giving 350 K for 400 K hides the mistake;
* the **heater range, PID, ramp and loop wiring** read, never driven: the
  front panel owns them, `discover()` reads back how the box is wired, and
  a run gets whatever is set. Raising the heater range needs an explicit
  confirm that this class never passes.

What is different from the console, deliberately: `note()` has no separate
audit file. The console kept one because it was the only program on the
instrument; here the session journal is where an operator looks, so a note
goes to the `audit` hook the service installs (`service.temperature`) and to
the log, not to a second file that nobody would read.
"""
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
"""One owner, now a lock rather than a process. Re-entrant because `read()`
makes several exchanges and a caller may already hold it."""

__all__ = ["DirectTemperatureController", "open_temperature_controller"]


class DirectTemperatureController:
    """`TemperatureController` over this process's own GPIB session.

    Constructing it opens the session and runs `discover()`, so a controller
    that exists is one that answered; anything else raises and the bench
    records the reason on the card rather than failing the service.
    """

    def __init__(self, resource: str = Connection.resource, *,
                 timeout_ms: int = Connection.timeout_ms,
                 max_setpoint_k: float = 350.0,
                 control_loop: int = 1,
                 simulate: bool = False,
                 audit: Callable[[str], None] | None = None):
        settings = Settings(limits=Limits(max_setpoint_k=max_setpoint_k),
                            connection=Connection(resource=resource,
                                                  timeout_ms=timeout_ms),
                            control_loop=control_loop)
        self.settings = settings
        self.resource = resource
        self.audit = audit
        self.last: TemperatureReading | None = None
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
        """Every write the instrument layer makes, for the audit hook."""
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
        """The shape `ConsoleTemperatureController.probe` returns, so the
        bench can treat the two the same. None when the instrument did not
        answer at all."""
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
        """One poll: the control input's temperature, the setpoint, whether a
        ramp is still walking it, and the heater range.

        A single bus error makes this `connected=False` with the reason in
        `status_text`, rather than raising -- the same contract the console
        client has, so the monitor and the bench card need no branch. Only a
        controller that cannot even be asked raises.
        """
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

    def _elapsed(self) -> float | None:
        """Seconds since this controller last wrote a setpoint. The console
        reported the same thing from its own clock; a temperature node uses
        it only for display, never to decide settling."""
        if self._setpoint_written_at is None:
            return None
        return time.monotonic() - self._setpoint_written_at

    # -- writing ----------------------------------------------------------
    def set_setpoint(self, kelvin: float) -> float:
        """Write the control setpoint and read back what the instrument holds.

        A setpoint above the ceiling raises `TemperatureError` with
        `refused=True` and the safety layer's own sentence -- the same shape
        the console's HTTP 403 produced, so `service.temperature` and every
        verdict that renders `console_message` keep working unchanged.
        Nothing is clamped.
        """
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
        """A line into the audit trail. Never raises: a log mark must not
        stop a run. Returns whether anything recorded it."""
        try:
            log.info("331 note: %s", text)
            if self.audit is not None:
                self.audit(str(text))
            return True
        except Exception:                                   # noqa: BLE001
            return False


def open_temperature_controller(rig_config: Any, *, simulate: bool = False):
    """The controller a rig should use, given `[temperature]` in rig.toml.

    Three states. Direct by default -- this process opens `address` and owns
    it. A non-empty `console` is the explicit escape hatch for the case the
    direct path exists to remove: somebody is running the 331 console and
    wants the service to go through it instead of fighting for the bus. Both
    empty means there is no cryostat on this bench, which is a configuration
    and not a failure -- the caller decides what to do with the refusal, and
    `service.rigs.Bench.build_real` records nothing at all.
    """
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
