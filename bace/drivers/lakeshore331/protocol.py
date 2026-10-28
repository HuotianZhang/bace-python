"""Build commands and parse replies for the Lake Shore Model 331.

This module performs no I/O, so command validation and reply parsing can be
tested without a cryostat. The owning driver serialises all exchanges:
the instrument replies only to its last query."""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Sequence, Tuple

#: The instrument's default terminator is CR/LF.
TERMINATOR = "\r\n"

#: The instrument accepts at most 64 characters per communication and one
#: query. It replies only to the last query received.
MAX_MESSAGE_CHARS = 64


class ProtocolError(ValueError):
    """Report an invalid reply or a message that breaches protocol limits."""


# --------------------------------------------------------------------------
# Enumerations, straight from the manual's parameter tables
# --------------------------------------------------------------------------

class Units(enum.IntEnum):
    """Represent the `CSET` temperature-unit codes."""

    KELVIN = 1
    CELSIUS = 2
    SENSOR = 3


class HeaterDisplay(enum.IntEnum):
    """Represent the `CSET` current or power heater-display codes."""

    CURRENT = 1
    POWER = 2


class HeaterRange(enum.IntEnum):
    """Represent the `RANGE` settings for the main heater.

    Full-scale power depends on resistance: high range gives 50 W at
    50 ohm, 25 W at 25 ohm and 10 W at 10 ohm. `full_scale_watts` applies
    the current-source scaling."""

    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class ControlMode(enum.IntEnum):
    """Represent the `CMODE` control modes."""

    MANUAL_PID = 1
    ZONE = 2
    OPEN_LOOP = 3
    AUTOTUNE_PID = 4
    AUTOTUNE_PI = 5
    AUTOTUNE_P = 6


class AnalogMode(enum.IntEnum):
    """Represent `ANALOG` modes; `LOOP` drives control loop 2."""

    OFF = 0
    INPUT = 1
    MANUAL = 2
    LOOP = 3


class HeaterFault(enum.IntEnum):
    """Represent `HTRST?` heater fault codes."""

    OK = 0
    OPEN_LOAD = 1
    SHORT = 2


class TuningState(enum.IntEnum):
    """Represent `TUNEST?` autotune state codes."""

    IDLE = 0
    TUNING = 1


#: Full-scale heater power in W at the tabulated resistances in ohm.
_FULL_SCALE_W = {
    10.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.100, HeaterRange.MEDIUM: 1.0, HeaterRange.HIGH: 10.0},
    25.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.250, HeaterRange.MEDIUM: 2.5, HeaterRange.HIGH: 25.0},
    50.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.500, HeaterRange.MEDIUM: 5.0, HeaterRange.HIGH: 50.0},
}


def full_scale_watts(rng: HeaterRange, heater_ohms: float = 50.0) -> float:
    """Return full-scale heater power in W for resistance `heater_ohms` in ohm.

    The instrument tables cover 10, 25 and 50 ohm. Other resistances scale
    from the 50 ohm column, as required for a current-source heater output."""
    table = _FULL_SCALE_W.get(float(heater_ohms))
    if table is not None:
        return table[HeaterRange(rng)]
    return _FULL_SCALE_W[50.0][HeaterRange(rng)] * (float(heater_ohms) / 50.0)


# --------------------------------------------------------------------------
# Reading status
# --------------------------------------------------------------------------

#: RDGST? bit weights.  A "000" reply means the reading is valid.
READING_STATUS_FLAGS: Sequence[Tuple[int, str]] = (
    (1, "invalid reading"),
    (16, "temperature under-range"),
    (32, "temperature over-range"),
    (64, "sensor units zero"),
    (128, "sensor units over-range"),
)


@dataclass(frozen=True)
class ReadingStatus:
    """Hold a decoded `RDGST?` sensor status."""

    raw: int

    @property
    def ok(self) -> bool:
        return self.raw == 0

    @property
    def flags(self) -> Tuple[str, ...]:
        return tuple(name for weight, name in READING_STATUS_FLAGS if self.raw & weight)

    def __str__(self) -> str:
        return "valid" if self.ok else ", ".join(self.flags) or "unknown fault (%d)" % self.raw


# --------------------------------------------------------------------------
# Configuration read back from the instrument
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LoopConfig:
    """Hold a `CSET?` reply describing control-loop wiring.

    The console reads this configuration but never writes it. Wiring stays
    on the front panel so the display describes the actual instrument."""

    loop: int
    input_channel: str
    units: Units
    powerup_enable: bool
    display: HeaterDisplay


@dataclass(frozen=True)
class AnalogConfig:
    """Hold an `ANALOG?` reply describing the analog voltage output.

    `mode is AnalogMode.LOOP` identifies control loop 2's heater output."""

    bipolar: bool
    mode: AnalogMode
    input_channel: str
    source: int
    high_value: float
    low_value: float
    manual_value: float


# --------------------------------------------------------------------------
# Message construction
# --------------------------------------------------------------------------

def check_message(message: str) -> str:
    """Return a validated message with at most 64 characters and one query.

    Reject malformed commands before they can produce an unexplained timeout."""
    if len(message) + len(TERMINATOR) > MAX_MESSAGE_CHARS:
        raise ProtocolError(
            "message is %d characters with terminators, limit is %d: %r"
            % (len(message) + len(TERMINATOR), MAX_MESSAGE_CHARS, message)
        )
    if message.count("?") > 1:
        raise ProtocolError("only one query is permitted per communication: %r" % message)
    return message


def fmt_number(value: float, decimals: int = 3) -> str:
    """Return a number formatted for an instrument parameter field."""
    return ("%+.*f" % (decimals, value))


# --------------------------------------------------------------------------
# Reply parsing.  Parse leniently: strip whitespace, tolerate leading '+' and
# varying field widths rather than assuming one column layout.
# --------------------------------------------------------------------------

def parse_float(reply: str) -> float:
    try:
        return float(reply.strip())
    except (TypeError, ValueError) as exc:
        raise ProtocolError("expected a number, got %r" % reply) from exc


def parse_int(reply: str) -> int:
    try:
        return int(reply.strip())
    except (TypeError, ValueError) as exc:
        raise ProtocolError("expected an integer, got %r" % reply) from exc


def parse_fields(reply: str, count: int) -> list:
    fields = [f.strip() for f in reply.strip().split(",")]
    if len(fields) != count:
        raise ProtocolError("expected %d comma-separated fields, got %r" % (count, reply))
    return fields


def parse_reading_status(reply: str) -> ReadingStatus:
    return ReadingStatus(parse_int(reply))


def parse_pid(reply: str) -> Tuple[float, float, float]:
    p, i, d = parse_fields(reply, 3)
    return parse_float(p), parse_float(i), parse_float(d)


def parse_ramp(reply: str) -> Tuple[bool, float]:
    enabled, rate = parse_fields(reply, 2)
    return bool(parse_int(enabled)), parse_float(rate)


def parse_cset(reply: str, loop: int) -> LoopConfig:
    channel, units, powerup, display = parse_fields(reply, 4)
    return LoopConfig(
        loop=loop,
        input_channel=channel.upper(),
        units=Units(parse_int(units)),
        powerup_enable=bool(parse_int(powerup)),
        display=HeaterDisplay(parse_int(display)),
    )


def parse_analog(reply: str) -> AnalogConfig:
    bipolar, mode, channel, source, high, low, manual = parse_fields(reply, 7)
    return AnalogConfig(
        bipolar=bool(parse_int(bipolar)),
        mode=AnalogMode(parse_int(mode)),
        input_channel=channel.upper(),
        source=parse_int(source),
        high_value=parse_float(high),
        low_value=parse_float(low),
        manual_value=parse_float(manual),
    )


# --------------------------------------------------------------------------
# Parameter validation, from the manual's stated ranges
# --------------------------------------------------------------------------

def validate_pid(p: float, i: float, d: float) -> None:
    """Validate PID values against instrument limits.

    Zero I or D disables the respective term. The bench instrument accepts
    I = 0; refusing it prevents writing back valid read-back settings.
    P must exceed zero for the control loop to operate."""
    if not 0.1 <= p <= 1000:
        raise ValueError("P must be between 0.1 and 1000, got %g" % p)
    if not 0 <= i <= 1000:
        raise ValueError("I must be between 0 (off) and 1000, got %g" % i)
    if not 0 <= d <= 200:
        raise ValueError("D must be between 0 (off) and 200, got %g" % d)


def validate_ramp_rate(rate: float) -> None:
    if not 0.1 <= rate <= 100:
        raise ValueError("ramp rate must be between 0.1 and 100 K/min, got %g" % rate)
