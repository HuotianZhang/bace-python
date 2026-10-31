"""Define the interfaces shared by real and simulated instruments.

Each `typing.Protocol` is satisfied by matching attributes and methods;
experiments accept an implementation without registration or inheritance.
Keep interfaces limited to experiment needs because every added member
requires all implementations to support it.

This module and `bace.core` do not import each other. Pulse levels and times
cross the boundary as floats, keeping drivers independent of the physical
model in `bace.core.pulses`."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ContextManager, Protocol, runtime_checkable

import numpy as np


@dataclass(frozen=True)
class Trace:
    """Hold an acquired transient in A with sample interval and origin in s.

    `y` includes division by R_s and `current_sign`, which is -1 on this
    bench, matching the LabVIEW current convention. Applying
    `bace.core.process.to_current` again divides by R_s twice; negating
    again undoes the recorded convention.

    `t0` is the instrument's record origin, negative for a trigger inside
    the record. `bace.core.process.charge` integrates in record time from
    zero, matching the LabVIEW arithmetic checked by
    `tests/test_regression_20260807.py` (manual 3.5,
    `docs/manual/3.5-testing.md`). `bace.experiment.transient.resolve_window` uses
    `t0` to translate field-relative integration limits. The recorder stores
    the origin per point for offline integration."""

    y: np.ndarray
    dt: float
    t0: float = 0.0
    count: int | None = None
    """Record how many acquisitions the digitiser folds into this trace.

    The Infiniium queries `:WAV:COUN?` while stopped. `None` means the count
    is unavailable. A done flag alone cannot confirm N_avg because `:ADER?`
    is set per acquisition, not per completed average
    (`docs/analysis/2026-09-06/README.md`)."""

    @property
    def n(self) -> int:
        return int(self.y.size)

    @property
    def duration_s(self) -> float:
        return (self.n - 1) * self.dt


# -- bias -----------------------------------------------------------------
@runtime_checkable
class BiasSource(Protocol):
    """Supply the pulse generator interface for V_pre followed by V_coll.

    Levels are in V at the generator output. The caller divides by amplifier
    gain and swaps and negates for inverted device polarity; the driver
    sends those levels without a device model."""

    def configure_shape(self, frequency_hz: float, *, duty_percent: float = ...,
                        edge_time_s: float = ...,
                        inverted_output: bool | None = ...) -> None:
        """Set pulse frequency in Hz, duty in percent, edge time in s and polarity.

        Return `None`. `inverted_output=None` preserves instrument polarity
        when device wiring is uncertain. The wrong polarity moves extraction
        to the other edge, microseconds outside the record, without an obvious
        data error. Read back and expose `output_polarity` even when unchanged
        so the run records the convention."""
        ...

    @property
    def output_polarity(self) -> str:
        """Expose `NORM`, `INV` or `?` from `configure_shape` or polarity read-back."""
        ...

    def configure_trigger(self, *, external: bool = ...,
                          positive_slope: bool = ...) -> None:
        """Set external edge arming from the 33220A Sync or immediate free-run.

        Return `None`. Free-running bias still triggers the scope and can give
        plausible Q at a random LED phase. Every run must therefore configure
        arming. Only the booleans cross this interface; the driver retains the
        LabVIEW sequence's 1.0 V threshold and 10 kohm input impedance."""
        ...

    def trigger_state(self) -> dict[str, str]:
        """Query arming and return `arm_source` and `arm_slope` as strings.

        Expected values are `EXT` or `IMM` and `POS` or `NEG`; a missing reply
        gives `?`. Other instrument sources, including `MAN` and `INT2`, remain
        visible. Record the read-back rather than echoing requested settings."""
        ...

    def set_levels(self, high_v: float, low_v: float, *,
                   delay_s: float, width_s: float) -> None: ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool:
        """Return the output flag required by the relay interlock.

        A source without this state cannot be routed safely."""
        ...


# -- acquisition ----------------------------------------------------------
@runtime_checkable
class Digitizer(Protocol):
    """Define transient acquisition and voltage read-back for the digitiser."""

    def configure_timebase(self, timebase_ns_per_div: float,
                           record_length: int) -> None: ...

    def configure_edge_trigger(self, source: str = ..., *, positive: bool = ...,
                               high_threshold: float | None = ...,
                               level: float | None = ...,
                               sweep: str = ...) -> None: ...

    def acquire(self, n_averages: int, *, source: str = ...,
                autorange_first: bool = ..., timeout_s: float = ...,
                window: tuple[float, float] | None = ...) -> Trace:
        """Acquire N_avg and return a current `Trace` in A with timing in s.

        `n_averages` sets N_avg; `timeout_s` is in s. `autorange_first=True`
        ranges the light trace only. The dark trace shares its range to keep
        digitiser scaling and offset calibration equal for subtraction. A pair
        taken dark first supplies `window`, the light trace's preceding range
        and offset in V at this point. A range fitted to dark can clip light."""
        ...

    @property
    def window(self) -> tuple[float, float] | None:
        """Return the active current-channel `(range, offset)` in V, or `None`.

        Autorange or `acquire(window=...)` establishes this state."""
        ...

    def fetch_volts(self, source: str) -> Trace:
        """Fetch one channel of the acquired record and return a `Trace` in V.

        Timing is in s. No R_s division is applied. Fetching Sync beside current
        records the trigger edge for each shot's timing check."""
        ...

    @property
    def clipped(self) -> bool:
        """Return whether the vertical range cannot hold the light trace.

        Every driver must expose this name so an absent property cannot
        silently become `False`. Runtime protocol checks include nonmethod
        members and verify the real drivers against this interface."""
        ...


# -- DC characterisation --------------------------------------------------
@dataclass(frozen=True)
class DCPoint:
    """Hold V_oc in V and short-circuit and saturation currents in A."""

    voc: float          # V
    jsc: float          # A (current, not density — area is an analysis choice)
    jsat: float         # A at the saturation bias
    v_sat: float        # V for the `jsat` reading


@runtime_checkable
class SourceMeter(Protocol):
    """Define DC measurements on the source-meter side of the relay."""

    def measure_dc(self, *, v_sat: float,
                   settle_s: float | None = ...) -> DCPoint:
        """Measure DC quantities and return a `DCPoint` with V and A values.

        `v_sat` is in V. `settle_s`, in s, overrides the per-quantity waits;
        leave it `None` for real data. V_oc on a slow device needs more settling
        than short-circuit current."""
        ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool: ...


# -- optics ---------------------------------------------------------------
@runtime_checkable
class Shutter(Protocol):
    """Define optical shutter control on DIO module 0, channel 0."""

    def unblock(self) -> None:
        """Open the shutter so light reaches the device; return `None`."""
        ...

    def shut(self) -> None:
        """Shut the shutter for a dark trace; return `None`."""
        ...

    @property
    def is_open(self) -> bool: ...


@runtime_checkable
class PowerMeter(Protocol):
    """Measure optical power with one owner of the Newport 1918-C.

    `bace.drivers.newport1918c.direct` opens USB in this process by default.
    `bace.drivers.newport1918c.console` requests HTTP readings when the
    meter console owns USB. Exactly one process can hold the device."""

    def set_wavelength(self, nm: float) -> None: ...

    def read_power(self) -> float:
        """Return optical power in W."""
        ...

    def read_statistics(self, n: int) -> tuple[float, float]:
        """Return mean and sample standard deviation over `n` readings, in W."""
        ...

    def set_range(self, index: int | None) -> dict:
        """Hold range `index`, or return to auto with `None`; return the read-back."""
        ...


# -- illumination ---------------------------------------------------------
@runtime_checkable
class LedSource(Protocol):
    """Drive the LED through the 33220A and its constant-gain amplifier.

    The DC level for V_oc must match the pulse high level at each point.
    `bace.core.illumination` enforces this; drivers send the supplied levels.
    `off` disables output. Polarity control and read-back are required
    because `INV` makes the Sync rising edge coincide with light off and
    the run must record that convention."""

    def set_dc(self, level_v: float) -> None:
        """Set steady LED drive in V and return `None` for DC measurements."""
        ...

    def set_pulse(self, high_v: float, low_v: float, *,
                  frequency_hz: float = ..., duty_percent: float = ...) -> None:
        """Set pulse levels in V, frequency in Hz and duty in percent; return `None`.

        `high_v` must match the DC level used for V_oc. `low_v` must be below
        the LED turn-on threshold."""
        ...

    def off(self) -> None:
        """Disable LED output and return `None`."""
        ...

    def enable_output(self, on: bool = True) -> None: ...

    def disable_output(self) -> None: ...

    @property
    def output_enabled(self) -> bool: ...

    @property
    def mode(self) -> str:
        """Return cached `OFF`, `DC`, `PULSE` or `?` mode.

        The illumination guard uses it to check that V_oc and the light trace
        use the same drive."""
        ...

    def set_polarity(self, inverted: bool) -> None:
        """Send `:OUTP:POL INV` or `NORM` and return `None`."""
        ...

    def polarity(self) -> str:
        """Query polarity and return `NORM`, `INV` or `?`."""
        ...


# -- routing --------------------------------------------------------------
@runtime_checkable
class Router(Protocol):
    """Connect the device to one source through interlocked contexts.

    Driving the amplifier and source meter into the device together can
    damage hardware. Contexts `dc` and `transient` enforce output disable
    before relay movement; the interface exposes no unguarded position write."""

    def dc(self) -> ContextManager[None]: ...

    def transient(self) -> ContextManager[None]: ...

    def park(self) -> None:
        """Disable both outputs and leave the relay in position; return `None`.

        Output disable provides safety. Avoiding movement preserves the
        mechanical relay's finite operation count."""
        ...

    @property
    def position(self) -> str: ...


# -- temperature ----------------------------------------------------------
@dataclass(frozen=True)
class TemperatureReading:
    """Hold one temperature-controller poll with temperature and setpoint in K.

    `kelvin` is the control input reading, or NaN if unavailable.
    `connected` distinguishes an answering console from an answering
    instrument. Retained readings can accompany `connected=False`.
    `ramping` reflects `RAMPST?`; an in-band reading is not settled while
    the instrument moves its working setpoint. `max_setpoint_k` reports
    the owner's ceiling without applying it here. `elapsed_s` is in s."""

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
    """Control the cryostat through one owner of its Lake Shore 331.

    The 331 replies only to the last query it receives. Direct ownership
    uses a lock; console ownership uses HTTP through the console's GPIB
    session. Competing owners can consume each other's replies.

    The interface writes setpoints, reads state and records journal notes.
    Heater range, PID, ramp and wiring stay on the front panel and are only
    read back. A run uses the instrument configuration it finds."""

    def read(self) -> TemperatureReading: ...

    def set_setpoint(self, kelvin: float) -> float:
        """Write a setpoint in K and return its confirmed value in K.

        The owner enforces its ceiling by refusal; implementations must not
        silently clamp an out-of-limit request."""
        ...

    def note(self, text: str) -> bool:
        """Record a journal note and return whether it succeeds.

        Failure never raises because a log entry must not stop a run."""
        ...
