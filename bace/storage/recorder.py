"""One listener on the event stream that writes both file sets.

It is a consumer, not a stage: `record()` passes every event straight through,
so the UI, the log and the recorder all see the same stream and adding a fourth
listener touches nothing here.

    for ev in record(run_transient_scan(rig, spec, cfg), recorder):
        ui.handle(ev)

**It flushes what it has, whenever it stops.** A run that is aborted, or that
dies on an instrument, still leaves a folder containing every completed loop —
with NaN in the rows that never ran, exactly as the original did. That is not a
nicety: the 2026-08-07 archive this port is validated against is itself a
partial run, 20 loops of a planned 100.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable, Iterator

import numpy as np

from ..core.process import RunningAverage
from ..experiment import events as E
from . import hdf5 as h5
from .legacy_dat import LegacyWriter
from .naming import RunMetadata


@dataclass
class RunRecorder:
    """Accumulates a run and writes it out.

    `root` is the directory runs are created under; the run gets its own folder
    named from the metadata.
    """

    root: str
    metadata: RunMetadata
    resolved: dict[str, str] = field(default_factory=dict)
    """Instrument readbacks, kept apart from the config so a reader can tell a
    *reading* from a *setting*. `InstrumentState` events add to it during the
    run; a caller that verified something before the run started -- the 33220A
    polarity, the 81150A arming -- can seed it here, because the run itself
    never reads those."""

    store_shots: bool = False
    write_legacy: bool = True
    write_hdf5: bool = True

    folder: str = field(init=False, default="")
    written: list[str] = field(init=False, default_factory=list)
    hdf5_error: str | None = field(init=False, default=None)
    """Set when the HDF5 file could not be written. The `.dat` files are always
    written first, so a run survives a missing h5py."""

    # accumulated state
    _n_steps: int = field(init=False, default=0)
    _n_loops: int = field(init=False, default=0)
    _configs: dict = field(init=False, default_factory=dict)
    _axis = None
    _values: np.ndarray | None = None
    _voc: float | None = None
    _setpoints: dict = field(init=False, default_factory=dict)
    _q: np.ndarray | None = None
    _light: RunningAverage | None = None
    _dark: RunningAverage | None = None
    _photo: np.ndarray | None = None
    _shots: np.ndarray | None = None
    _intensity: np.ndarray | None = None
    _dt: float = field(init=False, default=float("nan"))
    _finished: bool = field(init=False, default=False)

    # -- lifecycle --------------------------------------------------------
    def __enter__(self) -> "RunRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    def handle(self, ev: E.Event) -> None:
        if isinstance(ev, E.RunStarted):
            self._start(ev)
        elif isinstance(ev, E.InstrumentState):
            self.resolved.update(ev.values)
        elif isinstance(ev, E.AxisResolved):
            self._axis, self._values, self._voc = ev.axis, np.asarray(ev.values), ev.voc
        elif isinstance(ev, E.StepDone):
            self._step(ev)

    def _start(self, ev: E.RunStarted) -> None:
        self._n_steps, self._n_loops = ev.n_steps, ev.n_loops
        self._configs = ev.config
        self.folder = os.path.join(self.root, self.metadata.folder_name())
        os.makedirs(self.folder, exist_ok=True)
        self._q = np.full((self._n_loops, self._n_steps), np.nan)
        self._intensity = np.full((self._n_loops, self._n_steps), np.nan)

    def _step(self, ev: E.StepDone) -> None:
        i, loop = ev.step - 1, ev.loop
        if self._light is None:
            n = ev.light.y.size
            self._light = RunningAverage(self._n_steps, n)
            self._dark = RunningAverage(self._n_steps, n)
            self._photo = np.full((self._n_steps, n), np.nan)
            if self.store_shots:
                self._shots = np.full((self._n_loops, self._n_steps, n), np.nan)

        self._dt = ev.light.dt
        self._light.update(ev.step, loop, ev.light.y)
        self._dark.update(ev.step, loop, ev.dark.y)
        self._photo[i] = ev.photo_averaged
        self._q[loop - 1, i] = ev.q
        if ev.intensity_w is not None:
            self._intensity[loop - 1, i] = ev.intensity_w
        if self._shots is not None:
            self._shots[loop - 1, i] = ev.photo

        # keep the setpoints so the file is readable without knowing the axis
        self._setpoints[i] = ev.setpoint

    # -- output -----------------------------------------------------------
    def finish(self) -> list[str]:
        """Write everything accumulated so far. Safe to call twice."""
        if self._finished or self._light is None or self._axis is None:
            self._finished = True
            return self.written
        self._finished = True

        sp = [self._setpoints[i] for i in sorted(self._setpoints)]
        vpre = np.array([s.vpre for s in sp])
        vcoll = np.array([s.vcoll for s in sp])
        delay = np.array([s.delay_ns for s in sp])
        q_mean, q_std = _nan_stats(self._q)
        i_mean, i_std = _nan_stats(self._intensity)
        time_s = np.arange(self._photo.shape[1]) * self._dt

        if self.write_legacy:
            w = LegacyWriter(self.folder, self.metadata.stamp)
            self.written += [
                w.averages_q(vpre, vcoll, delay, q_mean, q_std),
                w.all_loops_q(vpre, self._q),
                w.trace_matrix("averages_photocurrent", time_s, vpre, self._photo),
                w.trace_matrix("averages_light", time_s, vpre, self._light.traces),
                w.trace_matrix("averages_dark", time_s, vpre, self._dark.traces),
            ]
            if np.isfinite(i_mean).any():
                self.written.append(w.averages_intensity(vpre, i_mean, i_std))

        if self.write_hdf5:
            try:
                self._write_hdf5(vpre, vcoll, delay, q_mean, q_std,
                                 time_s, i_mean, i_std)
            except Exception as exc:
                # An hour of measurement must not be lost because an optional
                # dependency is missing or a disk hiccuped. The legacy files are
                # already on disk by this point; record the failure and move on.
                self.hdf5_error = f"{type(exc).__name__}: {exc}"
        return self.written

    def _write_hdf5(self, vpre, vcoll, delay, q_mean, q_std, time_s,
                    i_mean, i_std) -> None:
        path = os.path.join(self.folder, f"run{self.metadata.stamp}.h5")
        ax = self._axis
        have_intensity = bool(np.isfinite(i_mean).any())
        h5.write_run(
            path,
            metadata=self.metadata.as_dict(),
            rig_config=self._configs.get("rig", {}),
            run_config=self._configs.get("run", {}),
            resolved=self.resolved,
            axis_name=ax.name, axis_unit=ax.unit,
            axis_attrs={"start": ax.start, "stop": ax.stop, "step": ax.step,
                        "centre_on_voc": bool(ax.centre_on_voc),
                        "voc": "" if self._voc is None else self._voc},
            values=self._values if self._values is not None else vpre,
            vpre=vpre, vcoll=vcoll, delay_ns=delay,
            q_all=self._q, q_mean=q_mean, q_std=q_std,
            time_s=time_s, light=self._light.traces, dark=self._dark.traces,
            photocurrent=self._photo, shots=self._shots,
            intensity_mean=i_mean if have_intensity else None,
            intensity_std=i_std if have_intensity else None,
        )
        self.written.append(path)


def _nan_stats(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Column mean and sample std, ignoring loops that never ran.

    `ddof=1` to match MathScript's `std`, and a column with one sample gets
    std 0 rather than NaN — the same convention `core.process` uses.
    """
    a = np.asarray(a, dtype=float)
    mean = np.full(a.shape[1], np.nan)
    std = np.full(a.shape[1], np.nan)
    for j in range(a.shape[1]):
        col = a[:, j]
        col = col[~np.isnan(col)]
        if col.size:
            mean[j] = col.mean()
            std[j] = col.std(ddof=1) if col.size > 1 else 0.0
    return mean, std


def record(events: Iterable[E.Event], recorder: RunRecorder) -> Iterator[E.Event]:
    """Pass events through while recording them, flushing on any exit."""
    try:
        for ev in events:
            recorder.handle(ev)
            yield ev
    finally:
        recorder.finish()
