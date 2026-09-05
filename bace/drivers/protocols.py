"""The contracts every instrument implements.

Eight roles, each with a real driver and a simulated one. Nothing here inherits
from anything and nothing registers itself: these are `typing.Protocol`
definitions, so a class satisfies one by having the right methods. That is the
whole substitution mechanism — swap an instrument by passing a different
object, never by editing the experiment.

The protocols are deliberately narrow. Only what the experiment layer actually
calls belongs here; a real driver may have twenty more methods and still fit.
Widening a protocol forces work on every implementation, so it should happen
only when the experiment genuinely needs the new call.

Note the direction of the dependency: this module imports nothing from
`core/`, and `core/` imports nothing from here. The four numbers that describe
a pulse (`high`, `low`, `delay`, `width`) cross the boundary as floats, not as
a `core.pulses.PulseLevels`, so a driver never has to know the physics module
exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ContextManager, Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Trace:
    """One acquired transient.

    `y` is **current in amps**, already divided by the sense resistor AND
    already carrying the rig's sign convention (`RigConfig.current_sign`,
    -1 on this bench). The LabVIEW scope driver did the division inside its
    fetch, and every real and simulated digitizer here reproduces it in the
    same place, with the sign beside it. Applying `core.process.to_current` to
    this would scale charges by 1/R a second time; negating it would undo a
    convention the file already records.

    `t0` is the record's own time origin as the instrument reports it
    (`:WAV:XOR?`). The processing chain does not use it: it integrates on
    `arange(n)*dt`, matching the original engine, so `t0_int` is measured from
    the *start of the record*, not from the trigger. Keep `t0` anyway — it is
    the only record of where the trigger sat, and any future re-referencing
    needs it.
    """

    y: np.ndarray
    dt: float
    t0: float = 0.0
    count: int | None = None
    """How many acquisitions the digitiser says it folded into this record
    (`:WAV:COUN?` on the Infiniium), read back after every acquisition since
    2026-09-05. None when the driver cannot say. Until then nothing checked
    that 200 hardware averages were 200: both this port and the LabVIEW
    original waited on a done flag the instrument sets per acquisition."""

    @property
    def n(self) -> int:
        return int(self.y.size)

    @property
    def duration_s(self) -> float:
        return (self.n - 1) * self.dt


# -- bias -----------------------------------------------------------------
@runtime_checkable
class BiasSource(Protocol):
    """The pulse generator that applies V_pre then V_coll (81150A).

    Levels are volts **at the generator output**: already divided by the
    amplifier gain, and already swapped-and-negated if the sample polarity is
    inverted. The driver does no physics.
    """

    def configure_shape(self, frequency_hz: float, *, duty_percent: float = ...,
                        edge_time_s: float = ...,
                        inverted_output: bool | None = ...) -> None:
        """`inverted_output=None` means **leave the output polarity alone**.

        Which level the device rests at between pulses depends on how the sample
        is wired, and that is not always known. Writing a polarity then is a
        guess, and a wrong guess moves the extraction to the other edge of the
        pulse -- microseconds away, outside the record -- with no symptom in the
        data. An implementation must then read the polarity back and expose it
        as `output_polarity`, so the run still records which convention it ran
        under.
        """
        ...

    @property
    def output_polarity(self) -> str:
        """`NORM`, `INV` or `?` -- what `configure_shape` set or found."""
        ...

    def configure_trigger(self, *, external: bool = ...,
                          positive_slope: bool = ...) -> None:
        """Arm from the external trigger input (the 33220A Sync) or free-run.

        In the protocol because the run path must write it. Until 2026-09-02
        only `tools/scan.py` armed the generator, as a pre-flight side effect;
        a run started any other way inherited whatever the front panel held,
        and a free-running pulse still triggers the scope and still integrates
        to a plausible charge -- at a random phase of the LED cycle, with no
        symptom in the data. Only the two booleans cross the boundary: the
        threshold and input impedance are the instrument's recovered constants
        (1.0 V into 10 kohm) and stay inside the driver.
        """
        ...

    def trigger_state(self) -> dict[str, str]:
        """Readback: `{"arm_source": "EXT" | "IMM" | "?", "arm_slope": "POS" |
        "NEG" | "?"}`. Strings, and `?` for an instrument that would not
        answer, on the same principle as `output_polarity`: the run records
        what the instrument reports, not what it was asked. A source that is
        neither EXT nor IMM (the 81150A also has MAN and INT2) is passed
        through as the instrument spelt it, because that is information and
        `?` would hide it."""
        ...

    def set_levels(self, high_v: float, low_v: float, *,
                   delay_s: float, width_s: float) -> None: ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool:
        """Read by the router before it moves the relay. A source that cannot
        answer this cannot be routed — see `drivers.routing`."""
        ...


# -- acquisition ----------------------------------------------------------
@runtime_checkable
class Digitizer(Protocol):
    """The oscilloscope (Infiniium DSO9054H)."""

    def configure_timebase(self, timebase_ns_per_div: float,
                           record_length: int) -> None: ...

    def configure_edge_trigger(self, source: str = ..., *, positive: bool = ...,
                               high_threshold: float | None = ...,
                               level: float | None = ...,
                               sweep: str = ...) -> None: ...

    def acquire(self, n_averages: int, *, source: str = ...,
                autorange_first: bool = ..., timeout_s: float = ...) -> Trace:
        """One hardware-averaged acquisition.

        `autorange_first=True` on the light trace only. The dark trace must
        inherit the vertical range the light trace left behind — re-ranging
        between the pair changes the digitiser scaling and invalidates the
        subtraction.
        """
        ...

    def fetch_volts(self, source: str) -> Trace:
        """One channel of the record already acquired, in volts at the input,
        with no sense-resistor division: the sync line the scope triggered on,
        fetched beside the current trace so a shot's file says what the
        trigger edge looked like (2026-09-05)."""
        ...

    @property
    def clipped(self) -> bool:
        """Whether the vertical window could not hold the last acquisition.

        In the protocol because the experiment layer reads it, and because a
        name only the simulator implements is a name that silently reports
        `False` on the real rig. That is exactly what happened: `transient.py`
        asked for `clipped`, `SimulatedDigitizer` had it, `Infiniium` called the
        same state `last_autorange_clipped`, nothing here required either — so
        `getattr(scope, "clipped", False)` returned the default for every shot
        ever taken on hardware, and the sim-backed test passed.

        `isinstance` against a runtime-checkable Protocol does check non-method
        members, so `test_every_real_driver_satisfies_its_protocol` now fails
        the next time a driver and the experiment layer disagree on a name.
        """
        ...


# -- DC characterisation --------------------------------------------------
@dataclass(frozen=True)
class DCPoint:
    """What the SourceMeter reports at one illumination level."""

    voc: float          # V
    jsc: float          # A (current, not density — area is an analysis choice)
    jsat: float         # A at the saturation bias
    v_sat: float        # the bias jsat was taken at


@runtime_checkable
class SourceMeter(Protocol):
    """The Keithley 24xx on the DC side of the relay."""

    def measure_dc(self, *, v_sat: float,
                   settle_s: float | None = ...) -> DCPoint:
        """V_oc, J_sc and J_sat at the illumination in force.

        `settle_s` overrides the driver's own per-quantity settle times, which
        differ (V_oc on a slow cell settles far more slowly than J_sc). Leave it
        None for real data.
        """
        ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool: ...


# -- optics ---------------------------------------------------------------
@runtime_checkable
class Shutter(Protocol):
    """The optical shutter (Deditec DIO module 0, channel 0)."""

    def unblock(self) -> None:
        """Open — light reaches the sample."""
        ...

    def shut(self) -> None:
        """Close — the dark trace."""
        ...

    @property
    def is_open(self) -> bool: ...


@runtime_checkable
class PowerMeter(Protocol):
    """Optical power reference (Newport 1918-C).

    Two implementations, and only one may be live: `newport1918c.direct`
    opens the USB device in this process (the default), `newport1918c.console`
    asks the meter's own console over HTTP when that program holds the
    handle. Only one process can hold the device, which is the whole reason
    there are two.
    """

    def set_wavelength(self, nm: float) -> None: ...

    def read_power(self) -> float:
        """W."""
        ...

    def read_statistics(self, n: int) -> tuple[float, float]:
        """(mean, sample std) over `n` samples, in W."""
        ...


# -- illumination ---------------------------------------------------------
@runtime_checkable
class LedSource(Protocol):
    """The generator driving the LED (33220A, through a fixed-gain amplifier).

    Two modes within one intensity point, and the level must be the same
    number in both: DC for the V_oc measurement, then the identical value as
    the pulse high level for the transient. `core.illumination` owns that
    invariant; the driver only sends levels.

    `off()` is in the contract because `jv.py` and `intensity_series.py` were
    each testing `hasattr(led, "off")` and falling back to `disable_output()`
    -- two spellings of one intent, and a fallback nobody would notice being
    taken. The polarity pair is here because it is the setting the whole
    experiment hangs on (`:OUTP:POL INV` is what makes the Sync's rising
    edge mean light-off), and a run has to be able to read it back into its
    own file.
    """

    def set_dc(self, level_v: float) -> None:
        """Steady illumination, for V_oc / J_sc / J_sat."""
        ...

    def set_pulse(self, high_v: float, low_v: float, *,
                  frequency_hz: float = ..., duty_percent: float = ...) -> None:
        """On/off square wave for the transient. `high_v` must equal the DC
        level V_oc was measured at; `low_v` must sit below the LED threshold."""
        ...

    def off(self) -> None:
        """Dark: output disabled, not merely a low level."""
        ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool: ...

    @property
    def mode(self) -> str:
        """`OFF`, `DC`, `PULSE` or `?` -- what the driver last set. Read by
        the illumination guard when it checks that V_oc was measured under
        the drive the transient will use."""
        ...

    def set_polarity(self, inverted: bool) -> None:
        """`:OUTP:POL INV` / `NORM`."""
        ...

    def polarity(self) -> str:
        """`NORM`, `INV` or `?` -- from the instrument, not from memory."""
        ...


# -- routing --------------------------------------------------------------
@runtime_checkable
class Router(Protocol):
    """The relay that decides which source is connected to the device.

    The only interlock on this rig: driving both the amplifier and the
    SourceMeter into the sample at once is the one software mistake that costs
    hardware rather than a dataset. So the contract is two context managers,
    not a `set_position` — a caller cannot express the unsafe state.
    """

    def dc(self) -> ContextManager[None]: ...

    def transient(self) -> ContextManager[None]: ...

    def park(self) -> None:
        """Both outputs off. The relay is left where it is; safety comes from
        the outputs being off, and a mechanical relay has a finite life."""
        ...

    @property
    def position(self) -> str: ...


# -- temperature ----------------------------------------------------------
@dataclass(frozen=True)
class TemperatureReading:
    """What the temperature controller reports at one poll.

    `kelvin` is the control input's reading (NaN when the console holds none
    yet); `connected` is the console's own word on whether the instrument
    behind it answers, carried rather than raised so a caller can tell
    "console up, instrument silent" from "console down". `ramping` is the
    331's RAMPST? -- True while a ramp is still walking the setpoint, which
    is why a reading inside the band is not yet settled. `max_setpoint_k` is
    the ceiling the console enforces; it is reported, never applied here.
    """

    kelvin: float
    setpoint_k: float | None
    ramping: bool | None
    heater_range: int | None
    connected: bool
    status_text: str
    elapsed_s: float | None
    max_setpoint_k: float | None


@runtime_checkable
class TemperatureController(Protocol):
    """The cryostat's temperature controller (Lake Shore 331).

    The 331 answers only the last query it received and cannot arbitrate
    between callers, so exactly one process may hold its GPIB session. That
    process is normally this one (`lakeshore331.controller`, the default),
    and the rule is then kept by a lock rather than by a process boundary;
    when the 331 console is running instead it owns the bus, and
    `lakeshore331.console` asks it over HTTP. Two owners is the failure both
    arrangements exist to prevent -- the same one-owner rule the 1918-C
    meter has.

    Either way the contract is small on purpose: write a setpoint, read the
    state, mark the audit trail. The heater range, the PID, the ramp and the
    loop wiring are not in it -- they are set on the front panel and only
    read back -- so what the instrument holds is what a run gets.
    """

    def read(self) -> TemperatureReading: ...

    def set_setpoint(self, kelvin: float) -> float:
        """Write the control setpoint; returns what the controller confirmed.
        Raises when the console refuses (above its ceiling): the ceiling is
        the console's to enforce, and an implementation must never clamp."""
        ...

    def note(self, text: str) -> bool:
        """A line into the controller's own audit log. Returns whether it was
        written; never raises, because a log mark must not stop a run."""
        ...
