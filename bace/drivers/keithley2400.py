"""Keithley 2400 SourceMeter — V_oc, J_sc, J_sat, the J–V sweep, and the panel.

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

**The panel** (`PanelSetup`, `apply_panel`, `read_panel`, 2026-09-07) is the
one thing here that is not from the original: the instrument driven by hand,
configured once and then read many times, for the console's SMU panel. It has
to be separate from the three routines above precisely because each of those
opens with `*RST` — correctly, since each is a measurement with its own state
— and a panel built out of them would drop the output between every reading.
The two directions of that separation are both load-bearing: `_prepare` clears
the panel, so a measurement leaves the instrument in none of the state a panel
put there and the panel knows; and the panel senses **both** quantities
(`:SENS:FUNC 'VOLT:DC','CURR:DC'`), which is the one place in this driver
where the V element of a `:READ?` is a measurement rather than the setpoint
`sweep_points` deliberately files.
"""
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


PANEL_FUNCTIONS: tuple[str, ...] = ("voltage", "current")
"""What the panel can source. The 2400 sources one of the two and senses the
other -- and, with `:FUNC:CONC ON`, senses both at once, which is what its own
display shows and what a panel needs."""

PANEL_STATIC: tuple[str, ...] = ("function", "terminals", "four_wire")
"""The three panel fields that are not changed under a live output.

Not a rule invented here. `:ROUT:TERM` throws a physical switch inside the
instrument and `:SYST:RSEN` decides which pair of leads is the voltmeter, so
both are a change of *wiring*; and swinging the source between volts and amps
with the output on takes the device through whatever the transition happens to
be. Everything else -- the level, either compliance, the integration time, the
filter, the source range -- is what the 2400's own knob and keys change with
the output on, so the panel changes those live too."""


NPLC_LIMITS = (0.01, 10.0)
AVERAGING_LIMITS = (1, 100)
"""What `PanelSetup` accepts, named because the budget ceiling below is
derived from them and the two must not drift apart."""


def panel_budget_for(nplc: float, averaging: int) -> float:
    """How long one `read_panel` under *these* settings can take, with room.

    A free function because the number is wanted for a setup that is not
    applied yet: the console sizes the wait for a source change on the
    settings the change is *asking* for, and merging them onto the panel the
    instrument holds is the job's own work, on the worker, afterwards.

    `sweep_budget_s`'s model for a single point with no settle, and for the
    same measured reason: an averaged reading is **four** apertures, because
    `:FUNC:CONC ON` measures voltage and current and the 2400 auto-zeroes
    each. NPLC 10 with a 100-deep filter -- both legal, both accepted -- is
    `100 x 4 x 10 / 50 Hz` = 80 s of integration, and the 15 + 2x around it is
    the margin `sweep_budget_s` explains: NI-488 rounds a GPIB timeout up to
    the next of 10/30/100/300 s, and a budget that lands just under one of
    those is a read cut off mid-integration.
    """
    return 15.0 + 2.0 * max(1, int(averaging)) * 4.0 * float(nplc) / 50.0


PANEL_BUDGET_MAX_S = panel_budget_for(NPLC_LIMITS[1], AVERAGING_LIMITS[1])
"""The longest read this panel will ever accept -- NPLC 10 with a 100-deep
filter, both legal on a 2400.

Wanted where the caller cannot know which setup the worker is inside. Shutdown
is the case: it sizes its wait before it can see what a source change already
in flight is about to apply, and a wait sized on the panel that change is
*replacing* ends with the worker killed mid-read and the queued output-off
never run. A ceiling costs nothing there, because the wait is a bound and not
a sleep -- the worker finishing early ends it."""


@dataclass(frozen=True)
class PanelSetup:
    """One state of the front panel: what is sourced, at what level, inside
    what limits.

    `SourceMeterConfig` beside it is the bench's *measurement* configuration --
    three settle times, one per DC quantity, because a routine is sequencing
    them. This is the panel's: no settle, because nobody is sequencing
    anything, and a `function` and a `level`, because on a panel those are the
    two controls there are.
    """

    function: str = "voltage"
    """`voltage` sources volts and the current is the measurement; `current`
    the other way round. Sourcing 0 A is the open-circuit voltage and sourcing
    0 V the short-circuit current -- the same two points `measure_voc` and
    `measure_jsc` take, by hand and without filing anything."""

    level: float = 0.0
    """Volts or amps, per `function`. The knob."""

    current_compliance_a: float = 0.05
    voltage_compliance_v: float = 2.0
    """Both are sent whichever is sourced: which one bites depends on the
    function, and a panel whose function the operator can swap would otherwise
    carry the other one's stale limit into the swap."""

    nplc: float = 1.0
    averaging: int = 1
    terminals: str = "FRON"
    four_wire: bool = False
    """As `SourceMeterConfig`. The 2400's range is 0.01 to 10 NPLC and 1 to 100
    readings in the filter, and this refuses outside it rather than letting the
    instrument refuse into a log nobody reads."""

    source_range: float | None = None
    """The source range to hold, or None for the instrument's own autorange.

    Fixed is what a sweep wants (`sweep_points` picks one for the whole curve
    so no range change lands mid-curve, output on). Auto is what a front panel
    opens on and what this defaults to: the operator is about to move the level
    by decades, and a fixed range would clip rather than follow."""

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
        """The unit `level` is in: `V` sourcing volts, `A` sourcing amps."""
        return "V" if self.function == "voltage" else "A"

    @property
    def limit(self) -> float:
        """The compliance that bites in this function -- amps while volts are
        sourced, volts while amps are."""
        return (self.current_compliance_a if self.function == "voltage"
                else self.voltage_compliance_v)

    @classmethod
    def from_config(cls, config: SourceMeterConfig, **over) -> "PanelSetup":
        """The panel a bench opens on: `rig.toml` and `run.toml`'s limits and
        timing, sourcing 0 V. Nothing is invented -- every field the two
        dataclasses share comes across, and the two the panel adds
        (`function`, `level`) open at the safest pair there is."""
        return cls(current_compliance_a=config.current_compliance_a,
                   voltage_compliance_v=config.voltage_compliance_v,
                   nplc=config.nplc, averaging=config.averaging,
                   terminals=config.terminals, four_wire=config.four_wire,
                   **over)


@dataclass(frozen=True)
class PanelReading:
    """One panel reading: both senses, and the compliance annunciator.

    `compliance` is the 2400's own `Cmpl` light (`:SENS:…:PROT:TRIP?`), not a
    comparison made here -- the instrument knows it is clamping and a reading
    taken at the limit is the source's limit, not the device's answer."""

    volts: float
    amps: float
    compliance: bool | None
    function: str
    level: float

    @property
    def ohms(self) -> float | None:
        """V/I, or None at zero current. Not `:SENS:FUNC 'RES'` -- that would
        put the instrument in its own resistance mode; this is the division
        the front panel's MATH does, and it says nothing at 0 A rather than
        dividing by it."""
        return None if self.amps == 0.0 else self.volts / self.amps


class Keithley2400:
    """One SMU session. Not thread-safe."""

    def __init__(self, resource, *, config: SourceMeterConfig = SourceMeterConfig(),
                 timeout_ms: int = 20000):
        self._io = resource
        self._io.timeout = timeout_ms
        self.config = config
        self._output = False
        self._panel: PanelSetup | None = None

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

    # -- the front panel --------------------------------------------------
    # Configure once, read many: the instrument holds a source and the caller
    # asks it for numbers, which is what the 2400's own front panel does and
    # what none of the routines below can do. They each open with `*RST` --
    # correctly, because each is a measurement with its own state -- and that
    # reset is why a panel cannot be built out of them: it would drop the
    # output between every reading.
    @property
    def panel(self) -> PanelSetup | None:
        """The panel state the instrument is in, or None when it is in none.

        `_prepare` clears it, so the first `measure_dc` or sweep after a panel
        session leaves the instrument in none of the state the panel put there
        and the panel knows. A panel that went on reading across a run would
        be reading a source somebody else configured."""
        return self._panel

    def apply_panel(self, setup: PanelSetup) -> PanelSetup:
        """Put the instrument into `setup` and leave it there.

        The **first** call configures from `*RST`, which is also what makes
        the output off; from then on only what moved is written, with the
        output wherever it is. That is the whole difference between a panel
        and a measurement: `_prepare`'s reset is deliberately not used here.

        `SourceMeterError` when a `PANEL_STATIC` field is changed under a live
        output -- the caller switches the output off, changes it and switches
        it back on, which is the sequence the instrument's own keys enforce --
        and when the output is live with **no** panel applied, which is the
        instrument somebody else left driving. The first call has to `*RST`
        and that would drop their output without saying so; refusing says it.
        """
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
            # A partial write: the level may have landed and the compliance
            # behind it failed, so the instrument is in neither `was` nor
            # `setup` and this driver cannot say which. Leaving `_panel` on the
            # old setup would have it report a source at a level it is no
            # longer at -- under-reporting a live one, since the level is
            # written before the limits. `None` is what "in a state this
            # driver did not configure" already means everywhere else here:
            # the panel reads unapplied, the output cannot be switched on, and
            # applying again is a `*RST` back to a known instrument.
            self._panel = None
            raise
        self._panel = setup
        return setup

    def _panel_cold(self, setup: PanelSetup) -> None:
        """From whatever the instrument was in, into the panel."""
        self._io.write("*RST")
        self._output = False
        self._io.write("*CLS;")
        # Both senses at once. Sourcing volts, the current is the measurement
        # and the voltage is the evidence the source is holding it -- and this
        # is the one place in this driver where the V element *is* a
        # measurement, because `:SENS:FUNC` asks for it. (`sweep_points` does
        # the opposite on purpose: only current is sensed there, so it files
        # the setpoint for V rather than the element the 2400 fills in.)
        self._io.write(":FUNC:CONC ON;")
        self._io.write(":SENS:FUNC 'VOLT:DC','CURR:DC';")
        # ASCII, because `read_panel` parses the reply as text. `*RST` has
        # just made it ASCII anyway; said out loud so the parse's assumption
        # is written down beside it rather than inherited.
        self._io.write(":FORM ASC;:FORM:ELEM VOLT,CURR;")
        self._io.write(":TRAC:FEED:CONT NEV;")
        self._io.write(":RES:MODE MAN;")
        self._io.write(":TRIG:COUN 1;")
        self._io.write(f":ROUT:TERM {setup.terminals};")
        self._io.write(f":SYST:RSEN {'ON' if setup.four_wire else 'OFF'};")
        # `:SOUR:DEL:AUTO` is left as `*RST` leaves it -- on. A sweep times its
        # own settle because the curve's points have to be comparable; a panel
        # reading is asked for one at a time and the instrument's own delay is
        # the right one.
        self._panel_write(None, setup)

    def _panel_write(self, was: PanelSetup | None, setup: PanelSetup) -> None:
        """Write what moved between `was` and `setup`; `was` None writes all
        of it. Only what moved, so a level typed twice a second does not
        re-send a compliance and a filter with it."""
        def moved(*names: str) -> bool:
            return was is None or any(getattr(was, n) != getattr(setup, n) for n in names)

        mode = "VOLT" if setup.function == "voltage" else "CURR"

        # A compliance that is being **tightened** is written before the level,
        # a loosened one after it. One request can do both -- 0 V on a 50 mA
        # limit to 5 V on a 1 mA limit is a plausible click -- and with the
        # output live the two commands are separate writes on the bus. Level
        # first, the device sees 5 V at up to the old 50 mA for as long as the
        # next write takes; that is a real amount of charge through a cell
        # somebody is measuring. Tightening first cannot hurt: the instrument
        # clamps at the new limit while it is still at the old level.
        #
        # The rule is "whichever change narrows what the device may see goes
        # first", which is also why a *loosened* compliance waits: it must not
        # widen the limit while the level is still the old, higher one.
        tighter: list[str] = []
        looser: list[str] = []
        for name, command in (
                ("current_compliance_a",
                 f":SENS:CURR:PROT:LEV {setup.current_compliance_a:g};"),
                ("voltage_compliance_v",
                 f":SENS:VOLT:PROT:LEV {setup.voltage_compliance_v:g};")):
            if not moved(name):
                continue
            # `was is None` is the cold path, where the output is off and the
            # order cannot matter -- before the level is the safe default.
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
        """One reading: both senses, and the compliance annunciator.

        **The output has to be on.** With it off a `:READ?` still triggers and
        still answers -- with the source disconnected inside the instrument,
        so near-zero volts and near-zero amps, which looks exactly like a
        measurement of a device that is not there. The 2400 shows dashes in
        that state; this raises rather than writing the same lie in numbers.
        """
        panel = self._panel
        if panel is None:
            raise SourceMeterError("the panel is not applied to this instrument")
        if not self._output:
            raise SourceMeterError("the output is OFF: there is nothing to read")
        # The session's own timeout is sized for a bench's ordinary traffic
        # and this one read can legally take 80 s (`panel_budget_s`), so it
        # is raised for the query and put back -- exactly what `sweep_points`
        # does with `sweep_budget_s`, and for the same reason: a read cut off
        # at the VISA layer is indistinguishable from an instrument that has
        # stopped answering.
        budget_ms = int(self.panel_budget_s() * 1000)
        old_timeout = getattr(self._io, "timeout", None)
        raise_it = old_timeout is not None and budget_ms > old_timeout
        if raise_it:
            self._io.timeout = budget_ms
        try:
            raw = self._io.query(":READ?").strip().split(",")
            # Which limit can bite is decided by what is being sourced, so
            # only that one is asked for: the other's TRIP is meaningless.
            node = "CURR" if panel.function == "voltage" else "VOLT"
            tripped = on_off(ask(self._io, f":SENS:{node}:PROT:TRIP?"))
        finally:
            if raise_it:
                self._io.timeout = old_timeout
        return PanelReading(volts=float(raw[0]), amps=float(raw[1]),
                            compliance=tripped, function=panel.function,
                            level=panel.level)

    # -- the three DC quantities -----------------------------------------
    def _prepare(self) -> None:
        """`*RST` clears compliance, ranges and mode, so everything that
        matters has to be re-sent after it. The original resets before each of
        the three measurements; kept, because it also clears any state a
        previous aborted run left behind."""
        self._io.write("*RST")
        self._output = False
        # The reset has just undone every panel command, so the panel is no
        # longer applied and `read_panel` says so instead of reading a
        # source this routine is about to configure for itself.
        self._panel = None
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

    def panel_budget_s(self) -> float:
        """How long one `read_panel` can take, with room.

        `sweep_budget_s`'s model for a single point with no settle, and for
        the same measured reason: an averaged reading is **four** apertures,
        because `:FUNC:CONC ON` measures voltage and current and the 2400
        auto-zeroes each.

        It matters here because the panel accepts what a panel should --
        NPLC 10 and a 100-deep filter are both legal on this instrument --
        and that pair is `100 x 4 x 10 / 50 Hz` = **80 s** of integration.
        A caller waiting a flat 30 s on it is telling the operator that a
        perfectly healthy read failed, and a VISA session left at 20 s cuts
        the read off before the instrument has finished it.
        """
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
