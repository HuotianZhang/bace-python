"""Put a different integration window on a stored run.

Finding the *integration* zero -- where the transient starts, and so where
`t0_int_s` should sit -- is a question for data that has already been taken,
not for the acquisition: the traces are in the HDF5 at full precision, so the
window can be moved offline and Q recomputed. (Finding the *light* zero is the
other kind, and that one needs a delay scan with the window pinned to the
pulse -- `recipes/run-delay.toml`.)

The arithmetic is `experiment.transient.resolve_window`'s, applied to the
file's own numbers: `t0_int_s` is measured from the field's arrival, which is
`axis/delay_ns` plus the rig's `trigger_offset_s` after the trigger, and the
record's origin is each step's `traces/trace_t0` (`:WAV:XOR?`). Files written
before schema 3 have no `trace_t0`; the record's geometry (`:TIM:POS` four
divisions in) stands in for it, half a sample out, and the result says so.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .process import charge


@dataclass(frozen=True)
class Reintegrated:
    """One window, applied to every step of a run."""

    t0_int_s: float
    t_int_width_s: float
    trigger_offset_s: float
    window_s: np.ndarray
    """(n_steps, 2): the window in record time."""
    q_mean: np.ndarray
    """(n_steps,): from the loop-averaged photocurrent -- exactly the mean of
    the per-loop charges, because both are linear."""
    q_loops: np.ndarray | None
    """(n_loops, n_steps) from `traces/shots`, when the run stored them."""
    q_std: np.ndarray | None
    """(n_steps,) sample std over the loops, when the run stored them."""
    clipped: np.ndarray
    """(n_steps,) True where the window runs past the record."""
    trace_t0_source: str


def trace_t0_of(run: dict) -> tuple[np.ndarray, str]:
    """Each step's `:WAV:XOR?`, or the timebase geometry standing in for it."""
    n_steps = np.asarray(run["photocurrent"]).shape[0]
    t0 = run.get("trace_t0")
    if t0 is not None and np.isfinite(np.asarray(t0, dtype=float)).all():
        return np.asarray(t0, dtype=float), "the file's /traces/trace_t0"
    time = np.asarray(run["time_s"], dtype=float)
    dt = float(time[1] - time[0]) if time.size > 1 else 0.0
    per_div = float(run["run_config"].get("timebase_ns_per_div", 200.0)) * 1e-9
    # `:TIM:POS` is four divisions and is the screen centre: the record begins
    # half a record before it, which is one division before the trigger.
    guess = per_div * 4.0 - (time.size * dt) / 2.0
    return np.full(n_steps, guess), "timebase geometry (this file predates /traces/trace_t0)"


def reintegrate(run: dict, t0_int_s: float, t_int_width_s: float,
                trigger_offset_s: float | None = None) -> Reintegrated:
    """`run` is `storage.hdf5.read_run`'s dict. `trigger_offset_s` defaults to
    the rig the run was taken on."""
    if not t_int_width_s > 0:
        raise ValueError(f"t_int_width_s must be positive, not {t_int_width_s!r}")
    if trigger_offset_s is None:
        trigger_offset_s = float(run["rig_config"].get("trigger_offset_s", 0.0))
    photo = np.asarray(run["photocurrent"], dtype=float)
    time = np.asarray(run["time_s"], dtype=float)
    dt = float(time[1] - time[0])
    delay_s = np.asarray(run["delay_ns"], dtype=float) * 1e-9
    trace_t0, source = trace_t0_of(run)

    start = t0_int_s + delay_s + trigger_offset_s - trace_t0
    window = np.column_stack([start, start + t_int_width_s])
    record_end = (photo.shape[1] - 1) * dt
    q_mean = np.array([charge(photo[i], dt, *window[i]) for i in range(photo.shape[0])])

    q_loops = q_std = None
    shots = run.get("shots")
    if shots is not None:
        shots = np.asarray(shots, dtype=float)          # (loops, steps, samples)
        q_loops = np.full(shots.shape[:2], np.nan)
        for l in range(shots.shape[0]):
            for i in range(shots.shape[1]):
                if np.isfinite(shots[l, i]).all():
                    q_loops[l, i] = charge(shots[l, i], dt, *window[i])
        q_std = np.array([_std(q_loops[:, i]) for i in range(q_loops.shape[1])])

    return Reintegrated(t0_int_s=t0_int_s, t_int_width_s=t_int_width_s,
                        trigger_offset_s=trigger_offset_s, window_s=window,
                        q_mean=q_mean, q_loops=q_loops, q_std=q_std,
                        clipped=window[:, 1] > record_end, trace_t0_source=source)


def sweep(run: dict, t0_values_s, t_int_width_s: float,
          trigger_offset_s: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Q(t0_int) for every step: `(t0 values, (n_t0, n_steps) charges)`.

    The integration zero is where this stops changing as `t0` moves earlier --
    once the window starts before the transient, moving it further only adds
    baseline."""
    t0s = np.asarray(list(t0_values_s), dtype=float)
    q = np.array([reintegrate(run, float(t0), t_int_width_s, trigger_offset_s).q_mean
                  for t0 in t0s])
    return t0s, q


def _std(col: np.ndarray) -> float:
    col = col[np.isfinite(col)]
    if col.size < 2:
        return 0.0
    return float(np.std(col, ddof=1))
