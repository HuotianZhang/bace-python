"""Site configuration and safety limits for the 331.

Vendored from the `TemperatureController` console project, with its console
specifics dropped: this copy has no log directory and creates nothing at
import, because the service owns those decisions (`bace/service/journal.py`).
What is kept is the part that is about *this cryostat* rather than about that
program -- the 350 K ceiling above all.

Every limit here is enforced in this process, never in the browser or the UI:
a stale tab or a second window must not be able to bypass them.  Values are
deliberately boring Python so they can be reviewed at a glance.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Limits:
    """Hard bounds for this cryostat."""

    #: Refuse any setpoint above this.  Set for the current cryostat.
    max_setpoint_k: float = 350.0
    min_setpoint_k: float = 0.0

    #: The service may always lower the heater range; raising it is a
    #: deliberate, confirmed action, never automatic.
    allow_automatic_range_increase: bool = False

    #: Consecutive bad RDGST?/HTRST? polls before the watchdog cuts the heater.
    max_consecutive_faults: int = 3

    #: Ramp rates the instrument itself accepts (manual: RAMP).
    min_ramp_k_per_min: float = 0.1
    max_ramp_k_per_min: float = 100.0


@dataclass(frozen=True)
class Connection:
    """How to reach the instrument.

    This 331 is set to IEEE-488 address 7.  (The manual's factory default is
    12, so that is what a fresh instrument would answer on; ours is not fresh.)
    The address is shown on the front panel under the Interface key.  It comes
    from `[temperature] address` in rig.toml; change the board index there if
    the GPIB card is not board 0.
    """

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

    #: Which control loop to drive.  Loop 1 is the main loop and the heater
    #: output; Loop 2 is the analog voltage output.  This is a choice, not
    #: something to infer from the instrument - both loops can be active at
    #: once, and writing to the wrong one fails silently.
    control_loop: int = 1

    #: Sensor the experiment cares about.  The *control* input is not set here:
    #: it is read back from the instrument's own CSET configuration.
    display_input: str = "A"


DEFAULT_SETTINGS = Settings()
