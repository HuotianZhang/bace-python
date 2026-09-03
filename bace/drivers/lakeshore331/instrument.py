"""Typed instrument API, configuration discovery, and the safety guard rails.

VENDORED, 2026-09-03, from the 331 console project
(D:/TemperatureController, console/ls331/), unchanged. It is the device
truth; when this file and that project disagree, they have drifted and one
of them is wrong. "The console" in the prose below means whichever program
owns the GPIB session -- in this repository that is the BACE service, and
`controller.DirectTemperatureController` is the owner.

Two rules hold this layer together.

**One owner.**  Exactly one object in the process may hold a
:class:`Lakeshore331`.  Everything else - the poller, the web handlers, a sweep
script - goes through that owner's queue.  The instrument answers only the last
query it received and cannot arbitrate between callers, so two callers sharing a
session will eventually read one another's replies.

**The instrument owns its configuration.**  Loop wiring (``CSET``), the analog
output (``ANALOG``) and sensor setup (``INTYPE``, ``INCRV``) are set on the
front panel and only ever *read* here.  The console discovers how the box is
wired at connect and adapts, which means what is on screen cannot drift away
from what the hardware is actually doing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from . import protocol as p
from .config import Limits, Settings, DEFAULT_SETTINGS

log = logging.getLogger(__name__)

AuditHook = Callable[[str, str], None]


class SafetyError(RuntimeError):
    """A request was refused because it would breach a configured limit."""


@dataclass(frozen=True)
class Discovery:
    """What the instrument reported about itself at connect.

    ``control_loop`` is the loop this console drives.  It is **not** inferred
    from the instrument, and that is deliberate: the 331's two loops can both
    be active at once - Loop 1 running the main heater while Loop 2 drives the
    analog output for a shield - so "the analog output is in control mode"
    says nothing about which loop matters to you.  Guessing it wrong is silent:
    ``SETP 2`` on a Loop 1 system is accepted, stored, and changes nothing.

    Loop 1 is the default because it is the main control loop and drives the
    heater output.  Pass ``--loop 2`` if the analog output is the one you
    actually control from.
    """

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
        """``HTR?`` reads Loop 1 only; Loop 2 reads back through ``AOUT?``."""
        return "AOUT?" if self.control_loop == 2 else "HTR?"

    @property
    def has_range_control(self) -> bool:
        """Loop 1 has Off/Low/Med/High; Loop 2's output is only on or off."""
        return self.control_loop == 1

    @property
    def analog_is_control_output(self) -> bool:
        """True when Loop 2 is also enabled, whether or not we are using it."""
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
    """Everything the console is allowed to ask the instrument to do."""

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
        """Read back how the instrument is configured, and remember it.

        ``control_loop`` says which loop this console drives; it defaults to
        the settings value, which is Loop 1.  Nothing here infers it - see the
        note on :class:`Discovery`.
        """
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
        """Heater output, from whichever channel this configuration uses."""
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
        """Set the control setpoint, refusing anything above the ceiling.

        Refused rather than silently clamped: a request for 400 K is a mistake,
        and quietly giving the user 350 K hides it.
        """
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
        """Change the heater range.  Lowering is free; raising needs a confirm."""
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
        """MODE 1 - remote, front panel still live.

        MODE 2 adds local lockout.  The keypad-lock feature keeps Alarm Reset
        and Heater Off working, but the manual documents the bus LLO message
        only as "prevents the use of instrument front panel controls" and does
        not promise the same carve-out, so this console does not use it.
        """
        self._write("MODE 1", reason="console taking remote control")

    # -- the kill path ----------------------------------------------------

    def emergency_stop(self, reason: str) -> None:
        """Cut heater power as directly as this configuration allows.

        ``RANGE 0`` is the documented off switch and is harmless in any
        configuration.  For a heater driven from the analog output there is no
        separate documented remote off, so the manual output is also zeroed and
        the loop dropped to open loop.  VERIFY THIS ON THE BENCH before relying
        on it (see README, "Bench checks").
        """
        log.error("EMERGENCY STOP: %s", reason)
        self._write("RANGE 0", reason="emergency stop: %s" % reason)
        if self.discovery is not None and self.discovery.control_loop == 2:
            self._write("MOUT %d,%s" % (self._loop, p.fmt_number(0.0, 2)),
                        reason="emergency stop: zero manual output")

    def check_faults(self) -> Optional[str]:
        """Return a description of any fault worth cutting the heater for."""
        status = self.reading_status(self.discovery.control_input if self.discovery else "A")
        if not status.ok:
            return "control sensor reports %s" % status
        fault = self.heater_fault()
        if fault is not p.HeaterFault.OK:
            return "heater reports %s" % fault.name.replace("_", " ").lower()
        return None
