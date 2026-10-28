"""Transient processing subtracts dark traces and computes baseline and Q.

LabVIEW numerical compatibility requires the last 10 % baseline when no
pre-trigger region is supplied, rectangular integration, and sample standard
deviation (`ddof=1`). `tests/test_regression_20260807.py` checks the baseline
and Q against the LabVIEW files. NumPy's default `ddof=0` gives smaller error
bars.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def to_current(trace_volts: np.ndarray, resistor_ohm: float,
               probe_attenuation: float = 1.0) -> np.ndarray:
    """Converts a voltage trace in V to device current in A through R_s.

    `resistor_ohm` is R_s in ohms. `probe_attenuation` is dimensionless.
    Traces from `drivers.infiniium` are already in A, matching the LabVIEW
    fetch with `50 Ohm? = TRUE`. Applying this conversion again scales Q by
    1/R_s a second time.

    This helper accepts voltage captures or channel reads with current
    conversion disabled. A correctly configured channel already accounts for
    probe attenuation through `:CHAN%d:PROB:EXT:GAIN`; the argument applies
    only when the trace does not include that correction.
    """
    return np.asarray(trace_volts, dtype=float) * probe_attenuation / resistor_ohm


PRE_TRIGGER_GUARD_S = 10e-9
"""The pre-trigger baseline ends this many seconds before the trigger.

Sync pickup enters the current channel at the trigger. The 10 ns guard keeps
the baseline clear of that pickup and the later voltage step (manual 2.3,
`docs/manual/2.3-signal.md`).
"""


def photocurrent(light: np.ndarray, dark: np.ndarray, dt: float, *,
                 offset_correct: bool = True,
                 baseline_end_s: float | None = None) -> np.ndarray:
    """Returns light minus dark in A, with optional baseline subtraction.

    `dt` is the sample interval in s. `baseline_end_s = None` selects the last
    10 % of the record, as required for LabVIEW numerical compatibility.
    A number selects samples before that record time in s, suitable for a
    pre-trigger baseline. Fewer than 20 such samples raises `ValueError`,
    because that region cannot provide a reliable baseline. A tail baseline
    underestimates Q by 10–20 % on a 2 µs record on this bench; on longer
    records drift enters the baseline (manual 2.3,
    `docs/manual/2.3-signal.md`).

    With a translated dark reference, the steps are:

        light trace: V_pre -> V_coll          (e.g. +0.8 -> -2.5 V)
        dark trace:  0 -> V_coll - V_pre       (e.g.  0.0 -> -3.3 V)

    The dark trace contains displacement current and dark current. Translating
    the voltage range keeps the diode out of forward bias, where it injects
    strongly in the dark. The caller supplies the dark reference; this function
    does not choose it.

    Equal voltage changes cancel displacement charge exactly only when C(V)
    is constant across both ranges. Near forward bias, varying capacitance
    leaves a residual. It is smaller at deep V_coll and larger at shallow
    V_coll or in thin devices. Retaining both raw traces keeps it checkable.
    """
    light = np.asarray(light, dtype=float)
    dark = np.asarray(dark, dtype=float)
    if light.shape != dark.shape:
        raise ValueError(f"trace shape mismatch: {light.shape} vs {dark.shape}")

    photo = light - dark
    if offset_correct:
        n = photo.size
        t = np.arange(n) * dt
        if baseline_end_s is None:
            total = dt * (n - 1)
            region = t > total * 0.9
        else:
            region = t < baseline_end_s
            if region.sum() < 20:
                raise ValueError(
                    f"the pre-trigger baseline ends at {baseline_end_s * 1e9:.1f} ns of "
                    f"record time, which leaves {int(region.sum())} samples before it; "
                    "a longer timebase puts more of the record before the trigger")
        if region.any():
            photo = photo - photo[region].mean()
    return photo


def baseline_is_flat(light: np.ndarray, dark: np.ndarray, *,
                     tolerance: float = 0.1) -> tuple[bool, float, float, float]:
    """Checks whether light minus dark has a tail consistent with its baseline.

    Subtracting the last 10 % is valid only after extraction decays. A tail
    that still contains signal subtracts that signal from the whole record
    and can produce an inverted trace with a plausible Q.

    For example, with the LED on throughout the record, this bench measures
    0.04 mA before the step and a steady 1.9 mA after it. Tail subtraction
    gives -1.9 mA before the step and zero after it, with Q about -3e-10 C.
    These values are measured on this bench.

    The comparison scales the head-to-tail difference by the largest absolute
    excursion. The return value is `(flat, head, tail, peak)` in trace units;
    `tolerance` is dimensionless. The traces remain unchanged.
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


def charge(photo: np.ndarray, dt: float, t0_int: float,
           t1_int: float | None = None) -> float:
    """Returns signed Q in C from a rectangular sum over `(t0_int, t1_int]`.

    `photo` is in A and `dt` is the sample interval in s. `t0_int` and
    `t1_int` are in s from the first sample of the record. `t1_int = None`
    integrates to the record end. The caller places the integration window
    relative to field arrival through
    `experiment.transient.resolve_window`. Q keeps the sign of `photo`.

    The rectangular sum matches LabVIEW numerics, checked by
    `tests/test_regression_20260807.py`. A trapezoidal sum changes that contract,
    even when its difference at thousands of samples is below the bench noise.
    """
    photo = np.asarray(photo, dtype=float)
    t = np.arange(photo.size) * dt
    inside = t > t0_int
    if t1_int is not None:
        inside &= t <= t1_int
    return float(photo[inside].sum() * dt)


@dataclass
class RunningAverage:
    """An incremental trace mean over loops at each point.

    Loop 1 seeds the buffer. Each later loop updates it as
    `(trace + (loop - 1) * self._buf[i]) / loop`, giving the arithmetic mean
    over the acquired loops at point `i`.
    The display can therefore update after each shot.
    """

    n_steps: int
    n_samples: int
    _buf: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self._buf = np.full((self.n_steps, self.n_samples), np.nan)

    def update(self, step: int, loop: int, trace: np.ndarray) -> np.ndarray:
        """Updates one point with a trace using 1-based `step` and `loop`."""
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
    """Per-shot Q in C with a mean and sample standard deviation per point."""

    n_loops: int
    n_steps: int
    _q: np.ndarray = field(init=False)

    def __post_init__(self) -> None:
        self._q = np.full((self.n_loops, self.n_steps), np.nan)

    def add(self, loop: int, step: int, q: float) -> None:
        self._q[loop - 1, step - 1] = q

    def summary(self) -> tuple[np.ndarray, np.ndarray]:
        """Returns mean Q and sample standard deviation in C for each point.

        Unmeasured shots are ignored. One measured shot has standard deviation
        zero; a point with no measured shots has NaN for both results.
        """
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
