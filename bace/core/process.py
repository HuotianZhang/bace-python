"""Transient processing: dark subtraction, baseline, averaging, charge.

Ported from the main processing MathScript node of TDCF-BACE_20160223.vi.
Two details that matter for numerical agreement with the original:

* the baseline is the mean of the **last 10 %** of the record
  (`mean(PhotoC(time > T/10*9))`), not a fixed number of samples;
* `std` in MathScript is the *sample* standard deviation, so `ddof=1` here.
  NumPy's default `ddof=0` would give systematically smaller error bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def to_current(trace_volts: np.ndarray, resistor_ohm: float,
               probe_attenuation: float = 1.0) -> np.ndarray:
    """Scope voltage -> device current through the sense resistor.

    Do NOT apply this to traces returned by `drivers.infiniium`. The LabVIEW
    scope driver already divides by the sense resistor inside its fetch
    (`Fetch (Waveform).vi` with `50 Ohm? = TRUE`), so the arrays coming back
    from an acquisition are **already in amps**. The Python driver reproduces
    that, and dividing again here would scale every charge by 1/R a second
    time.

    This helper exists only for the case where a trace arrives in volts --
    a manual capture, or a channel read with the division disabled.

    Probe attenuation reaches the instrument as `:CHAN%d:PROB:EXT:GAIN`, so a
    trace fetched from a correctly configured channel already accounts for it;
    the argument here is for traces that do not.
    """
    return np.asarray(trace_volts, dtype=float) * probe_attenuation / resistor_ohm


def photocurrent(light: np.ndarray, dark: np.ndarray, dt: float, *,
                 offset_correct: bool = True) -> np.ndarray:
    """Dark-subtracted transient, optionally baseline-corrected.

    `dark` is not a zero reference. It is the same measurement repeated in the
    dark over the same bias *swing*, translated down by V_oc:

        light   V_pre  ->  V_coll                 (e.g.  +0.8 -> -2.5 V)
        dark      0    ->  V_coll - V_pre         (e.g.   0.0 -> -3.3 V)

    so it carries the capacitive charge displaced by that swing plus the dark
    current, which is exactly the non-photogenerated background the light run
    also contains. Translating rather than repeating the absolute range keeps
    the diode out of forward bias, where in the dark it would inject heavily.

    Residual systematic: the two runs share an amplitude, not a range. The
    displaced charge is the integral of C(V) over each range, so the
    cancellation is exact only where C is flat across the swing. Junction
    capacitance is not flat near forward bias, so a residual survives -- small
    for deep V_coll, larger for shallow V_coll or thin devices. Keep both raw
    traces so this stays checkable.
    """
    light = np.asarray(light, dtype=float)
    dark = np.asarray(dark, dtype=float)
    if light.shape != dark.shape:
        raise ValueError(f"trace shape mismatch: {light.shape} vs {dark.shape}")

    photo = light - dark
    if offset_correct:
        n = photo.size
        t = np.arange(n) * dt
        total = dt * (n - 1)
        tail = t > total * 0.9
        if tail.any():
            photo = photo - photo[tail].mean()
    return photo


def baseline_is_flat(light: np.ndarray, dark: np.ndarray, *,
                     tolerance: float = 0.1) -> tuple[bool, float, float, float]:
    """Is the tail of `light - dark` a baseline, or is it still signal?

    `photocurrent(offset_correct=True)` subtracts the mean of the last 10 % of
    the record. That is right when the transient has decayed by then, and it is
    what the original did. It is silently wrong when the tail is *not* baseline:
    the correction then subtracts the signal itself and returns the trace's own
    mirror image, with a plausible-looking charge.

    That is not hypothetical. 2026-09-01 17:59, the first run with
    `:OUTP1:POL INV`: the device rested at V_oc (raw `light - dark` = 0.04 mA)
    and stepped into a **steady** 1.9 mA extraction current that never decayed,
    because the LED never turned off inside the record. The tail was the signal.
    `photo` came out reading −1.9 mA before the step and 0 after — the exact
    inverse of the truth — and Q came out a believable −3e−10 C.

    Compares the head (before any step can have arrived) with the tail, scaled
    by the largest excursion. Returns `(flat, head, tail, peak)` in the units of
    the traces. Judgement only: it changes no number.
    """
    photo = np.asarray(light, dtype=float) - np.asarray(dark, dtype=float)
    n = photo.size
    if n < 20:
        return True, 0.0, 0.0, 0.0
    head = float(photo[: max(1, n // 20)].mean())
    tail = float(photo[int(n * 0.9):].mean())
    peak = float(np.max(np.abs(photo)))
    if peak == 0.0:
        return True, head, tail, peak
    return abs(tail - head) <= tolerance * peak, head, tail, peak


def charge(photo: np.ndarray, dt: float, t0_int: float) -> float:
    """Extracted charge: the rectangular sum of the transient after `t0_int`.

    Kept as `sum(...)*dt` rather than a trapezoid so it matches the original
    exactly; with thousands of samples the difference is far below the noise,
    but it should be a deliberate change, not an accidental one.
    """
    photo = np.asarray(photo, dtype=float)
    t = np.arange(photo.size) * dt
    return float(photo[t > t0_int].sum() * dt)


@dataclass
class RunningAverage:
    """Incremental mean over loops, matching the original's update rule.

    Loop 1 seeds the buffer; loop L folds in as
    `(new + (L-1)*avg) / L`, which is the exact arithmetic mean of the
    first L loops and lets the display update after every single step.
    """

    n_steps: int
    n_samples: int
    _buf: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self._buf = np.full((self.n_steps, self.n_samples), np.nan)

    def update(self, step: int, loop: int, trace: np.ndarray) -> np.ndarray:
        """`step` and `loop` are 1-based, as in the original."""
        i = step - 1
        if loop == 1:
            self._buf[i] = trace
        else:
            self._buf[i] = (trace + (loop - 1) * self._buf[i]) / loop
        return self._buf[i]

    @property
    def traces(self) -> np.ndarray:
        return self._buf


@dataclass
class ChargeAccumulator:
    """Per-(loop, step) charges with running mean and sample std."""

    n_loops: int
    n_steps: int
    _q: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self._q = np.full((self.n_loops, self.n_steps), np.nan)

    def add(self, loop: int, step: int, q: float) -> None:
        self._q[loop - 1, step - 1] = q

    def summary(self) -> tuple[np.ndarray, np.ndarray]:
        """(mean, sample std) per step, ignoring loops not yet measured."""
        mean = np.full(self.n_steps, np.nan)
        std = np.full(self.n_steps, np.nan)
        for j in range(self.n_steps):
            col = self._q[:, j]
            col = col[~np.isnan(col)]
            if col.size:
                mean[j] = col.mean()
                std[j] = col.std(ddof=1) if col.size > 1 else 0.0
        return mean, std

    @property
    def all_charges(self) -> np.ndarray:
        return self._q
