"""The catalogue: provenance, and `build()` turning parameters into runs.

The build tests are transcript-style on the simulator: what the LED was told,
which side of the relay the device was on when each event came out, what the
folder was called. That is the layer where a mismatch between the console and
the run would silently bias every charge, so nothing here is satisfied by
"nothing raised".
"""
from __future__ import annotations

import json
import os
import pathlib

import numpy as np
import pytest

from bace.config import ConfigError
from bace.experiment import events as E
from bace.experiment.jv import JVCurveDone, JVFinished
from bace.experiment.rig import RigConfig
from bace.params import ParamValue, Source, run_toml_layer
from bace.service.modules import (Catalogue, ModuleError, RunContext, VocSource,
                                  jsonable)
from bace.service.rigs import Bench
from bace.storage.naming import RunMetadata

REPO = pathlib.Path(__file__).resolve().parents[1]
NO_SLEEP = lambda s: None                                      # noqa: E731

FAST = {"n_averages": 8, "settle_s": 0.0, "dark_settle_s": 0.0, "record_length": 400,
        "t0_int_s": 2.71e-7, "t0_int_reference": "record", "calibrate_trigger": False,
        "led_settle_s": 0.0}
"""What a bace test overrides on top of the recipe: the sim's geometry (see
`test_transient_sim.run` for why `t0_int_s` is pinned) and no settling."""


class FakeHistory:
    """What the catalogue asks of the journal, and nothing else."""

    def __init__(self, last_used=None, shot=None, settle=None):
        self.last_used = dict(last_used or {})
        self.shot = shot
        self.settle = dict(settle or {})

    def last_used_params(self, module):
        return self.last_used.get(module)

    def settle_history(self):
        return self.settle

    def shot_time_s(self, module="bace"):
        return self.shot


def catalogue(history=None, run_toml=None, sample=None) -> Catalogue:
    raw = run_toml_layer(REPO / "run.toml") if run_toml is None else run_toml
    return Catalogue(rig_config=RigConfig(), run_toml=raw, history=history, sample=sample)


def bench(seed: int = 2) -> Bench:
    return Bench.build_simulated(RigConfig(), seed=seed)


def make_ctx(tmp_path, node_path="bace", **kw) -> RunContext:
    kw.setdefault("sleep", NO_SLEEP)
    return RunContext(run_id="20260902_210000-001", node_path=node_path,
                      out_folder=str(tmp_path),
                      metadata=RunMetadata(sample="s4", material="SIM", pixel="a",
                                           temperature_k=290.0), **kw)


def voc_at(level: float, value: float = 0.906) -> VocSource:
    return VocSource(value=value, led_v=level, run_id="20260902_210000-000",
                     node_path="jv_bace", how="jv_bace")


# -- the catalogue -------------------------------------------------------------
def test_the_catalogue_lists_the_contract_modules():
    cat = catalogue()
    assert cat.names() == ["jv_dark", "jv_bace", "bace", "power", "temperature",
                           "park", "wait", "note"]
    bace = cat.spec("bace")
    assert (bace.kind, bace.status, bace.relay) == ("measurement", "built", "transient")
    assert bace.needs_voc_param == "centre_on_voc" and not bace.provides_voc
    assert bace.led_params == ("led_v", "led_low_v")
    assert bace.groups[:3] == ("axis", "pinned", "acquisition")
    assert set(bace.groups) == {"axis", "pinned", "acquisition", "illumination",
                                "processing", "timing", "trigger", "output", "sourcemeter"}
    jv = cat.spec("jv_bace")
    assert jv.provides_voc and jv.relay == "dc" and "led_start_v" in jv.led_params
    assert cat.spec("jv_dark").led_params == ()
    assert cat.spec("temperature").status == "partial"
    assert cat.spec("power").kind == "observer"
    assert cat.param_set("park").names == ()
    with pytest.raises(KeyError):
        cat.spec("nope")
    with pytest.raises(KeyError):
        cat.param_set("nope")


def test_bace_params_layer_default_run_toml_last_used_edited_and_inherited():
    """The provenance chain the console shows, end to end on the real recipe."""
    hist = FakeHistory(last_used={"bace": {"n_loops": 7, "settle_s": 0.3,
                                           "no_such_param": 1, "n_averages": "junk"}})
    cat = catalogue(history=hist)
    ps = cat.param_set("bace")

    # run.toml, table by table
    assert ps.get("n_averages") == ParamValue(20, Source.RUN_TOML, "run.toml [acquisition]")
    assert ps.get("t0_int_reference").value == "trigger"
    assert ps.get("centre_on_voc") == ParamValue(True, Source.RUN_TOML, "run.toml [axis]")
    assert ps.get("delay_ns") == ParamValue(88.0, Source.RUN_TOML, "run.toml [pinned]")
    assert ps.get("led_v") == ParamValue(1.02, Source.RUN_TOML, "run.toml [illumination]")
    assert ps.get("v_sat").value == -1.0 and ps.get("led_low_v").value == 0.4
    assert ps.get("smu_current_compliance_a") == \
        ParamValue(0.01, Source.RUN_TOML, "run.toml [sourcemeter]")
    assert ps.get("smu_terminals").value == "FRON"
    assert ps.get("store_shots").source is Source.RUN_TOML
    # not in the recipe: the dataclass default shows, attributed to nothing
    assert ps.get("shutter_settle_s") == ParamValue(0.0, Source.DEFAULT, "")
    assert ps.get("vpre") == ParamValue(0.0, Source.DEFAULT, "")
    assert ps.get("voc") == ParamValue(None, Source.DEFAULT, "")

    # last-used beats run.toml; a stale name or value is skipped, not fatal
    assert ps.get("n_loops") == ParamValue(7, Source.LAST_USED, "previous run")
    assert ps.get("settle_s").value == 0.3

    # edited beats last-used, coerced from what a form sends
    cat.edit("bace", {"n_loops": "3"})
    pv = cat.param_set("bace").get("n_loops")
    assert pv == ParamValue(3, Source.EDITED, "") and type(pv.value) is int
    with pytest.raises(ModuleError, match="^bace: n_loops"):
        cat.edit("bace", {"n_loops": "abc"})
    with pytest.raises(ModuleError, match="^bace: nope: no such parameter"):
        cat.edit("bace", {"nope": 1})
    assert cat.param_set("bace").get("n_loops").value == 3, "a bad batch changes nothing"
    cat.edit("bace", {"n_loops": None, "settle_s": 0.1})
    assert cat.param_set("bace").get("n_loops") == ParamValue(7, Source.LAST_USED, "previous run")
    assert cat.param_set("bace").get("settle_s") == ParamValue(0.1, Source.EDITED, "")
    cat.reset("bace")
    assert cat.param_set("bace").get("settle_s").value == 0.3

    # inherited on a resolver's copy: shown as inherited, not editable, and
    # the bench card is untouched by it
    copy = cat.param_set("bace")
    copy.update_layer(Source.INHERITED, {"led_v": 1.06}, detail="illumination loop")
    led = {w["name"]: w for w in copy.as_wire()}["led_v"]
    assert (led["value"], led["source"], led["detail"], led["editable"]) == \
        (1.06, "inherited", "illumination loop", False)
    assert cat.param_set("bace").get("led_v").source is Source.RUN_TOML
    with pytest.raises(ModuleError, match="^bace: max_current"):
        cat.edit("bace", {"max_current_a": 1})


def test_the_recipes_illumination_level_reaches_jv_bace_and_an_optional_jv_table():
    cat = catalogue()
    ps = cat.param_set("jv_bace")
    assert ps.get("led_start_v") == ParamValue(1.02, Source.RUN_TOML, "run.toml [illumination]")
    assert ps.get("led_stop_v").value == 1.02 and ps.get("led_low_v").value == 0.4
    assert ps.get("led_v").value is None and ps.get("dark").value is True
    assert ps.get("smu_nplc") == ParamValue(1.0, Source.RUN_TOML, "run.toml [sourcemeter]")
    assert cat.param_set("jv_dark").get("step_v").source is Source.DEFAULT

    raw = dict(run_toml_layer(REPO / "run.toml"))
    raw["jv"] = {"step_v": 0.01, "both_directions": True}
    ps = catalogue(run_toml=raw).param_set("jv_dark")
    assert ps.get("step_v") == ParamValue(0.01, Source.RUN_TOML, "run.toml [jv]")
    assert ps.get("both_directions").value is True
    raw["jv"] = {"stpe_v": 0.01}
    with pytest.raises(ConfigError, match=r"unknown key\(s\) in \[jv\]: stpe_v"):
        catalogue(run_toml=raw)
    with pytest.raises(ModuleError, match=r"\[sample\]: unknown key"):
        catalogue(sample={"smaple": "s4"})
    meta = catalogue(sample={"sample": "s4", "material": "PTQ10:IT-4F", "pixel": "a",
                             "temperature_k": 290, "operator": "hz"}).base_metadata()
    assert (meta.sample, meta.pixel, meta.temperature_k, meta.operator) == ("s4", "a", 290.0, "hz")


def test_as_wire_reports_what_the_module_needs():
    cat = catalogue()                                   # centre_on_voc from run.toml
    w = cat.as_wire("bace")
    assert w["name"] == "bace" and w["status"] == "built" and w["last"] is None
    assert w["needs"] == [{"code": "voc", "text": "none · run jv_bace first"}]
    assert {p["name"] for p in w["params"]} >= {"axis_name", "n_loops", "led_v", "voc",
                                                "n_averages", "smu_nplc", "store_shots"}
    json.dumps(w)
    assert cat.as_wire("bace", session_voc=voc_at(1.02))["needs"] == []
    off = cat.as_wire("bace", session_voc=voc_at(1.04))["needs"]
    assert off[0]["code"] == "voc" and "1.040 V" in off[0]["text"]
    cat.edit("bace", {"voc": 0.9})
    assert cat.as_wire("bace")["needs"] == []
    cat.reset("bace", "voc")
    cat.edit("bace", {"measure_dc": True})
    assert cat.as_wire("bace")["needs"] == []
    cat.reset("bace")

    silent = {"instruments": {"power": {"available": False, "reason": "refused"}},
              "unavailable": {"power": "not answering at :8918", "smu": "no such resource"}}
    assert cat.as_wire("power", bench=silent)["needs"] == \
        [{"code": "power", "text": "not answering at :8918"}]
    codes = [n["code"] for n in cat.as_wire("bace", bench=silent, session_voc=voc_at(1.02))["needs"]]
    assert codes == ["power"]
    assert [n["code"] for n in cat.as_wire("jv_dark", bench=silent)["needs"]] == ["smu"]
    assert cat.as_wire("power", bench={"instruments": {"power": {"available": True}}})["needs"] == []


def test_estimates_follow_the_cost_model():
    cat = catalogue(history=FakeHistory(shot=1.2))
    p = {**cat.param_set("bace").values(), "n_loops": 20}
    assert cat.estimate_s("bace", p) == pytest.approx(20 * 1 * 1.2)
    assert cat.estimate_text("bace", p) == "one shot ≈ 1.2 s · 20 loops × 1 pts ≈ 24 s"
    p.update({"axis_start": -0.02, "axis_stop": 0.02, "axis_step": 0.01})
    assert cat.estimate_s("bace", p) == pytest.approx(20 * 5 * 1.2)
    assert cat.estimate_s("bace", {"n_loops": 10}) == pytest.approx(12.0)   # partial: filled in
    default = catalogue()
    assert default.estimate_s("bace", {"n_loops": 10}) == pytest.approx(8.0)
    assert "(default)" in default.estimate_text("bace", {"n_loops": 10})
    assert default.shot_time() == (0.8, "default") and cat.shot_time() == (1.2, "journal")

    jv = cat.param_set("jv_bace").values()               # dark + one level, 71 points
    assert cat.estimate_s("jv_bace", jv) == pytest.approx(2 * (71 * 0.05 + 71 * 0.05 + 2.0))
    assert cat.estimate_text("jv_bace", jv).startswith("2 curves × 71 pts")
    assert cat.estimate_s("jv_dark", {}) == pytest.approx(1 * (71 * 0.05 + 71 * 0.05 + 2.0))
    two = {**jv, "led_start_v": 1.02, "led_stop_v": 1.06, "led_step_v": 0.04,
           "both_directions": True}
    assert cat.estimate_text("jv_bace", two).startswith("6 curves")
    assert cat.estimate_s("jv_dark", {"step_v": 0.0}) == 0.0
    assert "cannot estimate" in cat.estimate_text("jv_dark", {"step_v": 0.0})
    assert cat.estimate_s("wait", {"seconds": 90}) == 90.0
    assert cat.estimate_s("temperature", {"hold_s": 60}) == 60.0
    assert "—" in cat.estimate_text("temperature", {})
    assert cat.estimate_s("note", {}) == 0.0


# -- jv ------------------------------------------------------------------------------
def test_jv_dark_leaves_the_led_off_and_the_shutter_shut_and_writes_the_files(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    got: dict = {}
    ctx = make_ctx(tmp_path, node_path="jv_dark", on_data=lambda path, d: got.update({path: d}))
    seen, evs = [], []
    for ev in cat.build("jv_dark", {"step_v": 0.1}, ctx, b.rig):
        if isinstance(ev, JVCurveDone):
            seen.append((sim.bench.led_mode, sim.led.output_enabled,
                         sim.bench.shutter_open, sim.router.position))
        evs.append(ev)
    assert seen == [("OFF", False, False, "sourcemeter")]
    assert isinstance(evs[-1], JVFinished) and sim.bench.shots == 0
    assert not sim.bench.smu_output and not sim.bench.shutter_open

    assert len(ctx.folders) == 1
    folder = ctx.folders[0]
    name = os.path.basename(folder)
    assert name.startswith("s4_SIM_a_290K_") and "LED" not in name and "offsetcorr" not in name
    files = os.listdir(folder)
    assert any(f.startswith("BACE_JV_Data_") for f in files)
    assert any(f.startswith("BACE_JV_Parameters_") for f in files)
    assert any(f.startswith("jv") and f.endswith(".h5") for f in files)

    curves = got["jv_dark"]["curves"]
    assert len(curves) == 1 and curves[0]["dark"] is True and curves[0]["label"] == "dark"
    assert curves[0]["voltage"].size == 15 and curves[0]["metrics"]["voc"] is None
    json.dumps(jsonable(got["jv_dark"]))


def test_jv_bace_measures_one_level_when_led_v_is_inherited_else_the_range(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    ctx = make_ctx(tmp_path, node_path="led=1.060V/jv_bace", led_v=1.06)
    seen = []
    for ev in cat.build("jv_bace", {"step_v": 0.05}, ctx, b.rig):
        if isinstance(ev, JVCurveDone):
            seen.append((ev.label, sim.bench.led_mode, sim.bench.led_drive_v,
                         sim.bench.shutter_open))
    assert seen == [("dark", "OFF", 0.0, False), ("1.06 V", "DC", 1.06, True)]
    assert not sim.led.output_enabled and not sim.bench.shutter_open
    assert "1060mVLED" in os.path.basename(ctx.folders[0])

    ctx = make_ctx(tmp_path / "range", node_path="jv_bace")
    labels = [ev.label for ev in cat.build(
        "jv_bace", {"step_v": 0.05, "led_start_v": 1.02, "led_stop_v": 1.06,
                    "led_step_v": 0.04, "dark": False}, ctx, b.rig)
        if isinstance(ev, JVCurveDone)]
    assert labels == ["1.02 V", "1.06 V"]
    assert "LED" not in os.path.basename(ctx.folders[0]), "two levels: no single level to name"
    with pytest.raises(ModuleError, match="^jv_bace: led_step_v: must be positive"):
        cat.build("jv_bace", {"led_v": None, "led_start_v": 1.02, "led_stop_v": 1.06,
                              "led_step_v": 0.0}, ctx, b.rig)
    assert not sim.led.output_enabled


# -- bace ----------------------------------------------------------------------------
def test_bace_pulses_the_led_runs_on_the_amplifier_and_records_the_folder(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    got: dict = {}
    voc = voc_at(1.02)
    ctx = make_ctx(tmp_path, voc=voc, on_data=lambda path, d: got.update({path: d}))
    params = {**FAST, "n_loops": 2, "centre_on_voc": True, "axis_start": 0.0,
              "axis_stop": 0.0, "led_v": 1.02}
    seen, axis = [], None
    for ev in cat.build("bace", params, ctx, b.rig):
        if isinstance(ev, E.StepDone):
            seen.append((sim.bench.led_mode, sim.bench.led_drive_v, sim.led.output_enabled,
                         sim.led.last_levels, sim.led.frequency_hz, sim.router.position))
        elif isinstance(ev, E.AxisResolved):
            axis = ev
    assert seen and all(s == ("PULSE", 1.02, True, (1.02, 0.4), 500.0, "amplifier")
                        for s in seen)
    assert axis.voc == 0.906 and axis.values[0] == pytest.approx(0.906)
    assert not sim.led.output_enabled and not sim.bench.bias_output
    assert not sim.bench.shutter_open
    assert ctx.voc is voc

    name = os.path.basename(ctx.folders[0])
    assert name.startswith("s4_SIM_a_290K_1020mVLED_906mVVOC_offsetcorr_"), name
    files = os.listdir(ctx.folders[0])
    assert any(f.startswith("1_averagesQ") for f in files)
    assert any(f.startswith("run") and f.endswith(".h5") for f in files)

    data = got["bace"]
    assert set(data) >= {"axis", "values", "q_mean", "q_std", "q_all", "time_s", "light",
                         "dark", "photo", "last_shot", "kept", "requested"}
    assert (data["kept"], data["requested"]) == (2, 2)
    assert data["q_all"].shape == (2, 1) and np.isfinite(data["q_all"]).all()
    assert data["axis"]["name"] == "vpre" and data["values"].tolist() == [pytest.approx(0.906)]
    n = data["time_s"].size
    assert data["light"].shape == data["dark"].shape == data["photo"].shape == (1, n) and n > 0
    shot = data["last_shot"]
    assert shot["light"].size == n and shot["cumulative_q"].size > 0
    assert shot["cumulative_q"][-1] == pytest.approx(shot["q"])
    assert 0.0 <= shot["t0_int_record_s"] < data["time_s"][-1]
    json.dumps(jsonable(data))


def test_bace_refuses_a_voc_measured_at_another_level_before_touching_anything(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.04))
    with pytest.raises(ModuleError, match=r"^bace: voc: V_oc was measured at 1.04 V drive"):
        cat.build("bace", {**FAST, "centre_on_voc": True, "led_v": 1.02}, ctx, b.rig)
    assert not sim.led.output_enabled and sim.bench.led_mode == "OFF"
    assert sim.bench.shots == 0 and ctx.folders == []


def test_bace_refuses_centre_on_voc_without_a_source(tmp_path):
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path)
    with pytest.raises(ModuleError, match=r"^bace: centre_on_voc: no V_oc source .* 1.020 V"):
        cat.build("bace", {**FAST, "centre_on_voc": True, "led_v": 1.02}, ctx, b.rig)
    assert b.sim.bench.shots == 0
    # an absolute axis needs no source at all
    gen = cat.build("bace", {**FAST, "centre_on_voc": False, "axis_start": 0.9,
                             "axis_stop": 0.9, "n_loops": 1}, ctx, b.rig)
    # The LED read-back comes first (what the 33220A answered after being
    # set to pulse), then the engine starts.
    first = next(gen)
    assert isinstance(first, E.InstrumentState) and first.values["led_output"] == "ON"
    assert first.values["led_mode"] == "PULSE"
    assert isinstance(next(gen), E.RunStarted)
    gen.close()
    assert not b.sim.led.output_enabled


def test_measure_dc_supplies_the_voc_and_centres_the_axis_on_it(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    ctx = make_ctx(tmp_path)
    params = {**FAST, "n_loops": 1, "centre_on_voc": True, "led_v": 1.02, "measure_dc": True}
    dc = axis = None
    positions = []
    for ev in cat.build("bace", params, ctx, b.rig):
        if isinstance(ev, E.DCMeasured):
            dc = ev
            positions.append(("dc", sim.router.position, sim.bench.led_mode, sim.bench.led_drive_v))
        elif isinstance(ev, E.AxisResolved):
            axis = ev
        elif isinstance(ev, E.StepDone):
            positions.append(("shot", sim.router.position, sim.bench.led_mode, sim.bench.led_drive_v))
    # V_oc is read with the LED in DC (not PULSE), then the transient pulses
    # on the amplifier side. The read is under an open shutter -- proven by
    # dc.dc.voc being the light V_oc below, since the simulator now returns
    # the dark 0 V if the shutter was shut during the read.
    assert positions == [("dc", "sourcemeter", "DC", 1.02), ("shot", "amplifier", "PULSE", 1.02)]
    assert dc.led_drive_v == 1.02
    assert dc.dc.voc > 0.5, "the light V_oc, not the dark 0 V a shutter-shut read would give"
    assert dc.dc.voc == pytest.approx(sim.bench.device.voc(1.02), abs=0.01)
    assert axis.voc == dc.dc.voc and axis.values[0] == pytest.approx(dc.dc.voc)
    assert (ctx.voc.how, ctx.voc.led_v, ctx.voc.value) == ("measure_dc", 1.02, dc.dc.voc)
    assert ctx.voc.run_id == ctx.run_id and ctx.voc.node_path == "bace"
    assert f"{round(dc.dc.voc * 1000)}mVVOC" in os.path.basename(ctx.folders[0])
    assert not sim.bench.smu_output and not sim.bench.bias_output
    assert not sim.bench.shutter_open, "the shutter is shut again after the DC read"


def test_a_typed_voc_wins_over_the_session_source_and_says_so(tmp_path):
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.02, 0.906))
    params = {**FAST, "n_loops": 1, "centre_on_voc": True, "led_v": 1.02, "voc": 0.88}
    axis = next(ev for ev in cat.build("bace", params, ctx, b.rig)
                if isinstance(ev, E.AxisResolved))
    assert axis.voc == 0.88
    assert (ctx.voc.how, ctx.voc.value, ctx.voc.led_v) == ("typed", 0.88, 1.02)
    assert "880mVVOC" in os.path.basename(ctx.folders[0])


def test_the_illumination_loops_led_v_drives_the_led_and_the_folder(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    ctx = make_ctx(tmp_path, node_path="led=1.060V/bace", led_v=1.06, led_low_v=0.5,
                   voc=voc_at(1.06, 0.93), temperature_k=250.0)
    params = {**FAST, "n_loops": 1, "centre_on_voc": True, "led_v": 1.02}
    levels = [sim.led.last_levels for ev in cat.build("bace", params, ctx, b.rig)
              if isinstance(ev, E.StepDone)]
    assert levels == [(1.06, 0.5)], "the loop's level, not the card's 1.02"
    name = os.path.basename(ctx.folders[0])
    assert "250K" in name and "1060mVLED" in name and "930mVVOC" in name


def test_after_shot_stop_keeps_the_shot_and_still_serves_what_it_has(tmp_path):
    b = bench()
    cat = catalogue()
    polls = {"n": 0}

    def abort():
        polls["n"] += 1
        return polls["n"] > 2

    got: dict = {}
    ctx = make_ctx(tmp_path, voc=voc_at(1.02), abort=abort,
                   on_data=lambda path, d: got.update({path: d}))
    evs = list(cat.build("bace", {**FAST, "n_loops": 3, "centre_on_voc": True, "led_v": 1.02},
                         ctx, b.rig))
    assert isinstance(evs[-1], E.RunAborted) and evs[-1].reason == "requested"
    assert sum(isinstance(e, E.StepDone) for e in evs) == 2
    data = got["bace"]
    assert (data["kept"], data["requested"]) == (2, 3)
    assert data["q_all"].shape == (3, 1) and np.isnan(data["q_all"][2, 0])
    assert np.isfinite(data["q_mean"]).all() and data["last_shot"]["loop"] == 2
    assert not b.sim.led.output_enabled
    assert os.path.isdir(ctx.folders[0])


def test_walking_away_mid_run_switches_the_led_off(tmp_path):
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.02))
    gen = cat.build("bace", {**FAST, "n_loops": 5, "centre_on_voc": True, "led_v": 1.02},
                    ctx, b.rig)
    for _ in range(4):
        next(gen)
    assert b.sim.led.output_enabled and b.sim.bench.led_mode == "PULSE"
    gen.close()
    assert not b.sim.led.output_enabled and not b.sim.bench.bias_output


def test_build_refuses_bad_parameters_by_name_and_touches_nothing(tmp_path):
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.02))
    assert not os.listdir(tmp_path)
    ok = {**FAST, "centre_on_voc": True, "led_v": 1.02}
    for bad, match in (({"nope": 1}, r"^bace: no such parameter\(s\): nope"),
                       ({"n_loops": 0}, r"^bace: n_loops: 0 is below the minimum"),
                       ({"led_low_v": 1.2}, r"^bace: led_v: low level 1.2 V is not below"),
                       ({"smu_current_compliance_a": 0.1}, r"^bace: smu_current_compliance_a: .*ceiling"),
                       ({"axis_name": "delay_ns"}, r"^bace: axis: centre_on_voc is only"),
                       ({"axis_start": 0.0, "axis_stop": 0.1, "axis_step": 0.0}, r"^bace: axis: .*positive step"),
                       ({"delay_ns": -5.0}, r"^bace: delay_ns = -5"),
                       ({"t0_int_reference": "start"}, r"^bace: t0_int_reference: 'start' is not one of")):
        with pytest.raises(ModuleError, match=match):
            cat.build("bace", {**ok, **bad}, ctx, b.rig)
    assert b.sim.bench.shots == 0 and not b.sim.led.output_enabled
    with pytest.raises(KeyError):
        cat.build("nope", {}, ctx, b.rig)
    with pytest.raises(ModuleError, match="^jv_dark: step_v"):
        cat.build("jv_dark", {"step_v": 0.0}, ctx, b.rig)
    bare = Bench.build_simulated(RigConfig())
    bare.rig.smu = None
    with pytest.raises(ModuleError, match="needs a SourceMeter"):
        cat.build("jv_dark", {}, ctx, bare.rig)
    with pytest.raises(ModuleError, match="measure_dc: this bench has no SourceMeter"):
        cat.build("bace", {**ok, "measure_dc": True}, ctx, bare.rig)


def test_the_smu_compliance_reaches_a_driver_that_takes_one(tmp_path):
    from bace.drivers.keithley2400 import SourceMeterConfig

    class ConfiguredSMU:
        """The real driver's shape: a `config` the run may replace."""

        def __init__(self, inner):
            self.inner, self.config = inner, SourceMeterConfig()

        def __getattr__(self, name):
            return getattr(self.inner, name)

    b = bench()
    b.rig.smu = ConfiguredSMU(b.sim.smu)
    b.sim.router._smu = b.rig.smu
    cat = catalogue()
    ctx = make_ctx(tmp_path, node_path="jv_dark")
    list(cat.build("jv_dark", {"step_v": 0.1, "smu_current_compliance_a": 0.02,
                               "smu_nplc": 0.1}, ctx, b.rig))
    assert (b.rig.smu.config.current_compliance_a, b.rig.smu.config.nplc) == (0.02, 0.1)


# -- observers and utilities ------------------------------------------------------
def test_the_temperature_module_settles_through_the_331_when_the_rig_has_one(tmp_path):
    """The same helper the temperature loop uses: no pause, a reading per
    poll, the settled verdict, the context temperature is what was measured
    (not the setpoint), the clock is polls times the poll interval through
    the context's sleep, and the console's log got its marks."""
    from bace.service.temperature import TEMPERATURE_POLL_S

    b = Bench.build_simulated(RigConfig(temperature_console="sim"), seed=2)
    assert b.rig.temperature is b.sim.temperature
    cat = catalogue()
    slept = []
    ctx = make_ctx(tmp_path, node_path="T=250K/temperature", sleep=slept.append)
    evs = list(cat.build("temperature", {"setpoint_k": 250.0, "tolerance_k": 0.2,
                                         "hold_s": 10.0, "timeout_s": 600.0}, ctx, b.rig))
    kinds = [type(e).__name__ for e in evs]
    assert "NeedsOperator" not in kinds and kinds[-1] == "Verdict"
    reads = [e for e in evs if isinstance(e, E.TemperatureRead)]
    assert reads[0].in_band is False and reads[-1].in_band is True
    assert {r.source for r in reads} == {"simulated"} and {r.setpoint_k for r in reads} == {250.0}
    assert [r.in_band for r in reads] == sorted(r.in_band for r in reads), "in band once, in band since"
    assert len([r for r in reads if r.in_band]) == 3, "the entering poll plus two polls of dwell"
    settled = evs[-1]
    assert (settled.code, settled.level, settled.node_path) == \
        ("temperature.settled", "ok", "T=250K/temperature")
    assert settled.data["hold_s"] == 10.0 and settled.data["held_s"] == 10.0
    assert settled.data["settle_s"] == (len(reads) - 3) * TEMPERATURE_POLL_S
    assert settled.data["polls"] == len(reads) and settled.data["source"] == "simulated"
    assert settled.text == f"250 K reached in {settled.data['settle_s'] / 60:.0f} min, held 10 s"
    assert ctx.temperature_k == reads[-1].kelvin == settled.data["kelvin"] != 250.0
    assert slept == [TEMPERATURE_POLL_S] * (len(reads) - 1), "one sleep between polls, none after the last"
    assert b.sim.temperature.setpoints == [250.0]
    assert b.sim.temperature.notes == [
        f"bace {ctx.run_id} T=250K/temperature: setpoint 250 K +/-0.2 K hold 10 s",
        f"bace {ctx.run_id} T=250K/temperature: settled at {ctx.temperature_k:.3f} K"]

    # no controller and no hook: refused before anything runs, as before
    with pytest.raises(ModuleError, match="wait_for_operator"):
        cat.build("temperature", {}, make_ctx(tmp_path), bench().rig)


def test_the_temperature_module_pauses_for_the_operator_and_resumes_with_the_hook(tmp_path):
    b = bench()
    cat = catalogue()
    asked, slept = [], []
    ctx = make_ctx(tmp_path, node_path="T=250K/temperature", sleep=slept.append,
                   wait_for_operator=lambda d, on_poll=None: (
                       asked.append(d), {"note": "set by hand", "temperature_k": 250.1})[1])
    evs = list(cat.build("temperature", {"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 5.0},
                         ctx, b.rig))
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "OperatorResumed", "TemperatureRead"]
    need = evs[0]
    assert need.what == "temperature" and need.node_path == "T=250K/temperature"
    assert need.detail == {"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 5.0,
                           "timeout_s": 1800.0}
    assert asked == [need.detail]
    assert evs[1].note == "set by hand" and evs[1].detail["temperature_k"] == 250.1
    assert (evs[2].kelvin, evs[2].setpoint_k, evs[2].in_band, evs[2].source) == \
        (250.1, 250.0, True, "operator")
    assert ctx.temperature_k == 250.1 and slept == [5.0]

    # a resume without a temperature: no reading, the hold still happens
    ctx = make_ctx(tmp_path, wait_for_operator=lambda d, on_poll=None: {"note": "went ahead"},
                   sleep=slept.append)
    evs = list(cat.build("temperature", {"hold_s": 1.0}, ctx, b.rig))
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "OperatorResumed"]
    assert ctx.temperature_k is None and slept[-1] == 1.0

    # stopped while paused: an honest RunAborted, no hold
    ctx = make_ctx(tmp_path, wait_for_operator=lambda d, on_poll=None: {"stopped": True},
                   sleep=slept.append)
    evs = list(cat.build("temperature", {"hold_s": 9.0}, ctx, b.rig))
    assert [type(e).__name__ for e in evs] == ["NeedsOperator", "RunAborted"]
    assert evs[1].reason == "requested" and slept[-1] == 1.0

    with pytest.raises(ModuleError, match="wait_for_operator"):
        cat.build("temperature", {}, make_ctx(tmp_path), b.rig)


def test_the_utility_modules(tmp_path):
    b = bench()
    sim = b.sim
    cat = catalogue()
    slept = []
    ctx = make_ctx(tmp_path, sleep=slept.append)

    evs = list(cat.build("wait", {"seconds": 2.5}, ctx, b.rig))
    assert slept == [2.5] and isinstance(evs[0], E.Notice)

    evs = list(cat.build("note", {"text": "sample changed"}, ctx, b.rig))
    assert evs == [E.Notice("info", "sample changed")]

    sim.bias.enable_output(True)
    sim.shutter.unblock()
    evs = list(cat.build("park", {}, ctx, b.rig))
    assert isinstance(evs[0], E.Notice) and not sim.bench.bias_output
    assert not sim.bench.shutter_open

    sim.led.set_dc(1.02)
    sim.led.enable_output(True)
    sim.shutter.unblock()
    evs = list(cat.build("power", {"wavelength_nm": 532.0}, ctx, b.rig))
    assert len(evs) == 1 and isinstance(evs[0], E.PowerReading)
    assert evs[0].watts > 0 and evs[0].trustworthy and evs[0].source == "simulated"
    assert evs[0].wavelength_nm == 532.0 and sim.power.wavelength_nm == 532.0
    b.rig.power = None
    with pytest.raises(ModuleError, match="^power: no power meter"):
        cat.build("power", {}, ctx, b.rig)


def test_jsonable_turns_arrays_into_lists_and_nan_into_null():
    out = jsonable({"a": np.array([1.0, np.nan]), "b": np.float64(2.5), "c": (1, 2),
                    "d": float("inf"), "e": {"f": np.int64(3)}, "g": None})
    assert out == {"a": [1.0, None], "b": 2.5, "c": [1, 2], "d": None, "e": {"f": 3}, "g": None}
    json.dumps(out)


# -- the round of 2026-09-02 --------------------------------------------------------------
def test_the_bace_card_shows_the_sessions_voc_as_derived_at_the_same_level():
    """`/modules` used to say `voc: null · default` after a jv_bace while the
    run would centre on the session's V_oc. The card now applies the rule
    the pipeline resolver applies to a manual run."""
    cat = catalogue()
    p = {x["name"]: x for x in cat.as_wire("bace")["params"]}
    assert (p["voc"]["value"], p["voc"]["source"], p["voc"]["editable"]) == (None, "default", True)

    p = {x["name"]: x for x in cat.as_wire("bace", session_voc=voc_at(1.02))["params"]}
    assert p["voc"] == {**p["voc"], "value": 0.906, "source": "derived",
                        "detail": "jv_bace jv_bace (this session)", "editable": False}
    assert cat.as_wire("bace", session_voc=voc_at(1.02))["needs"] == []
    assert cat.param_set("bace").get("voc").value is None, "the catalogue's own layers untouched"

    # another level: nothing derived, and `needs` says which level to run
    p = {x["name"]: x for x in cat.as_wire("bace", session_voc=voc_at(1.04))["params"]}
    assert (p["voc"]["value"], p["voc"]["source"]) == (None, "default")
    # typed wins, as it does for the run; measure_dc measures, so nothing is derived
    cat.edit("bace", {"voc": 0.9})
    p = {x["name"]: x for x in cat.as_wire("bace", session_voc=voc_at(1.02))["params"]}
    assert (p["voc"]["value"], p["voc"]["source"]) == (0.9, "edited")
    cat.reset("bace")
    cat.edit("bace", {"measure_dc": True})
    p = {x["name"]: x for x in cat.as_wire("bace", session_voc=voc_at(1.02))["params"]}
    assert p["voc"]["source"] == "default"


def test_vpre_on_voc_pins_the_prebias_as_an_offset_from_the_voc_in_scope(tmp_path):
    b = bench()
    cat = catalogue()
    voc = voc_at(1.02, value=0.9)
    ctx = make_ctx(tmp_path, voc=voc)
    params = {**FAST, "n_loops": 1, "centre_on_voc": False, "vpre_on_voc": True,
              "vpre": 0.05, "axis_name": "delay_ns", "axis_start": 50.0, "axis_stop": 150.0,
              "axis_step": 50.0, "led_v": 1.02}
    starts = [ev for ev in cat.build("bace", params, ctx, b.rig) if isinstance(ev, E.StepStarted)]
    assert [s.setpoint.vpre for s in starts] == [pytest.approx(0.95)] * 3, "V_oc + 0.05 V"
    assert [s.setpoint.delay_ns for s in starts] == [50.0, 100.0, 150.0]
    assert ctx.voc is voc

    with pytest.raises(ModuleError, match="vpre_on_voc: no V_oc source"):
        cat.build("bace", params, make_ctx(tmp_path), b.rig)
    with pytest.raises(ModuleError, match="swept axis"):
        cat.build("bace", {**params, "axis_name": "vpre", "axis_start": 0.0, "axis_stop": 0.0,
                           "axis_step": 0.0}, make_ctx(tmp_path, voc=voc), b.rig)
    assert b.sim.bench.shots == 6, "three shots, light and dark: the refusals touched nothing"
    spec = {p.name: p for p in cat._params["bace"]}["vpre_on_voc"]
    assert spec.group == "pinned" and spec.default is False


def test_the_temperature_module_polls_live_while_it_waits_and_an_abort_unwinds(tmp_path, monkeypatch):
    """The module path has the loop path's hooks: readings taken during its
    pause go out through the context's `emit` while the wait lasts, and the
    context's `stop_mode` tells an abort (`AbortNow`, so the worker reports
    `aborted`) from an after_shot (an honest `RunAborted`)."""
    from bace.service import temperature as T
    from bace.service.worker import AbortNow, StopMode

    monkeypatch.setattr(T, "TEMPERATURE_POLL_S", 0.0)       # every on_poll reads
    b = Bench.build_simulated(RigConfig(temperature_console="sim"), seed=2)
    b.sim.temperature.time_constant_reads = 1000
    cat = catalogue()
    params = {"setpoint_k": 250.0, "tolerance_k": 0.2, "hold_s": 0.0, "timeout_s": 0.0}
    live, seen = [], []

    def wait(detail, on_poll=None):
        for _ in range(3):
            on_poll()
            seen.append(len(live))
        return {"note": "went ahead"}

    ctx = make_ctx(tmp_path, node_path="T=250K/temperature", emit=live.append,
                   wait_for_operator=wait)
    evs = list(cat.build("temperature", params, ctx, b.rig))
    assert [type(e).__name__ for e in evs] == ["TemperatureRead", "Verdict", "NeedsOperator",
                                               "OperatorResumed"]
    assert seen == [1, 2, 3], "each poll's reading reached the hook before the wait went on"
    assert all(isinstance(r, E.TemperatureRead) and r.source == "simulated" for r in live)
    assert ctx.temperature_k == live[-1].kelvin, "the last reading polled, nothing typed"

    ctx = make_ctx(tmp_path, node_path="T=250K/temperature",
                   stop_mode=lambda: StopMode.ABORT, wait_for_operator=wait)
    with pytest.raises(AbortNow):
        list(cat.build("temperature", params, ctx, b.rig))

    ctx = make_ctx(tmp_path, node_path="T=250K/temperature",
                   stop_mode=lambda: StopMode.AFTER_SHOT, wait_for_operator=wait)
    evs = list(cat.build("temperature", params, ctx, b.rig))
    assert [type(e).__name__ for e in evs] == ["RunAborted"] and evs[0].reason == "requested"


def test_bace_refuses_a_33220a_that_did_not_take_pulse_mode_or_its_output(tmp_path):
    """The 33220A is the master clock: in DC, or with the output off, nothing
    arms the 81150A and the scope has no sync. The first real service run
    (session 115857) had no read-back of either generator in its file; now the
    33220A is read back after being set, written into the file, and a
    generator that answers OFF or DC stops the module before the scan."""
    from bace.service.modules import ModuleError, _led_problems, _read_led_state

    class Led:
        """A 33220A that took nothing: reads back OFF and DC."""
        output_enabled = True
        mode = "PULSE"
        def __init__(self): self.log = []
        def set_pulse(self, *a, **k): self.log.append("set_pulse")
        def set_dc(self, *a, **k): self.log.append("set_dc")
        def enable_output(self, on=True): self.log.append(f"enable({on})")
        def disable_output(self): self.log.append("enable(False)")
        def off(self): self.log.append("off")
        def set_polarity(self, inverted): pass
        def polarity(self): return "INV"
        def read_state(self):
            return {"output": False, "polarity": "INV", "mode": "DC", "shape": "DC",
                    "high_v": 1.02, "low_v": 0.4, "frequency_hz": 500.0}
        def errors(self): return ['-221,"Settings conflict"']

    state = _read_led_state(Led())
    assert state["output"] == "OFF" and state["mode"] == "DC" and "Settings conflict" in state["errors"]
    problems = _led_problems(state, {"frequency": 500.0})
    assert any("OFF" in p for p in problems)
    assert any("DC" in p for p in problems)
    assert any("Settings conflict" in p for p in problems)
    # NORM polarity is the chain card's warn, never a refusal here
    assert not any("POL" in p for p in problems)

    b = bench()
    b.rig.led = Led()
    cat = catalogue()
    ctx = make_ctx(tmp_path)
    gen = cat.build("bace", {**FAST, "centre_on_voc": False, "axis_start": 0.9,
                             "axis_stop": 0.9, "n_loops": 1}, ctx, b.rig)
    first = next(gen)
    assert first.values["led_output"] == "OFF" and first.values["led_mode"] == "DC"
    with pytest.raises(ModuleError, match="33220A is not driving the LED"):
        next(gen)
    assert b.sim.bench.shots == 0, "nothing was acquired"
    assert "off" in b.rig.led.log, "the module still switches the LED off on its way out"


def test_a_simulated_33220a_that_answers_nothing_definite_is_not_refused(tmp_path):
    """`?` is not a problem: the simulator has no `read_state`, and a driver
    that cannot answer is not a generator that said no."""
    from bace.service.modules import _led_problems
    assert _led_problems({"output": "?", "mode": "?", "frequency_hz": "?", "polarity": "?"},
                         {"frequency": 500.0}) == []
