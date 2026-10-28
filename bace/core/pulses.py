"""Bias conditions map to Agilent 81150A pulse levels.

`delay_ns` reaches `:PULS:DEL1` without an added offset, matching the
LabVIEW scan sequence. Adding an offset shifts the displacement spike
and changes which part of the transient the integration window includes.

The measured Sync-to-field latency is 47.1 ns, fitted over a delay scan
(manual 2.2, `docs/manual/2.2-transient.md`). It belongs to the integration
window through `RigConfig.trigger_offset_s` and
`experiment.transient.resolve_window`, rather than the generator command.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PulseLevels:
    """Generator levels and timing for one shot.

    Levels are in V at the 81150A output, divided by the bias amplifier gain.
    `delay_s` and `width_s` are in s.
    """

    high_light: float
    low_light: float
    high_dark: float
    low_dark: float
    delay_s: float
    width_s: float


def pulse_levels(vpre: float, vcoll: float, pulse_amp: float,
                 delay_ns: float, width_ns: float, *,
                 invert: bool = False) -> PulseLevels:
    """Returns generator levels and timing for one shot.

    The light trace steps from V_pre to V_coll. The translated dark reference
    starts at 0 V and uses the same voltage change to remove the displacement
    current and RC response without forward injection.

    `vpre` and `vcoll` are device voltages in V; `delay_ns` and `width_ns`
    are in ns. `pulse_amp` is the dimensionless bias amplifier gain.
    `invert` swaps and negates both levels for opposite device polarity,
    matching the LabVIEW `NewSample` flag.

    `delay_s` supplies `:PULS:DEL1` relative to the arm. The field reaches the
    device `RigConfig.trigger_offset_s` later; the integration window accounts
    for that latency separately.
    """
    if pulse_amp == 0:
        raise ValueError("pulse_amp must be non-zero")

    delay_s = delay_ns * 1e-9
    if delay_s < 0:
        # A generator cannot fire before its arm. A negative `:PULS:DEL1`
        # enters the instrument error queue, which
        # `experiment.transient.run_transient_scan` does not read. Without this
        # check, affected points retain the preceding delay and create a flat
        # region in Q against delay that can be mistaken for device behaviour.
        raise ValueError(
            f"delay_ns = {delay_ns:g} asks the generator to fire "
            f"{-delay_s * 1e9:g} ns before its own trigger. The axis cannot go "
            "below 0 ns."
        )

    span = abs(vpre - vcoll)
    if not invert:
        hi_l, lo_l = vpre, vcoll
        hi_d, lo_d = 0.0, -span
    else:
        hi_l, lo_l = -vcoll, -vpre
        hi_d, lo_d = span, 0.0

    return PulseLevels(
        high_light=hi_l / pulse_amp,
        low_light=lo_l / pulse_amp,
        high_dark=hi_d / pulse_amp,
        low_dark=lo_d / pulse_amp,
        delay_s=delay_s,
        width_s=width_ns * 1e-9,
    )
