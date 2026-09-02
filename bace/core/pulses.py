"""Mapping from physical bias values to Agilent 81150A pulse levels.

Ported from the pulse-level MathScript node of TDCF-BACE_20160223.vi.

Two nodes in the original disagreed on the trigger-delay offset `T`
(47 ns in the single-step path, 0 ns in the looping path). Huotian confirmed
47 ns is the physical one, so it is the default here and lives in exactly
one place.
"""
from __future__ import annotations

from dataclasses import dataclass

TRIGGER_OFFSET_S = 0.0
"""Added to `delay_ns` before it reaches `:PULS:DEL1`. **Zero, measured.**

The original had two nodes that disagreed: 47 ns in the single-step path, 0 ns
in the looping path. This was 47e-9 until 2026-09-02 on the strength of the
single-step node. Then the LabVIEW engine and this port were run back to back on
the same device with the same `Delay(ns) = 90`, and the displacement spike in
the *dark* trace — which contains no light at all, so it is purely the voltage
step arriving — landed at:

    LabVIEW  -25.49 mA @ 328 ns
    port     -25.18 mA @ 376 ns

48 ns apart, 1 % apart in height. The port was writing `:PULS:DEL1 = 137 ns`
where LabVIEW wrote 90. **The looping path is the one a scan takes, and its
offset is 0.** Every delay quoted before that date is 47 ns high.

Left as a named constant, and overridable per rig through `rig.toml`, because
it is a property of the cabling: if the field really does arrive late on some
other bench, that is where to say so — measured, the way this one was."""


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
                 invert: bool = False,
                 trigger_offset_s: float = TRIGGER_OFFSET_S) -> PulseLevels:
    """Levels for one step.

    Light trace: the device sits at `vpre` under illumination, then steps to
    `vcoll`. Dark trace: the same voltage *step* with the same amplitude, but
    referenced to zero so no carriers are injected — this is what gets
    subtracted to remove the displacement/RC response.

    `invert` corresponds to the original's `NewSample` flag: it swaps and
    negates both levels for devices of the opposite polarity.
    """
    if pulse_amp == 0:
        raise ValueError("pulse_amp must be non-zero")

    delay_s = delay_ns * 1e-9 + trigger_offset_s
    if delay_s < 0:
        # A generator cannot fire before it is armed. Left unchecked this reaches
        # the instrument as a negative `:PULS:DEL1`, which is rejected into the
        # error queue -- and `run_transient_scan` does not read that queue, so
        # the affected steps would silently keep the previous delay and draw a
        # flat stretch in Q(delay) that looks like physics.
        raise ValueError(
            f"delay_ns = {delay_ns:g} with a trigger offset of "
            f"{trigger_offset_s * 1e9:g} ns asks the generator for "
            f"{delay_s * 1e9:g} ns of delay, which is before its own trigger. "
            f"The axis cannot go below {-trigger_offset_s * 1e9:g} ns."
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
