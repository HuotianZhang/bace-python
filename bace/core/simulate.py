"""Synthetic traces exercise the pipeline without instruments.

The LabVIEW simulation model combines an error-function rise, an exponential
decay and uniform noise. The dark trace contains a scaled signal, sample
noise and a constant random offset. The light trace contains signal and
sample noise; the intensity varies uniformly from 1.0 to 1.1.

Clipping the exponential argument prevents overflow before the rise. A zero
rise multiplied by an infinite decay would otherwise produce NaN in NumPy.
"""
from __future__ import annotations

import numpy as np
from scipy.special import erf


def synthetic_traces(n: int = 2500, t_span: float = 1e-6, *,
                     rise_index: int = 200, tau: float = 1e-7,
                     noise: float = 0.05, dark_scale: float = 0.8,
                     rng: np.random.Generator | None = None
                     ) -> tuple[np.ndarray, np.ndarray, float, float]:
    """Returns synthetic light and dark traces, sample interval and intensity.

    `t_span`, `tau` and the returned sample interval are in s. Trace amplitudes,
    `noise` and intensity use arbitrary units. `rise_index` selects the sample
    where the rise starts; `rng` makes the random sequence reproducible.
    """
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
