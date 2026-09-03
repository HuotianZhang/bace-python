r"""J–V output: the flat files and an HDF5 alongside.

The original wrote `BACE_JV_Data_<stamp>.dat` and
`BACE_JV_Parameters_<stamp>.dat`, with the column names recovered from the block
diagram (`Voltage`, `Current Density `, `LED Voltage`, `J_SC`, `V_OC`, `FF`).
Those names are reused and the house format is the same as the BACE files —
CRLF, tabs, `%10.5e` with an unpadded exponent, a `% ` header.

**Not byte-verified.** The BACE writer is checked against a real archive; this
one is not, because no J–V output folder has been read. If byte-compatibility
with the old J–V files matters, one sample pair is enough to pin it — the
difference would be in header wording and column order, not in the numbers.

The layout here is a small improvement on the original rather than a copy: the
curve file carries one column block per curve, labelled with its illumination
*and its sweep direction*, so a dark scan, a light scan and a hysteresis pair
stay in the same file as one measurement instead of becoming three files whose
relationship lives in someone's folder naming.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import numpy as np

from .legacy_dat import EOL, SEP, _write, render_table
from .numbers import lv_float

STEM_DATA = "BACE_JV_Data_"
STEM_PARAMETERS = "BACE_JV_Parameters_"

H_PARAMETERS = ("% LED Voltage/V ", "Voc/V", "Jsc/A", "Jsc/A cm-2", "Pmax/W",
                "Vmpp/V", "FF", "direction", "intensity/W")


def _label(curve) -> str:
    """`dark fwd`, `1.02 V rev`, `as found unknown fwd`.

    `curve.dark is None` means the run never set the light and could not read
    it either, so the curve carries the label `run_jv` built from the
    read-back; only a curve known to be dark is renamed `dark`.
    """
    d = "fwd" if curve.direction == "forward" else "rev"
    return f"{'dark' if curve.dark else curve.label} {d}"


def render_curves(curves) -> str:
    """One header line naming each column, then one row per sweep point.

    Curves may differ in length (a hysteresis pair does not, but a dark scan
    over a different range might), so short columns are padded with NaN rather
    than truncating everything to the shortest — losing measured points to make
    a rectangle is the wrong trade.
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
            head.append(f"J/A cm-2 ({tag})")
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
    """One file per J–V run, with every curve as its own group.

    Dark and light live together because a dark curve is only interpretable
    beside the light curve it belongs to, and hysteresis is only visible when
    both directions are in the same place.

    Schema `bace-jv/2` adds `/config/resolved` -- what the instruments
    reported during the run, folded from `InstrumentState` events the same
    way `bace-run/2` does it -- and a `shutter` attribute on every curve
    group. A light J-V taken with the shutter shut is a dark J-V wearing a
    light label, and until 2026-09-02 nothing in the file could say which it
    was. Readers of `bace-jv/1` are unaffected: nothing moved.

    `bace-jv/3` adds `illumination` on every curve group -- `dark`, `light` or
    `unknown` -- for the `jv` module, which sweeps under whatever light it
    finds and reads that back rather than setting it (`experiment.jv.
    illumination_state`). On an `unknown` curve the `dark` attribute is
    **absent**, not False: a reader that asks for it gets a KeyError, which is
    loud, where a False would have been a dark label on a curve nobody read.
    Every curve a run *set* the light for still carries `dark`, so files from
    `jv_bace` are `bace-jv/2` in every respect but the version.
    """
    import h5py

    from .hdf5 import COMPRESSION, _set_attrs

    with h5py.File(path, "w") as f:
        f.attrs["schema"] = "bace-jv/3"
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
            _set_attrs(sub, {
                "label": c.label, "direction": c.direction,
                "illumination": ("unknown" if dark is None
                                 else "dark" if dark else "light"),
                # `dark` only when the run knows. See the schema note above:
                # absent is the honest answer, False would be a claim.
                **({} if dark is None else {"dark": bool(dark)}),
                # "?" is honest: a rig without a shutter, or a run recorded
                # before the shutter state was on the stream, cannot say.
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
                d.attrs["unit"] = "A cm-2"
    return path


@dataclass
class JVRecorder:
    """Accumulates a J–V run and writes it out.

    Same contract as `RunRecorder`: a listener, not a stage, and it flushes
    whatever it has on any exit path — a series interrupted after the dark scan
    still leaves the dark scan on disk.
    """

    folder: str
    stamp: str
    metadata: dict
    rig_config: dict
    write_hdf5: bool = True
    resolved: dict = field(default_factory=dict)
    """Instrument readbacks, folded from `InstrumentState` events -- the
    shutter position before each curve, and anything a caller seeds here
    (the 33220A polarity it verified before the run). Same idea as
    `RunRecorder.resolved`: a *reading*, kept apart from the *settings*."""

    curves: list = field(default_factory=list)
    curve_states: list = field(default_factory=list)
    """The `resolved` snapshot in force when each curve was taken, so a
    file with a dark and a light curve says "shut" for one and "open" for
    the other rather than only the last value."""
    written: list[str] = field(default_factory=list)
    _config: dict = field(default_factory=dict)
    _finished: bool = False

    def __enter__(self) -> "JVRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    def handle(self, ev) -> None:
        from ..experiment.events import InstrumentState
        from ..experiment.jv import JVCurveDone, JVStarted
        if isinstance(ev, JVStarted):
            self._config = ev.config.get("jv", {})
        elif isinstance(ev, InstrumentState):
            self.resolved.update(ev.values)
        elif isinstance(ev, JVCurveDone):
            self.curves.append(ev)
            self.curve_states.append(dict(self.resolved))

    def finish(self) -> list[str]:
        if self._finished or not self.curves:
            self._finished = True
            return self.written
        self._finished = True
        os.makedirs(self.folder, exist_ok=True)
        self.written += JVWriter(self.folder, self.stamp).write(self.curves)
        if self.write_hdf5:
            self.written.append(write_hdf5(
                os.path.join(self.folder, f"jv{self.stamp}.h5"), self.curves,
                metadata=self.metadata, config=self._config,
                rig_config=self.rig_config, resolved=self.resolved,
                curve_states=self.curve_states))
        return self.written


def record(events, recorder: JVRecorder):
    """Pass events through while recording them, flushing on any exit."""
    try:
        for ev in events:
            recorder.handle(ev)
            yield ev
    finally:
        recorder.finish()
