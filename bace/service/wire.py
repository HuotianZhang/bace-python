"""Builds envelopes and applies WebSocket and journal payload limits.

The browser needs trace shape, while the journal needs scalars for parameter
and timing queries. `StepDone` traces are decimated to at most 1000 samples
on the WebSocket, retaining the first and last samples and declaring the
stride. The journal omits traces; recorders retain full data. This avoids
large JSON files and browser parsing work without losing recorded data
(docs/service-contract.md section 3).

    Event            WebSocket                         Journal
    `StepDone`       Decimated traces and verdict      Scalars and verdict
    `StepPhase`      Full, live only, `seq` null        Omitted
    `JVPoint`        Full, live only, `seq` null        Omitted
    `RunFinished`    No `photo_averaged`; 1-D arrays    Same; `q_all` omitted
                     retained                          above 10 000 cells
    `JVCurveDone`    Full                              Metrics and scalars
    `JVFinished`     Full                              Reduced curves
    Other events     Full                              Full

`JVFinished` curves use the same reduction as `JVCurveDone`, so the summary
cannot reintroduce arrays. `StepPhase` and `JVPoint` describe progress within
a shot or curve. They are sent only to current subscribers, with `seq` null,
and enter neither journal nor replay ring. Clients deduplicating by `seq`
must pass these unnumbered frames through.

Both payloads use `{seq, ts, run_id, node_path, type, data, decimated}`.
`decimated` names every shortened or omitted array, so a reader cannot
mistake a reduced trace for a short acquisition.

The per-shot verdict belongs to wire `data`, preserving the recorder's
file format. A rail requires repeated extreme samples within one trace.
Light and dark traces can share an extreme because the displacement spike
dominates both; that fact alone does not indicate clipping.
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
"""Identifies live events excluded from the journal, ring and numbering."""

RAIL_RUN_SAMPLES = 7
"""Defines a rail as seven consecutive samples at one trace extreme.
An averaged analogue trace does not normally retain one code this long.
The bench clipping criterion is 7-8 samples at a 0.5 ns sample interval
(docs/notes.md). Clipped traces measured on this bench can repeat an
extreme for about 70 samples, well above the criterion.
"""

RAIL_REPEAT_SAMPLES = 8
"""Flags an extreme repeated more than eight times within one trace.
This suggests insufficient digitiser range even without an auto-range
warning. It is suspicious rather than fatal (manual 4.2).
"""

WS_MAX_POINTS = 1000
"""Limits samples per WebSocket trace to the browser's drawing needs.
A 1000-sample line occupies the same width as a 4000-sample line.
`decimated` declares the stride to preserve the time axis.
"""

JOURNAL_MAX_Q_ALL_CELLS = 10_000
"""Limits journal `RunFinished.q_all` to 10 000 loop-by-point cells.
This keeps a 100-loop, 100-point result readable on one JSON line.
The HDF5 retains larger arrays (docs/service-contract.md section 3).
"""

STEPDONE_SCALARS: tuple[str, ...] = ("index", "loop", "step", "setpoint", "axis_value",
                                     "q", "q_mean", "q_std", "intensity_w", "clipped",
                                     "t0_int_record_s", "t1_int_record_s", "dark_first")
STEPDONE_ARRAYS: tuple[str, ...] = ("light", "dark", "photo", "photo_averaged",
                                    "sync_light", "sync_dark",
                                    "monitor_light", "monitor_dark")
JVCURVE_SCALARS: tuple[str, ...] = ("index", "label", "dark", "led_level_v", "direction",
                                    "intensity_w")
JVCURVE_ARRAYS: tuple[str, ...] = ("voltage", "current", "density")
SPIKE_LAG_NS = 0.25
"""Defines the spike-lag threshold that requires an edge check, in ns.
Good shots align to 0.01-0.07 ns; six jittered shots show 0.33-1.15 ns,
measured on this bench. The 0.25 ns threshold separates those ranges.

Lag alone does not establish jitter. Extracted charge added to the
displacement spike moves the correlation peak without moving the spike.
A synthetic trace with an unchanged no-light spike plus measured
photocurrent reproduces the lag to 0.01 ns over 220-290 K. Shifting the
dark trace cancels only about 4 % of the difference. The shapes differ,
rather than merely their timing
(docs/analysis/2026-09-06/spike-lag-and-dark-charge.md sections 1-2).
Re-integration after that shift changes Q by about 1 %
(docs/service-contract.md section 3).

Jitter convolves the spike with trigger scatter and widens its leading
edge. A warning therefore requires this lag and an edge above
`SPIKE_EDGE_NS`.
"""

SYNC_LAG_NS = 0.1
"""Defines a 0.1 ns limit for lag between the two sync traces.
The engine subtracts sample for sample. A real timing offset leaves part
of the 50 mA displacement spike in the difference, invalidating Q
regardless of spike correlation (docs/service-contract.md section 3).

Measured on this bench over 240 shots at 220-295 K: sync lag stays within
±0.004 ns while spike lag spans 0.135-0.479 ns. The limit is 25 times the
sync noise and below timing offsets that compromise subtraction. Spike
lag mixes timing and extracted charge; sync contains only the trigger.

`core.diagnostics.sync_lag_ns` bounds the trigger chain, not downstream
latency from sync to field arrival. Different latencies can move the
spikes while sync edges agree. The `ok` line reports the measured sync
and explicitly reports missing sync data instead of claiming alignment.
"""

SPIKE_EDGE_NS = 8.0
"""Defines the 8 ns limit for the displacement spike's 10-90 % edge.
Good edges span 6.0-6.5 ns; jittered edges span 8-13 ns. Jitter also
reduces spike heights by 4-14 %, but height alone cannot diagnose it
(docs/service-contract.md section 3).

With `experiment.transient.RunConfig.dark_reference="translated"`, the
same voltage change spans different absolute voltages. Capacitive currents agree only where
capacitance is independent of voltage. On this device the spike-height
difference grows from 0.4 % at 220 K to 1.1 % at 295 K. Stronger voltage
dependence can exceed 2 % without jitter, in shots also carrying
charge-induced lag. The leading edge instead follows the generator's
rise: it remains 6.0-6.5 ns over 220-295 K while spike decay varies by
11 ns. The edge criterion therefore accepts charge-induced lag.
"""

SHOT_CHECKS: tuple[tuple[str, str, str, str], ...] = (
    ("Rail", "warn",
     f"One trace holds a single extreme value for {RAIL_RUN_SAMPLES} or more consecutive "
     "samples: the scope clipped, and the shot's charge is meaningless.",
     "Reduce the swing, or check the auto-range."),
    ("Repeated extreme", "warn",
     f"An extreme value occurs more than {RAIL_REPEAT_SAMPLES} times in one trace: an "
     "averaged trace does not sit on one value, so the range is probably too small.",
     "Repeat the point; check the auto-range if it recurs."),
    ("Timing", "warn",
     f"The light and dark spikes are more than {SPIKE_LAG_NS:g} ns apart and an edge is "
     f"slower than {SPIKE_EDGE_NS:g} ns, or the Sync edges of the two traces are more "
     f"than {SYNC_LAG_NS:g} ns apart: the trigger moved between or during the traces, "
     "and the spike does not cancel.",
     "Check the Sync cables and the trigger threshold, then repeat the point."),
    ("Empty trace", "warn",
     "The scope returned no samples: there is nothing to integrate.",
     "Check the scope and its connection."),
)
"""Describes `shot_verdict` checks by name, level, condition and remedy.
Warnings mark shots without excluding them from the mean.
`tools/build_manual.py` uses this table to generate manual 4.2.
"""

VERDICT_KEYS: tuple[str, ...] = ("rail_light", "rail_dark", "rail_run_light", "rail_run_dark",
                                 "shared_extreme", "peak_light_a", "peak_dark_a",
                                 "spike_lag_ns", "edge_light_ns", "edge_dark_ns",
                                 "averages_light", "averages_dark",
                                 "sync_edge_light_ns", "sync_edge_dark_ns",
                                 "sync_lag_ns", "level", "text")
"""Reserves the keys every verdict carries.
Diagnostics may add keys but cannot replace them: a diagnostic `level`
must not override the rail check. `peak_light_a` and `peak_dark_a` retain
the signed extreme of each full trace before decimation. A 5:1 reduction
can skip the true peak, so the card cannot derive it from the wire trace.
"""


def is_ephemeral(event: Event) -> bool:
    """Returns whether an event is live-only, without a journal or `seq`."""
    return isinstance(event, EPHEMERAL)


# -- envelopes ------------------------------------------------------------
def make_envelope(seq: int, run_id: str | None, node_path: str, event: Event,
                  ts: float | None = None) -> Envelope:
    """Wraps an `Event` in a wire envelope with a Unix timestamp.

    `ts` defaults to the current time in seconds. A non-`Event` is refused:
    `envelope_to_wire` would pass a dictionary or string through with a
    type such as `dict`, which clients cannot dispatch. `RunQueued` is an
    `Event` and uses this same path.
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
    """Returns the WebSocket frame for `env` under the module's payload policy."""
    ev = env.event
    if isinstance(ev, StepDone):
        return envelope_to_wire(env, max_points=WS_MAX_POINTS)
    if isinstance(ev, RunFinished):
        data, info = _run_finished(ev)
        return _frame(env, data, info)
    return envelope_to_wire(env)


def journal_payload(env: Envelope, *, verdict: Mapping[str, Any] | None = None) -> dict | None:
    """Returns the journal payload, or `None` for a live-only event.

    Every `StepDone` line carries a verdict. Supplying the WebSocket's
    verdict, including bench diagnostics, keeps the live card and journal
    consistent. Without one, this function runs the rail check without
    diagnostics rather than omitting the judgement
    (docs/service-contract.md section 3).
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
    """Returns WebSocket and journal payloads with one shared verdict.

    The journal payload is `None` for a live-only event. Both `StepDone`
    payloads share the verdict object so the live card and journal agree.
    `diagnostics` comes from `rigs.Bench.shot_diagnostics` after the light
    acquisition and includes the auto-range pass count when available.
    """
    ws = ws_payload(env)
    verdict = None
    ev = env.event
    if isinstance(ev, StepDone):
        verdict = attach_verdict(ws["data"], ev, diagnostics)
    return ws, journal_payload(env, verdict=verdict)


def ephemeral_frame(run_id: str | None, node_path: str, event: Event,
                    ts: float | None = None) -> dict:
    """Returns a full live-only frame with `seq` null.

    Like the drop notice, it passes through client deduplication and never
    appears in replay (docs/service-contract.md section 3).
    """
    frame = ws_payload(make_envelope(0, run_id, node_path, event, ts))
    frame["seq"] = None
    return frame


# -- the per-shot verdict -------------------------------------------------
def _rail_run(a: np.ndarray) -> int:
    """Finds the longest consecutive run at a trace's minimum or maximum.

    Beyond its range a digitiser repeats the same code, forming a flat top.
    An averaged analogue trace rarely holds one code for seven samples.
    """
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
    """Judges a shot's clipping, timing and completeness.

    The verdict includes rail counts and run lengths, shared extremes,
    signed peaks in A, spike lag and edge times in ns, N_avg, sync edge
    times, `level`, `text` and additional diagnostics.

    Sample-by-sample subtraction requires aligned acquisitions. A timing
    error can leave a 50 mA displacement spike in the difference and produce
    a photocurrent ten times too large with Q's sign reversed, even when
    rail checks pass. This is measured on the bench
    (docs/analysis/2026-09-06/spike-lag-and-dark-charge.md section 0).

    Spike lag alone can reflect extracted charge rather than jitter.
    `_mistimed` warns when lag accompanies a smeared edge, or independently
    when sync traces disagree. Without either symptom, the `ok` line reports
    lag beside the sync evidence. Edge times distinguish trigger-chain
    faults; N_avg checks acquisition completeness. Timing needs `dt`, and
    counts and sync need `Trace` inputs. Bare arrays leave these diagnostics
    `None`, so only the rail checks apply.

    Rail checks concern one trace (manual 4.2):

    - At least `RAIL_RUN_SAMPLES` consecutive extreme samples imply a rail.
      The warning states that Q is meaningless.
    - More than `RAIL_REPEAT_SAMPLES` extreme samples suggest insufficient
      range. This is suspicious rather than fatal.

    `shared_extreme` is informational: the displacement spike dominates
    both traces, so valid traces can share it. `text` supplies the warning
    or the live card's one-line summary. Diagnostics merge as additional
    keys, such as `autorange_passes`; attempts to overwrite a key in
    `VERDICT_KEYS` are refused to protect the verdict.
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
            "text": "an empty trace: the scope returned no samples, so there "
                    "is nothing to judge and nothing to integrate"}
    else:
        light, dark = light_arr, dark_arr
        mistimed = _mistimed(align)
        rail_light, rail_dark = _rail_samples(light), _rail_samples(dark)
        run_light, run_dark = _rail_run(light), _rail_run(dark)
        shared = bool(light.min() == dark.min() or light.max() == dark.max())
        worst_run, worst = max(run_light, run_dark), max(rail_light, rail_dark)
        if worst_run >= RAIL_RUN_SAMPLES:
            which = ("light" if run_light >= run_dark else "dark")
            text = (f"the {which} trace holds one extreme value for {worst_run} consecutive "
                    "samples: that is the scope's rail, not the signal, so the charge "
                    "of this shot is meaningless. Widen the range or reduce the swing")
            level = "warn"
        elif worst > RAIL_REPEAT_SAMPLES:
            text = (f"an extreme value repeats {worst} times -- a real averaged trace "
                    "does not sit on one value, so the window is probably too small "
                    "even though the auto-range did not say so")
            level = "warn"
        elif mistimed:
            text = mistimed + " " + _sync_side(align, sync_light, sync_dark)
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
    """Computes and stores `shot_verdict` in `payload_data["verdict"]`.

    Returns that object so other payloads can share the same judgement.
    """
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
    """Returns alignment and completeness diagnostics, or `None` if unavailable."""
    from ..core.diagnostics import (edge_10_90_ns, spike_lag_ns, sync_edge_ns,
                                    sync_lag_ns)
    out: dict[str, Any] = {"spike_lag_ns": None, "edge_light_ns": None, "edge_dark_ns": None,
                           "averages_light": None, "averages_dark": None,
                           "sync_edge_light_ns": None, "sync_edge_dark_ns": None,
                           "sync_lag_ns": None}
    if dt and light.size and dark.size:
        out["spike_lag_ns"] = spike_lag_ns(light, dark, dt)
        out["edge_light_ns"] = edge_10_90_ns(light, dt)
        out["edge_dark_ns"] = edge_10_90_ns(dark, dt)
    yl, yd = getattr(sync_light, "y", None), getattr(sync_dark, "y", None)
    if yl is not None and yd is not None and getattr(sync_light, "dt", None):
        out["sync_lag_ns"] = sync_lag_ns(np.asarray(yl, dtype=float),
                                         np.asarray(yd, dtype=float),
                                         float(sync_light.dt),
                                         float(getattr(sync_light, "t0", 0.0)))
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


def _mistimed(align: Mapping[str, Any]) -> str:
    """Returns the timing fault description, or an empty string.

    Extracted charge can move spike correlation without a timing error.
    An unchanged no-light spike plus measured photocurrent reproduces the
    observed lag to 0.01 ns. Shifting the dark trace removes only about
    4 % of the difference
    (docs/analysis/2026-09-06/spike-lag-and-dark-charge.md section 2).
    A lag-only rule flags 177 of 492 good shots with Q within about 1 % of
    the unshifted value (docs/service-contract.md section 3).

    The two independent fault checks are:

    - Spike lag above `SPIKE_LAG_NS` together with an edge slower than
      `SPIKE_EDGE_NS`. Trigger scatter convolves and broadens the spike.
    - Sync lag above `SYNC_LAG_NS`, regardless of spike lag. Sync records
      the trigger alone. Charge-induced lag and a real offset can move
      spike correlation in opposite directions: a 0.5 ns offset can leave
      only 0.08 ns of spike lag. Gating sync on spike lag would miss it.

    `core.diagnostics.sync_lag_ns` bounds only the trigger chain.
    Latency after the sync edge remains outside that check. Spike height
    is also unsuitable: voltage-dependent capacitance produces real
    light-to-dark height differences that grow with temperature, including
    shots with charge-induced lag. `SPIKE_EDGE_NS` explains that constraint.
    """
    lag = align.get("spike_lag_ns")
    edges = [e for e in (align.get("edge_light_ns"), align.get("edge_dark_ns")) if e is not None]
    # Spike lag and a smeared edge together indicate jitter.
    if lag is not None and abs(lag) > SPIKE_LAG_NS and edges and max(edges) > SPIKE_EDGE_NS:
        return (f"the light and dark displacement spikes are {abs(lag):.2f} ns apart and "
                f"the spike edge is {max(edges):.1f} ns where a settled shot is 6 ns: that "
                "pair is the signature of the trigger jittering for the length of the "
                "shot, so their difference leaves the spike in the photocurrent and Q of "
                "this shot is not a charge.")
    # Sync is independent: charge and timing offsets can move spike
    # correlation in opposite directions. A 0.5 ns offset can read as
    # 0.08 ns spike lag, so gating on spike lag would miss misalignment
    # (docs/service-contract.md section 3).
    offset = align.get("sync_lag_ns")
    if offset is not None and abs(offset) > SYNC_LAG_NS:
        return (f"the two sync traces are {abs(offset):.2f} ns apart: the acquisitions "
                "really are offset in time, so the displacement spike does not cancel "
                "in their difference and Q of this shot is not a charge.")
    return ""


def _sync_side(align: Mapping[str, Any], sync_light: Any = None,
               sync_dark: Any = None) -> str:
    """Locates the indicated fault within the trigger chain from sync edges."""
    edges = [align.get("sync_edge_light_ns"), align.get("sync_edge_dark_ns")]
    if all(e is None for e in edges):
        if sync_light is None and sync_dark is None:
            return "No sync trace was fetched, so which side jitters cannot be said."
        # Fetched, but `core.diagnostics.sync_edge_ns` could not measure it.
        return ("A sync trace was fetched but no edge could be read off it, so "
                "which side jitters cannot be said.")
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
            # Without corroborating timing faults, lag is consistent with
            # charge added to the spike. The text must distinguish that from
            # a timing warning; `_mistimed` applies the fault checks.
            note = ""
            if abs(lag) > SPIKE_LAG_NS:
                # An aligned sync constrains the trigger chain only. Field arrival
                # follows the sync edge after a latency that can differ between
                # acquisitions. Report the measured sync instead of claiming spike
                # alignment.
                sync = align.get("sync_lag_ns")
                note = (f" · sync {abs(sync):.2f} ns apart" if sync is not None
                        else " · no sync to check the trigger")
            parts.append(f"spikes {abs(lag):.2f} ns apart" + note)
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
    """Builds the seven-key shape from `experiment.wire.envelope_to_wire`.

    The data is built here rather than by reflection.
    `tests/test_service_wire.py` checks that both paths agree.
    """
    return {"seq": int(env.seq), "ts": float(env.ts), "run_id": env.run_id,
            "node_path": env.node_path, "type": type(env.event).__name__,
            "data": data, "decimated": info}


def _run_finished(ev: RunFinished) -> tuple[dict, dict]:
    """Reduces `RunFinished` by omitting `photo_averaged` and retaining 1-D arrays.

    Replacement precedes conversion, avoiding a wasted conversion of a
    51-by-4000 matrix to Python lists.
    """
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
    """Copies data into JSON-safe types and refuses unsupported values.

    Arrays become lists, NumPy scalars become Python scalars, NaN and
    infinity become `None`, and dataclasses become dictionaries. Refusal
    names the type here, before serialization reaches the journal lock.
    """
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
