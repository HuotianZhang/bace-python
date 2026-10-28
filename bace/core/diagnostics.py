"""Raw-trace diagnostics check the timing and levels of each shot.

Trigger jitter can leave the displacement spike in light minus dark without
clipping either trace. Bench data show six of 23 shots with tenfold current
and reversed Q: spike heights differ by 4–14 %, edges broaden from 6 ns to
8–13 ns, and lags span 0.3–1.2 ns
(`docs/analysis/2026-09-06/spike-lag-and-dark-charge.md`). A 50 mA spike and
0.01 ns alignment on a good shot are measured on this bench.

`spike_lag_ns` cross-correlates displacement spikes with parabolic
interpolation below one sample interval. `edge_10_90_ns` measures spike edge
width. `sync_edge_ns` measures the trigger-channel edge: a smeared sync
edge locates jitter between Sync and the digitiser trigger; a sharp sync edge
beside a smeared spike locates it between Sync and the 81150A pulse.

The service and recorder share these NumPy calculations so the console and
stored diagnostics agree. This module has no instrument or event dependency.
"""
from __future__ import annotations

import numpy as np

SPIKE_WINDOW_BEFORE_S = 6e-8
"""The correlation window starts this many seconds before the spike extreme."""
SPIKE_WINDOW_AFTER_S = 1e-7
"""The correlation window ends this many seconds after the spike extreme.

The tail in this region remains common to both traces.
"""
MIN_SPIKE_A = 1e-6
"""The minimum peak-to-peak current in A for spike alignment."""
SPIKE_RATIO_MIN = 0.25
"""The minimum ratio of the smaller spike swing to the larger swing.

The traces must share a spike for alignment to be meaningful. Their swings
agree within 1 % as measured on this bench; simulated photocurrent can
double the light trace's swing.
"""


def spike_lag_ns(light: np.ndarray, dark: np.ndarray, dt: float) -> float | None:
    """Returns the dark displacement spike's lag behind the light spike in ns.

    A positive lag means the dark spike arrives later. `light` and `dark` are
    in A; `dt` is in s. Parabolic interpolation resolves fractions of a sample.
    Missing spikes, incompatible traces or an unusable correlation window
    return `None`.
    """
    a = np.asarray(light, dtype=float)
    b = np.asarray(dark, dtype=float)
    if a.size == 0 or b.size == 0 or a.size != b.size or not dt or not np.isfinite(dt):
        return None
    if np.ptp(a) < MIN_SPIKE_A or np.ptp(b) < MIN_SPIKE_A:
        return None
    # The same voltage change should give both traces a common spike.
    # A much smaller swing can mean noise or a different pulse; correlating
    # it produces no valid lag.
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
    # `np.correlate(x, y)[k]` sums `x[i + k - (n-1)] * y[i]`.
    # For a dark spike delayed by `s` samples, `y[i] = x[i - s]`, the peak is
    # at `k = (n-1) - s`. Negation makes a later dark spike positive.
    return float(-(shift - (x.size - 1)) * dt * 1e9)


MIN_SYNC_V = 0.1
"""The minimum peak-to-peak sync voltage in V for alignment.

The sync amplitude is 1.19 V and digitiser noise is about 2 mV, measured on
this bench. This threshold is about an order of magnitude from both.
Noise correlation can return an arbitrary lag, such as -1.54 ns measured
on this bench, falsely rejecting a shot or implying valid timing evidence.
`experiment.transient.MIN_SYNC_SWING_V` uses the same threshold to refuse
acquisition without a sync signal.
"""


def sync_lag_ns(sync_light: np.ndarray, sync_dark: np.ndarray,
                dt: float, t0: float = 0.0) -> float | None:
    """Returns the dark trigger edge's lag behind the light trigger edge in ns.

    `sync_light` and `sync_dark` are in V; `dt` and the record origin `t0` are
    in s. A positive lag means the dark edge arrives later, as in `spike_lag_ns`.

    The sync edge measures the trigger chain without device current. In
    contrast, spike correlation combines trigger offset and charge extraction.
    A changing latency from Sync to the device can move the displacement
    spikes while leaving sync edges aligned. Aligned sync edges therefore
    do not prove aligned displacement spikes.

    Across 240 shots at 220–295 K, spike lag spans 0.135–0.479 ns and increases
    with temperature, while sync lag stays within 0.004 ns at every point,
    as measured on this bench.

    Both traces must swing by at least `MIN_SYNC_V` and contain an edge that
    `sync_edge_ns` resolves. Otherwise the result is `None`, allowing the
    caller to report unchecked timing. Noise and single-sample glitches can
    produce arbitrary lags: -1.54 ns and 1.5 ns respectively, measured on this
    bench. Either can falsely reject a good shot or falsely attribute spike
    lag to charge when timing evidence is absent.
    """
    a = np.asarray(sync_light, dtype=float)
    b = np.asarray(sync_dark, dtype=float)
    if a.size == 0 or a.size != b.size or not dt or not np.isfinite(dt):
        return None
    if np.ptp(a) < MIN_SYNC_V or np.ptp(b) < MIN_SYNC_V:
        return None
    if sync_edge_ns(a, dt, t0) is None or sync_edge_ns(b, dt, t0) is None:
        return None
    # The steepest change locates an edge for either a pulse or a step.
    # Using distance from the median instead can select noise on a step's
    # long high level, giving 18 ns of false lag in the simulation.
    # The search stays in the trigger window validated by `sync_edge_ns`;
    # a reflection or later pulse elsewhere could otherwise control the lag.
    trig = int(round(-float(t0) / dt))
    edge_half = int(round(1e-7 / dt))
    w_lo, w_hi = max(0, trig - edge_half), min(a.size, trig + edge_half)
    if w_hi - w_lo < 8:
        return None
    i = w_lo + int(np.argmax(np.abs(np.diff(a[w_lo:w_hi]))))
    half = int(round(3e-8 / dt))
    lo, hi = max(0, i - half), min(a.size, i + half)
    if hi - lo < 8:
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
    return float(-(shift - (x.size - 1)) * dt * 1e9)


def pre_trigger_mean(trace: np.ndarray, dt: float, t0: float) -> float | None:
    """Returns the mean before the trigger guard, in the trace's units.

    `dt` and the record origin `t0` are in s. `PRE_TRIGGER_GUARD_S` excludes
    trigger pickup. With nothing driven, this measures the digitiser's zero at
    the selected range: 0.16 mA at 0.86 V on this bench
    (`docs/analysis/2026-10-26/scope-window/README.md` for the zero offset).
    An unavailable region or fewer than 20 samples returns `None`.
    """
    from .process import PRE_TRIGGER_GUARD_S
    a = np.asarray(trace, dtype=float)
    n = int((-t0 - PRE_TRIGGER_GUARD_S) / dt) if dt else 0
    if n < 20 or n > a.size:
        return None
    return float(a[:n].mean())


def late_offset(light: np.ndarray, dark: np.ndarray, dt: float, t0: float,
                window_end: float | None) -> float | None:
    """Returns the late light-minus-dark mean less its pre-trigger mean.

    The result uses trace units. `dt`, `t0` and `window_end` are in s;
    `window_end` is in record time. This measures the drift left after a
    pre-trigger baseline. A null run gives -1.2 ± 2.9 µA per shot (mean ±
    standard deviation), consistent with BACE on the same device (manual 2.3,
    `docs/manual/2.3-signal.md`). Missing timing or fewer than 20 samples in
    either region returns `None`.
    """
    p = np.asarray(light, dtype=float) - np.asarray(dark, dtype=float)
    head = pre_trigger_mean(p, dt, t0)
    if head is None or window_end is None or not dt:
        return None
    k = int(np.ceil(window_end / dt))
    if p.size - k < 20:
        return None
    return float(p[k:].mean() - head)


def edge_10_90_ns(trace: np.ndarray, dt: float) -> float | None:
    """Returns the displacement spike's leading-edge time from 10 to 90 % in ns.

    `trace` is in A and `dt` is in s. The spike is the absolute extreme; the
    baseline is the median 20–40 ns before it. An unavailable spike or
    baseline returns `None`.
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
    """Returns the trigger sync edge's 10–90 % rise or fall time in ns.

    `sync` is in V; `dt` and the record origin `t0` are in s. The search window
    is centred at the trigger position `-t0`. An absent or unresolved edge
    returns `None`, including a flat sync that trigger calibration rejects.

    The sync can be a narrow pulse. This bench's 5.5 ns pulse occupies 11 of
    400 search samples. Its 5th-to-95th percentile span is 0.038 V, below the
    0.30 V edge threshold, because both percentiles lie on the baseline
    (`docs/analysis/2026-09-06/spike-lag-and-dark-charge.md`). A nearly constant
    window therefore uses the excursion from its median to resolve the pulse;
    a step uses percentile levels.
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
        # A nearly constant window can contain a narrow sync pulse or no sync.
        # For a short pulse, both percentiles lie on the baseline; the median
        # and extreme instead define its leading edge. `_crossing_time` applies
        # the same crossing calculation as `edge_10_90_ns`.
        base = float(np.median(w))
        k = int(np.argmax(np.abs(w - base)))
        level = float(w[k])
        if abs(level - base) < 0.25 * np.ptp(a):
            return None
        # A single-sample glitch can supply the full excursion and pass the
        # amplitude checks. `_crossing_time` then gives coincident crossings
        # and zero width, falsely implying a sharper edge than the hardware
        # can produce. Requiring several samples and distinct crossings rejects
        # edges that the digitiser cannot resolve.
        toward = np.sign(level - base)
        above = (w - (base + 0.1 * (level - base))) * toward > 0
        first, last = k, k
        while first > 0 and above[first - 1]:
            first -= 1
        while last < w.size - 1 and above[last + 1]:
            last += 1
        if last - first < 1:
            return None
        edge = _crossing_time(w, k, base, level, dt)
        return edge if edge > 0.0 else None
    # The largest sample-to-sample change defines the edge direction.
    # The 10 % and 90 % crossings must bracket that transition.
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
