"""Per-shot diagnostics on the raw traces: is this shot the measurement it
claims to be?

Written after the first hardware day with the console (2026-09-04/05). Six
of twenty-three delay-scan shots had a photocurrent ten times too large and
a charge of the wrong sign, and every one passed the rail check, because
nothing was wrong with the window: the trigger had jittered for the length
of that shot. Both traces' displacement spikes were 4-14 % lower and their
10-90 % edges 8-13 ns instead of 6, and the light and dark spikes sat
0.3-1.2 ns apart where a good shot aligns to 0.01 ns -- so the 50 mA spike
no longer cancelled in `light - dark` and its residual *was* the
"photocurrent". Three numbers say so, and this module computes them:

* `spike_lag_ns` -- the light-to-dark lag of the displacement spike, by
  cross-correlation with parabolic sub-sample interpolation;
* `edge_10_90_ns` -- how fast the spike falls, which jitter smears;
* `sync_edge_ns` -- the same on the sync trace the scope triggered on,
  fetched beside each acquisition: a smeared sync edge puts the jitter
  between the sync and the scope's trigger, a sharp one beside a smeared
  spike puts it between the sync and the 81150A's pulse.

Pure numpy, no instrument and no event: the service's verdict and the
storage recorder both call it, so the live card and the file agree.
"""
from __future__ import annotations

import numpy as np

SPIKE_WINDOW_BEFORE_S = 6e-8
"""How far before the spike's extreme the correlation window starts."""
SPIKE_WINDOW_AFTER_S = 1e-7
"""And how far after: the spike's tail, where the two traces still match."""
MIN_SPIKE_A = 1e-6
"""Below this peak-to-peak swing there is no spike to align on."""
SPIKE_RATIO_MIN = 0.25
"""The smaller trace's swing must be at least this fraction of the larger's
for the two to share a spike worth aligning. On the rig the two agree to
1 %; the simulator's photocurrent can double the light trace's swing."""


def spike_lag_ns(light: np.ndarray, dark: np.ndarray, dt: float) -> float | None:
    """Lag of `dark`'s displacement spike behind `light`'s, in ns, sub-sample.

    Positive: the dark spike comes later. None when either trace has no
    spike to speak of, or the window does not fit the record.
    """
    a = np.asarray(light, dtype=float)
    b = np.asarray(dark, dtype=float)
    if a.size == 0 or b.size == 0 or a.size != b.size or not dt or not np.isfinite(dt):
        return None
    if np.ptp(a) < MIN_SPIKE_A or np.ptp(b) < MIN_SPIKE_A:
        return None
    # The same pulse drives both traces, so their spikes are the same size
    # (within 1 % on the rig). A dark trace with a fraction of the light's
    # swing has no spike in common to align on -- a bare-noise stand-in, or
    # a dark taken under a different pulse -- and correlating noise gives a
    # lag that means nothing.
    if np.ptp(b) < SPIKE_RATIO_MIN * np.ptp(a) or np.ptp(a) < SPIKE_RATIO_MIN * np.ptp(b):
        return None
    peak = int(np.argmax(np.abs(a)))
    lo = peak - int(round(SPIKE_WINDOW_BEFORE_S / dt))
    hi = peak + int(round(SPIKE_WINDOW_AFTER_S / dt))
    if lo < 0 or hi > a.size or hi - lo < 8:
        return None
    x = a[lo:hi] - a[lo:hi].mean()
    y = b[lo:hi] - b[lo:hi].mean()
    if not (np.abs(x).max() > 0 and np.abs(y).max() > 0):
        return None
    c = np.correlate(x, y, "full")
    k = int(np.argmax(c))
    if c[k] <= 0:
        return None
    shift = float(k)
    if 0 < k < c.size - 1:
        y0, y1, y2 = c[k - 1], c[k], c[k + 1]
        denom = y0 - 2.0 * y1 + y2
        if denom != 0:
            shift += 0.5 * (y0 - y2) / denom
    # numpy's `correlate(x, y)[k]` is sum x[i + k - (n-1)] * y[i]: a dark
    # spike `s` samples later than the light one (y[i] = x[i - s]) peaks at
    # k = (n-1) - s. Negated, so a dark spike that comes later is positive.
    return float(-(shift - (x.size - 1)) * dt * 1e9)


def edge_10_90_ns(trace: np.ndarray, dt: float) -> float | None:
    """The 10-90 % time of the displacement spike's leading edge, in ns.

    The spike is the trace's absolute extreme; the baseline is the median
    of the samples 20-40 ns before it. None without a spike or a baseline.
    """
    a = np.asarray(trace, dtype=float)
    if a.size == 0 or not dt or not np.isfinite(dt) or np.ptp(a) < MIN_SPIKE_A:
        return None
    i = int(np.argmax(np.abs(a)))
    far, near = int(round(4e-8 / dt)), int(round(2e-8 / dt))
    if i - far < 0:
        return None
    base = float(np.median(a[i - far:i - near]))
    extreme = float(a[i])
    if extreme == base:
        return None
    return _crossing_time(a, i, base, extreme, dt)


def sync_edge_ns(sync: np.ndarray, dt: float, t0: float) -> float | None:
    """The 10-90 % rise (or fall) time of the sync edge the scope triggered
    on, in ns: the edge nearest the trigger position `-t0`.

    None when the trace has no edge there (a flat sync is the "no sync"
    case the trigger calibration already refuses).
    """
    a = np.asarray(sync, dtype=float)
    if a.size < 16 or not dt or not np.isfinite(dt) or np.ptp(a) <= 0:
        return None
    trig = int(round(-float(t0) / dt))
    half = int(round(1e-7 / dt))
    lo, hi = max(0, trig - half), min(a.size, trig + half)
    if hi - lo < 8:
        return None
    w = a[lo:hi]
    low, high = float(np.percentile(w, 5)), float(np.percentile(w, 95))
    if high - low < 0.25 * np.ptp(a):
        return None
    # the edge: the largest single-step change inside the window says which
    # way it goes; the 10/90 levels are then crossed on either side of it
    d = np.diff(w)
    j = int(np.argmax(np.abs(d)))
    rising = d[j] > 0
    a10 = low + 0.1 * (high - low)
    a90 = low + 0.9 * (high - low)
    if rising:
        k10 = j
        while k10 > 0 and w[k10] > a10:
            k10 -= 1
        k90 = j
        while k90 < w.size - 1 and w[k90] < a90:
            k90 += 1
    else:
        k10 = j
        while k10 > 0 and w[k10] < a90:
            k10 -= 1
        k90 = j
        while k90 < w.size - 1 and w[k90] > a10:
            k90 += 1
    return float((k90 - k10) * dt * 1e9)


def _crossing_time(a: np.ndarray, i: int, base: float, extreme: float, dt: float) -> float:
    a10 = base + 0.1 * (extreme - base)
    a90 = base + 0.9 * (extreme - base)
    toward = np.sign(extreme - base)
    j10 = i
    while j10 > 0 and (a[j10] - a10) * toward > 0:
        j10 -= 1
    j90 = i
    while j90 > 0 and (a[j90] - a90) * toward > 0:
        j90 -= 1
    return float((j90 - j10) * dt * 1e9)
