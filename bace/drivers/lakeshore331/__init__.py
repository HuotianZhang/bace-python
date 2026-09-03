r"""Lake Shore 331 temperature controller.

Two ways to reach the same instrument, and the choice is `[temperature]` in
rig.toml:

* `controller.DirectTemperatureController` -- **the default.** This process
  opens `GPIB0::7::INSTR` and owns it. The instrument code under here
  (`protocol`, `transport`, `instrument`, `simulator`) is vendored from the
  `D:\TemperatureController` console project so the service needs no second
  program running to take a temperature.
* `console.ConsoleTemperatureController` -- the escape hatch, used only when
  `[temperature] console` names a URL. That console owns the GPIB session
  when it runs, and then the service must go through it rather than open a
  second session on the bus.

Exactly one of the two may be live at a time, because exactly one owner of
the bus may exist. That is the whole reason this module has two halves.
"""
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
