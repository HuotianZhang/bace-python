"""The contracts every instrument implements.

Six roles, each with a real driver and a simulated one. Nothing here inherits
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

    `y` is **current in amps**, already divided by the sense resistor — the
    LabVIEW scope driver did that division inside its fetch, and every real and
    simulated digitizer here reproduces it. Applying `core.process.to_current`
    to this would scale charges by 1/R a second time.

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
    """Optical power reference (Newport 1918-C)."""

    def set_wavelength(self, nm: float) -> None: ...

    def read_power(self) -> float:
        """W."""
        ...

    def read_statistics(self, n: int) -> tuple[float, float]:
        """(mean, sample std) over `n` samples, in W."""
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
