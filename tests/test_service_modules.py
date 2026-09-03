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
RECIPE = REPO / "tests" / "run-quickcheck.toml"
# The frozen quick-check recipe, not the lab's run.toml: these tests pin the
# numbers the run.toml layer delivers, and the lab's recipe changes with the
# measurement (it was brought in line with the validated 2026-09-02 settings).
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
    raw = run_toml_layer(RECIPE) if run_toml is None else run_toml
    return Catalogue(rig_config=RigConfig(), run_toml=raw, history=history, sample=sample)


def bench(seed: int = 2) -> Bench:
    return Bench.build_simulated(RigConfig(), seed=seed)


def make_ctx(tmp_path, node_path="bace", **kw) -> RunContext:
    kw.setdefault("sleep", NO_SLEEP)
    return RunContext(run_id="20260902_210000-001", node_path=node_path,
                      out_folder=str(tmp_path),
                      metadata=RunMetadata(sample="s4", material="SIM", pixel="a",
                                           temperature_k=290.0,
                                           temperature_how="typed"), **kw)


def voc_at(level: float, value: float = 0.906) -> VocSource:
    return VocSource(value=value, led_v=level, run_id="20260902_210000-000",
                     node_path="jv_bace", how="jv_bace")


# -- the catalogue -------------------------------------------------------------
def test_the_catalogue_lists_the_contract_modules():
    cat = catalogue()
    assert cat.names() == ["jv", "jv_bace", "bace", "light", "power", "temperature",
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
    assert cat.spec("jv").led_params == (), "the module that never sets a level"
    assert cat.spec("light").kind == "utility" and cat.spec("light").relay is None
    assert cat.param_set("jv").names.count("dark") == 0, \
        "`jv` has no illumination parameters at all -- that is the module"
    assert not [n for n in cat.param_set("jv").names if n.startswith("led")]
    assert cat.spec("temperature").status == "partial"
    assert cat.spec("power").kind == "observer"
    assert cat.param_set("park").names == ()
    with pytest.raises(KeyError):
        cat.spec("nope")
    with pytest.raises(KeyError):
        cat.param_set("nope")


def test_a_retired_module_name_says_what_replaces_it():
    """A recipe saved through `/pipelines/save` is a file on disk and outlives
    the catalogue, so an operator can open a tree naming `jv_dark` long after
    it stopped existing. Without this the answer is `jv_dark: 'jv_dark'` -- a
    `KeyError` repr that says neither what happened nor what to do.

    Refused, never rewritten: `jv_dark` maps to *two* nodes, so translating it
    would change the tree's shape and its node paths (and so its folder
    names); translating it to `jv` alone would change what is measured, from
    "make it dark and sweep" to "sweep under whatever is there", which is the
    failure the split exists to prevent."""
    cat = catalogue()
    with pytest.raises(KeyError, match="no longer a module"):
        cat.spec("jv_dark")
    text = str(pytest.raises(KeyError, cat.spec, "jv_dark").value)
    assert "light" in text and "shutter = shut" in text and "jv" in text
    # An name that never existed still reads as one, not as a retirement.
    with pytest.raises(KeyError) as plain:
        cat.spec("nope")
    assert "no longer" not in str(plain.value)


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
    assert cat.param_set("jv").get("step_v").source is Source.DEFAULT

    raw = dict(run_toml_layer(RECIPE))
    raw["jv"] = {"step_v": 0.01, "both_directions": True}
    ps = catalogue(run_toml=raw).param_set("jv")
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
    assert (meta.temperature_how, meta.temperature_source) == ("typed", ""), (
        "nobody read an instrument for this number, and the file must say so")
    bare = catalogue(sample={"sample": "s4"}).base_metadata()
    assert (bare.temperature_k, bare.temperature_how) == (None, ""), "no number, no provenance"


def test_the_context_temperature_prefers_the_tree_over_the_session(tmp_path):
    """Two levels, and no competition between them: what a temperature node
    bound wins for its subtree, the session's typed `[sample]` number is the
    fallback when no node bound one, and `how`/`source` travel with whichever
    won -- a run must never be filed with one level's kelvin and the other's
    provenance."""
    ctx = make_ctx(tmp_path)
    assert ctx.temperature() == (290.0, "typed", ""), "no node bound one: the session's"

    ctx.temperature_k, ctx.temperature_how = 250.1, "settled"
    ctx.temperature_source = "console"
    assert ctx.temperature() == (250.1, "settled", "console"), "the tree's, whole"


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
    assert [n["code"] for n in cat.as_wire("jv", bench=silent)["needs"]] == ["smu"]
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
    # `jv` is one curve and no LED settle: it sets no light, so there is
    # nothing to settle after and no dark-plus-levels plan to count.
    assert cat.estimate_s("jv", {}) == pytest.approx(71 * 0.05 + 71 * 0.05)
    assert cat.estimate_text("jv", {"both_directions": True}).startswith("2 curves")
    two = {**jv, "led_start_v": 1.02, "led_stop_v": 1.06, "led_step_v": 0.04,
           "both_directions": True}
    assert cat.estimate_text("jv_bace", two).startswith("6 curves")
    assert cat.estimate_s("jv", {"step_v": 0.0}) == 0.0
    assert "cannot estimate" in cat.estimate_text("jv", {"step_v": 0.0})
    assert cat.estimate_text("light", {"shutter": "open", "led_mode": "dc",
                                       "led_v": 1.02}) == "shutter open · LED DC 1.02 V"
    assert cat.estimate_text("light", {"shutter": "shut"}) == "shutter shut"
    assert cat.estimate_s("wait", {"seconds": 90}) == 90.0
    assert cat.estimate_s("temperature", {"hold_s": 60}) == 60.0
    assert "—" in cat.estimate_text("temperature", {})
    assert cat.estimate_s("note", {}) == 0.0


# -- jv ------------------------------------------------------------------------------
def test_jv_sweeps_under_the_light_it_finds_and_leaves_it_exactly_there(tmp_path):
    """`jv` is the J-V primitive: it owns the SourceMeter and nothing else.

    The LED is pulsing and the shutter is open going in -- as a `light` step
    or a `bace` before it would leave them -- and both are in the same state
    coming out. Under the old `jv_dark` the shutter would have been shut on
    the way in *and* in the `finally`; here the only thing unwound is the
    SourceMeter, which is the only thing this run switched on.
    """
    b = bench()
    sim = b.sim
    sim.led.set_pulse(1.02, 0.4, frequency_hz=500.0)
    sim.led.enable_output(True)
    sim.shutter.unblock()
    cat = catalogue()
    got: dict = {}
    ctx = make_ctx(tmp_path, node_path="jv", on_data=lambda path, d: got.update({path: d}))
    seen, evs = [], []
    for ev in cat.build("jv", {"step_v": 0.1}, ctx, b.rig):
        if isinstance(ev, JVCurveDone):
            seen.append((sim.bench.led_mode, sim.led.output_enabled,
                         sim.bench.shutter_open, sim.router.position))
        evs.append(ev)
    assert seen == [("PULSE", True, True, "sourcemeter")], "the light never moved"
    assert isinstance(evs[-1], JVFinished) and sim.bench.shots == 0
    assert not sim.bench.smu_output, "the SourceMeter is unwound: this run turned it on"
    assert sim.bench.shutter_open, "the shutter is not: this run never touched it"
    assert sim.led.output_enabled and sim.bench.led_mode == "PULSE"

    assert len(ctx.folders) == 1
    folder = ctx.folders[0]
    name = os.path.basename(folder)
    assert name.startswith("s4_SIM_a_290K_") and "LED" not in name and "offsetcorr" not in name
    files = os.listdir(folder)
    assert any(f.startswith("BACE_JV_Data_") for f in files)
    assert any(f.startswith("BACE_JV_Parameters_") for f in files)
    assert any(f.startswith("jv") and f.endswith(".h5") for f in files)

    curves = got["jv"]["curves"]
    assert len(curves) == 1 and curves[0]["dark"] is False
    assert curves[0]["label"] == "as found 1.02 V", "labelled by the read-back"
    assert curves[0]["voltage"].size == 15
    json.dumps(jsonable(got["jv"]))


def test_jv_records_a_shut_shutter_as_dark_because_it_read_it_not_because_it_shut_it(tmp_path):
    b = bench()
    b.sim.led.set_dc(1.02)
    b.sim.led.enable_output(True)
    b.sim.shutter.shut()
    cat = catalogue()
    got: dict = {}
    ctx = make_ctx(tmp_path, node_path="jv", on_data=lambda path, d: got.update({path: d}))
    evs = [ev for ev in cat.build("jv", {"step_v": 0.1}, ctx, b.rig)]
    curve = [e for e in evs if isinstance(e, JVCurveDone)][0]
    assert curve.dark is True and curve.label == "as found dark"
    assert curve.illumination["shutter"] == "shut" and curve.illumination["lit"] is False
    assert abs(curve.metrics.jsc) < 1e-5, "dark through the shutter"
    assert curve.metrics.voc is None, "a curve read as dark gets the dark metrics"


def test_an_unknown_curve_stays_unknown_through_every_consumer(tmp_path):
    """`JVCurveDone.dark` is three-valued, and `None` is falsy -- so every
    consumer that wrote `not ev.dark` or `bool(ev.dark)` silently promoted a
    curve nobody could read into a *known light* one. That has now been the
    same bug in four places (the V_oc capture, the HDF5 attribute, the data
    endpoint, the run summary), so this walks one unknown curve through all of
    them at once rather than pinning them one at a time.

    The bench has no shutter, so `illumination_state` cannot say whether light
    reached the sample -- which is the whole of the unknown case."""
    import h5py
    from bace.service.executor import _Tally

    b = bench()
    b.rig.shutter = None
    b.sim.led.set_dc(1.02)
    b.sim.led.enable_output(True)
    cat = catalogue()
    got: dict = {}
    ctx = make_ctx(tmp_path, node_path="jv", on_data=lambda path, d: got.update({path: d}))
    tally = _Tally(module="jv")
    curves = []
    for ev in cat.build("jv", {"step_v": 0.1}, ctx, b.rig):
        tally.handle(ev)
        if isinstance(ev, JVCurveDone):
            curves.append(ev)

    assert len(curves) == 1 and curves[0].dark is None
    assert curves[0].metrics.voc is not None, "the interpolation still happens"

    # 1 · the run summary must not advertise it as a measured V_oc
    assert tally.last_voc is None, "an unknown curve is not a light one"

    # 2 · the data endpoint carries the tri-state rather than coercing it
    curve = got["jv"]["curves"][0]
    assert curve["dark"] is None and curve["illumination"] == "unknown"
    json.dumps(jsonable(got["jv"]))                  # and it is still JSON

    # 3 · the file says unknown, and omits `dark` rather than claiming False
    path = [f for f in os.listdir(ctx.folders[0]) if f.endswith(".h5")][0]
    with h5py.File(os.path.join(ctx.folders[0], path), "r") as f:
        group = f["curves"][sorted(f["curves"])[0]]
        assert group.attrs["illumination"] == "unknown"
        assert "dark" not in group.attrs

    # 4 · and it never becomes the V_oc a bace would centre its axis on
    assert ctx.voc is None


# -- light ----------------------------------------------------------------------------
def test_light_sets_the_shutter_and_the_led_and_reports_what_the_bench_then_read(tmp_path):
    """The bench's two light switches as a pipeline node, so a `jv` inside a
    tree can be dark. It goes through the same `apply_led`/`apply_shutter` the
    `set-led-*` and `shutter-*` bench actions call, so a click and a node
    cannot drive the LED differently."""
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, node_path="light")
    evs = list(cat.build("light", {"shutter": "open", "led_mode": "dc", "led_v": 1.04},
                         ctx, b.rig))
    assert b.sim.bench.shutter_open and b.sim.bench.led_mode == "DC"
    assert b.sim.bench.led_drive_v == 1.04 and b.sim.led.output_enabled
    state = [e for e in evs if isinstance(e, E.InstrumentState)][-1]
    assert state.values["shutter"] == "open" and state.values["illumination"] == "light"
    assert state.values["led_level_v"] == 1.04
    notice = [e for e in evs if isinstance(e, E.Notice)][-1]
    assert "LED DC 1.04 V" in notice.text and "shutter open" in notice.text
    assert "light reaching the sample" in notice.text

    # And the other way: the shutter alone, the generator left at its thermal
    # steady state -- the 2026-09-02 operator instruction, as a node.
    evs = list(cat.build("light", {"shutter": "shut"}, ctx, b.rig))
    assert not b.sim.bench.shutter_open
    assert b.sim.bench.led_mode == "DC" and b.sim.led.output_enabled, "generator untouched"
    assert "dark at the sample" in [e for e in evs if isinstance(e, E.Notice)][-1].text


def test_light_needs_only_the_half_it_is_actually_setting():
    """The module exists so either half can be left alone, so a shutter-only
    node on a bench with no LED is runnable and must not be blocked by one --
    `bench.instrument` would otherwise refuse a pipeline `_build_light` is
    perfectly happy to run."""
    cat = catalogue()
    blind = {"unavailable": {"led": "no such resource", "shutter": "no DIO"}}
    codes = lambda p: [n["code"] for n in cat.needs("light", p, bench=blind)]   # noqa: E731
    assert codes({"shutter": "open", "led_mode": "leave"}) == ["shutter"]
    assert codes({"shutter": "leave", "led_mode": "dc"}) == ["led"]
    assert sorted(codes({"shutter": "open", "led_mode": "dc"})) == ["led", "shutter"]
    assert codes({"shutter": "leave", "led_mode": "leave"}) == [], "a node that sets nothing needs nothing"


def test_light_refuses_before_it_touches_anything(tmp_path):
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, node_path="light")
    with pytest.raises(ModuleError, match="nothing to do"):
        cat.build("light", {}, ctx, b.rig)
    with pytest.raises(ModuleError, match="threshold"):
        cat.build("light", {"led_mode": "pulse", "led_v": 1.02, "led_low_v": 1.2},
                  ctx, b.rig)
    assert b.sim.bench.led_mode != "PULSE", "a refused node touched nothing"
    bare = Bench.build_simulated(RigConfig(), seed=3)
    bare.rig.shutter = None
    with pytest.raises(ModuleError, match="no shutter"):
        cat.build("light", {"shutter": "open"}, ctx, bare.rig)

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
    assert sim.led.output_enabled and sim.bench.led_mode == "DC", (
        "left at DC on the way out; the shutter, shut, is the light switch")
    assert not sim.bench.shutter_open
    assert "1060mVLED" in os.path.basename(ctx.folders[0])

    ctx = make_ctx(tmp_path / "range", node_path="jv_bace")
    labels = [ev.label for ev in cat.build(
        "jv_bace", {"step_v": 0.05, "led_start_v": 1.02, "led_stop_v": 1.06,
                    "led_step_v": 0.04, "dark": False}, ctx, b.rig)
        if isinstance(ev, JVCurveDone)]
    assert labels == ["1.02 V", "1.06 V"]
    assert "LED" not in os.path.basename(ctx.folders[0]), "two levels: no single level to name"
    before = (sim.bench.led_mode, sim.bench.led_drive_v, sim.led.output_enabled)
    with pytest.raises(ModuleError, match="^jv_bace: led_step_v: must be positive"):
        cat.build("jv_bace", {"led_v": None, "led_start_v": 1.02, "led_stop_v": 1.06,
                              "led_step_v": 0.0}, ctx, b.rig)
    assert (sim.bench.led_mode, sim.bench.led_drive_v, sim.led.output_enabled) == before, (
        "refused before anything was touched")


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
    assert sim.led.output_enabled and sim.bench.led_mode == "PULSE", (
        "the LED keeps pulsing on the way out (2026-09-02); the shutter is the light switch")
    assert not sim.bench.bias_output
    assert not sim.bench.shutter_open
    assert ctx.voc is voc

    name = os.path.basename(ctx.folders[0])
    assert name.startswith("s4_SIM_a_290K_1020mVLED_906mVVOC_offsetcorr_"), name
    files = os.listdir(ctx.folders[0])
    assert any(f.startswith("1_averagesQ") for f in files)
    assert any(f.startswith("run") and f.endswith(".h5") for f in files)

    # No temperature node ran, so the 290 in that name is the session's typed
    # number and nothing more. The name cannot say that; the file does.
    from bace.storage.hdf5 import read_run
    h5 = [f for f in files if f.startswith("run") and f.endswith(".h5")][0]
    meta = read_run(os.path.join(ctx.folders[0], h5))["metadata"]
    assert (meta["temperature_k"], meta["temperature_how"], meta["temperature_source"]) == \
        (pytest.approx(290.0), "typed", "")

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
    # set to pulse), then the settle on the power meter, then the engine.
    first = next(gen)
    assert isinstance(first, E.InstrumentState) and first.values["led_output"] == "ON"
    assert first.values["led_mode"] == "PULSE"
    settled = next(gen)
    assert isinstance(settled, E.Notice) and settled.text.startswith("LED settled in")
    assert isinstance(next(gen), E.InstrumentState)
    assert isinstance(next(gen), E.RunStarted)
    gen.close()
    assert b.sim.led.output_enabled and not b.sim.bench.shutter_open


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
    assert b.sim.led.output_enabled and not b.sim.bench.shutter_open
    assert os.path.isdir(ctx.folders[0])


def test_walking_away_mid_run_shuts_the_shutter_and_leaves_the_led_pulsing(tmp_path):
    """The unwind: bias off, shutter shut, and the 33220A left exactly as the
    run set it. Switching it off here was what the operator asked to stop
    (2026-09-02): the next module then waits for the LED all over again."""
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.02))
    gen = cat.build("bace", {**FAST, "n_loops": 5, "centre_on_voc": True, "led_v": 1.02},
                    ctx, b.rig)
    for ev in gen:
        if isinstance(ev, E.StepDone):
            break
    assert b.sim.led.output_enabled and b.sim.bench.led_mode == "PULSE"
    gen.close()
    assert b.sim.led.output_enabled and b.sim.bench.led_mode == "PULSE"
    assert b.sim.led.last_levels == (1.02, 0.4)
    assert not b.sim.bench.bias_output and not b.sim.bench.shutter_open


def test_walking_away_during_the_led_settle_still_shuts_the_shutter(tmp_path):
    """The settle opens the shutter before the scan exists, so the scan's own
    finally cannot cover a walk-away there; the module's does."""
    b = bench()
    cat = catalogue()
    ctx = make_ctx(tmp_path, voc=voc_at(1.02))
    gen = cat.build("bace", {**FAST, "n_loops": 1, "centre_on_voc": True, "led_v": 1.02},
                    ctx, b.rig)
    first = next(gen)                                       # the LED read-back
    assert isinstance(first, E.InstrumentState) and "led_output" in first.values
    settled = next(gen)
    assert isinstance(settled, E.Notice) and b.sim.bench.shutter_open, "open for the meter"
    gen.close()
    assert not b.sim.bench.shutter_open and b.sim.led.output_enabled
    assert b.sim.bench.shots == 0 and ctx.folders == []


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
    with pytest.raises(ModuleError, match="^jv: step_v"):
        cat.build("jv", {"step_v": 0.0}, ctx, b.rig)
    bare = Bench.build_simulated(RigConfig())
    bare.rig.smu = None
    with pytest.raises(ModuleError, match="needs a SourceMeter"):
        cat.build("jv", {}, ctx, bare.rig)
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
    ctx = make_ctx(tmp_path, node_path="jv")
    list(cat.build("jv", {"step_v": 0.1, "smu_current_compliance_a": 0.02,
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


class _Led33220A:
    """A scripted 33220A for the read-back tests: takes every command, logs
    it, and answers `read_state` with whatever `state` says it holds."""
    output_enabled = True
    mode = "PULSE"

    def __init__(self, **state):
        self.log = []
        self.state = {"output": True, "polarity": "INV", "mode": "PULSE", "shape": "PULS",
                      "high_v": 1.02, "low_v": 0.4, "frequency_hz": 500.0,
                      "sync_output": True, **state}
        self.error_queue: list[str] = []

    def set_pulse(self, *a, **k): self.log.append("set_pulse")
    def set_dc(self, *a, **k): self.log.append("set_dc")
    def enable_output(self, on=True): self.log.append(f"enable({on})")
    def disable_output(self): self.log.append("enable(False)")
    def off(self): self.log.append("off")
    def set_polarity(self, inverted): pass
    def polarity(self): return "INV"
    def read_state(self): return dict(self.state)
    def errors(self): return list(self.error_queue)


def test_bace_refuses_a_33220a_that_did_not_take_pulse_mode_or_its_output(tmp_path):
    """The 33220A is the master clock: in DC, or with the output off, nothing
    arms the 81150A and the scope has no sync. The first real service run
    (session 115857) had no read-back of either generator in its file; now the
    33220A is read back after being set, written into the file, and a
    generator that answers OFF or DC stops the module before the scan -- and
    before the settle, so the refusal does not wait a minute on a meter
    reading stable darkness."""
    from bace.service.modules import ModuleError, _led_problems, _read_led_state

    led = _Led33220A(output=False, mode="DC", shape="DC")
    led.error_queue = ['-221,"Settings conflict"']
    state = _read_led_state(led)
    assert state["output"] == "OFF" and state["mode"] == "DC" and "Settings conflict" in state["errors"]
    assert state["sync_output"] == "ON"
    problems = _led_problems(state, {"frequency": 500.0})
    assert any("OFF" in p for p in problems)
    assert any("DC" in p for p in problems)
    assert any("Settings conflict" in p for p in problems)
    assert not any("Sync output" in p for p in problems), "the Sync answered ON"
    # NORM polarity is the chain card's warn, never a refusal here
    assert not any("POL" in p for p in problems)

    b = bench()
    b.rig.led = led
    cat = catalogue()
    ctx = make_ctx(tmp_path)
    gen = cat.build("bace", {**FAST, "centre_on_voc": False, "axis_start": 0.9,
                             "axis_stop": 0.9, "n_loops": 1}, ctx, b.rig)
    first = next(gen)
    assert first.values["led_output"] == "OFF" and first.values["led_mode"] == "DC"
    with pytest.raises(ModuleError, match="33220A is not driving the LED"):
        next(gen)
    assert b.sim.bench.shots == 0, "nothing was acquired"
    assert not b.sim.bench.shutter_open, "refused before the shutter was opened for the settle"
    assert "off" not in led.log, "the module leaves the generator alone; the shutter is the light switch"


def test_bace_refuses_a_33220a_whose_sync_output_is_off(tmp_path):
    """The Sync connector has its own front-panel key. With it off the 33220A
    pulses the LED as asked, the read-back says PULSE, ON, 500 Hz -- and
    nothing arms the 81150A, so the scope triggers on nothing and the scan
    measures noise. `OUTP:SYNC?` is the only place the chain says so."""
    from bace.service.modules import ModuleError, _led_problems, _read_led_state

    led = _Led33220A(sync_output=False)
    state = _read_led_state(led)
    assert state["sync_output"] == "OFF" and state["output"] == "ON" and state["mode"] == "PULSE"
    problems = _led_problems(state, {"frequency": 500.0})
    assert problems == ["Sync output is OFF (OUTP:SYNC? = 0): nothing arms the 81150A"]

    b = bench()
    b.rig.led = led
    cat = catalogue()
    ctx = make_ctx(tmp_path)
    gen = cat.build("bace", {**FAST, "centre_on_voc": False, "axis_start": 0.9,
                             "axis_stop": 0.9, "n_loops": 1}, ctx, b.rig)
    first = next(gen)
    assert first.values["led_sync_output"] == "OFF", "in the file, whatever happens next"
    with pytest.raises(ModuleError, match=r"Sync output is OFF \(OUTP:SYNC\? = 0\)"):
        next(gen)
    assert b.sim.bench.shots == 0 and ctx.folders == []


def test_a_simulated_33220a_that_answers_nothing_definite_is_not_refused(tmp_path):
    """`?` is not a problem: the simulator has no `read_state`, and a driver
    that cannot answer is not a generator that said no. The Sync included:
    only the real driver asks `OUTP:SYNC?`."""
    from bace.service.modules import _led_problems, _read_led_state
    assert _led_problems({"output": "?", "mode": "?", "frequency_hz": "?", "polarity": "?",
                          "sync_output": "?"}, {"frequency": 500.0}) == []
    assert _read_led_state(bench().sim.led)["sync_output"] == "?"


# -- the LED settle after DC -> pulse ---------------------------------------------
class _Meter:
    """A power meter that answers a scripted sequence of readings, the last
    one repeated for ever; `raises` makes every read fail."""

    def __init__(self, readings=(), raises=None):
        self.readings = list(readings)
        self.raises = raises
        self.reads = 0

    def read_power(self):
        self.reads += 1
        if self.raises is not None:
            raise self.raises
        i = min(self.reads, len(self.readings)) - 1
        return float(self.readings[i])

    def read_statistics(self, n):
        return self.read_power(), 0.0

    def set_wavelength(self, nm):
        pass


def _settle_transcript(b, tmp_path, **params):
    """Run a one-shot bace and return its events, the settle's notices and
    read-back, how many settle polls were slept, and every sleep. The
    scan's own per-step intensity read is off so a fake meter's `reads`
    counts the settle's polls and nothing else."""
    from bace.service.modules import LED_SETTLE_POLL_S
    cat = catalogue()
    slept: list[float] = []
    ctx = make_ctx(tmp_path, voc=voc_at(1.02), sleep=slept.append)
    evs = list(cat.build("bace", {**FAST, "n_loops": 1, "centre_on_voc": True, "led_v": 1.02,
                                  "read_intensity": False, **params}, ctx, b.rig))
    notices = [e for e in evs if isinstance(e, E.Notice) and "LED" in e.text]
    states = [e for e in evs if isinstance(e, E.InstrumentState) and "led_power_w" in e.values]
    polls = sum(1 for s in slept if s == LED_SETTLE_POLL_S)
    return evs, notices, states, polls, slept


def test_the_led_settle_reads_the_simulated_meter_behind_the_open_shutter(tmp_path):
    """The rig: LED -> shutter -> fibre -> splitter -> (meter + device), so the
    meter only sees light with the shutter open. The module opens it, polls
    every 0.5 s, and settles once a FULL FLAT SPAN has been seen: 10 s, not a
    count of readings -- three flat readings in 1.5 s passed on the rig at
    14:52 (2026-09-02) while the LED went on to droop 18 % over the next
    40 s. With the simulated LED flat from the start that is exactly one
    span's worth of polls, said on the stream and written into the file."""
    from bace.service.modules import (LED_SETTLE_POLL_S, LED_SETTLE_SPAN_S,
                                      _SPAN_POLLS)
    b = bench()
    seen = {}

    real = b.sim.power.read_power

    def watched():
        seen.setdefault("shutter", b.sim.bench.shutter_open)
        seen.setdefault("led", (b.sim.bench.led_mode, b.sim.bench.led_drive_v))
        return real()

    b.sim.power.read_power = watched
    evs, notices, states, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=0.0)
    assert seen == {"shutter": True, "led": ("PULSE", 1.02)}, "read under the pulse, shutter open"
    assert polls == _SPAN_POLLS == 20
    expected = b.sim.bench.device.led_current(1.02) * b.sim.power.w_per_unit
    [notice] = notices
    assert notice.level == "info"
    assert notice.text == f"LED settled in {_SPAN_POLLS * LED_SETTLE_POLL_S:g} s at " \
                          f"{float(states[0].values['led_power_w']):.2e} W " \
                          f"({_SPAN_POLLS} readings over {LED_SETTLE_SPAN_S:g} s within 2 %)"
    assert states[0].values["led_settle_s"] == "10"
    assert float(states[0].values["led_power_w"]) == pytest.approx(expected, rel=0.02)
    kinds = [type(e).__name__ for e in evs[:4]]
    assert kinds == ["InstrumentState", "Notice", "InstrumentState", "RunStarted"], (
        "read-back, settle, then the engine")
    assert isinstance(evs[-1], E.RunFinished)


def test_the_led_settle_waits_for_a_drifting_meter_to_agree_and_for_led_settle_s(tmp_path):
    """A LED still warming after DC -> pulse: the meter reads a rising value,
    and the run must not start until a FULL SPAN (10 s) of readings agrees
    within the tolerance -- on the poll clock, so the number the notice
    quotes is polls times 0.5 s, not wall time. The rig at 14:52 drooped
    about 0.25 %/s, which is ~2.5 % inside any 10 s window: outside the 2 %
    tolerance, so exactly the drift the span exists to catch."""
    from bace.service.modules import _SPAN_POLLS
    b = bench()
    # six drifting polls (2 % step each), then flat: the span is clean of
    # the drift only once the last drifting reading has left it.
    drifting = [1.00e-4, 1.02e-4, 1.04e-4, 1.06e-4, 1.08e-4, 1.10e-4]
    b.rig.power = _Meter(drifting + [1.130e-4] * 40)
    evs, notices, states, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=0.0)
    assert polls == b.rig.power.reads == len(drifting) + _SPAN_POLLS
    assert notices[0].text.startswith(f"LED settled in {polls * 0.5:g} s at 1.13e-04 W")
    assert states[0].values == {"led_power_w": "1.13e-04",
                                "led_settle_s": f"{polls * 0.5:g}"}

    # led_settle_s below the span changes nothing (the span is the floor);
    # above it, the extra polls are waited out
    b = bench()
    b.rig.power = _Meter([1.0e-4])
    evs, notices, states, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=12.0)
    assert polls == 24 and states[0].values["led_settle_s"] == "12"
    assert notices[0].text.startswith("LED settled in 12 s")

    # a wider tolerance takes the same slow drift as flat: one span, no more
    b = bench()
    b.rig.power = _Meter(drifting + [1.10e-4] * 40)
    _, notices, _, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=0.0,
                                                 led_settle_tolerance=0.15)
    assert polls == _SPAN_POLLS
    assert notices[0].text.startswith(f"LED settled in {_SPAN_POLLS * 0.5:g} s")


def test_a_meter_that_never_settles_is_given_up_on_with_a_warning_and_the_run_goes_on(tmp_path):
    """A bace that never starts is worse than one that says its light may not
    have been steady: at `led_settle_max_s` the wait ends with a warning that
    quotes the last readings, and the scan runs."""
    from bace.service.modules import LED_SETTLE_POLL_S

    class Drifting(_Meter):
        def read_power(self):
            self.reads += 1
            return 1.0e-4 * 1.05 ** self.reads      # 5 % per poll, for ever

    b = bench()
    b.rig.power = Drifting()
    evs, notices, states, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=0.0)
    assert polls * LED_SETTLE_POLL_S == 60.0 and b.rig.power.reads == 120
    [warning] = notices
    assert warning.level == "warning"
    assert warning.text.startswith("LED did not stabilise within 60 s: last readings ")
    assert warning.text.endswith(" W; going on")
    assert states[0].values["led_settle_s"] == "60"
    assert isinstance(evs[-1], E.RunFinished), "the scan ran regardless"
    assert b.sim.bench.shots > 0

    b = bench()
    b.rig.power = Drifting()
    _, notices, states, polls, _ = _settle_transcript(b, tmp_path, led_settle_s=0.0,
                                                      led_settle_max_s=5.0)
    assert polls == 10 and "within 5 s" in notices[0].text
    assert states[0].values["led_settle_s"] == "5"


def test_a_meter_that_raises_is_treated_as_absent_for_the_settle(tmp_path):
    """The 1918-C console down must not stop a scan that does not need it:
    one warning, then the fixed `led_settle_s`, as on a bench with no meter."""
    b = bench()
    b.rig.power = _Meter(raises=OSError("console not answering"))
    evs, notices, states, polls, slept = _settle_transcript(b, tmp_path, led_settle_s=2.0)
    [warning] = notices
    assert warning.level == "warning" and "console not answering" in warning.text
    assert "waiting the fixed 2 s" in warning.text
    assert polls == 1 and 1.5 in slept, "the poll spent, then the rest of the fixed wait"
    assert states == [], "no reading, no read-back"
    assert isinstance(evs[-1], E.RunFinished)

    b = bench()
    b.rig.power = None
    evs, notices, states, polls, slept = _settle_transcript(b, tmp_path, led_settle_s=2.0)
    assert notices == [] and states == [] and polls == 0 and 2.0 in slept, "no meter: the fixed wait"
    assert isinstance(evs[-1], E.RunFinished)


def test_the_settle_parameters_are_on_the_bace_card_with_provenance(tmp_path):
    """Plain parameters with defaults: run.toml has no key for them, the
    journal's last-used and the edited layer apply as to any other."""
    cat = catalogue(history=FakeHistory(last_used={"bace": {"led_settle_max_s": 30.0}}))
    ps = cat.param_set("bace")
    assert ps.get("led_settle_max_s") == ParamValue(30.0, Source.LAST_USED, "previous run")
    assert ps.get("led_settle_tolerance") == ParamValue(0.02, Source.DEFAULT, "")
    specs = {s.name: s for s in cat._params["bace"]}
    assert specs["led_settle_max_s"].unit == "s" and specs["led_settle_max_s"].group == "illumination"
    assert specs["led_settle_tolerance"].group == "illumination"
    cat.edit("bace", {"led_settle_tolerance": 0.05})
    assert cat.param_set("bace").get("led_settle_tolerance").value == 0.05
    with pytest.raises(ModuleError, match="led_settle_tolerance"):
        cat.edit("bace", {"led_settle_tolerance": -1.0})
