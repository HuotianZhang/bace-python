"""Mapping from physical bias values to Agilent 81150A pulse levels.

Ported from the pulse-level MathScript node of TDCF-BACE_20160223.vi.

`delay_ns` reaches `:PULS:DEL1` as it is. The original had two nodes that
disagreed on an offset `T` to add first (47 ns in the single-step path, 0 ns in
the looping path); this port carried 47 ns until 2026-09-02, when the LabVIEW
engine and the port were run back to back on the same device with the same
`Delay(ns) = 90` and the dark trace's displacement spike -- the voltage step
arriving, no light in it -- landed 48 ns apart. The looping path is the one a
scan takes, and its offset is 0, so no offset is added here at all.

The 47 ns is real, but it is not a command offset: it is the latency between
the 81150A's Sync (which the scope triggers on) and the field reaching the
device, and it belongs to the *integration window*, not to the generator. That
is `RigConfig.trigger_offset_s`, used by `experiment.transient.resolve_window`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PulseLevels:
    """Levels handed to the 81150A for one acquisition step, in volts at the
    generator output (i.e. already divided by the amplifier gain)."""

    high_light: float
    low_light: float
    high_dark: float
    low_dark: float
    delay_s: float
    width_s: float


def pulse_levels(vpre: float, vcoll: float, pulse_amp: float,
                 delay_ns: float, width_ns: float, *,
                 invert: bool = False) -> PulseLevels:
    """Levels for one step.

    Light trace: the device sits at `vpre` under illumination, then steps to
    `vcoll`. Dark trace: the same voltage *step* with the same amplitude, but
    referenced to zero so no carriers are injected — this is what gets
    subtracted to remove the displacement/RC response.

    `invert` corresponds to the original's `NewSample` flag: it swaps and
    negates both levels for devices of the opposite polarity.

    `delay_s` is what `:PULS:DEL1` is given, from the arm. The field reaches
    the device `RigConfig.trigger_offset_s` later than that; the integration
    window accounts for it, the generator never sees it.
    """
    if pulse_amp == 0:
        raise ValueError("pulse_amp must be non-zero")

    delay_s = delay_ns * 1e-9
    if delay_s < 0:
        # A generator cannot fire before it is armed. Left unchecked this reaches
        # the instrument as a negative `:PULS:DEL1`, which is rejected into the
        # error queue -- and `run_transient_scan` does not read that queue, so
        # the affected steps would silently keep the previous delay and draw a
        # flat stretch in Q(delay) that looks like physics.
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
