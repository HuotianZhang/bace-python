"""Provide direct and console-backed Lake Shore 331 drivers.

`[temperature]` in `rig.toml` selects the owner.
`controller.DirectTemperatureController` opens GPIB in the service by default.
`console.ConsoleTemperatureController` uses the HTTP URL in `console` when
the separate controller console owns GPIB. Exactly one backend may be live;
competing sessions can read each other's replies."""
from __future__ import annotations

from ..protocols import TemperatureReading
from .config import Connection, Limits, Settings
from .console import (DEFAULT_CONSOLE, READ_TIMEOUT_S, WRITE_TIMEOUT_S,
                      ConsoleTemperatureController, TemperatureError)
from .controller import DirectTemperatureController, open_temperature_controller
from .instrument import Lakeshore331, SafetyError
from .transport import TransportError, open_transport

__all__ = [
    "DEFAULT_CONSOLE", "READ_TIMEOUT_S", "WRITE_TIMEOUT_S",
    "ConsoleTemperatureController", "DirectTemperatureController",
    "open_temperature_controller", "TemperatureError", "TemperatureReading",
    "Lakeshore331", "SafetyError", "TransportError", "open_transport",
    "Connection", "Limits", "Settings",
]
