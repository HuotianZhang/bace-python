r"""J–V runs have tabular `.dat` exports and a full-precision HDF5 file.

The LabVIEW filenames `BACE_JV_Data_<stamp>.dat` and
`BACE_JV_Parameters_<stamp>.dat` are retained. The format uses CRLF, tabs,
`%10.5e` with an unpadded exponent and a `% ` header.

Every current density is in mA/cm², through `experiment.jv.current_density`.
Column names such as `J/mA cm-2` and `Jsc/mA cm-2` state the unit so readers
do not need this docstring to interpret a file.

Byte compatibility with LabVIEW J–V exports is unverified. Header wording and
column order require a sample file pair for comparison; the shared number
formatter is checked against BACE exports.

Each curve occupies a column block labelled by illumination and scan direction.
Dark, light and hysteresis curves therefore stay together as one measurement.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import numpy as np

from .legacy_dat import EOL, SEP, _write, render_table
from .numbers import lv_float

STEM_DATA = "BACE_JV_Data_"
STEM_PARAMETERS = "BACE_JV_Parameters_"

H_PARAMETERS = ("% LED Voltage/V ", "Voc/V", "Jsc/A", "Jsc/mA cm-2", "Pmax/W",
                "Vmpp/V", "FF", "direction", "intensity/W")


FLUSH_S = 2.0
"""The minimum interval in s between file rewrites during a curve.

Each incoming point can trigger a flush after this interval. Periodic
flushes limit data loss from a fault, stop or disconnection to the points
acquired after the last flush.
"""


@dataclass
class PartialCurve:
    """A curve accumulated from `JVPoint` events while acquisition continues.

    It is written with a partial label and point count, without metrics.
    Interpolating V_oc from an incomplete curve can imply a measurement
    the acquired range does not support.
    """

    index: int
    label: str
    dark: bool | None
    led_level_v: float | None
    direction: str
    of: int
    voltage: list = field(default_factory=list)
    current: list = field(default_factory=list)
    density: list | None = None
    intensity_w: float | None = None
    illumination: dict | None = None
    partial: bool = True

    @property
    def k(self) -> int:
        return len(self.voltage)

    @property
    def metrics(self):
        from ..experiment.jv import JVMetrics
        return JVMetrics(voc=None, jsc=None, p_max=None, v_mpp=None, j_mpp=None,
                         fill_factor=None)

    def add(self, ev) -> None:
        self.voltage.append(float(ev.voltage))
        self.current.append(float(ev.current))
        if ev.density is not None:
            if self.density is None:
                self.density = []
            self.density.append(float(ev.density))


def _label(curve) -> str:
    """Returns a curve label with illumination, direction and partial point count.

    Examples are `dark fwd`, `1.02 V rev` and `as found unknown fwd`, with
    `partial 12 of 36` for an unfinished curve. `curve.dark is None` retains
    the read-back label from `experiment.jv.run_jv`; only a known dark curve
    is labelled `dark`.
    """
    d = "fwd" if curve.direction == "forward" else "rev"
    tag = f"{'dark' if curve.dark else curve.label} {d}"
    if getattr(curve, "partial", False):
        tag += f" partial {curve.k} of {curve.of}"
    return tag


def render_curves(curves) -> str:
    """Renders named curve columns with one row per scan point.

    Curves can have different voltage ranges or lengths. NaN padding preserves
    all measured points; truncating to the shortest curve would discard data.
    Voltage is in V, current in A and current density in mA/cm².
    """
    if not curves:
        return ""
    head = ["% index"]
    cols: list[np.ndarray] = []
    for c in curves:
        tag = _label(c)
        head += [f"V/V ({tag})", f"I/A ({tag})"]
        cols += [np.asarray(c.voltage, dtype=float), np.asarray(c.current, dtype=float)]
        if c.density is not None:
            head.append(f"J/mA cm-2 ({tag})")
            cols.append(np.asarray(c.density, dtype=float))

    n = max(col.size for col in cols)
    padded = [np.concatenate([col, np.full(n - col.size, np.nan)]) for col in cols]

    out = [SEP.join(head) + EOL]
    for r in range(n):
        out.append(SEP.join([lv_float(r + 1)] + [lv_float(col[r]) for col in padded])
                   + EOL)
    return "".join(out)


def render_parameters(curves) -> str:
    rows = []
    for c in curves:
        m = c.metrics
        rows.append((
            np.nan if c.led_level_v is None else c.led_level_v,
            np.nan if m.voc is None else m.voc,
            np.nan if m.jsc is None else m.jsc,
            np.nan if (m.jsc is None or c.density is None)
            else float(np.interp(0.0, c.voltage, c.density)),
            np.nan if m.p_max is None else m.p_max,
            np.nan if m.v_mpp is None else m.v_mpp,
            np.nan if m.fill_factor is None else m.fill_factor,
            0.0 if c.direction == "forward" else 1.0,
            np.nan if c.intensity_w is None else c.intensity_w,
        ))
    return render_table(H_PARAMETERS, rows)


@dataclass
class JVWriter:
    folder: str
    stamp: str

    def write(self, curves) -> list[str]:
        os.makedirs(self.folder, exist_ok=True)
        return [_write(self.folder, STEM_DATA, self.stamp, render_curves(curves)),
                _write(self.folder, STEM_PARAMETERS, self.stamp,
                       render_parameters(curves))]


def write_hdf5(path: str, curves, *, metadata: dict, config: dict,
               rig_config: dict, resolved: dict | None = None,
               curve_states: list[dict] | None = None) -> str:
    """Writes every J–V curve as a group in one run file.

    Dark and light curves stay together for comparison; both scan directions
    are needed to assess hysteresis. `/config/resolved` and each curve's
    `shutter` attribute retain read-back independently of requested settings.
    These fields are available in schemas 2–4; other paths also support schema 1.

    Schemas 3–4 label illumination as `dark`, `light` or `unknown` using
    `experiment.jv.illumination_state`. For `unknown`, `dark` is absent:
    `False` would falsely claim known illumination. Access to an absent
    attribute raises `KeyError`. Curves with controlled illumination retain
    `dark`, including those from `jv_bace`.

    Schema 4 stores `density` in mA/cm² with unit `mA cm-2`. Schemas 1–3 use
    A/cm² with unit `A cm-2`. Readers must check the schema or unit: identical
    dataset names and shapes cannot reveal the factor of 1000 between them.
    """
    import h5py

    from .hdf5 import COMPRESSION, _set_attrs

    with h5py.File(path, "w") as f:
        f.attrs["schema"] = "bace-jv/4"
        f.attrs["n_curves"] = len(curves)
        _set_attrs(f.create_group("metadata"), metadata)
        cfg = f.create_group("config")
        _set_attrs(cfg.create_group("jv"), config)
        _set_attrs(cfg.create_group("rig"), rig_config)
        _set_attrs(cfg.create_group("resolved"), resolved or {})

        states = list(curve_states or [])
        g = f.create_group("curves")
        for i, c in enumerate(curves):
            sub = g.create_group(f"{c.index:02d}_{_label(c).replace(' ', '_')}")
            state = states[i] if i < len(states) else {}
            dark = getattr(c, "dark", None)
            partial = bool(getattr(c, "partial", False))
            _set_attrs(sub, {
                "label": c.label, "direction": c.direction,
                # Partial curves keep every acquired point and identify the intended
                # length through `points_planned`.
                "partial": partial,
                **({"points_planned": int(c.of)} if partial else {}),
                "illumination": ("unknown" if dark is None
                                 else "dark" if dark else "light"),
                # Unknown illumination requires an absent `dark` attribute;
                # `False` would assert a known light curve.
                **({} if dark is None else {"dark": bool(dark)}),
                # `"?"` preserves unknown state when no shutter or read-back event
                # supplies evidence.
                "shutter": state.get("shutter", "?"),
                "led_level_v": "" if c.led_level_v is None else c.led_level_v,
                "intensity_w": "" if c.intensity_w is None else c.intensity_w,
                **{k: ("" if v is None else v) for k, v in c.metrics.as_dict().items()},
            })
            d = sub.create_dataset("voltage", data=np.asarray(c.voltage, float),
                                   **COMPRESSION)
            d.attrs["unit"] = "V"
            d = sub.create_dataset("current", data=np.asarray(c.current, float),
                                   **COMPRESSION)
            d.attrs["unit"] = "A"
            d.attrs["sign"] = "as the instrument reported it"
            if c.density is not None:
                d = sub.create_dataset("density", data=np.asarray(c.density, float),
                                       **COMPRESSION)
                d.attrs["unit"] = "mA cm-2"
    return path


@dataclass
class JVRecorder:
    """Accumulates J–V curves and write completed and partial data.

    Like `RunRecorder`, this is an event listener that flushes on every exit.
    Interruption after a dark scan still preserves that scan on disk.
    """

    folder: str
    stamp: str
    metadata: dict
    rig_config: dict
    write_hdf5: bool = True
    resolved: dict = field(default_factory=dict)
    """Instrument read-back accumulated from `InstrumentState` events.

    This includes shutter position per curve and caller-supplied checks such
    as 33220A polarity. As in `RunRecorder.resolved`, readings stay separate
    from requested settings.
    """

    curves: list = field(default_factory=list)
    partial: PartialCurve | None = None
    """The active curve accumulated from `JVPoint` events, or `None`.

    Flushes include it alongside completed curves. Its `JVCurveDone` event
    replaces it with the completed curve.
    """
    curve_states: list = field(default_factory=list)
    """The `resolved` snapshot for each completed curve.

    Per-curve snapshots preserve different shutter states for dark and light
    curves instead of applying the last state to both.
    """
    written: list[str] = field(default_factory=list)
    _config: dict = field(default_factory=dict)
    _finished: bool = False
    _flushed_at: float = 0.0

    def __enter__(self) -> "JVRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    def handle(self, ev) -> None:
        from ..experiment.events import InstrumentState
        from ..experiment.jv import JVCurveDone, JVPoint, JVStarted
        if isinstance(ev, JVStarted):
            self._config = ev.config.get("jv", {})
        elif isinstance(ev, InstrumentState):
            self.resolved.update(ev.values)
        elif isinstance(ev, JVPoint):
            p = self.partial
            if p is None or p.index != ev.index or p.direction != ev.direction:
                p = self.partial = PartialCurve(
                    index=ev.index, label=ev.label, dark=ev.dark,
                    led_level_v=ev.led_level_v, direction=ev.direction, of=int(ev.of))
            p.add(ev)
            # Periodic flushes preserve the active curve's points if a fault
            # interrupts acquisition before its completion event.
            if time.monotonic() - self._flushed_at >= FLUSH_S:
                self.flush()
        elif isinstance(ev, JVCurveDone):
            self.curves.append(ev)
            self.curve_states.append(dict(self.resolved))
            self.partial = None
            self.flush()

    def _all(self) -> list:
        out = list(self.curves)
        if self.partial is not None and self.partial.k > 0:
            out.append(self.partial)
        return out

    def flush(self) -> list[str]:
        """Rewrite files with all completed curves and the active partial curve.

        J–V files occupy tens of kilobytes, so repeated rewrites are inexpensive.
        Paths stay the same and `written` lists each path once.
        """
        curves = self._all()
        if not curves:
            return self.written
        os.makedirs(self.folder, exist_ok=True)
        paths = JVWriter(self.folder, self.stamp).write(curves)
        if self.write_hdf5:
            states = list(self.curve_states)
            if self.partial is not None and self.partial.k > 0:
                states.append(dict(self.resolved))
            paths.append(write_hdf5(
                os.path.join(self.folder, f"jv{self.stamp}.h5"), curves,
                metadata=self.metadata, config=self._config,
                rig_config=self.rig_config, resolved=self.resolved,
                curve_states=states))
        for path in paths:
            if path not in self.written:
                self.written.append(path)
        self._flushed_at = time.monotonic()
        return self.written

    def finish(self) -> list[str]:
        if self._finished:
            return self.written
        self._finished = True
        return self.flush()


def record(events, recorder: JVRecorder):
    """Yields each event unchanged while recording, and flush on every exit."""
    try:
        for ev in events:
            recorder.handle(ev)
            yield ev
    finally:
        recorder.finish()
