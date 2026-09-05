"""The primary store: one HDF5 file per run, carrying its own recipe.

The `.dat` export is what existing analysis reads, but it is lossy — six
significant figures, and no record of which instrument was set to what. This
file is the one that has to be sufficient to reconstruct the run years later, so
it holds the arrays at full precision plus every configuration value that
produced them.

Layout::

    /                       attrs: schema, created, software version
    /metadata               attrs: sample, material, pixel, temperature_k,
                            temperature_how/-_source (where that kelvin came
                            from: typed, setpoint, settled, operator -- absent
                            in files written before they existed), ...
    /config/rig             attrs: sense resistor, pulse amp, channels, ...
    /config/run             attrs: averages, timebase, t0_int, settle times, ...
    /config/resolved        attrs: what the instruments reported at setup --
                            :OUTP:POL in force, and anything else the recipe
                            asked to be left alone. Absent in schema 1 files.
    /axis                   attrs: name, unit, start, stop, step, centre_on_voc, voc
      values      (n_steps,)          the swept quantity
      vpre        (n_steps,)          full setpoint at every step, including
      vcoll       (n_steps,)          the two that were pinned -- so a file is
      delay_ns    (n_steps,)          readable without knowing which was swept
    /charge
      q           (n_loops, n_steps)  per shot; NaN where a loop never ran
      mean        (n_steps,)          nan-aware
      std         (n_steps,)          nan-aware, sample (ddof=1)
    /traces
      time        (n_samples,)        record time, from the first sample
      trace_t0    (n_steps,)          :WAV:XOR? -- where the record begins
                                      relative to the trigger (negative:
                                      the trigger is inside the record)
      window      (n_steps, 2)        the integration window Q was taken
                                      over, record time; pinned to the pulse
                                      so it moves along a delay axis
      light       (n_steps, n_samples)   loop-averaged, amps
      dark        (n_steps, n_samples)
      photocurrent(n_steps, n_samples)
      shots       (n_loops, n_steps, n_samples)   only if store_shots=True
    /intensity
      mean, std   (n_steps,)          W, when a power meter was present

**Partial runs are first class.** The 2026-08-07 archive is one: 100 loops
planned, 20 completed, the rest NaN. `loops_completed` is an attribute, and
every consumer should read it rather than assuming the array is full.

`shots` is off by default. At 51 axis points x 100 loops x 4000 samples it is
163 MB per run, which is the honest version of the original's `Big File?` — a
choice about disk, made explicitly, rather than a switch nobody remembered.
"""
from __future__ import annotations

from typing import Any

import numpy as np

SCHEMA = "bace-run/3"
"""3 adds `/traces/trace_t0` and `/traces/window`; 2 added `/config/resolved`. Readers of 1 are unaffected: nothing moved, and
`read_run` returns `{}` for the group when it is absent."""
COMPRESSION = dict(compression="gzip", compression_opts=4, shuffle=True)


def _require_h5py():
    try:
        import h5py
    except ImportError as exc:                       # pragma: no cover
        raise ImportError(
            "HDF5 storage needs h5py — pip install h5py. The legacy .dat writer "
            "has no such dependency, so a rig without h5py can still record."
        ) from exc
    return h5py


def _set_attrs(node, mapping: dict[str, Any]) -> None:
    for k, v in mapping.items():
        if v is None:
            v = ""
        if isinstance(v, bool):
            v = np.bool_(v)
        node.attrs[k] = v


def write_run(path: str, *, metadata: dict, rig_config: dict, run_config: dict,
              resolved: dict | None = None,
              axis_name: str, axis_unit: str, axis_attrs: dict,
              values: np.ndarray, vpre: np.ndarray, vcoll: np.ndarray,
              delay_ns: np.ndarray, q_all: np.ndarray, q_mean: np.ndarray,
              q_std: np.ndarray, time_s: np.ndarray, light: np.ndarray,
              dark: np.ndarray, photocurrent: np.ndarray,
              shots: np.ndarray | None = None,
              intensity_mean: np.ndarray | None = None,
              intensity_std: np.ndarray | None = None,
              sync_light: np.ndarray | None = None,
              sync_dark: np.ndarray | None = None,
              diagnostics: dict | None = None,
              window_s: np.ndarray | None = None,
              trace_t0_s: np.ndarray | None = None,
              software: str = "bace") -> str:
    h5py = _require_h5py()
    q_all = np.asarray(q_all, dtype=float)
    completed = int((~np.isnan(q_all)).all(axis=1).sum())

    with h5py.File(path, "w") as f:
        f.attrs["schema"] = SCHEMA
        f.attrs["software"] = software
        f.attrs["loops_planned"] = q_all.shape[0]
        f.attrs["loops_completed"] = completed
        f.attrs["complete"] = np.bool_(completed == q_all.shape[0])

        _set_attrs(f.create_group("metadata"), metadata)
        cfg = f.create_group("config")
        _set_attrs(cfg.create_group("rig"), rig_config)
        _set_attrs(cfg.create_group("run"), run_config)
        # Readings, not settings. A run told to leave a polarity alone records
        # nothing about it in `run`; without this group the file cannot say
        # which convention produced its own numbers.
        _set_attrs(cfg.create_group("resolved"), resolved or {})

        ax = f.create_group("axis")
        _set_attrs(ax, {"name": axis_name, "unit": axis_unit, **axis_attrs})
        for name, arr, unit in (("values", values, axis_unit),
                                ("vpre", vpre, "V"),
                                ("vcoll", vcoll, "V"),
                                ("delay_ns", delay_ns, "ns")):
            d = ax.create_dataset(name, data=np.asarray(arr, dtype=float))
            d.attrs["unit"] = unit

        ch = f.create_group("charge")
        for name, arr in (("q", q_all), ("mean", q_mean), ("std", q_std)):
            d = ch.create_dataset(name, data=np.asarray(arr, dtype=float), **COMPRESSION)
            d.attrs["unit"] = "C"
        ch["q"].attrs["note"] = "NaN where that loop was not run"

        tr = f.create_group("traces")
        d = tr.create_dataset("time", data=np.asarray(time_s, dtype=float), **COMPRESSION)
        d.attrs["unit"] = "s"
        d.attrs["origin"] = "first sample of the record, not the trigger"
        if trace_t0_s is not None:
            d = tr.create_dataset("trace_t0", data=np.asarray(trace_t0_s, dtype=float))
            d.attrs["unit"] = "s"
            d.attrs["note"] = (":WAV:XOR? per step: record time of the trigger is "
                               "-trace_t0")
        if window_s is not None:
            d = tr.create_dataset("window", data=np.asarray(window_s, dtype=float))
            d.attrs["unit"] = "s"
            d.attrs["note"] = ("(step, [start, end]) of the charge integral in record "
                               "time; t0_int_s + delay + trigger_offset_s - trace_t0, "
                               "then + t_int_width_s")
        for name, arr in (("light", light), ("dark", dark),
                          ("photocurrent", photocurrent)):
            d = tr.create_dataset(name, data=np.asarray(arr, dtype=float), **COMPRESSION)
            d.attrs["unit"] = "A"
        if shots is not None:
            d = tr.create_dataset("shots", data=np.asarray(shots, dtype=float),
                                  **COMPRESSION)
            d.attrs["unit"] = "A"
            d.attrs["note"] = "(loop, step, sample) photocurrent, un-averaged"
        # The trigger channel out of the same records (2026-09-05): what the
        # edge the scope fired on looked like, loop-averaged like the
        # currents. Volts, not amps -- it is a sync line, not the resistor.
        for name, arr in (("sync_light", sync_light), ("sync_dark", sync_dark)):
            if arr is None:
                continue
            d = tr.create_dataset(name, data=np.asarray(arr, dtype=float), **COMPRESSION)
            d.attrs["unit"] = "V"
            d.attrs["note"] = "the trigger channel, same record as the current trace"
        if diagnostics:
            # Per-shot, (loop, step): is this shot the measurement it claims
            # to be? Average counts from `:WAV:COUN?`, the light-to-dark
            # spike lag and the spike and sync edges (`core.diagnostics`).
            dg = f.create_group("diagnostics")
            for name, arr in diagnostics.items():
                d = dg.create_dataset(name, data=np.asarray(arr, dtype=float))
                d.attrs["unit"] = "ns" if name.endswith("_ns") else "acquisitions"
            dg.attrs["note"] = "(loop, step); NaN where a shot could not say"

        if intensity_mean is not None:
            it = f.create_group("intensity")
            for name, arr in (("mean", intensity_mean), ("std", intensity_std)):
                if arr is None:
                    continue
                d = it.create_dataset(name, data=np.asarray(arr, dtype=float))
                d.attrs["unit"] = "W"
    return path


def read_run(path: str) -> dict:
    """Everything back as a plain dict — no lazy handles to leak."""
    h5py = _require_h5py()
    out: dict = {}
    with h5py.File(path, "r") as f:
        out["attrs"] = dict(f.attrs)
        out["metadata"] = dict(f["metadata"].attrs)
        out["rig_config"] = dict(f["config/rig"].attrs)
        out["run_config"] = dict(f["config/run"].attrs)
        # Missing in files written before the group existed.
        rs = f.get("config/resolved")
        out["resolved"] = dict(rs.attrs) if rs is not None else {}
        out["axis"] = dict(f["axis"].attrs)
        for k in ("values", "vpre", "vcoll", "delay_ns"):
            out[k] = f["axis"][k][()]
        for k in ("q", "mean", "std"):
            out["q_all" if k == "q" else f"q_{k}"] = f["charge"][k][()]
        for k in f["traces"]:
            out[k if k != "time" else "time_s"] = f["traces"][k][()]
        if "intensity" in f:
            for k in f["intensity"]:
                out[f"intensity_{k}"] = f["intensity"][k][()]
        if "diagnostics" in f:
            out["diagnostics"] = {k: f["diagnostics"][k][()] for k in f["diagnostics"]}
    return out
