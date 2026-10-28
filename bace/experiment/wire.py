"""Wire conversion serialises event dataclasses and records array decimation.

The browser needs JSON-compatible values. Reflection over `dataclasses.fields`
keeps serialisation aligned with event fields without a separate case for
each event type.

NumPy arrays become lists, NumPy scalars become Python scalars, nested
dataclasses become dictionaries, tuples become lists and dictionary keys
become strings. Non-finite floats become `null`: JSON does not support NaN
or infinity, although `json.dumps` can emit nonstandard tokens for them.

A `Trace` carries `y`, `dt`, `t0`, `n` and `count`. `n` is the full record
length even after decimation. Kept sample `i` lies at
`t0 + i * stride * dt`, except that the final kept sample always lies at
`t0 + (n - 1) * dt`. For `n = 4000` and stride 5, indices are 0, 5, ..., 3995,
3999. Placing the last sample a full stride after 3995 misplaces the tail by
four samples.

With `max_points`, long one-dimensional numeric arrays keep the first sample,
every stride-th sample and the last. Metadata records `n_full` and `stride`
under each dotted field path; the array remains a plain list in both full
and decimated frames. Two-dimensional arrays are not decimated.
`RunFinished.photo_averaged`, with shape (points, samples), becomes `null`
and metadata marks it `omitted`; the service provides it from a data endpoint.
`RunFinished.q_all`, with shape (loops, points), remains complete.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np

from ..drivers.protocols import Trace
from .events import Envelope, Event, RunFinished

# Explicit class fields become `null` with omission metadata whenever
# `max_points` requests decimation. A size rule could discard `q_all` for a
# run with many points, so omission is declared per event class.
_OMIT_WHEN_DECIMATING: dict[type, tuple[str, ...]] = {
    RunFinished: ("photo_averaged",),
}


def _stride(n: int, max_points: int) -> int:
    """The stride is the smallest spacing that fits both endpoints in `max_points`.

    `range(0, n, k)` has `(n - 1) // k + 1` samples. With
    `k = ceil((n - 1) / (max_points - 1))`, it has at most `max_points` samples.
    Appending the final sample adds one only if the range has room for it.
    """
    return max(1, math.ceil((n - 1) / (max_points - 1)))


def _decimate(arr: np.ndarray, max_points: int) -> tuple[np.ndarray, int]:
    n = int(arr.shape[0])
    k = _stride(n, max_points)
    idx = list(range(0, n, k))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return arr[idx], k


def _finite(value: Any) -> Any:
    """The finite-value conversion replaces non-finite floats with `None` in lists."""
    if isinstance(value, list):
        return [_finite(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _scalar(value: Any) -> Any:
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        f = float(value)
        return f if math.isfinite(f) else None
    return value


def _join(path: str, name: Any) -> str:
    return f"{path}.{name}" if path else str(name)


def _convert(value: Any, path: str, max_points: int | None,
             info: dict[str, dict]) -> Any:
    if isinstance(value, Trace):
        # `n` preserves the full record length so a consumer can distinguish
        # a short record from a decimated one.
        return {"y": _convert(value.y, _join(path, "y"), max_points, info),
                "dt": _scalar(value.dt), "t0": _scalar(value.t0),
                "n": int(value.n),
                "count": None if value.count is None else int(value.count)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        omit = _OMIT_WHEN_DECIMATING.get(type(value), ()) if max_points else ()
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            p = _join(path, f.name)
            if f.name in omit and isinstance(v, np.ndarray) and v.ndim >= 2:
                info[p] = {"omitted": True}
                out[f.name] = None
                continue
            out[f.name] = _convert(v, p, max_points, info)
        return out
    if isinstance(value, np.ndarray):
        numeric = value.dtype.kind in "fiub"
        if (max_points is not None and value.ndim == 1 and numeric
                and value.size > max_points):
            kept, k = _decimate(value, max_points)
            info[path] = {"n_full": int(value.size), "stride": int(k)}
            return _finite(kept.tolist())
        return _finite(value.tolist())
    if isinstance(value, dict):
        return {str(k): _convert(v, _join(path, k), max_points, info)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_convert(v, _join(path, i), max_points, info)
                for i, v in enumerate(value)]
    return _scalar(value)


def to_wire(event: Event, *, max_points: int | None = None) -> tuple[dict, dict]:
    """The wire conversion returns JSON-compatible event data and decimation metadata.

    `info` is empty when no array is decimated or omitted. `max_points` must be
    at least two because both endpoints must survive.
    """
    if max_points is not None and max_points < 2:
        raise ValueError(
            f"max_points must be at least 2, not {max_points}: decimation keeps "
            "the first and the last sample, and one slot cannot hold both")
    info: dict[str, dict] = {}
    data = _convert(event, "", max_points, info)
    return data, info


def envelope_to_wire(env: Envelope, *, max_points: int | None = None) -> dict:
    """The envelope conversion returns the WebSocket frame with flattened metadata.

    `type` is the event class name for dispatch. `data` contains the event and
    `decimated` contains `to_wire` metadata, empty for a full frame.
    """
    data, info = to_wire(env.event, max_points=max_points)
    return {"seq": int(env.seq), "ts": float(env.ts), "run_id": env.run_id,
            "node_path": env.node_path, "type": type(env.event).__name__,
            "data": data, "decimated": info}
