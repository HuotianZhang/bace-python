"""The Keithley 2400 measures DC quantities and supplies the console panel.

The bench instrument is a Model 2400 at `GPIB0::24::INSTR`, serial 4473504,
firmware C34, with a 200 V, 1 A, 20 W envelope. The relay connects it to the
device separately from the bias amplifier. `output_enabled` supplies the
relay interlock in `bace.drivers.routing`.

The DC sequence follows the LabVIEW `measJsc,Voc.vi` commands with compliance
set before output enable and voltage compliance used while sourcing current.
Returned current keeps the instrument sign: negative at 0 V under light.
The LabVIEW `positive jSC?` control does not affect its command sequence;
this driver also leaves sign conversion to analysis.

The panel configures once and reads repeatedly. DC routines reset before
each measurement to clear state from interrupted runs; using them for panel
reads drops the output between readings. `_prepare` clears the panel state.
The panel senses both V and A, so its voltage element is measured. The
current-only sweep instead records the source setpoint as voltage."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from .protocols import DCPoint
from .readback import ask, on_off


class SourceMeterError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMeterConfig:
    """Define source-meter limits and timing.

    The three DC settle times differ because V_oc on a slow device settles
    more slowly than short-circuit current."""

    current_compliance_a: float = 0.05
    """The most current the 2400 will pass while it sources voltage
    (`:SENS:CURR:PROT:LEV`); it clamps there rather than going higher.
    The limit protects the device; the 2400 can deliver 1 A into a small cell."""

    voltage_compliance_v: float = 2.0
    """The most voltage in V the 2400 will put across the device while it sources
    current (`:SENS:VOLT:PROT:LEV`).
    At 0 A for V_oc, this bounds voltage if a contact opens."""

    settle_jsc_ms: float = 500.0
    """How long, in ms, the 2400 holds 0 V before reading the current, for J_sc.
    A shorter wait can measure the device's RC response instead of steady state."""

    settle_voc_ms: float = 500.0
    """How long, in ms, the 2400 holds 0 A before reading the voltage, for V_oc.
    A slow device needs the longest wait here. V_oc centres the scan axis."""

    settle_jsat_ms: float = 500.0
    """How long, in ms, the 2400 holds `v_sat` before reading the current, for J_sat."""

    nplc: float = 1.0
    """How long the 2400 integrates each reading, in **power-line cycles**
    (`:SENS:*:NPLC`).

    At 50 Hz, 1 NPLC is 20 ms and rejects mains hum over one full cycle.
    Lower values are faster and noisier; higher values are slower and quieter.
    Integration time scales with point count, averaging and `nplc`; the full
    budget includes auto-zero apertures in `Keithley2400.sweep_budget_s`."""

    averaging: int = 1
    """How many readings the 2400 averages into each point (`:AVER:COUN`, with
    a repeating filter).
    A value of 1 disables the filter. Each extra reading adds integration time,
    so averaging multiplies the sweep duration together with `nplc`."""

    terminals: str = "FRON"          # FRON or REAR
    """Which set of terminals on the 2400 is live (`:ROUT:TERM`): `FRON` the
    front panel, `REAR` the back.
    `FRON` is the front panel; `REAR` is the back. This must match device wiring,
    or the source meter reads an open circuit."""

    four_wire: bool = False
    """Kelvin sensing (`:SYST:RSEN ON`): the 2400 measures voltage on a
    separate pair of leads, so the reading excludes the drop down the current
    leads.
    Separate voltage leads exclude the current-lead drop. Four wires are required;
    enabling it with two wires gives no reading. This bench uses two-wire sensing."""


PANEL_FUNCTIONS: tuple[str, ...] = ("voltage", "current")
"""List the source functions accepted by the panel.
The 2400 sources one quantity and can sense both V and A simultaneously
with `:FUNC:CONC ON`, as required for the panel display."""

PANEL_STATIC: tuple[str, ...] = ("function", "terminals", "four_wire")
"""List panel fields that require output off before a change.

`:ROUT:TERM` moves an internal switch; `:SYST:RSEN` selects voltage leads.
Both change wiring. Changing source function with output on also exposes
the device to a transition between voltage and current control. Level,
compliance, integration, filter and range can change with output on, as
on the instrument's front panel."""


NPLC_LIMITS = (0.01, 10.0)
AVERAGING_LIMITS = (1, 100)
"""Bound accepted `PanelSetup` values and the maximum read budget.
Sharing these limits prevents validation and shutdown timing from differing."""


def panel_budget_for(nplc: float, averaging: int) -> float:
    """Return a conservative panel-read budget in s for the requested settings.

    The console needs this before the worker applies or merges a setup.
    An averaged reading uses four apertures: concurrent voltage and current
    senses, each with auto-zero. At 50 Hz, NPLC 10 and 100 readings take
    80 s. The margin is 15 s plus twice the integration time, as in
    `Keithley2400.sweep_budget_s`. On this bench NI-488 rounds timeouts up
    to 10, 30, 100 or 300 s; a budget near a boundary can cut off a read."""
    return 15.0 + 2.0 * max(1, int(averaging)) * 4.0 * float(nplc) / 50.0


PANEL_BUDGET_MAX_S = panel_budget_for(NPLC_LIMITS[1], AVERAGING_LIMITS[1])
"""Bound the longest accepted panel read in s: NPLC 10 and 100 readings.

Shutdown cannot know the setup an active worker is applying. A wait based
on the preceding setup can kill that worker mid-read before queued output
disable runs. The maximum is a deadline, not a sleep; early completion ends
the wait immediately."""


@dataclass(frozen=True)
class PanelSetup:
    """Define a panel source function, level and limits.

    `SourceMeterConfig` sequences three DC quantities with separate settle
    times. The panel holds one source state and uses the instrument delay."""

    function: str = "voltage"
    """Select voltage or current sourcing.
    `voltage` sources V and measures A; `current` sources A and measures V.
    At 0 A the reading is V_oc; at 0 V it is short-circuit current, as in
    `Keithley2400.measure_voc` and `Keithley2400.measure_jsc`."""

    level: float = 0.0
    """Set the source level in V or A according to `function`."""

    current_compliance_a: float = 0.05
    voltage_compliance_v: float = 2.0
    """Send both compliance limits for either source function.
    The active limit depends on `function`; setting both prevents a function
    change from activating a stale limit."""

    nplc: float = 1.0
    averaging: int = 1
    terminals: str = "FRON"
    four_wire: bool = False
    """Use the limits and wiring described by `SourceMeterConfig`.
    The panel accepts 0.01 to 10 NPLC and 1 to 100 filter readings. Rejecting
    other values here prevents instrument errors from being missed in a log."""

    source_range: float | None = None
    """Hold a source range in V or A, or use autorange for `None`.

    `Keithley2400.sweep_points` holds one range to avoid changes with output
    on. The panel defaults to autorange so large level changes can be followed
    without clipping at a manually held range."""

    def __post_init__(self) -> None:
        if self.function not in PANEL_FUNCTIONS:
            raise ValueError(f"a 2400 sources volts or amps, not {self.function!r}: "
                             f"one of {', '.join(PANEL_FUNCTIONS)}")
        if self.terminals not in ("FRON", "REAR"):
            raise ValueError(f"terminals is FRON or REAR, not {self.terminals!r}")
        if not NPLC_LIMITS[0] <= float(self.nplc) <= NPLC_LIMITS[1]:
            raise ValueError(f"nplc is between {NPLC_LIMITS[0]:g} and {NPLC_LIMITS[1]:g} "
                             f"on a 2400, not {self.nplc:g}")
        if not AVERAGING_LIMITS[0] <= int(self.averaging) <= AVERAGING_LIMITS[1]:
            raise ValueError(f"averaging is between {AVERAGING_LIMITS[0]} and "
                             f"{AVERAGING_LIMITS[1]} readings, not {self.averaging}")
        if self.source_range is not None and float(self.source_range) <= 0.0:
            raise ValueError("source_range is a positive full-scale value, or null "
                             "for the instrument's autorange")
        if self.current_compliance_a <= 0.0 or self.voltage_compliance_v <= 0.0:
            raise ValueError("a compliance is a positive limit")
        for name in ("level", "current_compliance_a", "voltage_compliance_v", "nplc"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number, not {value}")

    @property
    def unit(self) -> str:
        """Return `V` for voltage sourcing or `A` for current sourcing."""
        return "V" if self.function == "voltage" else "A"

    @property
    def limit(self) -> float:
        """Return active current compliance in A or voltage compliance in V.

        Voltage sourcing uses current compliance; current sourcing uses voltage
        compliance."""
        return (self.current_compliance_a if self.function == "voltage"
                else self.voltage_compliance_v)

    @classmethod
    def from_config(cls, config: SourceMeterConfig, **over) -> "PanelSetup":
        """Build panel settings from configured limits, timing and wiring.

        Shared fields come from `SourceMeterConfig`. Source function and level
        default to voltage at 0 V unless the caller overrides them."""
        return cls(current_compliance_a=config.current_compliance_a,
                   voltage_compliance_v=config.voltage_compliance_v,
                   nplc=config.nplc, averaging=config.averaging,
                   terminals=config.terminals, four_wire=config.four_wire,
                   **over)


@dataclass(frozen=True)
class PanelReading:
    """Hold measured voltage in V, current in A and the compliance state.

    `compliance` comes from the instrument's `:SENS:...:PROT:TRIP?` status.
    A clamped reading reflects a source limit rather than the free device
    response."""

    volts: float
    amps: float
    compliance: bool | None
    function: str
    level: float

    @property
    def ohms(self) -> float | None:
        """Return V/I in ohm, or `None` at 0 A.

        This mirrors front-panel MATH without entering the instrument's separate
        resistance measurement mode."""
        return None if self.amps == 0.0 else self.volts / self.amps


class Keithley2400:
    """Control one source-meter VISA session without concurrent access."""

    def __init__(self, resource, *, config: SourceMeterConfig = SourceMeterConfig(),
                 timeout_ms: int = 20000):
        self._io = resource
        self._io.timeout = timeout_ms
        self.config = config
        self._output = False
        self._panel: PanelSetup | None = None
        self.read_retries = 0
        """Count nonnumeric `:READ?` replies that trigger another measurement."""

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
        """Query `:OUTP?`, return a boolean or `None`, and update `output_enabled`.

        The service refreshes the router's cached interlock flag before relay
        moves and read-back. This detects output enabled outside this driver."""
        on = on_off(ask(self._io, ":OUTP?"))
        if on is not None:
            self._output = on
        return on

    # -- the front panel --------------------------------------------------
    # The panel holds a source state between reads. Measurement routines
    # start with `*RST` to establish their own state; using them for panel
    # reads drops the output each time.
    @property
    def panel(self) -> PanelSetup | None:
        """Return the applied `PanelSetup`, or `None` if no panel state is valid.

        `_prepare` clears it before a DC measurement or sweep so a panel cannot
        keep reading a source configured by another routine."""
        return self._panel

    def apply_panel(self, setup: PanelSetup) -> PanelSetup:
        """Send changed panel settings and return `None`.

        The first application resets with `*RST` and leaves output off. Later
        applications write changed fields while preserving output state.
        `PANEL_STATIC` changes require output off. A live output with no valid
        panel state also raises `SourceMeterError`: resetting it would silently
        drop a source this driver does not own."""
        was = self._panel
        if was is None and self._output:
            raise SourceMeterError(
                "the output is ON and this instrument is not on the panel -- "
                "something else is driving it. Switch the output off first; "
                "applying a panel resets the instrument, which would drop it "
                "with nothing said")
        if was is not None and self._output:
            changed = [f for f in PANEL_STATIC if getattr(setup, f) != getattr(was, f)]
            if changed:
                raise SourceMeterError(
                    f"the output is ON: {', '.join(changed)} cannot be changed under "
                    "it -- switch the output off, change it, switch it back on")
        try:
            if was is None:
                self._panel_cold(setup)
            else:
                self._panel_write(was, setup)
        except Exception:                                    # noqa: BLE001
            # A partial write can leave the source in neither `was` nor `setup`.
            # Keeping the preceding panel state can under-report a live level.
            # `None` marks the state unknown, prevents enabling output and makes the
            # next application reset to a known state.
            self._panel = None
            raise
        self._panel = setup
        return setup

    def _panel_cold(self, setup: PanelSetup) -> None:
        """Reset and send the full panel configuration; return `None`."""
        self._io.write("*RST")
        self._output = False
        self._io.write("*CLS;")
        # Both voltage and current are sensed for the panel. Voltage confirms
        # that the source holds its level. `sweep_points` senses only current
        # and records the voltage setpoint instead.
        self._io.write(":FUNC:CONC ON;")
        self._io.write(":SENS:FUNC 'VOLT:DC','CURR:DC';")
        # Explicit ASCII format matches the text parser in `read_panel`,
        # independently of the reset default.
        self._io.write(":FORM ASC;:FORM:ELEM VOLT,CURR;")
        self._io.write(":TRAC:FEED:CONT NEV;")
        self._io.write(":RES:MODE MAN;")
        self._io.write(":TRIG:COUN 1;")
        self._io.write(f":ROUT:TERM {setup.terminals};")
        self._io.write(f":SYST:RSEN {'ON' if setup.four_wire else 'OFF'};")
        # The panel retains the reset's automatic source delay for single reads.
        # A sweep instead sets an explicit settle time to make points comparable.
        self._panel_write(None, setup)

    def _panel_write(self, was: PanelSetup | None, setup: PanelSetup) -> None:
        """Send fields changed between `was` and `setup`; return `None`.

        `was=None` sends all fields. Avoiding unchanged writes lets frequent
        level adjustments leave compliance and filters undisturbed."""
        def moved(*names: str) -> bool:
            return was is None or any(getattr(was, n) != getattr(setup, n) for n in names)

        mode = "VOLT" if setup.function == "voltage" else "CURR"

        # Tighter compliance precedes a level change; looser compliance follows.
        # For example, 0 V at 50 mA to 5 V at 1 mA must not expose the device to
        # 5 V at 50 mA during the next bus write. Tightening first clamps at
        # 1 mA while still at 0 V.
        #
        # Loosening waits so a lower requested level takes effect before a
        # higher limit can pass charge at the preceding level.
        tighter: list[str] = []
        looser: list[str] = []
        for name, command in (
                ("current_compliance_a",
                 f":SENS:CURR:PROT:LEV {setup.current_compliance_a:g};"),
                ("voltage_compliance_v",
                 f":SENS:VOLT:PROT:LEV {setup.voltage_compliance_v:g};")):
            if not moved(name):
                continue
            # `was is None` means output is off; compliance before level remains
            # the safe default.
            widening = was is not None and getattr(setup, name) > getattr(was, name)
            (looser if widening else tighter).append(command)

        if moved("function"):
            self._io.write(f":SOUR:FUNC:MODE {mode};")
        if moved("function", "source_range"):
            if setup.source_range is None:
                self._io.write(f":SOUR:{mode}:RANG:AUTO ON;")
            else:
                self._io.write(f":SOUR:{mode}:RANG:AUTO OFF;"
                               f":SOUR:{mode}:RANG {float(setup.source_range):g};")
        for command in tighter:
            self._io.write(command)
        if moved("function", "level", "source_range"):
            self._io.write(f":SOUR:{mode}:LEV {float(setup.level):g};")
        for command in looser:
            self._io.write(command)
        if moved("nplc"):
            self._io.write(f":SENS:CURR:NPLC {setup.nplc:g};"
                           f":SENS:VOLT:NPLC {setup.nplc:g};")
        if moved("averaging"):
            if int(setup.averaging) > 1:
                self._io.write(f":AVER ON;:AVER:COUN {int(setup.averaging)};"
                               ":AVER:TCON REP;")
            else:
                self._io.write(":AVER OFF;")
        # `_panel_cold` has already sent these two; only a later change does.
        if was is not None and moved("terminals"):
            self._io.write(f":ROUT:TERM {setup.terminals};")
        if was is not None and moved("four_wire"):
            self._io.write(f":SYST:RSEN {'ON' if setup.four_wire else 'OFF'};")

    def read_panel(self) -> PanelReading:
        """Send `:READ?` and return V, A and the active compliance status.

        Output must be on. With output off the 2400 still triggers and replies,
        but its internally disconnected source gives near-zero V and A. This
        can resemble an absent device; the front panel shows dashes. The method
        raises rather than recording those values as measurements."""
        panel = self._panel
        if panel is None:
            raise SourceMeterError("the panel is not applied to this instrument")
        if not self._output:
            raise SourceMeterError("the output is OFF: there is nothing to read")
        # A panel read can take 80 s at valid settings (`panel_budget_s`).
        # Raise the session timeout for this query and restore it afterwards,
        # so a valid integration is not mistaken for an unresponsive instrument.
        budget_ms = int(self.panel_budget_s() * 1000)
        old_timeout = getattr(self._io, "timeout", None)
        raise_it = old_timeout is not None and budget_ms > old_timeout
        if raise_it:
            self._io.timeout = budget_ms
        try:
            raw = self._read()
            # Only the protection status opposite the sourced quantity is meaningful.
            node = "CURR" if panel.function == "voltage" else "VOLT"
            tripped = on_off(ask(self._io, f":SENS:{node}:PROT:TRIP?"))
        finally:
            if raise_it:
                self._io.timeout = old_timeout
        return PanelReading(volts=float(raw[0]), amps=float(raw[1]),
                            compliance=tripped, function=panel.function,
                            level=panel.level)

    def _read(self) -> list[float]:
        """Query `:READ?` and return numbers, retrying one nonnumeric reply.

        On this bench, a GPIB reply can contain `)1*088301A)08` in place of
        `+1.088301E-08`, with bits 1 and 2 absent from nondigit characters.
        A second query measures at the same level and costs one reading's time.
        A second bad reply raises and includes both replies for diagnosis."""
        replies = []
        for _ in range(2):
            raw = str(self._io.query(":READ?")).strip()
            replies.append(raw)
            try:
                return [float(x) for x in raw.split(",")]
            except ValueError:
                self.read_retries += 1
        raise SourceMeterError(
            f"the 2400 answered :READ? with {replies[0]!r} and then {replies[1]!r}, "
            "neither a number: check the GPIB cables and that every instrument on "
            "the bus is powered")

    # -- the three DC quantities -----------------------------------------
    def _prepare(self) -> None:
        """Reset and restore measurement wiring and averaging; return `None`.

        `*RST` clears compliance, ranges and mode, including state retained
        from an interrupted run. Required settings must follow every reset."""
        self._io.write("*RST")
        self._output = False
        # The reset has just undone every panel command, so the panel is no
        # longer applied and `read_panel` says so instead of reading a
        # source this routine is about to configure for itself.
        self._panel = None
        self._io.write(f":ROUT:TERM {self.config.terminals};")
        # Reset selects two-wire sensing. Send the configured sense mode after
        # it so four-wire measurements use the separate voltage leads.
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
        # Compliance must precede output enable. Otherwise the device sees the
        # reset's limit between enable and the next command.
        self.enable_output(True)
        time.sleep(settle_ms / 1000.0)
        value = self._read()[0]
        self.disable_output()
        return value

    def _source_current_measure_voltage(self, level_a: float, settle_ms: float) -> float:
        self._prepare()
        self._io.write(":SOUR:FUNC:MODE CURR;")
        self._io.write(f":SOUR:CURR:LEV {level_a:g};")
        self._io.write(":SENS:FUNC 'VOLT:DC';")
        # Current sourcing requires voltage compliance; current protection does
        # not bound it. At 0 A this limits output voltage if a contact opens.
        self._io.write(f":SENS:VOLT:PROT:LEV {self.config.voltage_compliance_v:g};")
        self._io.write(f":SENS:VOLT:NPLC {self.config.nplc:g};")
        self._io.write(":FORM:ELEM VOLT;")
        self.enable_output(True)
        time.sleep(settle_ms / 1000.0)
        value = self._read()[0]
        self.disable_output()
        return value

    def measure_jsc(self, settle_ms: float | None = None) -> float:
        """Source 0 V, settle, read current in A and disable output.

        `settle_ms` is in ms. Current is negative under illumination."""
        return self._source_voltage_measure_current(
            0.0, self.config.settle_jsc_ms if settle_ms is None else settle_ms)

    def measure_voc(self, settle_ms: float | None = None) -> float:
        """Source 0 A, settle, return V_oc in V and disable output.

        `settle_ms` is in ms."""
        return self._source_current_measure_voltage(
            0.0, self.config.settle_voc_ms if settle_ms is None else settle_ms)

    def measure_jsat(self, v_sat: float, settle_ms: float | None = None) -> float:
        """Source `v_sat` in V and return saturation current in A after settling.

        The reverse voltage must complete extraction. `settle_ms` is in ms.
        Output is disabled after the read."""
        return self._source_voltage_measure_current(
            v_sat, self.config.settle_jsat_ms if settle_ms is None else settle_ms)

    def measure_dc(self, *, v_sat: float, settle_s: float | None = None) -> DCPoint:
        """Measure short-circuit current, V_oc and saturation current in that order.

        Return `DCPoint` with voltages in V and currents in A. `v_sat` is in V.
        `settle_s`, in s, overrides all three configured waits. Real data needs
        the per-quantity waits; a short override is for simulation checks."""
        ms = None if settle_s is None else settle_s * 1000.0
        jsc = self.measure_jsc(ms)
        voc = self.measure_voc(ms)
        jsat = self.measure_jsat(v_sat, ms)
        return DCPoint(voc=voc, jsc=jsc, jsat=jsat, v_sat=v_sat)

    # -- J-V sweep --------------------------------------------------------
    def sweep_points(self, start_v: float, stop_v: float, points: int, *,
                     settle_s: float = 0.0):
        """Source each voltage, settle, read current and yield `(V, A)` pairs.

        `start_v` and `stop_v` are in V; `settle_s` is in s. Each yielded point
        lets the J-V experiment display and save partial data and stop between
        points. On this bench GPIB round trips cost about 30 ms per point
        against 0.8 s of measurement; an instrument sweep occupies the bus for
        about 30 s at 36 points or one minute at 71 points.

        One source range covers both ends, as with the instrument sweep's
        `BEST` setting. Autoranging a voltage source through +/-0.2 V can change
        range with output on, causing a glitch and a change in source behaviour.
        The range is therefore set before output enable. `:SOUR:DEL` times each
        settle inside the trigger model, independent of host and storage delays.
        It also disables automatic source delay.

        Voltage in the yielded pair is the sent setpoint. Only current is sensed:
        on this bench `:SENS:FUNC?` answers `CURR:DC`. With the `REP` filter and
        current autorange, the unsensed voltage element can lag by 0.09 V,
        closing about 17 % of the gap per point and returning to the setpoint
        at each current range change. Treating it as measured voltage adds
        0.09 V kinks near 1 V and lowers maximum-power voltage, maximum power
        and fill factor by 10 to 15 % on this bench.

        For two-wire operation the source setpoint estimates device voltage
        within source accuracy, 0.02 % plus 0.6 mV, against sub-millivolt lead
        drop on this bench. Four-wire operation requires actual voltage sensing.
        `:FORM:ELEM CURR` requests only the measured quantity.

        This J-V operation stays outside `SourceMeter` so transient routines
        cannot start a sweep through that interface."""
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
                raw = self._read()
                # The yielded voltage matches the formatted level sent, including
                # rounding of `numpy.linspace` values such as 0.49999999999999994.
                yield float(level), float(raw[0])
        finally:
            self.last_sweep_s = time.monotonic() - started
            self.disable_output()
            if raise_it:
                self._io.timeout = old_timeout

    def sweep(self, start_v: float, stop_v: float, points: int, *,
              settle_s: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Collect `sweep_points` and return voltage in V and current in A as arrays."""
        got = list(self.sweep_points(start_v, stop_v, points, settle_s=settle_s))
        return (np.array([v for v, _ in got], dtype=float),
                np.array([i for _, i in got], dtype=float))

    def sweep_budget_s(self, points: int, settle_s: float = 0.0) -> float:
        """Return a conservative sweep timeout in s for `points` and `settle_s`.

        Concurrent V and A senses each auto-zero, giving four apertures per
        averaged reading. The model is settle + averaging * 4 * NPLC / 50 Hz.
        Bench timings are 0.83 s per point at NPLC 1, averaging 10 and 50 ms
        delay: 36 points in 30 s and 71 in 57 s.

        On this bench NI-488 rounds timeouts up to 10, 30, 100 or 300 s. A
        28 s budget becomes 30 s and can cut off a slightly longer sweep.
        Twice the model plus 15 s leaves margin; an unresponsive instrument
        can therefore take longer to time out. `sweep_points` uses `points=1`
        for each read; callers can budget the whole curve."""
        per_point_s = (settle_s
                       + max(1, int(self.config.averaging)) * 4.0 * self.config.nplc / 50.0)
        return 15.0 + 2.0 * points * per_point_s

    def panel_budget_s(self) -> float:
        """Return the panel-read timeout in s for the applied or default settings.

        `panel_budget_for` allows four apertures for concurrent V and A senses
        with auto-zero. NPLC 10 with 100 readings is valid and takes 80 s at
        50 Hz. A 20 s or 30 s timeout interrupts a healthy read."""
        panel = self._panel
        return panel_budget_for(
            panel.nplc if panel is not None else self.config.nplc,
            panel.averaging if panel is not None else self.config.averaging)

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
