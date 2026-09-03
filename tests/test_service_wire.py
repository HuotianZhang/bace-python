"""The payload policy: what a StepDone weighs on the WebSocket and in the journal.

`service.wire` is the one place that decides sizes, so these tests pin the
table in contract section 3 with realistic events -- a 4000-point `Trace`,
a 101 x 100 `q_all` -- and check that both payloads survive `json.dumps`,
that the journal never carries a trace, and that the service's own verdict
is attached to the wire data and not to the dataclass.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from bace.core.axis import Axis, Setpoint
from bace.drivers.protocols import Trace
from bace.experiment import events as E
from bace.experiment import jv as J
from bace.experiment.wire import envelope_to_wire
from bace.service import wire as W

N = 4000
DT, T0 = 5e-10, -1.995e-7


def _trace(seed: int, scale: float = 1e-3) -> Trace:
    rng = np.random.default_rng(seed)
    t = np.arange(N) * DT
    y = -scale * np.exp(-(t - 3.3e-7).clip(0) / 7e-8) * (t > 3.3e-7)
    return Trace(y=y + rng.normal(0, 1e-5, N), dt=DT, t0=T0)


def step_done(light=None, dark=None) -> E.StepDone:
    light = light or _trace(1)
    dark = dark or _trace(2, scale=0.0)
    photo = light.y - dark.y
    return E.StepDone(index=3, loop=2, step=2, setpoint=Setpoint(0.906, -1.0, 88.0),
                      axis_value=0.906, light=light, dark=dark, photo=photo,
                      photo_averaged=photo, q=-3.47e-10, q_mean=-3.5e-10,
                      q_std=np.float64(1.2e-12), intensity_w=None, clipped=False)


def run_finished(n_loops: int, n_steps: int) -> E.RunFinished:
    return E.RunFinished(axis=Axis("vpre", 0.80, 1.00, 0.20 / max(n_steps - 1, 1)),
                         values=np.linspace(0.80, 1.00, n_steps),
                         q_mean=np.full(n_steps, -3.5e-10),
                         q_std=np.full(n_steps, 1e-12),
                         q_all=np.full((n_loops, n_steps), -3.5e-10),
                         photo_averaged=np.zeros((n_steps, N)), dt=DT, elapsed_s=42.0)


def curve(index: int = 1, dark: bool = False) -> J.JVCurveDone:
    v = np.linspace(-0.2, 1.2, 71)
    i = 1e-9 * (np.exp(v / 0.05) - 1) - (0.0 if dark else 2e-4)
    return J.JVCurveDone(index=index, label="dark" if dark else "1.02 V", dark=dark,
                         led_level_v=None if dark else 1.02, direction="forward",
                         voltage=v, current=i,
                         density=J.current_density(i, 0.0 if dark else 0.04),
                         metrics=J.metrics(v, i, dark=dark),
                         intensity_w=None if dark else 2.07e-5)


def env(event: E.Event, seq: int = 7) -> E.Envelope:
    return W.make_envelope(seq, "20260902_205200-003", "T=250K/led=1.020V/bace", event,
                           ts=1_788_390_180.5)


def roundtrip(payload: dict) -> dict:
    text = json.dumps(payload, allow_nan=False)
    assert "NaN" not in text and "Infinity" not in text
    return json.loads(text)


# -- StepDone ---------------------------------------------------------------
def test_step_done_is_decimated_on_the_ws_and_scalar_only_in_the_journal():
    ev = step_done()
    ws, journal = W.payloads(env(ev), diagnostics={"autorange_passes": 2})

    for name in ("light", "dark"):
        assert len(ws["data"][name]["y"]) <= 1000
        assert ws["data"][name]["n"] == N, "the record length survives decimation"
        assert ws["decimated"][f"{name}.y"] == {"n_full": N, "stride": 5}
    assert len(ws["data"]["photo"]) <= 1000
    assert ws["decimated"]["photo_averaged"]["n_full"] == N
    assert ws["data"]["q"] == -3.47e-10 and ws["data"]["setpoint"]["vpre"] == 0.906

    assert set(journal["data"]) == set(W.STEPDONE_SCALARS) | {"verdict"}
    for name in W.STEPDONE_ARRAYS:
        assert name not in journal["data"]
        assert journal["decimated"][name] == {"omitted": True}
    assert journal["data"]["setpoint"] == {"vpre": 0.906, "vcoll": -1.0, "delay_ns": 88.0}
    assert journal["data"]["q_std"] == pytest.approx(1.2e-12)
    assert journal["data"]["intensity_w"] is None

    assert ws["data"]["verdict"] == journal["data"]["verdict"], "one verdict, both payloads"
    verdict = ws["data"]["verdict"]
    assert verdict["level"] == "ok" and verdict["shared_extreme"] is False
    assert verdict["autorange_passes"] == 2
    assert verdict["text"] == "autorange pass 2 · no shared extreme · 1 rail sample"
    assert set(W.VERDICT_KEYS) <= set(verdict)
    assert verdict["peak_light_a"] == pytest.approx(float(ev.light.y.min()))

    for payload in (ws, journal):
        back = roundtrip(payload)
        assert back["type"] == "StepDone" and back["seq"] == 7
        assert back["run_id"] == "20260902_205200-003"
        assert back["node_path"] == "T=250K/led=1.020V/bace"
    assert len(json.dumps(journal)) < 2000, "a journal line is scalars, not a trace"


def test_the_journal_step_done_always_carries_a_verdict():
    journal = W.journal_payload(env(step_done()))
    assert journal["data"]["verdict"]["level"] == "ok"
    assert "autorange_passes" not in journal["data"]["verdict"]


def test_the_verdict_is_on_the_wire_data_and_not_on_the_dataclass():
    ev = step_done()
    ws = W.ws_payload(env(ev))
    assert "verdict" not in ws["data"]
    returned = W.attach_verdict(ws["data"], ev.light.y, ev.dark.y, {"autorange_passes": 1})
    assert ws["data"]["verdict"] is returned
    assert not hasattr(ev, "verdict")
    roundtrip(ws)


# -- the verdict ------------------------------------------------------------
def test_a_flat_top_inside_one_trace_is_the_rail_and_a_shared_extreme_is_only_a_fact():
    """The design's corrected rule (03-states section C, 2026-09-02): light
    and dark *should* share an extreme, because the displacement spike
    dominates both; only a run of identical samples inside a single trace
    is evidence of clipping. A verdict that warned on the shared extreme
    fired on every good shot on the real bench."""
    light = Trace(np.maximum(_trace(1).y, -5e-4), DT, T0)       # clipped at a rail
    dark = Trace(np.maximum(_trace(3).y, -5e-4), DT, T0)        # the same rail
    v = W.shot_verdict(light.y, dark.y)
    assert v["level"] == "warn" and v["shared_extreme"] is True
    assert v["rail_run_light"] >= 7 and v["rail_light"] > 8
    assert "consecutive samples" in v["text"] and "rail" in v["text"]
    assert v["peak_light_a"] == pytest.approx(-5e-4)

    alone = W.shot_verdict(light.y, _trace(2, scale=0.0).y)
    assert alone["level"] == "warn" and alone["shared_extreme"] is False
    assert alone["rail_run_dark"] <= 1 and "consecutive" in alone["text"]
    json.dumps(alone)

    # a good shot whose two traces share their minimum exactly: ok, and the
    # shared extreme is stated in the summary line, not judged
    a, b = _trace(1).y.copy(), _trace(2, scale=0.0).y.copy()
    b[100] = a.min()
    good = W.shot_verdict(a, b, {"autorange_passes": 2})
    assert good["level"] == "ok" and good["shared_extreme"] is True
    assert good["text"] == "autorange pass 2 · shared extreme · 1 rail sample"
    assert good["rail_run_light"] == 1 and good["rail_run_dark"] == 1

    # a repeated extreme that is not a flat top: the window is suspect
    c = _trace(1).y.copy()
    c[::40][:12] = c.min()                                       # 12 hits, none adjacent
    suspect = W.shot_verdict(c, _trace(2, scale=0.0).y)
    assert suspect["level"] == "warn" and suspect["rail_light"] >= 12
    assert suspect["rail_run_light"] < 7 and "window is probably too small" in suspect["text"]


def test_the_peak_is_taken_from_the_full_trace_before_decimation():
    """The card's "peak · light -9.58 mA" must not come from a 5:1 thinned
    trace: put the extreme on a sample the stride skips."""
    y = _trace(2, scale=0.0).y.copy()
    y[1003] = -9.58e-3                                           # not a multiple of 5
    ws, journal = W.payloads(env(step_done(light=Trace(y, DT, T0))))
    assert ws["decimated"]["light.y"]["stride"] == 5
    assert min(ws["data"]["light"]["y"]) > -9.58e-3, "the thinned trace missed it"
    assert ws["data"]["verdict"]["peak_light_a"] == pytest.approx(-9.58e-3)
    assert journal["data"]["verdict"]["peak_light_a"] == pytest.approx(-9.58e-3)
    assert ws["data"]["verdict"]["peak_dark_a"] is not None


def test_a_step_phase_is_live_only():
    ev = E.StepPhase(index=4, phase="acquire light", k=3, of=7)
    ws, journal = W.payloads(env(ev))
    assert journal is None and ws["data"] == {"index": 4, "phase": "acquire light", "k": 3, "of": 7}
    assert W.is_ephemeral(ev) and not W.is_ephemeral(E.Notice("info", "x"))
    frame = W.ephemeral_frame("r-001", "bace", ev, ts=5.0)
    assert frame["seq"] is None and frame["type"] == "StepPhase" and frame["ts"] == 5.0
    assert frame["run_id"] == "r-001" and frame["node_path"] == "bace"
    roundtrip(frame)


def test_an_empty_trace_is_a_warning_not_a_crash():
    v = W.shot_verdict(np.empty(0), _trace(2).y)
    assert v["level"] == "warn" and "empty" in v["text"]
    assert v["peak_light_a"] is None and v["rail_run_light"] == 0


def test_a_diagnostic_may_not_overrule_the_verdict():
    with pytest.raises(ValueError, match="collides"):
        W.shot_verdict(_trace(1).y, _trace(2).y, {"level": "ok"})
    v = W.shot_verdict(_trace(1).y, _trace(2).y,
                       {"autorange_windows": [(0.1, 0.0, False)], "passes": np.int64(3)})
    assert v["autorange_windows"] == [[0.1, 0.0, False]] and v["passes"] == 3
    json.dumps(v)


# -- RunFinished ------------------------------------------------------------
def test_run_finished_omits_the_trace_matrix_and_keeps_the_arrays_whole():
    ev = run_finished(n_loops=3, n_steps=1200)                  # more steps than WS_MAX_POINTS
    ws = W.ws_payload(env(ev))
    assert ws["data"]["photo_averaged"] is None
    assert ws["decimated"]["photo_averaged"] == {"omitted": True, "shape": [1200, N]}
    assert len(ws["data"]["values"]) == 1200, "values are never decimated"
    assert len(ws["data"]["q_all"]) == 3 and len(ws["data"]["q_all"][0]) == 1200
    assert set(ws["decimated"]) == {"photo_averaged"}
    roundtrip(ws)


def test_the_journal_drops_q_all_only_above_ten_thousand_cells():
    small = W.journal_payload(env(run_finished(n_loops=100, n_steps=100)))
    assert len(small["data"]["q_all"]) == 100, "10 000 cells is still kept"
    assert "q_all" not in small["decimated"]

    big = W.journal_payload(env(run_finished(n_loops=101, n_steps=100)))
    assert big["data"]["q_all"] is None
    assert big["decimated"]["q_all"] == {"omitted": True, "cells": 10100}
    assert big["data"]["photo_averaged"] is None
    assert len(big["data"]["q_mean"]) == 100
    roundtrip(big)


# -- J-V ----------------------------------------------------------------------
def test_a_jv_curve_is_whole_on_the_ws_and_metrics_only_in_the_journal():
    ev = curve()
    ws, journal = W.payloads(env(ev))
    assert len(ws["data"]["voltage"]) == 71 and ws["decimated"] == {}

    assert set(journal["data"]) == set(W.JVCURVE_SCALARS) | {"metrics", "n_points"}
    assert journal["data"]["n_points"] == 71
    assert journal["data"]["label"] == "1.02 V" and journal["data"]["led_level_v"] == 1.02
    assert journal["data"]["metrics"] == ev.metrics.as_dict()
    assert isinstance(journal["data"]["metrics"]["voc"], float)
    assert journal["decimated"] == {n: {"omitted": True} for n in W.JVCURVE_ARRAYS}
    roundtrip(journal)

    dark = W.journal_payload(env(curve(0, dark=True)))
    assert dark["data"]["metrics"]["voc"] is None and dark["data"]["led_level_v"] is None


def test_jv_finished_reduces_its_curves_the_same_way():
    ev = J.JVFinished(curves=(curve(0, True), curve(1)), elapsed_s=3.0)
    ws, journal = W.payloads(env(ev))
    assert len(ws["data"]["curves"][1]["voltage"]) == 71
    assert journal["data"]["n_curves"] == 2
    assert "voltage" not in journal["data"]["curves"][1]
    assert journal["data"]["curves"][1]["n_points"] == 71
    roundtrip(journal)


# -- everything else ----------------------------------------------------------
@pytest.mark.parametrize("event", [
    E.LoopDone(loop=1, q_mean=np.arange(3, dtype=float), q_std=np.zeros(3)),
    E.AxisResolved(axis=Axis("vpre", 0.0, 0.0, centre_on_voc=True),
                   values=np.array([0.906]), voc=0.906),
    E.Progress(done=1, total=9, elapsed_s=2.5, eta_s=None, node_path="T=250K"),
    E.Notice("warning", "the tail is not a baseline"),
    E.RunStateChanged(state="running", reason="started"),
    E.RunAborted(reason="requested", done=3, total=9),
], ids=lambda e: type(e).__name__)
def test_everything_else_goes_whole_to_both(event):
    ws, journal = W.payloads(env(event))
    full = envelope_to_wire(env(event))
    assert ws == full and journal == full
    assert ws["decimated"] == {}
    roundtrip(ws)


# -- envelopes --------------------------------------------------------------
def test_make_envelope_stamps_now_and_refuses_non_events():
    before = 1_700_000_000.0
    e = W.make_envelope(12, None, "", E.Notice("info", "x"))
    assert e.seq == 12 and e.run_id is None and e.ts > before
    assert W.make_envelope(1, "r", "bace", E.Notice("info", "x"), ts=5).ts == 5.0
    with pytest.raises(TypeError, match="wraps an Event"):
        W.make_envelope(1, "r", "bace", {"type": "RunQueued"})
    queued = E.RunQueued(kind="manual", module="bace", tree={"kind": "module", "module": "bace"},
                         params={"n_loops": 3})
    ws, journal = W.payloads(W.make_envelope(2, "r", "", queued, ts=1.0))
    assert ws == journal and ws["type"] == "RunQueued"
    assert ws["data"] == {"kind": "manual", "module": "bace",
                          "tree": {"kind": "module", "module": "bace"}, "params": {"n_loops": 3},
                          "resolved": None, "name": "", "folder": None}
    with pytest.raises(TypeError, match="run_id"):
        W.make_envelope(1, 3, "bace", E.Notice("info", "x"))


def test_both_frames_have_the_shape_envelope_to_wire_defines():
    keys = set(envelope_to_wire(env(E.Notice("info", "x"))))
    assert set(W.ws_payload(env(run_finished(2, 3)))) == keys
    assert set(W.journal_payload(env(step_done()))) == keys
    assert set(W.journal_payload(env(curve()))) == keys
    assert keys == {"seq", "ts", "run_id", "node_path", "type", "data", "decimated"}
