"""The primary HDF5 store keeps full-precision data and configuration per run.

The `.dat` export has six significant figures and lacks instrument settings.
HDF5 retains arrays, requested settings and read-back so the run can be
reconstructed from its file.

Layout (`n_steps` counts points)::

    /                       attrs: schema, software, loops_planned,
                            loops_completed, complete
    /metadata               attrs: sample, material, pixel, temperature_k,
                            temperature_how, temperature_source, ...
    /config/rig             attrs: R_s, bias amplifier gain, channels, ...
    /config/run             attrs: N_avg, timebase, window, settle times, ...
    /config/resolved        attrs: instrument read-back, including settings
                            the recipe leaves unchanged; absent in schema 1
    /axis                   attrs: name, unit, start, stop, step, centre_on_voc,
                            voc
      values      (n_steps,)          axis values in the axis unit
      vpre        (n_steps,)          V_pre in V at every point
      vcoll       (n_steps,)          V_coll in V at every point
      delay_ns    (n_steps,)          delay in ns at every point
    /charge
      q           (n_loops, n_steps)  Q in C per shot; NaN if unmeasured
      mean        (n_steps,)          mean Q in C, ignoring NaN
      std         (n_steps,)          sample standard deviation in C, ddof=1
    /traces
      time        (n_samples,)        s from the first sample of the record
      trace_t0    (n_steps,)          :WAV:XOR? in s relative to the trigger;
                                     negative when the trigger is in the record
      window      (n_steps, 2)        integration limits in record time, s;
                                     tied to field arrival along a delay axis
      light       (n_steps, n_samples)   light traces averaged over loops, A
      dark        (n_steps, n_samples)   dark traces averaged over loops, A
      photocurrent(n_steps, n_samples)   baseline-corrected light minus dark, A
      shots       (n_loops, n_steps, n_samples)   A; only if store_shots=True
    /intensity
      mean, std   (n_steps,)          W; available with power measurements

Setpoints include constant conditions so readers need not infer them from
the axis name. Temperature method and source can be absent and must not be
inferred from the temperature value alone.

Partial runs retain NaN for unmeasured shots. `loops_completed` counts full
loops; consumers must read it rather than assume full arrays. The LabVIEW
compatibility case in `tests/test_storage.py` contains 20 of 100 loops.

`shots` is off by default: 51 points × 100 loops × 4000 samples require about
163 MB before compression. Storing each shot is an explicit disk-space choice.
"""
from __future__ import annotations

from typing import Any

import numpy as np

SCHEMA = "bace-run/3"
"""The run schema identifier defines optional timing and read-back fields.

Schema 3 contains `/traces/trace_t0` and `/traces/window`. Schema 2 contains
`/config/resolved`. Dataset paths are shared with schema 1; `read_run`
returns an empty dictionary when the resolved group is absent.
"""
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
              monitor_light: np.ndarray | None = None,
              monitor_dark: np.ndarray | None = None,
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
        # A recipe that leaves polarity unchanged cannot establish its value.
        # Separate read-back records which convention produces the stored data.
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
        # The trigger channel shares the current record and loop averaging.
        # Its edge diagnoses timing. Units are V because it measures Sync,
        # rather than the voltage across R_s converted to A.
        for name, arr in (("sync_light", sync_light), ("sync_dark", sync_dark)):
            if arr is None:
                continue
            d = tr.create_dataset(name, data=np.asarray(arr, dtype=float), **COMPRESSION)
            d.attrs["unit"] = "V"
            d.attrs["note"] = "the trigger channel, same record as the current trace"
        for name, arr in (("monitor_light", monitor_light), ("monitor_dark", monitor_dark)):
            if arr is None:
                continue
            d = tr.create_dataset(name, data=np.asarray(arr, dtype=float), **COMPRESSION)
            d.attrs["unit"] = "V"
            d.attrs["note"] = "the monitor channel, same record as the current trace"
        if diagnostics:
            # Per-shot diagnostics preserve timing and N_avg checks that averaging
            # traces can hide: `:WAV:COUN?` counts, spike lag and spike and sync
            # edge widths from `core.diagnostics`.
            dg = f.create_group("diagnostics")
            for name, arr in diagnostics.items():
                d = dg.create_dataset(name, data=np.asarray(arr, dtype=float))
                d.attrs["unit"] = ("ns" if name.endswith("_ns") else
                                   "uA" if name.endswith("_ua") else
                                   "1 = dark trace first" if name == "dark_first" else
                                   "acquisitions")
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
    """Returns the run as an in-memory dictionary with no open HDF5 handles."""
    h5py = _require_h5py()
    out: dict = {}
    with h5py.File(path, "r") as f:
        out["attrs"] = dict(f.attrs)
        out["metadata"] = dict(f["metadata"].attrs)
        out["rig_config"] = dict(f["config/rig"].attrs)
        out["run_config"] = dict(f["config/run"].attrs)
        # Schema 1 permits an absent resolved group.
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
