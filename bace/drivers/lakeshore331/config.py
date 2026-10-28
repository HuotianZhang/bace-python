"""Define connection settings and cryostat safety limits for the Lake Shore 331.

The 350 K ceiling and other limits are enforced in the owning process so a
stale console page or second window cannot bypass them. Import creates no
files; the service owns journal paths through `bace.service.journal`."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limits:
    """Define temperature, heater and watchdog limits for this cryostat."""

    #: Refuse any setpoint above this.  Set for the current cryostat.
    max_setpoint_k: float = 350.0
    min_setpoint_k: float = 0.0

    #: The service may always lower the heater range; raising it is a
    #: deliberate, confirmed action, never automatic.
    allow_automatic_range_increase: bool = False

    #: Consecutive bad `RDGST?` or `HTRST?` polls before heater disable.
    max_consecutive_faults: int = 3

    #: Ramp-rate limits accepted by the instrument's `RAMP` command, in K/min.
    min_ramp_k_per_min: float = 0.1
    max_ramp_k_per_min: float = 100.0


@dataclass(frozen=True)
class Connection:
    """Define the instrument address and polling settings.

    This bench uses IEEE-488 address 7; the factory address is 12. The front
    panel's Interface key shows the address. `[temperature] address` in
    `rig.toml` also selects the GPIB board index."""

    resource: str = "GPIB0::7::INSTR"
    timeout_ms: int = 3000
    read_termination: str = "\r\n"
    write_termination: str = "\r\n"


@dataclass(frozen=True)
class Polling:
    fast_interval_s: float = 1.0   # KRDG? A, KRDG? B, heater output, RDGST? A
    slow_interval_s: float = 5.0   # SETP?, RANGE?, RAMPST?, TUNEST?, HTRST?


@dataclass(frozen=True)
class Settings:
    limits: Limits = field(default_factory=Limits)
    connection: Connection = field(default_factory=Connection)
    polling: Polling = field(default_factory=Polling)

    #: Select the driven control loop: 1 controls the main heater; 2 controls
    #: analog voltage output. Both can be active. Selecting the wrong loop
    #: can silently leave the intended heater unchanged.
    control_loop: int = 1

    #: Select the sensor read by the experiment. The control input comes from
    #: instrument `CSET` read-back rather than this setting.
    display_input: str = "A"


DEFAULT_SETTINGS = Settings()
