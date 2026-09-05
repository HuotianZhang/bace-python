"""Envelopes, and the one place that decides what each event weighs on the wire.

Two consumers read every event, and they cannot afford the same payload. The
WebSocket feeds a browser drawing the live trace: it wants the *shape* of a
4000-point record, not the 4000 points, so `StepDone` goes out decimated to a
thousand samples (first and last kept, stride declared). The journal is a log
that is read back to answer "what was `n_loops` last time" and "how long does
a shot take": it wants the scalars and never a trace -- the recorders keep the
data. If both consumers took the dataclass as it came, a 100-loop scan would
write a gigabyte of JSON into a file whose only job is to answer those two
questions, and the browser would spend its time parsing samples it cannot
draw.

This is contract section 3, implemented once so nothing else decides sizes:

    event            WebSocket                           journal
    StepDone         light, dark, photo, photo_averaged  scalars only (index, loop,
                     decimated to <= 1000 points         step, setpoint, axis_value,
                                                         q, q_mean, q_std,
                                                         intensity_w, clipped) plus
                                                         the service's verdict
    StepPhase        in full, live only: seq null,       not written
                     not in the ring
    JVPoint          in full, live only, like StepPhase  not written
    RunFinished      photo_averaged omitted; values,     the same, minus q_all above
                     q_mean, q_std, q_all in full        10 000 cells
    JVCurveDone      in full                             metrics, label, n_points and
                                                         the other scalars; no arrays
    JVFinished       in full                             its curves reduced the same way
    everything else  in full                             in full

`JVFinished` is not in the contract's table; it carries every `JVCurveDone`
again, so the journal reduces the curves inside it exactly as it reduces the
curves themselves -- otherwise the arrays the table keeps out would come back
in through the summary event. `StepPhase` is per-shot noise (six or seven a
shot) that says where inside the shot the run is: a live card wants it, a
log and a replay do not, so it is *ephemeral* -- sent to the subscribers
that are there, with `seq` null like the drop notice, and never journaled or
kept in the ring. A client that dedupes on `seq` passes it through.

Both payloads are one frame, `{seq, ts, run_id, node_path, type, data,
decimated}`, so a journal reader and a WebSocket client parse the same
shape; `decimated` names every array that was thinned or dropped, so neither
can mistake a shortened array for a short record.

The per-shot verdict is the service's judgement about a shot and is
attached to the wire `data` here rather than added to the dataclass: the
recorder's files must not change because the console learned to say "0 rail
samples". The rule is the design's, corrected 2026-09-02 (the design
pack 03-states section C, docs/ui-rules.md): only a run of identical samples *inside one trace* is
evidence of the digitiser's rail, and an extreme repeated more than a few
times is a window that is probably too small. Light and dark sharing an
extreme is reported as a fact and judged as nothing -- the displacement
spike dominates both traces, so good data shares one.
"""
from __future__ import annotations

import dataclasses
import math
import time
from typing import Any, Mapping

import numpy as np

from ..bench.checks import _rail_samples
from ..experiment.events import Envelope, Event, RunFinished, StepDone, StepPhase
from ..experiment.jv import JVCurveDone, JVFinished, JVPoint
from ..experiment.wire import envelope_to_wire, to_wire

EPHEMERAL: tuple[type, ...] = (StepPhase, JVPoint)
"""Events sent live and never journaled, ringed or numbered."""

RAIL_RUN_SAMPLES = 7
"""Consecutive samples on one extreme that make a rail: the design's "7-8
consecutive samples (an ~11 ns flat top)" at 0.5 ns per sample. Report 033251
had about seventy."""

RAIL_REPEAT_SAMPLES = 8
"""An extreme repeated more than this many times anywhere in a trace: the
window is probably too small even though the auto-range did not say so.
Suspicious, not fatal."""

WS_MAX_POINTS = 1000
"""Samples per trace on the WebSocket. A browser draws a 1000-point line at
the same width as a 4000-point one; the stride is declared in `decimated` so
the time axis still comes out right."""

JOURNAL_MAX_Q_ALL_CELLS = 10_000
"""Above this, `RunFinished.q_all` (loops x steps) is dropped from the
journal. A 100-loop, 100-step run is the boundary: ten thousand numbers on
one line is still a line a person can open; the HDF5 holds the rest."""

STEPDONE_SCALARS: tuple[str, ...] = ("index", "loop", "step", "setpoint", "axis_value",
                                     "q", "q_mean", "q_std", "intensity_w", "clipped",
                                     "t0_int_record_s", "t1_int_record_s")
STEPDONE_ARRAYS: tuple[str, ...] = ("light", "dark", "photo", "photo_averaged",
                                    "sync_light", "sync_dark")
JVCURVE_SCALARS: tuple[str, ...] = ("index", "label", "dark", "led_level_v", "direction",
                                    "intensity_w")
JVCURVE_ARRAYS: tuple[str, ...] = ("voltage", "current", "density")
SPIKE_LAG_NS = 0.25
"""Above this light-to-dark lag of the displacement spike the shot is
judged jittered. Good shots on the rig align to 0.01-0.07 ns; the six bad
ones of 2026-09-05 sat 0.33-1.15 ns apart (`core.diagnostics`)."""

VERDICT_KEYS: tuple[str, ...] = ("rail_light", "rail_dark", "rail_run_light", "rail_run_dark",
                                 "shared_extreme", "peak_light_a", "peak_dark_a",
                                 "spike_lag_ns", "edge_light_ns", "edge_dark_ns",
                                 "averages_light", "averages_dark",
                                 "sync_edge_light_ns", "sync_edge_dark_ns", "level",
                                 "text")
"""The keys every verdict carries. A diagnostic may add to them, never
replace one: a `shot_diagnostics()` that happened to return `level` would
otherwise silently overrule the saturation check. `peak_*_a` is the signed
extreme of each full trace, computed before decimation: the card's
"peak · light -9.58 mA" cannot be read off a 5:1 thinned trace, whose kept
samples can straddle the true extreme."""


def is_ephemeral(event: Event) -> bool:
    """Sent live, never journaled, ringed or given a `seq` (`StepPhase`,
    `JVPoint`)."""
    return isinstance(event, EPHEMERAL)


# -- envelopes ------------------------------------------------------------
def make_envelope(seq: int, run_id: str | None, node_path: str, event: Event,
                  ts: float | None = None) -> Envelope:
    """Wrap one event for the wire. `ts` defaults to now, in unix seconds.

    Refuses anything that is not an `Event`: a dict or a string would pass
    through `envelope_to_wire` as itself and reach the browser with
    `type: "dict"`, which no client dispatches on. `RunQueued` is an `Event`
    like the rest (`experiment.events`), so the session wraps it here too.
    """
    if not isinstance(event, Event):
        raise TypeError(
            f"an Envelope wraps an Event, not {type(event).__name__}; every "
            "frame on the wire, RunQueued included, is a dataclass from experiment.events")
    if run_id is not None and not isinstance(run_id, str):
        raise TypeError(f"run_id must be a str or None, not {type(run_id).__name__}")
    return Envelope(seq=int(seq), ts=time.time() if ts is None else float(ts),
                    run_id=run_id, node_path=str(node_path), event=event)


# -- the payload policy ---------------------------------------------------
def ws_payload(env: Envelope) -> dict:
    """The frame a WebSocket client receives for `env`, per the table above."""
    ev = env.event
    if isinstance(ev, StepDone):
        return envelope_to_wire(env, max_points=WS_MAX_POINTS)
    if isinstance(ev, RunFinished):
        data, info = _run_finished(ev)
        return _frame(env, data, info)
    return envelope_to_wire(env)


def journal_payload(env: Envelope, *, verdict: Mapping[str, Any] | None = None) -> dict | None:
    """The line the journal writes for `env`, per the table above; None for
    an ephemeral event, which is not written at all.

    A `StepDone` line always carries a `verdict`. Pass the one that was
    attached to the WebSocket payload (with the bench's diagnostics) so the
    live card and the session log say the same thing about the shot; when
    none is given the saturation check is run here, without diagnostics,
    rather than leaving the line with no judgement at all.
    """
    ev = env.event
    if is_ephemeral(ev):
        return None
    if isinstance(ev, StepDone):
        data = {name: _plain(getattr(ev, name)) for name in STEPDONE_SCALARS}
        data["verdict"] = (dict(verdict) if verdict is not None
                           else shot_verdict(ev.light.y, ev.dark.y, dt=ev.light.dt,
                                             light=ev.light, dark=ev.dark,
                                             sync_light=ev.sync_light, sync_dark=ev.sync_dark))
        return _frame(env, data, {name: {"omitted": True} for name in STEPDONE_ARRAYS})
    if isinstance(ev, RunFinished):
        data, info = _run_finished(ev)
        cells = int(np.asarray(ev.q_all).size)
        if cells > JOURNAL_MAX_Q_ALL_CELLS:
            data["q_all"] = None
            info["q_all"] = {"omitted": True, "cells": cells}
        return _frame(env, data, info)
    if isinstance(ev, JVCurveDone):
        return _frame(env, _curve_scalars(ev),
                      {name: {"omitted": True} for name in JVCURVE_ARRAYS})
    if isinstance(ev, JVFinished):
        data = {"curves": [_curve_scalars(c) for c in ev.curves],
                "elapsed_s": _plain(ev.elapsed_s), "n_curves": len(ev.curves)}
        return _frame(env, data,
                      {f"curves.{name}": {"omitted": True} for name in JVCURVE_ARRAYS})
    return envelope_to_wire(env)


def payloads(env: Envelope, *,
             diagnostics: Mapping[str, Any] | None = None) -> tuple[dict, dict | None]:
    """`(websocket, journal)` for one envelope, the verdict computed once;
    the journal payload is None for an ephemeral event.

    The call the session's fan-out task wants: one per envelope, and a
    `StepDone` gets the identical `verdict` object in both payloads, so the
    live card and the session log cannot disagree about a shot. `diagnostics`
    is what `rigs.Bench.shot_diagnostics()` returned after the light
    acquisition -- the autorange pass count, when the driver has it.
    """
    ws = ws_payload(env)
    verdict = None
    ev = env.event
    if isinstance(ev, StepDone):
        verdict = attach_verdict(ws["data"], ev, diagnostics)
    return ws, journal_payload(env, verdict=verdict)


def ephemeral_frame(run_id: str | None, node_path: str, event: Event,
                    ts: float | None = None) -> dict:
    """The WebSocket frame of an ephemeral event: the full payload with
    `seq` null, the same shape as the drop notice, so a client that dedupes
    on `seq` lets it through and a replay never contains it."""
    frame = ws_payload(make_envelope(0, run_id, node_path, event, ts))
    frame["seq"] = None
    return frame


# -- the per-shot verdict -------------------------------------------------
def _rail_run(a: np.ndarray) -> int:
    """The longest run of consecutive samples sitting on the trace's own
    minimum or maximum. A digitiser that ran out of window returns the same
    code for every sample past it, so a rail is a flat top; an averaged
    analogue trace essentially never holds one code for seven samples."""
    if a.size == 0:
        return 0
    best = 0
    for extreme in (a.min(), a.max()):
        on = np.concatenate(([False], a == extreme, [False]))
        edges = np.flatnonzero(np.diff(on.astype(np.int8)))
        if edges.size:
            best = max(best, int((edges[1::2] - edges[::2]).max()))
    return best


def _peak(a: np.ndarray) -> float | None:
    if a.size == 0:
        return None
    return float(a[int(np.argmax(np.abs(a)))])


def shot_verdict(light_y, dark_y,
                 diagnostics: Mapping[str, Any] | None = None, *,
                 dt: float | None = None,
                 light: Any = None, dark: Any = None,
                 sync_light: Any = None, sync_dark: Any = None) -> dict[str, Any]:
    """`{rail_light, rail_dark, rail_run_light, rail_run_dark, shared_extreme,
    peak_light_a, peak_dark_a, spike_lag_ns, edge_light_ns, edge_dark_ns,
    averages_light, averages_dark, sync_edge_light_ns, sync_edge_dark_ns,
    level, text, ...diagnostics}`.

    Three rules. The third is the shot's *alignment* (2026-09-05,
    `core.diagnostics`): the light and dark displacement spikes must sit on
    top of each other, because `light - dark` is what the photocurrent is.
    On six shots of the first hardware day they were 0.3-1.2 ns apart -- the
    trigger had jittered for the length of the shot -- and the 50 mA spike's
    residual was reported as a photocurrent ten times the real one, with the
    charge's sign flipped, while every rail check passed. `spike_lag_ns`
    beyond `SPIKE_LAG_NS` is `warn`: "Q of this shot is not a charge". The
    edge times and the sync edges are reported beside it so the two sides of
    the trigger chain can be told apart; the average counts say whether the
    digitiser folded what was asked. All need `dt` (and the `Trace`s for the
    counts and the syncs); called with bare arrays they are None and the
    judgement is the two rail rules alone.

    The two rail rules, both about a single trace (03-states section C, corrected):

    * a run of `RAIL_RUN_SAMPLES` or more consecutive samples on one extreme
      is the digitiser's rail -- `warn`, and the text says the charge is
      meaningless;
    * an extreme repeated more than `RAIL_REPEAT_SAMPLES` times is a window
      that is probably too small -- `warn`, suspicious rather than fatal.

    `shared_extreme` is reported and judged as nothing: the displacement
    spike dominates both traces, so light and dark *should* share one, and a
    rule that warned on it fired on good data. `text` is the warning, or the
    one-line summary the live card shows ("autorange pass 2 · shared extreme
    · 1 rail sample"). Diagnostics are merged in flat, so a client reads
    `verdict.autorange_passes` beside `verdict.rail_light`; a diagnostic that
    would overwrite one of the fixed keys is refused rather than silently
    winning.
    """
    light_arr = np.asarray(light_y, dtype=float)
    dark_arr = np.asarray(dark_y, dtype=float)
    diag = dict(diagnostics or {})
    align = _alignment(light_arr, dark_arr, dt, light, dark, sync_light, sync_dark)
    if light_arr.size == 0 or dark_arr.size == 0:
        verdict: dict[str, Any] = {
            "rail_light": 0, "rail_dark": 0, "rail_run_light": 0, "rail_run_dark": 0,
            "shared_extreme": False, "peak_light_a": _peak(light_arr), "peak_dark_a": _peak(dark_arr),
            **align,
            "level": "warn",
            "text": "an empty trace: the digitiser returned no samples, so there "
                    "is nothing to judge and nothing to integrate"}
    else:
        light, dark = light_arr, dark_arr
        rail_light, rail_dark = _rail_samples(light), _rail_samples(dark)
        run_light, run_dark = _rail_run(light), _rail_run(dark)
        shared = bool(light.min() == dark.min() or light.max() == dark.max())
        worst_run, worst = max(run_light, run_dark), max(rail_light, rail_dark)
        if worst_run >= RAIL_RUN_SAMPLES:
            which = ("light" if run_light >= run_dark else "dark")
            text = (f"the {which} trace holds one extreme value for {worst_run} consecutive "
                    "samples: that is the digitiser's rail, not the signal, so the charge "
                    "of this shot is meaningless. Widen the range or reduce the swing")
            level = "warn"
        elif worst > RAIL_REPEAT_SAMPLES:
            text = (f"an extreme value repeats {worst} times -- a real averaged trace "
                    "does not sit on one value, so the window is probably too small "
                    "even though the auto-range did not say so")
            level = "warn"
        elif align["spike_lag_ns"] is not None and abs(align["spike_lag_ns"]) > SPIKE_LAG_NS:
            lag = align["spike_lag_ns"]
            edges = ", ".join(f"{e:.1f}" for e in (align["edge_light_ns"], align["edge_dark_ns"])
                              if e is not None)
            text = (f"the light and dark displacement spikes are {abs(lag):.2f} ns apart "
                    f"(spike edges {edges} ns): the trigger jittered during this shot, "
                    "so their difference leaves the spike in the photocurrent and Q "
                    "of this shot is not a charge. " + _sync_side(align))
            level = "warn"
        else:
            level, text = "ok", _ok_text(rail_light, rail_dark, shared, diag, align)
        verdict = {"rail_light": int(rail_light), "rail_dark": int(rail_dark),
                   "rail_run_light": int(run_light), "rail_run_dark": int(run_dark),
                   "shared_extreme": shared,
                   "peak_light_a": _peak(light), "peak_dark_a": _peak(dark),
                   **align,
                   "level": level, "text": text}
    for key, value in diag.items():
        if key in VERDICT_KEYS:
            raise ValueError(
                f"diagnostic {key!r} collides with the verdict's own field; the "
                "saturation check must not be overruled by a driver readback")
        verdict[str(key)] = _plain(value)
    return verdict


def attach_verdict(payload_data: dict, ev: StepDone,
                   diagnostics: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compute `shot_verdict` for the shot and store it as
    `payload_data["verdict"]`. Returns the verdict so the caller can put the
    same object elsewhere."""
    if not isinstance(payload_data, dict):
        raise TypeError("attach_verdict wants the payload's `data` dict, not "
                        f"{type(payload_data).__name__}")
    verdict = shot_verdict(ev.light.y, ev.dark.y, diagnostics, dt=ev.light.dt,
                           light=ev.light, dark=ev.dark,
                           sync_light=ev.sync_light, sync_dark=ev.sync_dark)
    payload_data["verdict"] = verdict
    return verdict


def _alignment(light: np.ndarray, dark: np.ndarray, dt: float | None,
               light_tr: Any, dark_tr: Any, sync_light: Any, sync_dark: Any) -> dict[str, Any]:
    """The alignment and completeness numbers (`core.diagnostics`), None
    where the inputs cannot give them."""
    from ..core.diagnostics import edge_10_90_ns, spike_lag_ns, sync_edge_ns
    out: dict[str, Any] = {"spike_lag_ns": None, "edge_light_ns": None, "edge_dark_ns": None,
                           "averages_light": None, "averages_dark": None,
                           "sync_edge_light_ns": None, "sync_edge_dark_ns": None}
    if dt and light.size and dark.size:
        out["spike_lag_ns"] = spike_lag_ns(light, dark, dt)
        out["edge_light_ns"] = edge_10_90_ns(light, dt)
        out["edge_dark_ns"] = edge_10_90_ns(dark, dt)
    for key, tr in (("averages_light", light_tr), ("averages_dark", dark_tr)):
        count = getattr(tr, "count", None)
        out[key] = None if count is None else int(count)
    for key, tr in (("sync_edge_light_ns", sync_light), ("sync_edge_dark_ns", sync_dark)):
        y = getattr(tr, "y", None)
        if y is not None and getattr(tr, "dt", None):
            out[key] = sync_edge_ns(np.asarray(y, dtype=float), float(tr.dt),
                                    float(getattr(tr, "t0", 0.0)))
    return {k: (None if v is None else (float(v) if isinstance(v, float) else v))
            for k, v in out.items()}


def _sync_side(align: Mapping[str, Any]) -> str:
    """Which side of the trigger chain the sync edges point at."""
    edges = [align.get("sync_edge_light_ns"), align.get("sync_edge_dark_ns")]
    if all(e is None for e in edges):
        return "No sync trace was fetched, so which side jitters cannot be said."
    text = "/".join("?" if e is None else f"{e:.1f}" for e in edges)
    return (f"The sync edges are {text} ns: if those are as sharp as on good shots, the "
            "jitter is between the sync and the 81150A's pulse; if they are smeared "
            "too, it is between the sync and the scope's trigger.")


def _ok_text(rail_light: int, rail_dark: int, shared: bool, diag: Mapping[str, Any],
             align: Mapping[str, Any] | None = None) -> str:
    parts = []
    passes = diag.get("autorange_passes")
    if passes is not None:
        parts.append(f"autorange pass {passes}")
    parts.append("shared extreme" if shared else "no shared extreme")
    worst = max(int(rail_light), int(rail_dark))
    parts.append(f"{worst} rail sample" + ("" if worst == 1 else "s"))
    if align:
        lag = align.get("spike_lag_ns")
        if lag is not None:
            parts.append(f"spikes {abs(lag):.2f} ns apart")
        edge = align.get("edge_light_ns")
        if edge is not None:
            parts.append(f"edge {edge:.1f} ns")
        n_l, n_d = align.get("averages_light"), align.get("averages_dark")
        if n_l is not None and n_d is not None:
            parts.append(f"{n_l} avg" if n_l == n_d else f"{n_l}/{n_d} avg")
        s_l = align.get("sync_edge_light_ns")
        if s_l is not None:
            parts.append(f"sync edge {s_l:.1f} ns")
    return " · ".join(parts)


# -- pieces ---------------------------------------------------------------
def _frame(env: Envelope, data: Any, info: dict) -> dict:
    """The same seven keys `experiment.wire.envelope_to_wire` produces, for
    a payload whose `data` was built here rather than by reflection. Kept
    to one shape on purpose; `test_service_wire` asserts the two agree."""
    return {"seq": int(env.seq), "ts": float(env.ts), "run_id": env.run_id,
            "node_path": env.node_path, "type": type(env.event).__name__,
            "data": data, "decimated": info}


def _run_finished(ev: RunFinished) -> tuple[dict, dict]:
    """`RunFinished` with `photo_averaged` (steps x samples) dropped and
    every 1-D array whole. The matrix is replaced before conversion rather
    than converted and discarded: 51 x 4000 floats to a Python list is real
    work for nothing."""
    shape = list(np.asarray(ev.photo_averaged).shape)
    slim = dataclasses.replace(ev, photo_averaged=np.empty((0, 0)))
    data, info = to_wire(slim)
    data["photo_averaged"] = None
    info["photo_averaged"] = {"omitted": True, "shape": shape}
    return data, info


def _curve_scalars(c: JVCurveDone) -> dict:
    data = {name: _plain(getattr(c, name)) for name in JVCURVE_SCALARS}
    data["metrics"] = _plain(c.metrics)
    data["n_points"] = int(np.asarray(c.voltage).size)
    return data


def _plain(value: Any) -> Any:
    """A JSON-safe copy: arrays to lists, numpy scalars to Python ones, NaN
    and infinity to None, dataclasses to dicts. Anything else is refused --
    a journal line that `json.dumps` cannot write must fail here, with the
    type named, not inside the journal's lock."""
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        f = float(value)
        return f if math.isfinite(f) else None
    if value is None or isinstance(value, str):
        return value
    raise TypeError(f"{type(value).__name__} has no JSON form on the wire")
