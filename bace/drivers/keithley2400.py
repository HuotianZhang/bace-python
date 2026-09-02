"""Keithley 2400 SourceMeter — V_oc, J_sc, J_sat, and the J–V sweep.

Confirmed on the bench: `KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34 Sep 21
2016` at `GPIB0::24::INSTR`. A plain 2400, so 200 V / 1 A / 20 W envelope — not
a 2401 or 2440.

The measurement routine below is `measJsc,Voc.vi` command for command, with one
deliberate change and one correction, both called out where they happen. This
instrument sits on the far side of the relay from the amplifier, so
`output_enabled` is a real interlock input, not decoration: `drivers.routing`
reads it before it will move the relay.

Sign convention: the current returned at 0 V is whatever the instrument reads,
which for an illuminated cell is negative. The original had a `positive jSC?`
control for flipping it, but that control appears in no block diagram — it was
displayed and saved and never read — so nothing here flips anything. Sign
handling is the analysis's business.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from .protocols import DCPoint
from .readback import ask, on_off


class SourceMeterError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMeterConfig:
    """Limits and timing. The three settle times are separate controls on the
    original's panel (`Settle Time Jsc/Voc/Jsat (ms)`) because they genuinely
    differ: V_oc on a slow cell settles far more slowly than J_sc."""

    current_compliance_a: float = 0.05
    """Hard limit while sourcing voltage. Sized for the pixel, not the
    instrument — a 2400 will happily deliver 1 A into a small cell."""

    voltage_compliance_v: float = 2.0
    """Hard limit while sourcing current. With 0 A sourced this is what stops
    the output swinging to the rail if a contact opens."""

    settle_jsc_ms: float = 500.0
    settle_voc_ms: float = 500.0
    settle_jsat_ms: float = 500.0
    nplc: float = 1.0
    averaging: int = 1
    terminals: str = "FRON"          # FRON or REAR
    four_wire: bool = False


class Keithley2400:
    """One SMU session. Not thread-safe."""

    def __init__(self, resource, *, config: SourceMeterConfig = SourceMeterConfig(),
                 timeout_ms: int = 20000):
        self._io = resource
        self._io.timeout = timeout_ms
        self.config = config
        self._output = False

    # -- identity ---------------------------------------------------------
    def identify(self) -> str:
        idn = self._io.query("*IDN?").strip()
        if "KEITHLEY" not in idn.upper():
            raise SourceMeterError(f"expected a Keithley, got {idn!r}")
        return idn

    def default_setup(self) -> None:
        self._io.write("*ESE 1;*SRE 32;*CLS;")
        self._io.write(":FUNC:CONC ON;:FUNC:ALL;")
        self._io.write(":TRAC:FEED:CONT NEV;")
        self._io.write(":FORM:BORD NORM;:FORM SRE;")
        self._io.write(":RES:MODE MAN;")
        self._io.write(f":SYST:RSEN {'ON' if self.config.four_wire else 'OFF'};")

    # -- output -----------------------------------------------------------
    def enable_output(self, on: bool = True) -> None:
        self._io.write(f":OUTP {'ON' if on else 'OFF'};")
        self._output = bool(on)

    def disable_output(self) -> None:
        self.enable_output(False)

    @property
    def output_enabled(self) -> bool:
        return self._output

    def read_output(self) -> bool | None:
        """`:OUTP?` from the instrument; the cached `output_enabled` follows it.

        Not a protocol member. The router's interlock reads the cached flag,
        and the service refreshes it here before a relay move and at every
        read-back, so a SourceMeter left ON by hand is seen rather than
        remembered as off."""
        on = on_off(ask(self._io, ":OUTP?"))
        if on is not None:
            self._output = on
        return on

    # -- the three DC quantities -----------------------------------------
    def _prepare(self) -> None:
        """`*RST` clears compliance, ranges and mode, so everything that
        matters has to be re-sent after it. The original resets before each of
        the three measurements; kept, because it also clears any state a
        previous aborted run left behind."""
        self._io.write("*RST")
        self._output = False
        self._io.write(f":ROUT:TERM {self.config.terminals};")
        if self.config.averaging > 1:
            self._io.write(f":AVER ON;:AVER:COUN {int(self.config.averaging)};"
                           ":AVER:TCON REP;")
        else:
            self._io.write(":AVER OFF;")

    def _source_voltage_measure_current(self, level_v: float, settle_ms: float) -> float:
        self._prepare()
        self._io.write(":SOUR:FUNC:MODE VOLT;")
        self._io.write(f":SOUR:VOLT:LEV {level_v:g};")
        self._io.write(":SENS:FUNC 'CURR:DC';")
        self._io.write(f":SENS:CURR:PROT:LEV {self.config.current_compliance_a:g};")
        self._io.write(f":SENS:CURR:NPLC {self.config.nplc:g};")
        self._io.write(":FORM:ELEM CURR;")
        # DEVIATION: the original enabled the output and only then set the
        # compliance. Between those two commands the SMU sits at whatever *RST
        # left, which is not the limit anyone chose. Compliance first here.
        self.enable_output(True)
        time.sleep(settle_ms / 1000.0)
        value = float(self._io.query(":READ?").strip().split(",")[0])
        self.disable_output()
        return value

    def _source_current_measure_voltage(self, level_a: float, settle_ms: float) -> float:
        self._prepare()
        self._io.write(":SOUR:FUNC:MODE CURR;")
        self._io.write(f":SOUR:CURR:LEV {level_a:g};")
        self._io.write(":SENS:FUNC 'VOLT:DC';")
        # CORRECTION: the recovered routine sets a *current* protection here,
        # which does nothing while the instrument is sourcing current. Sourcing
        # 0 A, the limit that matters is the voltage compliance -- it is what
        # bounds the output if a contact opens mid-measurement.
        self._io.write(f":SENS:VOLT:PROT:LEV {self.config.voltage_compliance_v:g};")
        self._io.write(f":SENS:VOLT:NPLC {self.config.nplc:g};")
        self._io.write(":FORM:ELEM VOLT;")
        self.enable_output(True)
        time.sleep(settle_ms / 1000.0)
        value = float(self._io.query(":READ?").strip().split(",")[0])
        self.disable_output()
        return value

    def measure_jsc(self, settle_ms: float | None = None) -> float:
        """Current at 0 V. Negative under illumination."""
        return self._source_voltage_measure_current(
            0.0, self.config.settle_jsc_ms if settle_ms is None else settle_ms)

    def measure_voc(self, settle_ms: float | None = None) -> float:
        """Voltage at 0 A."""
        return self._source_current_measure_voltage(
            0.0, self.config.settle_voc_ms if settle_ms is None else settle_ms)

    def measure_jsat(self, v_sat: float, settle_ms: float | None = None) -> float:
        """Current at the saturation bias (`Vcoll corresponding to Jsat` on the
        original's panel) — the reverse bias where extraction is complete."""
        return self._source_voltage_measure_current(
            v_sat, self.config.settle_jsat_ms if settle_ms is None else settle_ms)

    def measure_dc(self, *, v_sat: float, settle_s: float | None = None) -> DCPoint:
        """All three, in the original's order: J_sc, V_oc, J_sat.

        `settle_s`, if given, overrides all three configured settle times —
        useful for a fast dry run, not for real data.
        """
        ms = None if settle_s is None else settle_s * 1000.0
        jsc = self.measure_jsc(ms)
        voc = self.measure_voc(ms)
        jsat = self.measure_jsat(v_sat, ms)
        return DCPoint(voc=voc, jsc=jsc, jsat=jsat, v_sat=v_sat)

    # -- J-V sweep --------------------------------------------------------
    def sweep(self, start_v: float, stop_v: float, points: int, *,
              settle_s: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """A linear voltage sweep, returning (V, I).

        Used by the J–V experiment, not by the transient one, so it is not part
        of the `SourceMeter` protocol — the transient layer must not be able to
        start a sweep by accident.
        """
        if points < 2:
            raise ValueError("a sweep needs at least two points")
        self._prepare()
        self._io.write(":SOUR:FUNC:MODE VOLT;")
        self._io.write(":SOUR:VOLT:MODE SWE;")
        self._io.write(f":SOUR:VOLT:STAR {start_v:g};:SOUR:VOLT:STOP {stop_v:g};")
        self._io.write(f":SOUR:SWE:POIN {int(points):d};:SOUR:SWE:SPAC LIN;")
        self._io.write(":SENS:FUNC 'CURR:DC';")
        self._io.write(f":SENS:CURR:PROT:LEV {self.config.current_compliance_a:g};")
        self._io.write(f":SENS:CURR:NPLC {self.config.nplc:g};")
        self._io.write(":FORM:ELEM VOLT,CURR;")
        self._io.write(f":SOUR:DEL {settle_s:g};")
        self._io.write(f":TRIG:COUN {int(points):d};")
        # The whole sweep is ONE `:READ?`: the instrument steps, settles,
        # integrates and averages every point before it answers. That is
        # points x (source delay + averaging x NPLC / 50 Hz) plus stepping
        # overhead -- with the validated recipe (71 points, 50 ms delay,
        # averaging 10, NPLC 1) about 18 s, and the session's blanket
        # 20 s VISA timeout cut it off on the rig (VI_ERROR_TMO, session
        # 20260902_143927). So the timeout is sized from the sweep, with
        # a factor two for auto-zero and stepping, and restored after.
        per_point_s = (settle_s
                       + max(1, int(self.config.averaging)) * self.config.nplc / 50.0)
        budget_ms = int((10.0 + 2.0 * points * per_point_s) * 1000)
        old_timeout = getattr(self._io, "timeout", None)
        if old_timeout is not None and budget_ms > old_timeout:
            self._io.timeout = budget_ms
        self.enable_output(True)
        try:
            raw = self._io.query(":READ?").strip()
        finally:
            self.disable_output()
            if old_timeout is not None and budget_ms > old_timeout:
                self._io.timeout = old_timeout
        flat = np.array([float(x) for x in raw.split(",")])
        return flat[0::2], flat[1::2]

    def abort(self) -> None:
        self._io.write("ABOR;:TRIG:CLE;")
        self.disable_output()

    def errors(self) -> list[str]:
        out: list[str] = []
        while True:
            r = self._io.query(":SYST:ERR?").strip()
            if r.startswith(("0,", "+0,")):
                return out
            out.append(r)
            if len(out) > 20:
                return out

    def close(self) -> None:
        try:
            self.disable_output()
        finally:
            self._io.close()
