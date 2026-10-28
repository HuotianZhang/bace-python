"""Provide real GPIB and simulated transports for the Lake Shore 331.

Both expose the same exchange interface. The bus owner must enforce one
caller. Internal locks keep each exchange intact, preventing commands and
replies from interleaving when the transport has concurrent callers."""
from __future__ import annotations

import logging
import threading
from typing import Optional

from . import protocol as p
from .config import Connection
from .simulator import Model331Simulator

log = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Report a session-open or message-exchange failure."""


class VisaTransport:
    """Exchange messages with the 331 through PyVISA over IEEE-488.

    Windows GPIB requires a vendor VISA layer such as NI-488.2 or Keysight
    IO Libraries. The pure Python `pyvisa-py` backend cannot drive the board."""

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

        # The Model 331 requires repeat addressing on the IEEE-488 controller.
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
        # Close only this session. `pyvisa.ResourceManager` is shared within the
        # process; closing it also closes the bench's scope, generators and source
        # meter. This transport does not own those sessions.
        try:
            self._inst.close()
        except Exception:  # pragma: no cover
            pass


class SimulatedTransport:
    """Expose the Model 331 simulator through the transport interface."""

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
    """Return a real GPIB transport or a simulated transport.

    `simulated` selects the model. `simulate_loop` chooses control loop 1
    for the current-source heater or 2 for the analog heater."""
    if simulated:
        log.info("using the simulator (loop %d); no instrument will be contacted",
                 simulate_loop)
        return SimulatedTransport(Model331Simulator(control_loop=simulate_loop))
    return VisaTransport(connection)
