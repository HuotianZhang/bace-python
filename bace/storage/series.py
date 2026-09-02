r"""Output for an intensity series.

Three files at the top of a series folder, plus one full run folder per
illumination point (written by the ordinary `RunRecorder`, so every point is a
complete BACE dataset in its own right and can be re-analysed alone).

* `<yymmdd>_<HHMMSS>_LED_Voc_Jsc_BACE Parameters.dat` — the original's summary.
  Filename and column names are recovered from the block diagram: a two-digit
  year, the timestamp as a *prefix*, and a space in the name. Reproduced as
  found. Written **only when the axis has exactly three points**, because the
  format has `Vpre1/2/3` columns baked in and cannot express anything else —
  which is precisely the assumption the axis abstraction removes.
* `series_<stamp>.dat` — the general form: one row per (LED level, axis point).
  Always written, whatever the axis length.
* `series_<stamp>.h5` — everything, at full precision.

**Not byte-verified.** No series summary file has been read, so unlike the BACE
per-run writer this one is a reconstruction from the recovered strings. One
sample would pin it.

Columns that need a calibration nobody recorded are written as NaN rather than
guessed: J_sc and J_sat in mA/cm² need the pixel area, and the intensity column
needs the factor converting meter watts to irradiance at the sample. A number
that looks calibrated and is not is worse than a gap.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np

from .legacy_dat import EOL, SEP, _write, render_table
from .naming import RunMetadata
from .numbers import lv_float
from .recorder import RunRecorder

LEGACY_TITLE = "LED_Voc_Jsc_BACE Parameters"
LEGACY_COLUMNS = (
    "% Voc [V]", "Jsc [mA/cm2]", "LED [V]",
    "Vpre1 [V]", "Vpre2 [V]", "Vpre3 [V]",
    "Vcoll1", "Vcoll2", "Vcoll3",
    "Delay time1 [ns]", "Delay time2 [ns]", "Delay time3 [ns]",
    "Q1 [C]", "Q2 [C]", "Q3 [C]",
    "stdDevQ1 [C]", "stdDevQ2 [C]", "stdDevQ3 [C]",
    "Jsat [mA/cm2]", "LED Intensity [mW/cm^2]",
)
LONG_COLUMNS = ("% LED [V]", "Voc [V]", "Jsc [A]", "Jsat [A]",
                "axis", "Vcoll [V]", "Delay [ns]", "Q [C]", "stdDevQ [C]",
                "Intensity [W]")

NAN = float("nan")


def _density(current: float | None, area_cm2: float) -> float:
    """A -> mA/cm². NaN when the area is unknown, never a silent 1 cm²."""
    if current is None or area_cm2 <= 0:
        return NAN
    return current * 1e3 / area_cm2


def render_legacy(points, *, area_cm2: float, intensity_factor: float) -> str:
    rows = []
    for p in points:
        v = np.asarray(p.axis_values, dtype=float)
        q = np.asarray(p.q_mean, dtype=float)
        sd = np.asarray(p.q_std, dtype=float)
        if v.size != 3:
            raise ValueError(
                f"the legacy summary has Vpre1/2/3 columns and this run has "
                f"{v.size} axis points. Use the long format instead — the "
                "three-point shape is the assumption the axis parameter removes."
            )
        rows.append((
            NAN if p.voc is None else p.voc,
            _density(None if p.dc is None else p.dc.jsc, area_cm2),
            p.level_v,
            v[0], v[1], v[2],
            p.vcoll, p.vcoll, p.vcoll,
            p.delay_ns, p.delay_ns, p.delay_ns,
            q[0], q[1], q[2],
            sd[0], sd[1], sd[2],
            _density(None if p.dc is None else p.dc.jsat, area_cm2),
            NAN if (p.intensity_w is None or intensity_factor <= 0)
            else p.intensity_w * intensity_factor,
        ))
    return f"% {LEGACY_TITLE}{EOL}" + render_table(LEGACY_COLUMNS, rows)


def render_long(points) -> str:
    out = [SEP.join(LONG_COLUMNS) + EOL]
    for p in points:
        v = np.asarray(p.axis_values, dtype=float)
        q = np.asarray(p.q_mean, dtype=float)
        sd = np.asarray(p.q_std, dtype=float)
        for j in range(v.size):
            out.append(SEP.join(lv_float(x) for x in (
                p.level_v,
                NAN if p.voc is None else p.voc,
                NAN if p.dc is None else p.dc.jsc,
                NAN if p.dc is None else p.dc.jsat,
                v[j], p.vcoll, p.delay_ns, q[j], sd[j],
                NAN if p.intensity_w is None else p.intensity_w,
            )) + EOL)
    return "".join(out)


def write_hdf5(path: str, points, *, metadata: dict, config: dict) -> str:
    import h5py

    from .hdf5 import COMPRESSION, _set_attrs

    with h5py.File(path, "w") as f:
        f.attrs["schema"] = "bace-series/1"
        f.attrs["n_levels"] = len(points)
        _set_attrs(f.create_group("metadata"), metadata)
        cfg = f.create_group("config")
        for name, table in config.items():
            _set_attrs(cfg.create_group(name), table)

        def col(fn):
            return np.array([fn(p) for p in points], dtype=float)

        g = f.create_group("levels")
        for name, arr, unit in (
            ("led_v", col(lambda p: p.level_v), "V"),
            ("voc", col(lambda p: NAN if p.voc is None else p.voc), "V"),
            ("jsc", col(lambda p: NAN if p.dc is None else p.dc.jsc), "A"),
            ("jsat", col(lambda p: NAN if p.dc is None else p.dc.jsat), "A"),
            ("intensity", col(lambda p: NAN if p.intensity_w is None
                              else p.intensity_w), "W"),
        ):
            d = g.create_dataset(name, data=arr)
            d.attrs["unit"] = unit

        # Axis lengths are equal within a series, so this is rectangular.
        q = f.create_group("charge")
        for name, arr in (
            ("axis", np.array([p.axis_values for p in points], dtype=float)),
            ("mean", np.array([p.q_mean for p in points], dtype=float)),
            ("std", np.array([p.q_std for p in points], dtype=float)),
        ):
            d = q.create_dataset(name, data=arr, **COMPRESSION)
            d.attrs["shape"] = "(level, axis point)"
    return path


@dataclass
class SeriesRecorder:
    """Writes the series summary, and a full run folder per illumination point.

    Each point's folder is an ordinary BACE dataset — same five `.dat` files,
    same HDF5 — so any single intensity can be re-analysed on its own without
    knowing it was part of a series.
    """

    root: str
    metadata: RunMetadata
    pixel_area_cm2: float = 0.0
    intensity_factor: float = 0.0
    store_shots: bool = False

    folder: str = field(init=False, default="")
    written: list[str] = field(init=False, default_factory=list)
    points: list = field(init=False, default_factory=list)
    _config: dict = field(init=False, default_factory=dict)
    _current: RunRecorder | None = field(init=False, default=None)
    _voc: float | None = field(init=False, default=None)
    _finished: bool = field(init=False, default=False)

    def __enter__(self) -> "SeriesRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    # -- stream -----------------------------------------------------------
    def handle(self, ev) -> None:
        from ..experiment import intensity_series as S
        from ..experiment.events import DCMeasured

        if isinstance(ev, S.SeriesStarted):
            self._config = ev.config
            self.folder = os.path.join(self.root,
                                       f"{self.metadata.folder_name()}_series")
            os.makedirs(self.folder, exist_ok=True)
        elif isinstance(ev, DCMeasured):
            self._voc = ev.dc.voc
        elif isinstance(ev, S.IlluminationSet) and ev.mode == "pulse":
            self._close_current()
            # Each point carries the time *it* ran, not the series start: a
            # series that repeats a level would otherwise collide, and a file
            # stamped hours before it was written is a small lie in the record.
            meta = replace(self.metadata, led_drive_v=ev.level_v, voc_v=self._voc,
                           started=datetime.now())
            self._current = RunRecorder(self.folder, meta,
                                        store_shots=self.store_shots)
        elif isinstance(ev, S.SeriesPointDone):
            self.points.append(ev)
            self._close_current()
        elif self._current is not None:
            self._current.handle(ev)

    def _close_current(self) -> None:
        if self._current is not None:
            self.written += self._current.finish()
            self._current = None

    # -- output -----------------------------------------------------------
    def finish(self) -> list[str]:
        if self._finished:
            return self.written
        self._finished = True
        self._close_current()
        if not self.points or not self.folder:
            return self.written

        stamp = self.metadata.stamp
        self.written.append(_write(self.folder, "series_", stamp,
                                   render_long(self.points)))
        if all(np.asarray(p.axis_values).size == 3 for p in self.points):
            legacy = render_legacy(self.points, area_cm2=self.pixel_area_cm2,
                                   intensity_factor=self.intensity_factor)
            name = f"{self.metadata.started.strftime('%y%m%d_%H%M%S')}_{LEGACY_TITLE}.dat"
            path = os.path.join(self.folder, name)
            with open(path, "w", newline="", encoding="ascii") as fh:
                fh.write(legacy)
            self.written.append(path)

        self.written.append(write_hdf5(
            os.path.join(self.folder, f"series_{stamp}.h5"), self.points,
            metadata=self.metadata.as_dict(), config=self._config))
        return self.written


def record(events, recorder: SeriesRecorder):
    """Pass events through while recording them, flushing on any exit."""
    try:
        for ev in events:
            recorder.handle(ev)
            yield ev
    finally:
        recorder.finish()
