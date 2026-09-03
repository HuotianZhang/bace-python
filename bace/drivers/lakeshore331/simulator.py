"""A Model 331 that exists only in software.

VENDORED, 2026-09-03, from the 331 console project
(D:/TemperatureController, console/ls331/), unchanged. It is the device
truth; when this file and that project disagree, they have drifted and one
of them is wrong. "The console" in the prose below means whichever program
owns the GPIB session -- in this repository that is the BACE service, and
`controller.DirectTemperatureController` is the owner.

The simulator speaks the same command language as the real instrument, so it
sits behind the same transport interface and exercises the same parsers.  That
is the point: the whole console can be built and demonstrated without being in
front of the cryostat, and the write path can be tested including its refusals.

The thermal model is a deliberately simple first-order one:

    C dT/dt = P_heater - G (T - T_sink)

with constant heat capacity C and link conductance G.  Real cryostats have
strongly temperature-dependent C and G, so treat the dynamics as plausible
rather than predictive - it is here to drive a user interface, not to replace
a tuning run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict

from . import protocol as p


#: Roughly where a full-power heater should top out, and how sluggish the
#: stage should feel.  Used only to size the default thermal model.
_DEFAULT_MAX_REACHABLE_K = 330.0
_DEFAULT_TIME_CONSTANT_S = 2000.0


@dataclass
class ThermalModel:
    """Sample stage bolted to a cold sink, with a heater."""

    temperature_k: float = 295.0
    sink_k: float = 77.0
    heat_capacity_j_per_k: float = 7.9
    link_w_per_k: float = 0.00395

    @classmethod
    def for_heater(cls, max_watts: float, sink_k: float = 77.0, start_k: float = 295.0):
        """A stage a heater of ``max_watts`` can actually drive.

        The link is sized so full power settles near
        :data:`_DEFAULT_MAX_REACHABLE_K`, and the heat capacity so the time
        constant is about half an hour either way - which keeps a 1 W analog
        output and a 50 W current source feeling like the same cryostat.
        """
        link = max_watts / max(1.0, _DEFAULT_MAX_REACHABLE_K - sink_k)
        return cls(
            temperature_k=start_k,
            sink_k=sink_k,
            link_w_per_k=link,
            heat_capacity_j_per_k=link * _DEFAULT_TIME_CONSTANT_S,
        )

    def advance(self, heater_w: float, dt_s: float) -> None:
        if dt_s <= 0:
            return
        # Sub-step so a long gap between polls stays numerically sane.
        steps = max(1, int(dt_s / 0.25) + 1)
        h = dt_s / steps
        for _ in range(steps):
            loss = self.link_w_per_k * (self.temperature_k - self.sink_k)
            self.temperature_k += h * (heater_w - loss) / self.heat_capacity_j_per_k
            if self.temperature_k < 0.0:
                self.temperature_k = 0.0


@dataclass
class _Loop:
    setpoint_k: float = 100.0
    working_setpoint_k: float = 295.0
    p_gain: float = 50.0
    i_reset: float = 20.0
    d_rate: float = 0.0
    mode: p.ControlMode = p.ControlMode.MANUAL_PID
    ramp_on: bool = False
    ramp_k_per_min: float = 5.0
    manual_output_pct: float = 0.0
    integral: float = 0.0
    output_pct: float = 0.0


class Model331Simulator:
    """Accepts 331 command strings and returns 331-shaped replies.

    ``control_loop`` decides which heater the simulated instrument reports:

    * ``1`` - the ordinary current-source heater output, with the Off / Low /
      Medium / High ranges.  This is the default because it is the usual
      configuration.
    * ``2`` - the analog voltage output driving the heater, which the 331
      offers only as Off or On.

    The console reads this back from the instrument rather than being told, so
    running the simulator on one setting and the instrument on the other is
    safe - but the page will look different, which is worth knowing when
    something appears to be missing.
    """

    def __init__(
        self,
        control_loop: int = 1,
        control_input: str = "A",
        heater_ohms: float = 50.0,
        analog_max_w: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        model: ThermalModel = None,
    ) -> None:
        self.clock = clock
        if model is None:
            max_watts = (
                analog_max_w if control_loop == 2
                else p.full_scale_watts(p.HeaterRange.HIGH, heater_ohms)
            )
            model = ThermalModel.for_heater(max_watts)
        self.model = model
        self.control_loop = control_loop
        self.control_input = control_input.upper()
        self.heater_ohms = heater_ohms
        self.analog_max_w = analog_max_w

        self.idn = "LSCI,MODEL331S,SIM000001,1.0"
        self.remote_mode = 1
        self.heater_range = p.HeaterRange.OFF
        self.heater_fault = p.HeaterFault.OK
        self.tuning = p.TuningState.IDLE
        self.loops: Dict[int, _Loop] = {1: _Loop(), 2: _Loop()}

        # Sensor B sits on the cold head and is not controlled.
        self.sensor_b_offset_k = -6.0

        self._last_t = self.clock()
        self._last_temp = self.model.temperature_k

    # -- physics ----------------------------------------------------------

    @property
    def _loop(self) -> _Loop:
        return self.loops[self.control_loop]

    def _full_scale_w(self) -> float:
        if self.control_loop == 2:
            # The analog output is a 1 W voltage source; it has no ranges, only
            # on and off.
            return self.analog_max_w if self.heater_range != p.HeaterRange.OFF else 0.0
        return p.full_scale_watts(self.heater_range, self.heater_ohms)

    def advance(self) -> None:
        """Step the model forward to the current clock time."""
        now = self.clock()
        dt = now - self._last_t
        if dt <= 0:
            return
        self._last_t = now
        loop = self._loop

        # Setpoint ramp: the instrument walks the working setpoint towards the
        # target, which is what RAMPST? reports on.
        if loop.ramp_on and loop.working_setpoint_k != loop.setpoint_k:
            step = loop.ramp_k_per_min * dt / 60.0
            delta = loop.setpoint_k - loop.working_setpoint_k
            loop.working_setpoint_k += step if delta > 0 else -step
            if (delta > 0) != (loop.setpoint_k - loop.working_setpoint_k > 0):
                loop.working_setpoint_k = loop.setpoint_k
        elif not loop.ramp_on:
            loop.working_setpoint_k = loop.setpoint_k

        temperature = self.model.temperature_k
        if loop.mode == p.ControlMode.OPEN_LOOP:
            output = loop.manual_output_pct
            loop.integral = 0.0
        else:
            error = loop.working_setpoint_k - temperature
            derivative = (temperature - self._last_temp) / dt if dt > 0 else 0.0
            raw = (
                loop.p_gain * error
                + loop.p_gain * loop.i_reset * loop.integral / 1000.0
                - loop.p_gain * loop.d_rate * derivative / 100.0
            )
            output = min(100.0, max(0.0, raw))
            # Anti-windup: only integrate while the output is off its stops.
            if 0.0 < raw < 100.0:
                loop.integral += error * dt

        if self.heater_range == p.HeaterRange.OFF:
            output = 0.0
        loop.output_pct = output

        self._last_temp = temperature
        self.model.advance(self._full_scale_w() * output / 100.0, dt)

    # -- command dispatch -------------------------------------------------

    def write(self, message: str) -> None:
        p.check_message(message)
        for part in message.split(";"):
            self._apply(part.strip())

    def query(self, message: str) -> str:
        p.check_message(message)
        parts = [part.strip() for part in message.split(";")]
        for part in parts[:-1]:
            self._apply(part)
        return self._answer(parts[-1])

    def _split(self, command: str):
        head, _, tail = command.partition(" ")
        args = [a.strip() for a in tail.split(",")] if tail.strip() else []
        return head.strip().upper(), args

    def _apply(self, command: str) -> None:
        self.advance()
        head, args = self._split(command)
        if head == "SETP":
            loop = self.loops[int(args[0])]
            loop.setpoint_k = float(args[1])
        elif head == "RANGE":
            self.heater_range = p.HeaterRange(int(args[0]))
        elif head == "PID":
            loop = self.loops[int(args[0])]
            loop.p_gain, loop.i_reset, loop.d_rate = (float(a) for a in args[1:4])
        elif head == "RAMP":
            loop = self.loops[int(args[0])]
            loop.ramp_on = bool(int(args[1]))
            loop.ramp_k_per_min = float(args[2])
            if loop.ramp_on:
                loop.working_setpoint_k = self.model.temperature_k
        elif head == "CMODE":
            self.loops[int(args[0])].mode = p.ControlMode(int(args[1]))
        elif head == "MOUT":
            self.loops[int(args[0])].manual_output_pct = float(args[1])
        elif head == "MODE":
            self.remote_mode = int(args[0])
        elif head in ("CSET", "ANALOG", "INTYPE", "INCRV", "IEEE"):
            # The console does not write these; accept and ignore so a stray
            # command in a script does not look like a comms failure.
            pass
        elif head.startswith("*"):
            pass
        else:
            raise p.ProtocolError("simulator does not implement %r" % command)

    def _answer(self, command: str) -> str:
        self.advance()
        head, args = self._split(command)
        temp = self.model.temperature_k
        loop = self._loop

        if head == "*IDN?":
            return self.idn
        if head == "KRDG?":
            value = temp if (args and args[0].upper() == "A") else temp + self.sensor_b_offset_k
            return p.fmt_number(value)
        if head == "CRDG?":
            value = temp if (args and args[0].upper() == "A") else temp + self.sensor_b_offset_k
            return p.fmt_number(value - 273.15)
        if head == "SRDG?":
            # A silicon diode: roughly 0.5 V at room temperature, rising cold.
            return p.fmt_number(1.6 - 0.0037 * temp, decimals=4)
        if head == "RDGST?":
            return "000"
        if head == "HTR?":
            return p.fmt_number(self.loops[1].output_pct, decimals=1)
        if head == "AOUT?":
            return p.fmt_number(self.loops[2].output_pct, decimals=1)
        if head == "HTRST?":
            return str(int(self.heater_fault))
        if head == "RANGE?":
            return str(int(self.heater_range))
        if head == "SETP?":
            return p.fmt_number(self.loops[int(args[0])].setpoint_k)
        if head == "PID?":
            target = self.loops[int(args[0])]
            return ",".join(p.fmt_number(v, 1) for v in (target.p_gain, target.i_reset, target.d_rate))
        if head == "CMODE?":
            return str(int(self.loops[int(args[0])].mode))
        if head == "MOUT?":
            return p.fmt_number(self.loops[int(args[0])].manual_output_pct, decimals=1)
        if head == "RAMP?":
            target = self.loops[int(args[0])]
            return "%d,%s" % (int(target.ramp_on), p.fmt_number(target.ramp_k_per_min, 1))
        if head == "RAMPST?":
            target = self.loops[int(args[0])]
            ramping = target.ramp_on and abs(target.working_setpoint_k - target.setpoint_k) > 1e-6
            return "1" if ramping else "0"
        if head == "TUNEST?":
            return str(int(self.tuning))
        if head == "MODE?":
            return str(self.remote_mode)
        if head == "CSET?":
            which = int(args[0])
            channel = self.control_input if which == self.control_loop else "B"
            return "%s,1,0,2" % channel
        if head == "ANALOG?":
            mode = p.AnalogMode.LOOP if self.control_loop == 2 else p.AnalogMode.OFF
            return "0,%d,%s,1,%s,%s,%s" % (
                int(mode), self.control_input,
                p.fmt_number(300.0), p.fmt_number(0.0), p.fmt_number(0.0),
            )
        if head == "INTYPE?":
            return "1,0"
        if head == "INCRV?":
            return "01"
        raise p.ProtocolError("simulator does not implement %r" % command)
