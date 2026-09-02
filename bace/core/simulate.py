"""Synthetic transients, for running the whole pipeline without hardware.

Ported from the simulation MathScript node of TDCF-BACE_20160223.vi:

    N = 2500; t = linspace(0,1e-6,N); dt = t(2)-t(1);
    t0 = t(200); T = 1e-7;
    LC = (erf((t-t0)*5e8)+1) .* exp(-(t-t0)/T);
    DC = 0.8*LC + 0.05*rand(1,N) + 0.02*rand(1);
    LC = LC + 0.05*rand(1,N);
    Int = 1 + 0.1*rand(1);

The exponential is clipped before evaluation: for t well before t0 the
error-function factor is zero while `exp(-(t-t0)/T)` overflows, and 0*inf is
NaN in NumPy where MATLAB's ordering happened to hide it.
"""
from __future__ import annotations

import numpy as np
from scipy.special import erf


def synthetic_traces(n: int = 2500, t_span: float = 1e-6, *,
                     rise_index: int = 200, tau: float = 1e-7,
                     noise: float = 0.05, dark_scale: float = 0.8,
                     rng: np.random.Generator | None = None
                     ) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Return (light, dark, dt, intensity)."""
    rng = rng or np.random.default_rng()
    t = np.linspace(0.0, t_span, n)
    dt = float(t[1] - t[0])
    t0 = float(t[rise_index])

    rise = erf((t - t0) * 5e8) + 1.0
    decay = np.exp(np.clip(-(t - t0) / tau, -700.0, 700.0))
    signal = np.where(rise > 0.0, rise * decay, 0.0)

    dark = dark_scale * signal + noise * rng.random(n) + 0.02 * rng.random()
    light = signal + noise * rng.random(n)
    intensity = 1.0 + 0.1 * rng.random()
    return light, dark, dt, float(intensity)
