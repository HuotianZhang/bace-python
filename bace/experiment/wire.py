"""Events on the wire: dataclasses to JSON-able dicts, with honest decimation.

The service fans the event stream out to a browser, and a browser cannot take
a 4000-point numpy array, a NaN, or a frozen dataclass. This is the one place
that conversion happens, and it is reflection over `dataclasses.fields` rather
than one hand-written case per event: a hand-written serialiser drifts from
the dataclass it copies the first time someone adds a field, and adding an
event to `events.py` should need nothing here.

Rules, in the order they are likely to surprise:

* numpy arrays become lists of Python numbers; NaN and infinity become `null`.
  JSON has no spelling for them, and `json.dumps` would otherwise emit `NaN`,
  a token no browser's parser accepts.
* `Trace` becomes `{"y", "dt", "t0", "n", "count"}`. `n` is the **full** record length
  even when `y` has been decimated, so the UI can still put the samples on
  their time axis: kept sample `i` of `len(y)` sits at `t0 + i * stride * dt`
  for every `i` but the last, and the last sits at `t0 + (n - 1) * dt` -- it
  is the record's final sample, appended whenever `n - 1` is not a multiple
  of the stride (with n = 4000 and stride 5 the kept indices are 0, 5, ...,
  3995, 3999). A client that placed it one stride after 3995 would draw the
  tail 4 samples early.
* nested dataclasses become dicts, tuples become lists, numpy scalars become
  Python scalars, dict keys become strings.
* With `max_points` set, a 1-D numeric array longer than that is
  stride-decimated: keep the first sample, every k-th after it, and always
  the last. The second return value records `{"<dotted.path>": {"n_full": N,
  "stride": k}}` for each array it touched, and the array stays a plain list,
  so the shape the UI sees is the same in a decimated frame and a full one.
  2-D arrays are never decimated: `RunFinished.photo_averaged` (steps x
  samples) is dropped to `null` and recorded as `{"omitted": true}` -- the
  service serves it from a data endpoint -- while `q_all` (loops x steps) is
  small and goes whole.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np

from ..drivers.protocols import Trace
from .events import Envelope, Event, RunFinished

# Fields that are replaced by `null` (and reported as omitted) whenever a
# consumer asks for decimation at all. Named per class rather than by a size
# rule so a small run with many steps cannot silently lose `q_all`.
_OMIT_WHEN_DECIMATING: dict[type, tuple[str, ...]] = {
    RunFinished: ("photo_averaged",),
}


def _stride(n: int, max_points: int) -> int:
    """The smallest k for which first + every k-th + last fits in `max_points`.

    `range(0, n, k)` has `(n - 1) // k + 1` elements; with
    `k = ceil((n - 1) / (max_points - 1))` that is at most `max_points`, and
    appending the last sample when it was not already hit adds one only when
    the range fell short by at least one -- so the result never exceeds
    `max_points`.
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
    """`tolist()` output with every non-finite float turned into None."""
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
        # Special-cased for `n`: the record length survives decimation, so a
        # consumer can tell a short record from a decimated one.
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
    """`(data, decimation info)` for one event. `data` survives `json.dumps`.

    `info` is `{}` when nothing was decimated or omitted, so a consumer can
    test it for truth. `max_points` must be at least 2: one point cannot keep
    both the first and the last sample.
    """
    if max_points is not None and max_points < 2:
        raise ValueError(
            f"max_points must be at least 2, not {max_points}: decimation keeps "
            "the first and the last sample, and one slot cannot hold both")
    info: dict[str, dict] = {}
    data = _convert(event, "", max_points, info)
    return data, info


def envelope_to_wire(env: Envelope, *, max_points: int | None = None) -> dict:
    """The frame a WebSocket client receives: envelope fields flattened,
    `type` is the event's class name (what a client dispatches on), `data` the
    event itself, `decimated` the info from `to_wire` (`{}` when full)."""
    data, info = to_wire(env.event, max_points=max_points)
    return {"seq": int(env.seq), "ts": float(env.ts), "run_id": env.run_id,
            "node_path": env.node_path, "type": type(env.event).__name__,
            "data": data, "decimated": info}
