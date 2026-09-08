"""The Keithley 2400 SourceMeter, panel mode only — a standalone copy.

**A deliberate second copy** of the panel half of `bace/drivers/keithley2400.py`,
for the same reason `examples/shutter_console` carries its own `ctypes` block:
this folder has to run on a machine that has the instrument and a Python and
nothing else. Go to the package's driver for the real thing — the three DC
quantities, the J-V sweep, the sign conventions, and the bench history behind
each of them. None of that is here; a front panel does not need it.

What *is* here is the part a panel is: configure once, read many, with `*RST`
only on the way in and only what moved written after that. The package driver
explains why that cannot be built out of its measurement routines (each opens
with `*RST`, which would drop the output between every reading), and why the
panel senses **both** quantities — `:SENS:FUNC \'VOLT:DC\',\'CURR:DC\'` makes the
V element of a `:READ?` a measurement rather than a setpoint.

Confirmed on the bench as `KEITHLEY INSTRUMENTS INC.,MODEL 2400,4473504,C34 Sep
21 2016` at `GPIB0::24::INSTR`. A plain 2400: 200 V / 1 A / 20 W.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


def ask(io: Any, query: str) -> str | None:
    """The instrument's reply, stripped, or None when the query fails.

    Never raises. `read_output` below is the caller that matters: a `:OUTP?`
    that throws during an unwind would mask the exception already propagating,
    and this console asks it on every path that decides whether the source is
    live.
    """
    try:
        reply = io.query(query)
    except Exception:                                        # noqa: BLE001
        return None
    text = str(reply).strip()
    return text if text else None


def on_off(reply: str | None) -> bool | None:
    """`:OUTP?`: `1`/`ON` -> True, `0`/`OFF` -> False, anything else -> None.

    **None is "it did not say", and the console keeps it distinct from off.**
    """
    if reply is None:
        return None
    up = reply.strip().upper().rstrip(";")
    if up in ("1", "ON", "+1"):
        return True
    if up in ("0", "OFF", "+0"):
        return False
    return None


class SourceMeterError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceMeterConfig:
    """The bench's limits and timing, as `run.toml` gives them.

    Only the fields a panel opens on; the package's version carries the three
    settle times the measurement routines need as well.
    """

    current_compliance_a: float = 0.05
    """The most current the 2400 will pass while it sources voltage
    (`:SENS:CURR:PROT:LEV`); it clamps there rather than going higher. Sized
    for the pixel, not the instrument -- a 2400 will happily deliver 1 A into
    a small cell."""

    voltage_compliance_v: float = 2.0
    """The same the other way round, while it sources current."""

    nplc: float = 1.0
    """Integration time in power-line cycles. 1 NPLC is 20 ms on 50 Hz mains
    and rejects its hum."""

    averaging: int = 1
    """How many readings the 2400 averages into each one it reports; 1 is the
    filter off."""

    terminals: str = "FRON"          # FRON or REAR
    """Which set of terminals the source comes out of."""

    four_wire: bool = False
    """Remote sense. Four-wire removes the lead resistance from the
    measurement and needs the extra pair actually connected."""


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

    The package driver's measured model for a single point with no settle,
    and for the same reason: an averaged reading is **four** apertures, because
    `:FUNC:CONC ON` measures voltage and current and the 2400 auto-zeroes
    each. NPLC 10 with a 100-deep filter -- both legal, both accepted -- is
    `100 x 4 x 10 / 50 Hz` = 80 s of integration, and the 15 + 2x around it
    is margin the bench earned: NI-488 rounds a GPIB timeout up to the next of
    10/30/100/300 s, and a budget that lands just under one of those is a read
    cut off mid-integration.
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
    the other way round. Sourcing 0 A is the open-circuit voltage and
    sourcing 0 V the short-circuit current -- the same two points the package's
    `measure_voc` and `measure_jsc` take, but by hand and without filing
    anything."""

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

        In the package driver a measurement clears this, so a panel session
        and a run cannot be mistaken for each other. Here there is nothing to
        clear it but `apply_panel` itself -- this folder only ever drives the
        instrument by hand."""
        return self._panel

    def apply_panel(self, setup: PanelSetup) -> PanelSetup:
        """Put the instrument into `setup` and leave it there.

        The **first** call configures from `*RST`, which is also what makes
        the output off; from then on only what moved is written, with the
        output wherever it is. That is the whole difference between a panel
        and a measurement, which resets on every call.

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
        # does for a sweep, and for the same reason: a read cut off
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


    def panel_budget_s(self) -> float:
        """How long one `read_panel` can take, with room.

        `panel_budget_for` on whatever is applied, or on the configuration
        when nothing is.

        It matters because the panel accepts what a panel should --
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
