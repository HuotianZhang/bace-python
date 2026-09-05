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
    """The most current the 2400 will pass while it sources voltage
    (`:SENS:CURR:PROT:LEV`); it clamps there rather than going higher. Sized
    for the pixel, not the instrument — a 2400 will happily deliver 1 A into a
    small cell."""

    voltage_compliance_v: float = 2.0
    """The most voltage the 2400 will put across the device while it sources
    current (`:SENS:VOLT:PROT:LEV`). With 0 A sourced — the V_oc reading — this
    is what stops the output swinging to the rail if a contact opens."""

    settle_jsc_ms: float = 500.0
    """How long the 2400 holds 0 V before reading the current, for J_sc.
    Too short and the number is the cell's RC, not its steady state."""

    settle_voc_ms: float = 500.0
    """How long the 2400 holds 0 A before reading the voltage, for V_oc.
    The longest of the three on a slow cell -- and the one that matters most,
    because this V_oc is what a `bace` scan centres its axis on."""

    settle_jsat_ms: float = 500.0
    """How long the 2400 holds `v_sat` before reading the current, for J_sat."""

    nplc: float = 1.0
    """How long the 2400 integrates each reading, in **power-line cycles**
    (`:SENS:*:NPLC`).
    1 NPLC is 20 ms on 50 Hz mains and rejects mains hum by integrating over a
    whole cycle; below 1 the reading gets faster and noisier, above 1 slower
    and quieter. It multiplies the whole sweep: `points x (settle + averaging x
    NPLC / 50 Hz)`."""

    averaging: int = 1
    """How many readings the 2400 averages into each point (`:AVER:COUN`, with
    a repeating filter). 1 switches the filter off. Costs NPLC per extra
    reading, so it multiplies the sweep time with `nplc`."""

    terminals: str = "FRON"          # FRON or REAR
    """Which set of terminals on the 2400 is live (`:ROUT:TERM`): `FRON` the
    front panel, `REAR` the back. **A physical fact about how the rig is
    cabled, not a preference** -- set to the side the sample is actually
    wired to, or the sweep reads an open circuit."""

    four_wire: bool = False
    """Kelvin sensing (`:SYST:RSEN ON`): the 2400 measures voltage on a
    separate pair of leads, so the reading excludes the drop down the current
    leads. Needs four wires to the sample; with only two connected, turning
    this on reads nothing. Off is the two-wire default this rig uses."""


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
        # `:SYST:RSEN` is deliberately not here: `_prepare` opens every
        # measurement with `*RST`, which puts the sense back to two-wire, so
        # a four-wire setting only counts if it is sent after that reset.

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
        # After the reset, or it is the reset's two-wire that measures: until
        # 2026-09-05 this was sent once in `default_setup` and undone here.
        self._io.write(f":SYST:RSEN {'ON' if self.config.four_wire else 'OFF'};")
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
    def sweep_points(self, start_v: float, stop_v: float, points: int, *,
                     settle_s: float = 0.0):
        """A linear voltage sweep, one point at a time: source, settle, read,
        yield `(v, i)`. The J-V experiment consumes this so a curve is on the
        screen and on disk as it is measured, not after the last point.

        Until 2026-09-04 the sweep was the 2400's own (`:SOUR:VOLT:MODE SWE`,
        one `:READ?` for every point), which blocked the bus for the whole
        curve -- 30 s for 36 points, a minute for 71 -- with nothing to show,
        nothing saved if it failed, and a stop that could not land until it
        was over. Point by point costs the GPIB round trips, some 30 ms a
        point against 0.8 s of measuring, and buys the stream, the partial
        file, and an abort that lands between two points.

        Two things the instrument's sweep did that the first point-by-point
        version lost, both back since 2026-09-05 (curves came out rougher):

        * **One source range for the whole curve.** The 2400's sweep ranges
          `BEST` -- the lowest range that holds every point. A fixed source
          after `*RST` auto-ranges instead, so a curve through +-0.2 V
          changed range mid-sweep, output on, with the glitch and the change
          of source character that brings. `:SOUR:VOLT:RANG` is set from
          the two ends before the output comes on.
        * **The settle is the instrument's.** `:SOUR:DEL` inside the trigger
          model, as the sweep had it, not a `time.sleep` on the host: every
          point gets exactly `settle_s` between the step and the first
          aperture, whatever the GPIB turn-around, the stream and the
          recorder's rewrite add between two points. It also switches the
          2400's auto delay off, which the reset had left on top.

        **The voltage yielded is the setpoint, not the instrument's V
        element** (2026-09-05). Only current is sensed here -- `:SENS:FUNC?`
        on the bench answers `"CURR:DC"` alone -- so the 2400 is not
        measuring voltage, and what it puts in the V element of a `:READ?`
        is not one: on the rig it lagged the setpoint by up to 0.09 V with
        an exponential memory across points (about 17 % of the gap closed
        per point) and snapped back to the exact setpoint at every current
        range change -- with the filter confirmed `REP`, autorange on. Filed
        as the curve's voltage it put 0.09 V kinks near 1 V wherever the
        current changed range and pulled V_mpp, P_max and FF down by ten to
        fifteen percent (runs 20260904_234336 and 20260905_1108xx against
        the instrument-sweep run 20260904_220109). Two-wire, the setpoint
        *is* the best estimate of the device's voltage -- 0.02 % plus 0.6 mV
        of source accuracy against a sub-millivolt lead drop -- and it is
        what the 2400's own sweep filed for every curve before 2026-09-04.
        So `:FORM:ELEM CURR` asks for the one thing measured, and the point
        is `(level sourced, current read)`. Four-wire would be the moment
        to sense voltage for real; that is not this rig.

        Used by the J-V experiment, not by the transient one, so it is not
        part of the `SourceMeter` protocol -- the transient layer must not
        be able to start a sweep by accident.
        """
        if points < 2:
            raise ValueError("a sweep needs at least two points")
        self._prepare()
        self._io.write(":SOUR:FUNC:MODE VOLT;")
        # One range for the whole curve, chosen from its ends (what the
        # 2400's own sweep calls BEST), so no range change lands mid-sweep.
        self._io.write(f":SOUR:VOLT:RANG {max(abs(float(start_v)), abs(float(stop_v))):g};")
        # The first point's level before the output comes on, so the device
        # sees `start_v` and never a *RST 0 V on the way there.
        self._io.write(f":SOUR:VOLT:LEV {start_v:g};")
        # The settle, timed by the trigger model between the step and the
        # measurement. `:SOUR:DEL` also turns `:SOUR:DEL:AUTO` off.
        self._io.write(f":SOUR:DEL {max(0.0, float(settle_s)):g};")
        self._io.write(":SENS:FUNC 'CURR:DC';")
        self._io.write(f":SENS:CURR:PROT:LEV {self.config.current_compliance_a:g};")
        self._io.write(f":SENS:CURR:NPLC {self.config.nplc:g};")
        # Current only: voltage is not sensed, and the V element the 2400
        # would fill in is not a measurement (see the docstring).
        self._io.write(":FORM:ELEM CURR;")
        self._io.write(":TRIG:COUN 1;")
        # One point is settle + averaging x four apertures (`sweep_budget_s`):
        # under a second at the validated recipe, inside the session's 20 s.
        # The budget still applies, for a recipe that asks for more.
        budget_ms = int(self.sweep_budget_s(1, settle_s) * 1000)
        old_timeout = getattr(self._io, "timeout", None)
        raise_it = old_timeout is not None and budget_ms > old_timeout
        if raise_it:
            self._io.timeout = budget_ms
        self.enable_output(True)
        started = time.monotonic()
        try:
            for x in np.linspace(float(start_v), float(stop_v), int(points)):
                level = f"{x:g}"
                self._io.write(f":SOUR:VOLT:LEV {level};")
                # `:READ?` steps to the level, waits `:SOUR:DEL`, integrates.
                raw = self._io.query(":READ?").strip().split(",")
                # The level as the instrument received it, not linspace's
                # 0.49999999999999994 -- the point's voltage is what was sourced.
                yield float(level), float(raw[0])
        finally:
            self.last_sweep_s = time.monotonic() - started
            self.disable_output()
            if raise_it:
                self._io.timeout = old_timeout

    def sweep(self, start_v: float, stop_v: float, points: int, *,
              settle_s: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """`sweep_points`, collected: (V, I) as arrays."""
        got = list(self.sweep_points(start_v, stop_v, points, settle_s=settle_s))
        return (np.array([v for v, _ in got], dtype=float),
                np.array([i for _, i in got], dtype=float))

    def sweep_budget_s(self, points: int, settle_s: float = 0.0) -> float:
        """How long `points` of a sweep take, with room: the VISA budget
        `sweep_points` gives each of its reads (`points=1`), and the
        whole-curve number a caller can plan on.

        Each averaged reading is *four* apertures, not one: `:FUNC:CONC ON`
        measures voltage and current, and the 2400 auto-zeroes each. So a
        point is `settle + averaging x 4 x NPLC / 50 Hz`, and the rig agrees:
        0.83 s per point at NPLC 1, averaging 10, 50 ms delay (2026-09-04,
        36-point sweeps in 30 s; 71-point in 57 s on 2026-09-02). The model
        this replaced counted one aperture, which put a 36-point budget at
        28 s -- and NI-488 has no 28 s: it rounds a GPIB timeout up to the
        next of 10, 30, 100, 300 s, so three sweeps of ~30 s passed under
        the 30 s it became and the fourth, a moment longer, did not (run
        20260904_214634-024). Twice the model plus 15 s, so a sweep has to
        be badly wrong before it is cut off; the worst case is a run that
        waits a little longer for an instrument that is not answering.
        """
        per_point_s = (settle_s
                       + max(1, int(self.config.averaging)) * 4.0 * self.config.nplc / 50.0)
        return 15.0 + 2.0 * points * per_point_s

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
