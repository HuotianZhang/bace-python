"""The acceptance pair: the service against LabVIEW on the rig, 2026-09-02.

`bench-archive/` asserts the *maths*: the same traces in give the same numbers
out, to 1.6e-06. This asserts the *chain*: the service, driving the real
instruments from a parked bench, measured the same physics the LabVIEW engine
measured on the same device half an hour earlier. Different question, different
evidence, and it needs both runs beside each other -- which is what
`acceptance/20260902_service-vs-labview/` is.

Three things are checked, and each one failed at some point during that day:

1. **The service's own numbers follow from its own traces.** Recomputing the
   photocurrent and the charge from the light and dark stored in its HDF5
   reproduces the Q it reported. This is the 2026-08-07 regression again, on
   the current device, the current polarity and the service's code path.

2. **The two runs agree on sign and shape.** Both photocurrents peak negative,
   within 30 ns of each other, and both tails return to zero. Not on Q to a few
   percent: these are two runs thirty-one minutes apart and this device moved
   by 2.8x in three hours on 2026-09-01, so a tight numerical assertion here
   would be a test of whether the sample drifted. Sign and shape is exactly
   what an INV/NORM mistake breaks, and exactly what was wrong for a whole day.

3. **The validated configuration is what was actually recorded.** The pair was
   measured with `invert_polarity = true` and `:OUTP1:POL NORM`, and the file
   says so in `/config/resolved` -- a readback, not a setting. The recipes
   called `true` + `INV` "combination 4" and were wrong; if a recipe change
   ever reverts that, this fails rather than the next rig day discovering it.

Skips when the folder is not there, the way the archive regression does.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pytest

from bace.core.process import charge, photocurrent

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "acceptance", "20260902_service-vs-labview")

T0_RECORD_S = 320e-9
"""Where both sides integrate from, in record time. The service asked for
120.5 ns after the trigger and its own record puts the trigger 199.5 ns in;
the LabVIEW panel says 3.20E-7 outright. The same window, spelled twice."""

MAX_PEAK_SEPARATION_NS = 30.0
MAX_TAIL_FRACTION = 0.05
"""How flat "returns to zero" has to be: the mean of the last tenth of the
record, against the peak. The rejected polarity of 2026-09-01 sat at 4.45 mA
here -- a hundred percent of its peak -- so this is a wide gate that only a
real failure trips."""


def _need(path: str) -> str:
    if not os.path.isdir(path):
        pytest.skip(f"no acceptance archive at {path}; it ships with the "
                    "repository and can be restored from the lab PC")
    return path


def _matrix(folder: str, pattern: str):
    """A LabVIEW trace matrix: a header row of times, then one row per V_pre."""
    rows = open(glob.glob(os.path.join(folder, pattern))[0]).read().splitlines()
    t = np.array([float(x) for x in rows[0].split("\t")[1:] if x.strip()])
    labels, data = [], []
    for row in rows[1:]:
        cells = row.split("\t")
        values = [float(x) for x in cells[1:] if x.strip()]
        if values:
            labels.append(float(cells[0]))
            data.append(values)
    return t, np.array(labels), np.array(data)


def _labview_q(folder: str):
    path = glob.glob(os.path.join(folder, "1_averagesQ*.dat"))[0]
    rows = [r.split("\t") for r in open(path).read().splitlines()[1:] if r.strip()]
    return np.array([float(r[0]) for r in rows]), np.array([float(r[3]) for r in rows])


def _shape(t: np.ndarray, y: np.ndarray):
    """(peak, its time in ns, |tail mean| / |peak|)."""
    k = int(np.argmax(np.abs(y)))
    tail = abs(float(y[-len(y) // 10:].mean()))
    return float(y[k]), float(t[k] * 1e9), tail / abs(float(y[k]))


def _service():
    from bace.storage.hdf5 import read_run
    folder = _need(os.path.join(ROOT, "service_153722"))
    return read_run(glob.glob(os.path.join(folder, "run*.h5"))[0])


# -- 1. the service's numbers follow from the service's traces ---------------
def test_the_services_charge_follows_from_its_own_traces():
    """Its HDF5 stores the light and dark averaged over loops, and Q per loop.
    Photocurrent, baseline correction and integration are all linear, so the
    charge of the averaged traces is the average of the charges -- and that is
    the q_mean the run reported. A mismatch means the recorded numbers did not
    come from the recorded data."""
    d = _service()
    t = d["time_s"]
    dt = float(t[1] - t[0])
    mine = photocurrent(d["light"][0], d["dark"][0], dt, offset_correct=True)

    np.testing.assert_allclose(mine, d["photocurrent"][0], atol=1e-15)
    q = charge(mine, dt, T0_RECORD_S)
    assert q == pytest.approx(float(d["q_mean"][0]), rel=1e-9)
    assert q == pytest.approx(float(np.mean(d["q_all"])), rel=1e-9), (
        "the charge of the mean traces is the mean of the per-loop charges")


# -- 2. the two runs agree on sign and shape --------------------------------
def test_the_service_and_labview_agree_on_sign_and_shape():
    """The acceptance criterion, and the one an INV/NORM mistake breaks: both
    transients extract in the same direction, at the same time in the record,
    and both settle back to zero. The 14:52 run of that day -- everything else
    right, the generator at INV -- peaked at +0.23 mA with a tail the engine
    itself flagged as "not a baseline"; it would fail every clause here."""
    lv = _need(os.path.join(ROOT, "labview_150640"))
    t_lv, vpre_lv, light = _matrix(lv, "4_averagesLightCurrent*.dat")
    _, _, dark = _matrix(lv, "4_averagesDarkCurrent*.dat")
    dt_lv = float(t_lv[1] - t_lv[0])
    mid = len(vpre_lv) // 2                       # the V_pre nearest V_oc
    photo_lv = photocurrent(light[mid], dark[mid], dt_lv, offset_correct=True)

    d = _service()
    t_svc = d["time_s"]
    photo_svc = d["photocurrent"][0]

    peak_lv, at_lv, tail_lv = _shape(t_lv, photo_lv)
    peak_svc, at_svc, tail_svc = _shape(t_svc, photo_svc)

    assert peak_lv < 0 and peak_svc < 0, (
        f"both extract in the same direction: LabVIEW {peak_lv * 1e3:+.3f} mA, "
        f"service {peak_svc * 1e3:+.3f} mA")
    assert abs(at_lv - at_svc) < MAX_PEAK_SEPARATION_NS, (
        f"the transient sits at {at_lv:.1f} ns for LabVIEW and {at_svc:.1f} ns "
        "for the service")
    assert tail_lv < MAX_TAIL_FRACTION and tail_svc < MAX_TAIL_FRACTION, (
        f"light - dark must return to zero: tails {tail_lv:.3f} and "
        f"{tail_svc:.3f} of the peak")

    _, q_lv = _labview_q(lv)
    q_svc = float(d["q_mean"][0])
    assert q_lv[mid] < 0 and q_svc < 0
    ratio = q_svc / q_lv[mid]
    assert 0.4 < ratio < 2.5, (
        f"Q {q_svc:.3e} against {q_lv[mid]:.3e} C, ratio {ratio:.2f}. A wide "
        "gate on purpose: two runs 31 min apart, and this device moved 2.8x in "
        "three hours on 2026-09-01. It catches an order of magnitude, which is "
        "what a wrong polarity costs, and nothing else")


def test_the_port_reproduces_labviews_own_charge_from_labviews_own_traces():
    """The 2026-08-07 regression's question, asked again on the device that is
    on the rig now and at the polarity settled that day: LabVIEW's light and
    dark, through `bace.core`, give LabVIEW's Q. The archive run is a different
    device, so this is the only place the two are the same sample."""
    lv = _need(os.path.join(ROOT, "labview_150640"))
    t, vpre, light = _matrix(lv, "4_averagesLightCurrent*.dat")
    _, _, dark = _matrix(lv, "4_averagesDarkCurrent*.dat")
    vpre_q, q_ref = _labview_q(lv)
    dt = float(t[1] - t[0])
    np.testing.assert_allclose(vpre, vpre_q)

    for i, v in enumerate(vpre):
        mine = charge(photocurrent(light[i], dark[i], dt, offset_correct=True),
                      dt, T0_RECORD_S)
        # the file carries six significant figures, so one written digit is
        # ~1e-6 of the value; four of those covers the rounding of the traces
        # it was computed from as well
        assert mine == pytest.approx(q_ref[i], rel=4e-6), (
            f"V_pre {v}: recomputed {mine:.6e} against the file's {q_ref[i]:.6e}")


# -- 3. the configuration the pair was measured in --------------------------
def test_the_file_records_the_validated_polarity_and_sign_convention():
    """`invert_polarity = true` WITH `:OUTP1:POL NORM`, and `current_sign =
    -1`. The recipes labelled `true` + `INV` "combination 4" and called it the
    answer for a day and a half; `/config/resolved` is a readback, so this is
    the instrument's account of what ran, not the recipe's."""
    d = _service()
    assert d["resolved"]["bias_output_polarity"] == "NORM"
    assert d["resolved"]["bias_polarity_source"] == "set by this run", (
        "not inherited from whatever the front panel held")
    assert bool(d["run_config"]["invert_polarity"]) is True
    assert d["run_config"]["output_polarity"] == "NORM"
    assert float(d["rig_config"]["current_sign"]) == -1.0
    # the settle the operator asked for, on both traces
    assert float(d["run_config"]["shutter_settle_s"]) == pytest.approx(5.0)
    assert int(d["run_config"]["n_averages"]) == 200
    # and the trigger chain the run set and read back
    assert d["resolved"]["bias_arm_source"] == "EXT"
    assert d["resolved"]["led_output_polarity"] == "INV", (
        "the 33220A's Sync rising edge means light OFF; this one is INV")


def test_the_journals_of_the_rig_day_are_kept_and_readable():
    """Three sessions, in order: the run that refused to measure a silent
    CHAN3, the run that measured the wrong polarity beautifully, and the one
    that was accepted. They are the evidence for `docs/notes.md` hardware defects 10
    and 11, and a realistic fixture if the journal readers ever need one."""
    import json

    folder = _need(os.path.join(ROOT, "journals"))
    files = sorted(glob.glob(os.path.join(folder, "*.jsonl")))
    assert len(files) == 3

    sessions = {}
    for path in files:
        lines = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
        assert lines[0]["type"] == "SessionStarted"
        assert lines[0]["data"]["mode"] == "rig", "these are real-rig sessions"
        sessions[os.path.basename(path)[:15]] = lines

    refused = sessions["20260902_125751"]
    assert any(e["type"] == "RunFailed" and "no sync on CHAN3" in e["data"]["error"]
               for e in refused), "the circular trigger calibration, caught"

    wrong_polarity = sessions["20260902_144844"]
    states = [e["data"]["values"] for e in wrong_polarity
              if e["type"] == "InstrumentState"]
    assert any(v.get("bias_output_polarity") == "INV" for v in states)
    steps = [e for e in wrong_polarity if e["type"] == "StepDone"]
    assert steps and all(e["data"]["q"] > 0 for e in steps), (
        "INV gives a positive charge on this device: the sign flip that hid "
        "behind the collapsed peak")

    accepted = sessions["20260902_153357"]
    states = [e["data"]["values"] for e in accepted if e["type"] == "InstrumentState"]
    assert any(v.get("bias_output_polarity") == "NORM" for v in states)
    steps = [e for e in accepted if e["type"] == "StepDone"]
    assert steps and all(e["data"]["q"] < 0 for e in steps)
