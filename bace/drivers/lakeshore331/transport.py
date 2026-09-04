"""Byte pipes to the instrument: a real GPIB session, or the simulator.

VENDORED, 2026-09-03, from the 331 console project
(D:/TemperatureController, console/ls331/), unchanged. It is the device
truth; when this file and that project disagree, they have drifted and one
of them is wrong. "The console" in the prose below means whichever program
owns the GPIB session -- in this repository that is the BACE service, and
`controller.DirectTemperatureController` is the owner.

Both classes expose the same two methods, so nothing above this layer knows
which one it has.  Neither class enforces single-caller access - that is the
bus owner's job (see instrument.py's module docstring) - but both take an
internal lock so an accidental second caller fails loudly rather than
silently interleaving with a reply.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from . import protocol as p
from .config import Connection
from .simulator import Model331Simulator

log = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """The session could not be opened, or a message could not be exchanged."""


class VisaTransport:
    """PyVISA session to the 331 over IEEE-488.

    Requires a vendor VISA layer (NI-488.2 or Keysight IO Libraries) - the pure
    Python ``pyvisa-py`` backend cannot drive a GPIB board on Windows.
    """

    def __init__(self, connection: Connection) -> None:
        try:
            import pyvisa  # imported here so the rest of the package works without it
        except ImportError as exc:  # pragma: no cover - depends on the machine
            raise TransportError(
                "pyvisa is not installed; run 'pip install pyvisa' and make sure "
                "NI-488.2 or Keysight IO Libraries is installed as well"
            ) from exc

        self.connection = connection
        self._lock = threading.Lock()
        try:
            self._rm = pyvisa.ResourceManager()
            self._inst = self._rm.open_resource(connection.resource)
        except Exception as exc:  # pragma: no cover - depends on the machine
            raise TransportError(
                "could not open %s: %s" % (connection.resource, exc)
            ) from exc

        self._inst.timeout = connection.timeout_ms
        self._inst.read_termination = connection.read_termination
        self._inst.write_termination = connection.write_termination

        # The manual is explicit: "The Model 331 IEEE-488 Interface requires
        # that repeat addressing be enabled on the bus controller."
        if hasattr(self._inst, "enable_repeat_addressing"):
            try:
                self._inst.enable_repeat_addressing = True
            except Exception:  # pragma: no cover - non-GPIB resources
                log.warning("could not enable repeat addressing on %s", connection.resource)

    def write(self, message: str) -> None:
        p.check_message(message)
        with self._lock:
            log.debug(">> %s", message)
            try:
                self._inst.write(message)
            except Exception as exc:  # pragma: no cover
                raise TransportError("write %r failed: %s" % (message, exc)) from exc

    def query(self, message: str) -> str:
        p.check_message(message)
        with self._lock:
            log.debug(">> %s", message)
            try:
                reply = self._inst.query(message)
            except Exception as exc:  # pragma: no cover
                raise TransportError("query %r failed: %s" % (message, exc)) from exc
            log.debug("<< %s", reply.strip())
            return reply.strip()

    def close(self) -> None:
        # Only the session. `pyvisa.ResourceManager()` hands every caller in
        # the process the same cached instance, so closing it here closed the
        # bench harness's and the service's scope, generator and Keithley
        # sessions along with the 331 -- pass 2 of `bace.bench` reported all
        # four "not reachable" two seconds after identifying them
        # (2026-09-04). The console this was vendored from owned the only
        # ResourceManager in its process; here it never does.
        try:
            self._inst.close()
        except Exception:  # pragma: no cover
            pass


class SimulatedTransport:
    """The simulator dressed as a transport."""

    def __init__(self, simulator: Optional[Model331Simulator] = None) -> None:
        self.simulator = simulator if simulator is not None else Model331Simulator()
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        with self._lock:
            log.debug(">> %s", message)
            self.simulator.write(message)

    def query(self, message: str) -> str:
        with self._lock:
            log.debug(">> %s", message)
            reply = self.simulator.query(message)
            log.debug("<< %s", reply)
            return reply

    def close(self) -> None:
        pass


def open_transport(connection: Connection, simulated: bool = False,
                   simulate_loop: int = 1):
    """Open the real instrument, or the simulator when ``simulated`` is set.

    ``simulate_loop`` chooses which heater the simulated instrument reports:
    1 for the current-source output (Off/Low/Medium/High), 2 for the analog
    voltage output (Off/On).
    """
    if simulated:
        log.info("using the simulator (loop %d); no instrument will be contacted",
                 simulate_loop)
        return SimulatedTransport(Model331Simulator(control_loop=simulate_loop))
    return VisaTransport(connection)
