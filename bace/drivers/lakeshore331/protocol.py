"""Wire-level protocol for the Lake Shore Model 331.

VENDORED, 2026-09-03, from the 331 console project
(D:/TemperatureController, console/ls331/), unchanged. It is the device
truth; when this file and that project disagree, they have drifted and one
of them is wrong. "The console" in the prose below means whichever program
owns the GPIB session -- in this repository that is the BACE service, and
`controller.DirectTemperatureController` is the owner.

This module performs no I/O.  It builds command strings and parses instrument
replies into typed values, which is what makes the whole command layer testable
without a cryostat attached.

Reference: Model 331 user's manual, chapter 6 (Remote Operation).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Sequence, Tuple

#: Manual 6.1.1 - default terminator is CR/LF.
TERMINATOR = "\r\n"

#: Manual 6.1.2.4 - "The total communication string must not exceed 64
#: characters in length."  Also: only one query per communication, and the
#: instrument replies only to the *last* query it received.
MAX_MESSAGE_CHARS = 64


class ProtocolError(ValueError):
    """A reply could not be parsed, or a message would violate the protocol."""


# --------------------------------------------------------------------------
# Enumerations, straight from the manual's parameter tables
# --------------------------------------------------------------------------

class Units(enum.IntEnum):
    """CSET <units>."""

    KELVIN = 1
    CELSIUS = 2
    SENSOR = 3


class HeaterDisplay(enum.IntEnum):
    """CSET <current/power> - how the front panel shows heater output."""

    CURRENT = 1
    POWER = 2


class HeaterRange(enum.IntEnum):
    """RANGE.

    Wattages are the manual's headline figures for a 50 ohm heater; the actual
    full-scale power scales with heater resistance (25 W at 25 ohm, 10 W at
    10 ohm on High).  See :func:`full_scale_watts`.
    """

    OFF = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class ControlMode(enum.IntEnum):
    """CMODE."""

    MANUAL_PID = 1
    ZONE = 2
    OPEN_LOOP = 3
    AUTOTUNE_PID = 4
    AUTOTUNE_PI = 5
    AUTOTUNE_P = 6


class AnalogMode(enum.IntEnum):
    """ANALOG <mode>.  LOOP means the analog output is driving Loop 2."""

    OFF = 0
    INPUT = 1
    MANUAL = 2
    LOOP = 3


class HeaterFault(enum.IntEnum):
    """HTRST?."""

    OK = 0
    OPEN_LOAD = 1
    SHORT = 2


class TuningState(enum.IntEnum):
    """TUNEST?."""

    IDLE = 0
    TUNING = 1


#: Manual 4.13, "Loop 1 Full Scale Heater Power at Typical Resistance".
_FULL_SCALE_W = {
    10.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.100, HeaterRange.MEDIUM: 1.0, HeaterRange.HIGH: 10.0},
    25.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.250, HeaterRange.MEDIUM: 2.5, HeaterRange.HIGH: 25.0},
    50.0: {HeaterRange.OFF: 0.0, HeaterRange.LOW: 0.500, HeaterRange.MEDIUM: 5.0, HeaterRange.HIGH: 50.0},
}


def full_scale_watts(rng: HeaterRange, heater_ohms: float = 50.0) -> float:
    """Full-scale Loop 1 heater power for a range, given the heater resistance.

    The manual only tabulates 10, 25 and 50 ohm; anything else is scaled from
    the 50 ohm column, which is exact for a current-source output.
    """
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
    """Decoded RDGST? response."""

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
    """CSET? <loop> - how a control loop is wired up.

    The console reads this; it never writes it.  Loop wiring belongs to the
    front panel, so that what the instrument is doing and what the screen says
    cannot drift apart.
    """

    loop: int
    input_channel: str
    units: Units
    powerup_enable: bool
    display: HeaterDisplay


@dataclass(frozen=True)
class AnalogConfig:
    """ANALOG? - the analog voltage output.

    ``mode is AnalogMode.LOOP`` means the analog output is driving Loop 2 as a
    control output, i.e. it is the heater.
    """

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
    """Validate a message against the documented limits, then return it.

    Enforces the 64-character cap and the one-query-per-communication rule so a
    malformed command is caught here rather than as a silent timeout.
    """
    if len(message) + len(TERMINATOR) > MAX_MESSAGE_CHARS:
        raise ProtocolError(
            "message is %d characters with terminators, limit is %d: %r"
            % (len(message) + len(TERMINATOR), MAX_MESSAGE_CHARS, message)
        )
    if message.count("?") > 1:
        raise ProtocolError("only one query is permitted per communication: %r" % message)
    return message


def fmt_number(value: float, decimals: int = 3) -> str:
    """Format a number the way the instrument's parameter fields expect."""
    return ("%+.*f" % (decimals, value))


# --------------------------------------------------------------------------
# Reply parsing.  Parse leniently: strip whitespace, tolerate leading '+' and
# varying field widths rather than assuming a fixed column layout.
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
    """Check PID values against the instrument's documented ranges.

    Zero is allowed for I and D: it is how the 331 expresses "integral off" and
    "derivative off", the manual's own tuning procedure starts there, and a real
    instrument was found holding I = 0.  Refusing it would mean the console
    could not write back the values it had just read.

    P must be greater than zero - manual 2.6.1: "The Proportional term, also
    called gain, must have a value greater than zero for the control loop to
    operate."
    """
    if not 0.1 <= p <= 1000:
        raise ValueError("P must be between 0.1 and 1000, got %g" % p)
    if not 0 <= i <= 1000:
        raise ValueError("I must be between 0 (off) and 1000, got %g" % i)
    if not 0 <= d <= 200:
        raise ValueError("D must be between 0 (off) and 200, got %g" % d)


def validate_ramp_rate(rate: float) -> None:
    if not 0.1 <= rate <= 100:
        raise ValueError("ramp rate must be between 0.1 and 100 K/min, got %g" % rate)
