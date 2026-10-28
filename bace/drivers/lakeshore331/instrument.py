"""Provide typed Lake Shore 331 operations and enforce safety limits.

Exactly one owner holds a `Lakeshore331`. The service uses
`controller.DirectTemperatureController` and a lock for all exchanges.
The instrument answers only its last query, so concurrent callers can
consume one another's replies.

The front panel owns loop wiring (`CSET`), analog output (`ANALOG`) and
sensor setup (`INTYPE`, `INCRV`). Connection discovery reads these settings
so the console describes the instrument configuration it actually controls."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from . import protocol as p
from .config import Limits, Settings, DEFAULT_SETTINGS

log = logging.getLogger(__name__)

AuditHook = Callable[[str, str], None]


class SafetyError(RuntimeError):
    """Report a request that breaches a configured limit."""


@dataclass(frozen=True)
class Discovery:
    """Describe instrument configuration read at connection.

    `control_loop` explicitly selects the driven loop. Both loops can be
    active, so analog control mode does not identify the intended heater.
    `SETP 2` on a loop-1 system is accepted and stored without affecting
    that heater. Loop 1 drives the main heater; loop 2 drives analog output.
    Loop 1 is the default. A loop-2 installation must select it explicitly."""

    idn: str
    control_loop: int
    loop1: p.LoopConfig
    loop2: p.LoopConfig
    analog: p.AnalogConfig

    @property
    def control_config(self) -> p.LoopConfig:
        return self.loop2 if self.control_loop == 2 else self.loop1

    @property
    def control_input(self) -> str:
        return self.control_config.input_channel

    @property
    def heater_query(self) -> str:
        """Return `HTR?` for control loop 1 or `AOUT?` for control loop 2."""
        return "AOUT?" if self.control_loop == 2 else "HTR?"

    @property
    def has_range_control(self) -> bool:
        """Return whether the selected heater has multiple power ranges.

        Control loop 1 has off, low, medium and high; loop 2 is only off or on."""
        return self.control_loop == 1

    @property
    def analog_is_control_output(self) -> bool:
        """Return whether control loop 2 is enabled, regardless of the selected loop."""
        return self.analog.mode is p.AnalogMode.LOOP

    def describe(self) -> str:
        lines = [
            self.idn,
            "  controlling      : loop %d (%s)" % (
                self.control_loop,
                "analog voltage output" if self.control_loop == 2 else "heater output"),
            "  control input    : %s" % self.control_input,
            "  setpoint units   : %s" % self.control_config.units.name.lower(),
            "  heater readback  : %s" % self.heater_query,
            "  heater ranges    : %s" % (
                "off/low/med/high" if self.has_range_control else "off/on"),
            "  power-up enable  : %s" % (
                "on (WARNING: the loop resumes after a power cycle)"
                if self.control_config.powerup_enable else "off"),
        ]
        if self.analog_is_control_output and self.control_loop != 2:
            lines.append(
                "  note             : the analog output is also configured as a control\n"
                "                     output (Loop 2). This console is not touching it.\n"
                "                     Use --loop 2 if that is the one you control from.")
        return "\n".join(lines)


class Lakeshore331:
    """Expose instrument reads and writes through configured safety limits."""

    def __init__(
        self,
        transport,
        settings: Settings = DEFAULT_SETTINGS,
        audit: Optional[AuditHook] = None,
    ) -> None:
        self.transport = transport
        self.settings = settings
        self.limits: Limits = settings.limits
        self._audit = audit
        self.discovery: Optional[Discovery] = None

    # -- plumbing ---------------------------------------------------------

    def _write(self, message: str, reason: str = "") -> None:
        self.transport.write(message)
        if self._audit is not None:
            self._audit(message, reason)
        log.info("write %s%s", message, (" (%s)" % reason) if reason else "")

    def _query(self, message: str) -> str:
        return self.transport.query(message)

    @property
    def _loop(self) -> int:
        if self.discovery is None:
            raise RuntimeError("call discover() before controlling the instrument")
        return self.discovery.control_loop

    # -- discovery --------------------------------------------------------

    def discover(self, control_loop: int = None) -> Discovery:
        """Query identity, both `CSET?` replies and `ANALOG?`; return `Discovery`.

        `control_loop` defaults to the configured loop, normally 1. It must be
        selected explicitly because both instrument control loops can be active."""
        if control_loop is None:
            control_loop = self.settings.control_loop
        if control_loop not in (1, 2):
            raise ValueError("control loop must be 1 or 2, got %r" % (control_loop,))

        idn = self._query("*IDN?")
        if "331" not in idn:
            log.warning("connected instrument does not look like a 331: %s", idn)
        self.discovery = Discovery(
            idn=idn,
            control_loop=control_loop,
            loop1=p.parse_cset(self._query("CSET? 1"), loop=1),
            loop2=p.parse_cset(self._query("CSET? 2"), loop=2),
            analog=p.parse_analog(self._query("ANALOG?")),
        )
        log.info("connected to %s", idn)
        log.debug("configuration:\n%s", self.discovery.describe())
        return self.discovery

    # -- reads ------------------------------------------------------------

    def kelvin(self, channel: str = "A") -> float:
        return p.parse_float(self._query("KRDG? %s" % channel.upper()))

    def celsius(self, channel: str = "A") -> float:
        return p.parse_float(self._query("CRDG? %s" % channel.upper()))

    def sensor_units(self, channel: str = "A") -> float:
        return p.parse_float(self._query("SRDG? %s" % channel.upper()))

    def reading_status(self, channel: str = "A") -> p.ReadingStatus:
        return p.parse_reading_status(self._query("RDGST? %s" % channel.upper()))

    def heater_output_percent(self) -> float:
        """Query the configured heater output and return percent.

        Loop 1 uses `HTR?`; loop 2 uses `AOUT?`. Discovery must precede the read."""
        if self.discovery is None:
            raise RuntimeError("call discover() first")
        return p.parse_float(self._query(self.discovery.heater_query))

    def heater_fault(self) -> p.HeaterFault:
        return p.HeaterFault(p.parse_int(self._query("HTRST?")))

    def heater_range(self) -> p.HeaterRange:
        return p.HeaterRange(p.parse_int(self._query("RANGE?")))

    def setpoint(self) -> float:
        return p.parse_float(self._query("SETP? %d" % self._loop))

    def pid(self) -> Tuple[float, float, float]:
        return p.parse_pid(self._query("PID? %d" % self._loop))

    def ramp(self) -> Tuple[bool, float]:
        return p.parse_ramp(self._query("RAMP? %d" % self._loop))

    def is_ramping(self) -> bool:
        return bool(p.parse_int(self._query("RAMPST? %d" % self._loop)))

    def tuning_state(self) -> p.TuningState:
        return p.TuningState(p.parse_int(self._query("TUNEST?")))

    def control_mode(self) -> p.ControlMode:
        return p.ControlMode(p.parse_int(self._query("CMODE? %d" % self._loop)))

    # -- writes, each through its guard rail ------------------------------

    def set_setpoint(self, kelvin: float, reason: str = "") -> None:
        """Send `SETP` with the control setpoint in K and return `None`.

        Requests outside configured bounds raise `SafetyError`. Clamping a
        400 K request to 350 K hides a setup error, so the request is refused."""
        if kelvin > self.limits.max_setpoint_k:
            raise SafetyError(
                "setpoint %.3f K exceeds the %.1f K limit for this cryostat"
                % (kelvin, self.limits.max_setpoint_k)
            )
        if kelvin < self.limits.min_setpoint_k:
            raise SafetyError(
                "setpoint %.3f K is below the %.1f K limit"
                % (kelvin, self.limits.min_setpoint_k)
            )
        self._write("SETP %d,%s" % (self._loop, p.fmt_number(kelvin)), reason)

    def set_heater_range(self, rng: p.HeaterRange, confirmed: bool = False, reason: str = "") -> None:
        """Send `RANGE` and return `None`, requiring confirmation for an increase."""
        rng = p.HeaterRange(rng)
        current = self.heater_range()
        if rng > current and not confirmed:
            raise SafetyError(
                "raising the heater range from %s to %s must be confirmed"
                % (current.name, rng.name)
            )
        self._write("RANGE %d" % int(rng), reason)

    def set_pid(self, p_gain: float, i_reset: float, d_rate: float, reason: str = "") -> None:
        if self.tuning_state() is p.TuningState.TUNING:
            raise SafetyError("autotune is running; PID values cannot be changed")
        p.validate_pid(p_gain, i_reset, d_rate)
        self._write(
            "PID %d,%s,%s,%s"
            % (self._loop, p.fmt_number(p_gain, 1), p.fmt_number(i_reset, 1), p.fmt_number(d_rate, 1)),
            reason,
        )

    def set_ramp(self, enabled: bool, k_per_min: float, reason: str = "") -> None:
        p.validate_ramp_rate(k_per_min)
        self._write(
            "RAMP %d,%d,%s" % (self._loop, int(bool(enabled)), p.fmt_number(k_per_min, 1)),
            reason,
        )

    def set_manual_output(self, percent: float, reason: str = "") -> None:
        if not 0.0 <= percent <= 100.0:
            raise SafetyError("manual output must be between 0 and 100 %%, got %g" % percent)
        self._write("MOUT %d,%s" % (self._loop, p.fmt_number(percent, 2)), reason)

    def set_control_mode(self, mode: p.ControlMode, reason: str = "") -> None:
        self._write("CMODE %d,%d" % (self._loop, int(p.ControlMode(mode))), reason)

    def take_remote_control(self) -> None:
        """Send `MODE 1` for remote control with the front panel live; return `None`.

        `MODE 2` adds local lockout. Keypad lock preserves Alarm Reset and Heater
        Off, but the bus `LLO` description does not guarantee those controls
        remain usable. This driver therefore avoids local lockout."""
        self._write("MODE 1", reason="console taking remote control")

    # -- the kill path ----------------------------------------------------

    def emergency_stop(self, reason: str) -> None:
        """Send `RANGE 0` to cut heater power and return `None`.

        For a heater on analog output, also zero `MOUT` before selecting open
        loop with `CMODE`. Analog output has no separate documented remote-off
        command. `MOUT` alone does not stop PID output. Switching mode before
        zeroing can apply a stale manual output to the heater.

        This bench uses control loop 1. The loop-2 stop sequence is untested on
        hardware here; verify it on the intended bench before relying on it."""
        log.error("EMERGENCY STOP: %s", reason)
        self._write("RANGE 0", reason="emergency stop: %s" % reason)
        if self.discovery is not None and self.discovery.control_loop == 2:
            self._write("MOUT %d,%s" % (self._loop, p.fmt_number(0.0, 2)),
                        reason="emergency stop: zero manual output")
            self._write("CMODE %d,%d" % (self._loop, int(p.ControlMode.OPEN_LOOP)),
                        reason="emergency stop: drop loop to open loop")

    def check_faults(self) -> Optional[str]:
        """Query sensor and heater status and return a fault reason or `None`."""
        status = self.reading_status(self.discovery.control_input if self.discovery else "A")
        if not status.ok:
            return "control sensor reports %s" % status
        fault = self.heater_fault()
        if fault is not p.HeaterFault.OK:
            return "heater reports %s" % fault.name.replace("_", " ").lower()
        return None
