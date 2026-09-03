"""The pipeline: tree in, schedule and verdicts out, nothing touched.

`pipeline.py` is pure logic, so these tests need no simulator -- but they do
need a catalogue, and the real one lives in `service.modules`. The fake below
implements the shared interface (`names/spec/param_set/estimate_s`) over the
*real* dataclasses (`Axis`, `ScanSpec`, `RunConfig`, `JVConfig`,
`SourceMeterConfig`) so the parameter names are the contract's and a test
that passes here passes against the catalogue. When `service.modules` is
present its `ModuleSpec`/`VocSource` are used; until then local stand-ins
with the same fields.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

import pytest

from bace.core.axis import Axis, ScanSpec
from bace.drivers.keithley2400 import SourceMeterConfig
from bace.experiment.jv import JVConfig
from bace.experiment.rig import RigConfig
from bace.experiment.transient import RunConfig
from bace.params import ParamSet, ParamSpec, Source, specs_from_dataclass
from bace.service import pipeline as P

try:
    from bace.service.modules import ModuleError, ModuleSpec, VocSource
except ImportError:                                   # modules.py not landed yet
    @dataclass(frozen=True)
    class ModuleSpec:
        name: str
        title: str
        kind: str
        status: str
        relay: str | None
        provides_voc: bool
        needs_voc_param: str | None
        led_params: tuple[str, ...]
        groups: tuple[str, ...]

    @dataclass(frozen=True)
    class VocSource:
        value: float
        led_v: float
        run_id: str
        node_path: str
        how: str
        ts: float

    class ModuleError(ValueError):
        pass


RUN_CHOICES = {"t0_int_reference": ("record", "trigger", "pulse"),
               "dark_reference": ("translated", "same"),
               "output_polarity": ("auto", "NORM", "INV", "leave")}
RIG = RigConfig()
NOW = 1_788_390_000.0


# -- the fake catalogue -----------------------------------------------------
def _smu_specs() -> list[ParamSpec]:
    return specs_from_dataclass(
        SourceMeterConfig, group="sourcemeter",
        exclude=("settle_jsc_ms", "settle_voc_ms", "settle_jsat_ms", "averaging",
                 "terminals", "four_wire"),
        rename={"current_compliance_a": "smu_current_compliance_a",
                "voltage_compliance_v": "smu_voltage_compliance_v",
                "nplc": "smu_nplc"},
        units={"current_compliance_a": "A", "voltage_compliance_v": "V"})


def _jv_specs(*, light: bool) -> list[ParamSpec]:
    exclude = ("led_levels_v",) if light else ("led_levels_v", "dark", "led_settle_s")
    specs = specs_from_dataclass(JVConfig, group="jv", exclude=exclude,
                                 units={"start_v": "V", "stop_v": "V", "step_v": "V",
                                        "settle_s": "s", "led_settle_s": "s"})
    if light:
        specs += [ParamSpec("led_start_v", "float", 1.020, unit="V", group="led"),
                  ParamSpec("led_stop_v", "float", 1.020, unit="V", group="led"),
                  ParamSpec("led_step_v", "float", 0.020, unit="V", group="led"),
                  ParamSpec("led_low_v", "float", 0.4, unit="V", group="led")]
    return specs + _smu_specs()


def _bace_specs() -> list[ParamSpec]:
    axis = specs_from_dataclass(
        Axis, group="axis",
        rename={"name": "axis_name", "start": "axis_start", "stop": "axis_stop",
                "step": "axis_step"},
        defaults={"name": "vpre", "start": 0.0, "stop": 0.0, "centre_on_voc": True})
    pinned = specs_from_dataclass(ScanSpec, group="pinned", exclude=("axis",),
                                  units={"vpre": "V", "vcoll": "V", "delay_ns": "ns"})
    run = specs_from_dataclass(RunConfig, group="acquisition", choices=RUN_CHOICES)
    extra = [
        ParamSpec("store_shots", "bool", False, group="output"),
        ParamSpec("led_v", "float", 1.020, unit="V", group="illumination"),
        ParamSpec("led_low_v", "float", 0.4, unit="V", group="illumination"),
        ParamSpec("voc", "float", None, unit="V", nullable=True, group="illumination"),
        ParamSpec("measure_dc", "bool", False, group="illumination"),
        ParamSpec("v_sat", "float", -1.0, unit="V", group="illumination"),
    ]
    return axis + pinned + run + extra + _smu_specs()


SPECS = {
    "jv": ModuleSpec("jv", "jv", "measurement", "built", "dc", False,
                     None, (), ("jv", "sourcemeter")),
    "jv_bace": ModuleSpec("jv_bace", "jv_bace", "measurement", "built", "dc", True,
                          None, ("led_start_v", "led_stop_v", "led_step_v", "led_low_v"),
                          ("jv", "led", "sourcemeter")),
    "bace": ModuleSpec("bace", "bace", "measurement", "built", "transient", False,
                       "centre_on_voc", ("led_v", "led_low_v"),
                       ("axis", "pinned", "acquisition", "illumination", "sourcemeter")),
    "power": ModuleSpec("power", "power", "observer", "built", None, False, None, (), ()),
    "temperature": ModuleSpec("temperature", "temperature", "utility", "partial", None,
                              False, None, (), ()),
    "light": ModuleSpec("light", "light", "utility", "built", None, False, None,
                        ("led_v", "led_low_v"), ("illumination", "timing")),
    "park": ModuleSpec("park", "park", "utility", "built", None, False, None, (), ()),
    "wait": ModuleSpec("wait", "wait", "utility", "built", None, False, None, (), ()),
    "note": ModuleSpec("note", "note", "utility", "built", None, False, None, (), ()),
}


def _specs_for(name: str) -> list[ParamSpec]:
    if name == "jv":
        return _jv_specs(light=False)
    if name == "jv_bace":
        return _jv_specs(light=True)
    if name == "bace":
        return _bace_specs()
    if name == "power":
        return [ParamSpec("wavelength_nm", "float", 530.0, unit="nm"),
                ParamSpec("samples", "int", 0)]
    if name == "temperature":
        return [ParamSpec("setpoint_k", "float", 295.0, unit="K"),
                ParamSpec("tolerance_k", "float", 0.2, unit="K"),
                ParamSpec("hold_s", "float", 60.0, unit="s"),
                ParamSpec("timeout_s", "float", 1800.0, unit="s")]
    if name == "light":
        return [ParamSpec("shutter", "enum", "leave", choices=("open", "shut", "leave")),
                ParamSpec("led_mode", "enum", "leave",
                          choices=("dc", "pulse", "off", "leave")),
                ParamSpec("led_v", "float", 1.0, unit="V"),
                ParamSpec("led_low_v", "float", 0.4, unit="V"),
                ParamSpec("pulse_frequency_hz", "float", 500.0, unit="Hz"),
                ParamSpec("duty_percent", "float", 50.0, unit="%"),
                ParamSpec("settle_s", "float", 0.0, unit="s")]
    if name == "wait":
        return [ParamSpec("seconds", "float", 1.0, unit="s")]
    if name == "note":
        return [ParamSpec("text", "str", "")]
    return []


class FakeCatalogue:
    """The shared interface, minimally: fresh ParamSets over the real
    dataclasses, an `edited` layer standing in for the bench, and the
    contract's cost formulas."""

    def __init__(self, *, history=None, edited: dict[str, dict] | None = None):
        self.history = history
        self.edited = edited or {}

    def names(self) -> list[str]:
        return list(SPECS)

    def spec(self, name: str) -> ModuleSpec:
        try:
            return SPECS[name]
        except KeyError:
            raise ModuleError(f"{name}: no such module") from None

    def param_set(self, name: str) -> ParamSet:
        self.spec(name)
        ps = ParamSet(_specs_for(name))
        if name in self.edited:
            ps.set_layer(Source.EDITED, self.edited[name], "bench")
        return ps

    def estimate_s(self, name: str, params: dict) -> float:
        t_shot = P.shot_time(self.history)[0]
        if name == "bace":
            axis = Axis(params["axis_name"], params["axis_start"], params["axis_stop"],
                        params["axis_step"], params["centre_on_voc"])
            return params["n_loops"] * axis.n_points * t_shot
        if name.startswith("jv"):
            levels = P.light_levels(params) if name == "jv_bace" else ()
            cfg = JVConfig(start_v=params["start_v"], stop_v=params["stop_v"],
                           step_v=params["step_v"], settle_s=params["settle_s"],
                           both_directions=params["both_directions"],
                           dark=params.get("dark", True), led_levels_v=tuple(levels),
                           led_settle_s=params.get("led_settle_s", 0.0))
            n_points = int(cfg.points().size)
            n_curves = ((1 if cfg.dark else 0) + len(levels)) * (2 if cfg.both_directions else 1)
            return n_curves * (n_points * cfg.settle_s + n_points * 0.05 + cfg.led_settle_s)
        if name == "temperature":
            return float(params["hold_s"])
        if name == "wait":
            return float(params["seconds"])
        return 0.0

    def estimate_text(self, name: str, params: dict) -> str:
        return f"{self.estimate_s(name, params):.0f} s"


class FakeHistory:
    def __init__(self, settle: dict[float, list[float]] | None = None,
                 shot: float | None = None):
        self._settle = settle or {}
        self._shot = shot

    def last_used_params(self, module):
        return None

    def settle_history(self):
        return dict(self._settle)

    def shot_time_s(self, module="bace"):
        return self._shot


# -- trees -------------------------------------------------------------------
TEMPERATURES = [295, 290, 280, 270, 260, 250, 240, 230, 220]


def canonical(n_loops: int = 100, **bace_params) -> dict:
    """The design's tree: 9 temperatures x 5 levels x [jv_bace, bace]."""
    return {"kind": "loop", "loop": "temperature", "label": "T",
            "values_k": list(TEMPERATURES), "tolerance_k": 0.2, "hold_s": 60,
            "timeout_s": 1800,
            "children": [
                {"kind": "loop", "loop": "illumination", "led_start_v": 1.010,
                 "led_stop_v": 1.030, "led_step_v": 0.005, "led_low_v": 0.4,
                 "led_settle_s": 2.0,
                 "children": [
                     {"kind": "module", "module": "jv_bace", "params": {}},
                     {"kind": "module", "module": "bace",
                      "params": {"n_loops": n_loops, **bace_params}}]}]}


def illumination(children, **kw) -> dict:
    return {"kind": "loop", "loop": "illumination", "levels_v": [1.020],
            "led_low_v": 0.4, "led_settle_s": 0.0, "children": children, **kw}


def module(name: str, **params) -> dict:
    return {"kind": "module", "module": name, "params": params}


def bench_snapshot(*, led_pol="INV", arm="EXT", slope="POS", bias_pol="NORM",
                   bias_on=False, smu_on=False, relay="amplifier",
                   relay_how="readback", power=True, read_at=NOW - 30.0) -> dict:
    """A `/bench` snapshot as the contract draws it. `bias_pol` defaults to
    NORM because that is what the fake's recipe writes (`inverted_output`
    False); the design's INV goes with a recipe that asks for it."""
    return {
        "read_at": read_at,
        "instruments": {
            "relay": {"position": relay, "how": relay_how},
            "bias": {"output": bias_on, "polarity": bias_pol, "arm_source": arm,
                     "arm_slope": slope},
            "smu": {"output": smu_on, "compliance": {"current_a": 0.01, "voltage_v": 2.0}},
            "led": {"output": False, "polarity": led_pol},
            "power": {"available": power, "watts": 1.407e-3},
        },
        "chain": {"read_at": read_at, "items": [
            {"key": "led_polarity", "label": "33220A POL", "value": led_pol,
             "expected": "INV", "level": "ok" if led_pol == "INV" else "warn",
             "fix": "set-33220a-pol-inv"},
            {"key": "bias_arm", "label": "81150A ARM", "value": arm, "expected": "EXT",
             "level": "ok", "fix": "arm-81150a-ext"},
            {"key": "bias_arm_slope", "label": "SLOP", "value": slope, "expected": "POS",
             "level": "ok", "fix": "arm-81150a-ext"},
            {"key": "bias_polarity", "label": "81150A POL", "value": bias_pol,
             "expected": "leave", "level": "info", "fix": None},
        ]},
    }


def validate(tree, *, catalogue=None, bench=None, history=None, session_voc=None,
             power_available=None, rig=RIG) -> P.Validation:
    return P.validate(tree, catalogue or FakeCatalogue(history=history), bench=bench,
                      history=history, session_voc=session_voc, rig_config=rig,
                      power_available=power_available, now=NOW)


def levels(v: P.Validation, code: str) -> list[str]:
    return [c.level for c in v.by_code(code)]


# -- parse_tree ----------------------------------------------------------------
def test_the_canonical_tree_parses_with_a_rounded_level_count():
    tree = P.parse_tree(canonical())
    assert isinstance(tree, P.Loop) and tree.loop == "temperature"
    assert tree.values == tuple(TEMPERATURES)
    ill = tree.children[0]
    assert isinstance(ill, P.Loop)
    # 1.010 -> 1.030 step 0.005 is five levels; truncation would give four.
    assert ill.values == (1.01, 1.015, 1.02, 1.025, 1.03)
    assert ill.params == {"led_low_v": 0.4, "led_settle_s": 2.0}
    assert [c.module for c in ill.children] == ["jv_bace", "bace"]
    assert ill.children[1].key == "temperature/illumination/bace"


def test_temperature_ranges_use_the_same_rule():
    tree = P.parse_tree({"kind": "loop", "loop": "temperature", "start_k": 295,
                         "stop_k": 220, "step_k": 25, "children": [module("note")]})
    assert tree.values == (295.0, 270.0, 245.0, 220.0)


@pytest.mark.parametrize("bad, match", [
    ({"kind": "pipeline"}, "kind"),
    ({"kind": "loop", "loop": "humidity", "children": [module("note")]}, "humidity"),
    ({"kind": "loop", "loop": "repeat", "count": 2, "children": []}, "at least one child"),
    ({"kind": "loop", "loop": "repeat", "count": 0, "children": [module("note")]}, "count"),
    ({"kind": "loop", "loop": "illumination", "levels_v": [], "children": [module("note")]},
     "non-empty"),
    ({"kind": "loop", "loop": "illumination", "levels_v": [1.02], "led_start_v": 1.0,
      "children": [module("note")]}, "not both"),
    ({"kind": "loop", "loop": "illumination", "led_start_v": 1.0, "led_stop_v": 1.1,
      "led_step_v": 0.0, "children": [module("note")]}, "positive"),
    ({"kind": "loop", "loop": "temperature", "values_k": [295, "cold"],
      "children": [module("note")]}, "finite number"),
    ({"kind": "loop", "loop": "temperature", "values_k": [295], "hold": 5,
      "children": [module("note")]}, "unknown key"),
    ({"kind": "module", "module": "bace", "parms": {}}, "unknown key"),
    ({"kind": "module", "module": "bace", "params": [1, 2]}, "params"),
])
def test_malformed_trees_are_refused_by_name(bad, match):
    with pytest.raises(P.TreeError, match=match):
        P.parse_tree(bad)


def test_a_tree_error_names_the_node():
    bad = canonical()
    bad["children"][0]["children"][1]["params"] = "n_loops=3"
    with pytest.raises(P.TreeError) as exc:
        P.parse_tree(bad)
    assert exc.value.node_path == "temperature/illumination/bace"


def test_the_root_name_is_the_folder_stem_and_nowhere_else():
    tree = P.parse_tree({**canonical(), "name": "Txill"})
    assert tree.name == "Txill"
    nested = canonical()
    nested["children"][0]["name"] = "no"
    with pytest.raises(P.TreeError, match="unknown key"):
        P.parse_tree(nested)


def test_the_tree_round_trips_through_as_wire():
    tree = P.parse_tree(canonical())
    again = P.parse_tree(json.loads(json.dumps(tree.as_wire())))
    assert again == tree


# -- node paths and the schedule ---------------------------------------------
def test_the_canonical_tree_resolves_to_ninety_module_steps():
    sched = P.resolve(P.parse_tree(canonical()), FakeCatalogue())
    modules = sched.modules
    assert len(modules) == 90
    assert modules[0].node_path == "T=295K/led=1.010V/jv_bace"
    assert modules[1].node_path == "T=295K/led=1.010V/bace"
    assert modules[-1].node_path == "T=220K/led=1.030V/bace"
    assert sched.node_paths == [s.node_path for s in modules]
    kinds = [s.kind for s in sched.steps]
    assert kinds[:4] == ["loop-enter", "loop-enter", "module", "module"]
    assert len(sched.steps) == 9 + 45 + 90 + 45 + 9
    assert sched.counters == {"temperatures": 9, "levels": 5, "modules": 90,
                              "shots": 9 * 5 * 100}


def test_duplicate_siblings_and_repeats_are_numbered():
    tree = P.parse_tree({"kind": "loop", "loop": "repeat", "count": 2,
                         "children": [module("bace", measure_dc=True),
                                      module("bace", measure_dc=True),
                                      module("note")]})
    sched = P.resolve(tree, FakeCatalogue())
    assert sched.node_paths == ["rep=1/bace", "rep=1/bace#2", "rep=1/note",
                                "rep=2/bace", "rep=2/bace#2", "rep=2/note"]


def test_a_manual_run_is_a_single_node_with_the_module_name_as_path():
    sched = P.resolve(P.parse_tree(module("bace", measure_dc=True)), FakeCatalogue())
    assert [s.node_path for s in sched.steps] == ["bace"]
    assert sched.counters == {"temperatures": 0, "levels": 0, "modules": 1, "shots": 1}


def test_the_illumination_loop_owns_led_v_and_jv_bace_hands_voc_to_bace():
    sched = P.resolve(P.parse_tree(canonical()), FakeCatalogue(
        edited={"bace": {"led_v": 1.060, "voc": 0.90}}))       # stale bench values
    jv, bace = sched.modules[0], sched.modules[1]
    for name in ("led_start_v", "led_stop_v"):
        assert jv.params[name].value == 1.010
        assert jv.params[name].source is Source.INHERITED
        assert jv.params[name].detail == "illumination loop"
    assert jv.params["led_low_v"].value == 0.4
    assert jv.params["led_low_v"].source is Source.INHERITED
    assert bace.params["led_v"].value == 1.010
    assert bace.params["led_v"].source is Source.INHERITED
    voc = bace.params["voc"]
    assert voc.source is Source.DERIVED and voc.value is None
    assert voc.detail == "jv_bace T=295K/led=1.010V/jv_bace"
    assert bace.detail["voc"] == {"how": "jv_bace", "led_v": 1.010,
                                  "node_path": "T=295K/led=1.010V/jv_bace", "value": None}
    # the typed 0.90 is not what the run will use, and the wire says so
    assert bace.params["n_loops"].value == 100
    assert bace.params["n_loops"].source is Source.EDITED
    assert bace.params["n_loops"].detail == "pipeline node"
    later = sched.modules[3]                                    # T=295K/led=1.015V/bace
    assert later.params["voc"].detail == "jv_bace T=295K/led=1.015V/jv_bace"


def test_relay_transitions_are_marked_at_every_jv_bace_boundary():
    sched = P.resolve(P.parse_tree(canonical()), FakeCatalogue())
    mods = sched.modules
    assert [m.relay for m in mods[:4]] == ["dc", "transient", "dc", "transient"]
    assert mods[0].detail["relay_transition"] is False          # nothing before it
    assert all(m.detail["relay_transition"] for m in mods[1:])
    assert mods[1].detail["relay_from"] == "dc"


def test_temperature_steps_need_the_operator_and_carry_the_setpoint():
    sched = P.resolve(P.parse_tree(canonical()), FakeCatalogue())
    enter = sched.steps[0]
    assert enter.kind == "loop-enter" and enter.needs_operator
    assert enter.node_path == "T=295K" and enter.value == 295
    assert enter.detail["setpoint_k"] == 295 and enter.detail["hold_s"] == 60
    assert enter.detail["tolerance_k"] == 0.2 and enter.detail["timeout_s"] == 1800
    assert enter.detail["settle_s"] is None and enter.estimate_s is None
    assert sched.steps[1].kind == "loop-enter" and not sched.steps[1].needs_operator
    assert sched.steps[1].estimate_s == 2.0
    assert sched.modules[1].detail["temperature_k"] == 295.0


def test_an_unknown_module_or_parameter_is_a_tree_error_with_the_path():
    with pytest.raises(P.TreeError) as exc:
        P.resolve(P.parse_tree(illumination([module("jv_bace"), module("bake")])),
                  FakeCatalogue())
    assert exc.value.node_path == "led=1.020V/bake"
    with pytest.raises(P.TreeError, match="n_lops") as exc:
        P.resolve(P.parse_tree(module("bace", n_lops=3)), FakeCatalogue())
    assert exc.value.node_path == "bace"


# -- V_oc sources ------------------------------------------------------------
def test_a_centred_bace_alone_in_an_illumination_loop_is_invalid():
    v = validate(illumination([module("bace")]))
    assert not v.valid
    [bad] = v.by_code("voc.source")
    assert bad.level == "invalid" and bad.node_path == "led=1.020V/bace"
    assert "1.02 V" in bad.text and "measure_dc" in bad.text
    assert v.schedule.modules[0].detail["voc"] is None


def test_measure_dc_is_a_voc_source_of_its_own():
    v = validate(illumination([module("bace", measure_dc=True)]))
    assert v.valid
    step = v.schedule.modules[0]
    assert step.detail["voc"]["how"] == "measure_dc"
    assert step.params["voc"].source is Source.DERIVED
    assert step.params["voc"].detail == "measure_dc on this node"
    [ok] = v.by_code("voc.source")
    assert ok.level == "ok" and "measure_dc" in ok.text


def test_a_bace_not_centred_on_voc_needs_no_source():
    v = validate(illumination([module("bace", centre_on_voc=False)]))
    assert v.valid and levels(v, "voc.source") == ["ok"]


def test_a_jv_bace_at_another_level_does_not_qualify():
    tree = illumination([module("jv_bace"), module("bace")])
    tree["levels_v"] = [1.020, 1.030]
    v = validate(tree)
    assert v.valid
    # each bace takes the jv_bace from its own iteration, never the other one
    for s in v.schedule.modules:
        if s.module == "bace":
            assert s.detail["voc"]["led_v"] == s.detail["led_v"]
            assert s.detail["voc"]["node_path"].startswith(s.node_path.split("/")[0])


def test_a_jv_bace_before_the_loop_serves_the_loop_at_its_levels():
    """A J-V taken at this temperature before the illumination loop is still
    the J-V for this temperature; one taken at another temperature is not."""
    tree = {"kind": "loop", "loop": "temperature", "values_k": [295, 250],
            "children": [module("jv_bace", led_start_v=1.010, led_stop_v=1.030,
                                led_step_v=0.010),
                         {"kind": "loop", "loop": "illumination",
                          "levels_v": [1.010, 1.020, 1.025],
                          "children": [module("bace")]}]}
    v = validate(tree)
    assert not v.valid
    bad = v.by_code("voc.source")
    assert [b.node_path for b in bad] == ["T=295K/led=1.025V/bace"]
    assert bad[0].data["count"] == 2                            # both temperatures
    served = [s for s in v.schedule.modules if s.module == "bace" and s.detail["voc"]]
    assert {s.detail["voc"]["node_path"] for s in served} == {"T=295K/jv_bace",
                                                              "T=250K/jv_bace"}
    assert served[0].node_path.startswith("T=295K") and served[0].detail["voc"]["node_path"] == "T=295K/jv_bace"


def test_the_sessions_voc_serves_a_manual_run_at_the_same_level_only():
    voc = VocSource(value=1.0423, led_v=1.020, run_id="s-002", node_path="jv_bace",
                    how="jv_bace", ts=NOW - 300)
    v = validate(module("bace"), session_voc=voc)
    assert v.valid
    step = v.schedule.modules[0]
    assert step.params["voc"].value == 1.0423
    assert step.params["voc"].source is Source.DERIVED
    assert step.params["voc"].detail == "jv_bace jv_bace (this session)"
    assert step.detail["voc"]["session"] is True

    v = validate(module("bace", led_v=1.030), session_voc=voc)
    assert not v.valid
    assert levels(v, "voc.source") == ["ok"]                    # a source exists...
    [bad] = v.by_code("voc.coupling")                           # ...at the wrong level
    assert bad.level == "invalid" and "1.02" in bad.text and "1.03" in bad.text
    assert bad.data["voc_led_v"] == 1.020 and bad.data["led_v"] == 1.030

    # inside an illumination loop the session's V_oc is never used
    v = validate(illumination([module("bace")]), session_voc=voc)
    assert levels(v, "voc.source") == ["invalid"]
    # nor inside a temperature loop: V_oc at 295 K says nothing about 250 K
    v = validate({"kind": "loop", "loop": "temperature", "values_k": [250],
                  "children": [module("bace")]}, session_voc=voc)
    assert levels(v, "voc.source") == ["invalid"]


def test_a_typed_voc_is_a_warning_not_a_source_the_loop_trusts():
    v = validate(module("bace", voc=0.95))
    assert v.valid
    [w] = v.by_code("voc.typed")
    assert w.level == "warn" and "0.95" in w.text and "typed by hand" in w.text
    assert v.schedule.modules[0].detail["voc"]["how"] == "typed"

    # a jv_bace in scope beats it, and the wire shows derived, not edited
    v = validate(illumination([module("jv_bace"), module("bace", voc=0.95)]))
    assert levels(v, "voc.typed") == ["ok"]
    assert v.schedule.modules[1].params["voc"].source is Source.DERIVED

    # the session's own typed V_oc is flagged the same way
    typed = VocSource(1.0, 1.020, "s-001", "bace", "typed", NOW)
    v = validate(module("bace"), session_voc=typed)
    assert levels(v, "voc.typed") == ["warn"] and "session" in v.by_code("voc.typed")[0].text


# -- the rest of the check catalogue -----------------------------------------
def test_every_check_id_is_reported_in_order_for_a_clean_tree():
    v = validate(canonical(), bench=bench_snapshot())
    assert [c.code for c in v.checks] == list(P.CHECKS)
    by = {c.code: c.level for c in v.checks}
    assert by["temperature.not-wired"] == "warn"
    assert by["trigger.auto"] == "info"                          # run.toml's AUTO
    assert by["intensity.factor"] == "info"
    assert all(lvl == "ok" for code, lvl in by.items()
               if code not in ("temperature.not-wired", "trigger.auto", "intensity.factor"))
    assert v.valid


def test_a_module_typing_led_v_inside_an_illumination_loop_is_invalid():
    v = validate(illumination([module("jv_bace"), module("bace", led_v=1.030)]))
    assert not v.valid
    [bad] = v.by_code("tree.owned-param")
    assert bad.level == "invalid" and bad.node_path == "led=1.020V/bace"
    assert bad.data["params"] == ["led_v"]
    # the loop still won in the schedule: what would run is what the loop says
    assert v.schedule.modules[1].params["led_v"].value == 1.020
    v = validate(illumination([module("jv_bace", led_step_v=0.01), module("bace")]))
    assert levels(v, "tree.owned-param") == ["invalid"]
    v = validate(module("bace", led_v=1.030, measure_dc=True))  # outside a loop: fine
    assert levels(v, "tree.owned-param") == ["ok"]


def test_a_low_level_above_the_led_threshold_is_refused_by_led_drive():
    tree = illumination([module("jv_bace"), module("bace")], led_low_v=1.2)
    tree["led_low_v"] = 1.2
    v = validate(tree)
    assert not v.valid
    bad = v.by_code("led.levels")
    assert {b.level for b in bad} == {"invalid"}
    assert {b.node_path for b in bad} == {"led=1.020V/jv_bace", "led=1.020V/bace"}
    assert "below the LED threshold" in bad[0].text
    assert bad[0].data["led_low_v"] == 1.2 and bad[0].data["threshold_v"] == RIG.led_threshold_v
    # a level below threshold, the other refusal, reaches the same id
    v = validate(module("bace", led_v=0.5, measure_dc=True))
    assert levels(v, "led.levels") == ["invalid"]


def test_a_compliance_above_the_bench_ceiling_is_crit():
    v = validate(module("bace", measure_dc=True, smu_current_compliance_a=0.1))
    assert not v.valid
    [bad] = v.by_code("smu.ceiling")
    assert bad.level == "crit" and "0.1 A" in bad.text and "0.05 A" in bad.text
    assert bad.data["max_current_compliance_a"] == RIG.max_current_compliance_a
    v = validate(module("jv", smu_voltage_compliance_v=9.0))
    assert levels(v, "smu.ceiling") == ["crit"]
    v = validate(module("jv"))
    assert levels(v, "smu.ceiling") == ["ok"]


def test_axis_geometry_is_judged_by_axis_scanspec_and_pulse_levels():
    v = validate(module("bace", measure_dc=True, axis_name="delay_ns", axis_start=0.0,
                        axis_stop=100.0, axis_step=10.0))
    [bad] = v.by_code("axis.geometry")
    assert bad.level == "invalid" and "centre_on_voc" in bad.text
    v = validate(module("bace", measure_dc=True, axis_start=0.0, axis_stop=1.0,
                        axis_step=0.0, centre_on_voc=False))
    assert "positive step" in v.by_code("axis.geometry")[0].text
    v = validate(module("bace", measure_dc=True, n_loops=0))
    assert levels(v, "axis.geometry") == ["invalid"]
    v = validate(module("bace", measure_dc=True, centre_on_voc=False,
                        axis_name="delay_ns", axis_start=-50.0, axis_stop=50.0,
                        axis_step=10.0), rig=RigConfig(trigger_offset_s=0.0))
    [bad] = v.by_code("axis.geometry")
    assert bad.level == "invalid" and "before its own trigger" in bad.text
    v = validate(module("bace", measure_dc=True))
    [ok] = v.by_code("axis.geometry")
    assert ok.level == "ok" and "centred on V_oc" in ok.text


def test_a_temperature_loop_warns_that_the_331_is_not_wired():
    v = validate(canonical())
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn" and "pauses" in w.text and "220-295 K" in w.text
    assert w.node_path == "T=295K" and w.data["setpoints"] == [float(t) for t in TEMPERATURES]
    v = validate(module("bace", measure_dc=True))
    assert levels(v, "temperature.not-wired") == ["ok"]
    v = validate(canonical(), rig=RigConfig(temperature_console="http://127.0.0.1:8331"))
    assert "8331" in v.by_code("temperature.not-wired")[0].text
    assert "not read back yet" in v.by_code("temperature.not-wired")[0].text


def with_temperature(*, wired: bool, connected: bool = True, source: str = "console",
                     reason: str | None = None, **kw) -> dict:
    """A bench snapshot with the 331 block as `rigs.read_temperature_console`
    renders it: attached (`wired`), and the console's own `connected`."""
    snap = bench_snapshot(**kw)
    snap["instruments"]["temperature"] = (
        {"wired": True, "kelvin": 249.9, "setpoint_k": 250.0, "in_band": None,
         "source": source, "connected": connected, "ramping": False, "heater_range": 3,
         "status_text": "ok" if connected else "the instrument is not answering",
         "max_setpoint_k": 350.0}
        if wired else
        {"wired": False, "kelvin": None, "setpoint_k": None, "in_band": None, "source": None})
    if reason:
        snap["unavailable"] = {"temperature": reason}
    return snap


def test_the_temperature_check_says_which_path_the_loop_takes():
    """`ok` and no pause on the schedule when the read-back showed the 331
    attached and answering; `warn` and a pause when there is no controller,
    when the named console was silent at start-up, and when the console is up
    with the instrument silent behind it -- each in its own words."""
    named = RigConfig(temperature_console="http://127.0.0.1:8331")
    v = validate(canonical(), bench=with_temperature(wired=True), rig=named)
    [ok] = v.by_code("temperature.not-wired")
    assert ok.level == "ok" and ok.node_path == "T=295K"
    assert ok.text.startswith("331 console attached: settles automatically at each of the "
                              "9 temperatures (220-295 K), +/-0.2 K, hold 1 min, timeout 30 min")
    assert "350 K ceiling and the heater range are read, never driven" in ok.text
    assert (ok.data["wired"], ok.data["connected"], ok.data["timeout_s"]) == (True, True, 1800)
    assert v.valid and not any(s.needs_operator for s in v.schedule.steps), (
        "the Dry run says no pause at any temperature")

    v = validate(canonical(), bench=with_temperature(wired=True, source="simulated"), rig=named)
    assert v.by_code("temperature.not-wired")[0].text.startswith("simulated 331 attached")

    v = validate(canonical(), bench=with_temperature(wired=True, connected=False), rig=named)
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn" and w.text.startswith("331 console attached but silent")
    assert "instrument is not answering" in w.text and "pauses at each of the 9" in w.text
    assert w.data["connected"] is False and v.schedule.steps[0].needs_operator

    reason = "the 331 console is not answering at http://127.0.0.1:8331 (start it)"
    v = validate(canonical(), bench=with_temperature(wired=False, reason=reason), rig=named)
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn" and w.text.startswith("331 not wired: the run pauses")
    assert "not answering (start the 331 console)" in w.text and w.data["reason"] == reason
    assert v.schedule.steps[0].needs_operator

    v = validate(canonical(), bench=with_temperature(wired=False))
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn" and "for a manual set" in w.text and "8331" not in w.text
    assert w.data["wired"] is False and v.schedule.steps[0].needs_operator
    assert v.valid, "a pause is a warning, never a block"


def test_a_temperature_module_step_pauses_or_settles_by_the_same_rule():
    v = validate(module("temperature", setpoint_k=250.0), bench=with_temperature(wired=True))
    assert v.schedule.modules[0].needs_operator is False
    v = validate(module("temperature", setpoint_k=250.0), bench=with_temperature(wired=False))
    assert v.schedule.modules[0].needs_operator is True
    v = validate(module("temperature", setpoint_k=250.0))
    assert v.schedule.modules[0].needs_operator is True, "nothing read back: assume the pause"
    assert v.by_code("temperature.not-wired")[0].text == "no temperature loop"


def test_an_unwired_331_is_advisory_not_a_missing_instrument():
    """The card's `needs` line says `temperature` when the 331 is not wired;
    `bench.instrument` must not turn that into `invalid`, because the node
    runs either way -- it pauses."""
    class Cat(FakeCatalogue):
        def needs(self, name, params, *, bench=None, session_voc=None):
            if name == "temperature":
                return [{"code": "temperature", "text": "331 not wired · pauses for a manual set"}]
            return []

    v = validate(module("temperature", setpoint_k=250.0), catalogue=Cat(),
                 bench=with_temperature(wired=False))
    [ok] = v.by_code("bench.instrument")
    assert ok.level == "ok" and v.valid


def test_a_temperature_loop_inside_an_illumination_loop_warns_with_the_cost():
    inside_out = {"kind": "loop", "loop": "illumination", "led_start_v": 1.010,
                  "led_stop_v": 1.030, "led_step_v": 0.005,
                  "children": [{"kind": "loop", "loop": "temperature",
                                "values_k": TEMPERATURES,
                                "children": [module("jv_bace"), module("bace")]}]}
    v = validate(inside_out)
    [w] = v.by_code("temperature.inside-illumination")
    assert w.level == "warn" and "45 times instead of 9" in w.text
    assert w.data == {"settles": 45, "instead_of": 9, "extra_s": None,
                      "node_key": "illumination/temperature"}
    assert w.node_path == "led=1.010V/T=295K"
    assert "unknown" in w.text                                   # no history, no hours
    history = FakeHistory(settle={float(t): [600.0] for t in TEMPERATURES})
    v = validate(inside_out, history=history)
    [w] = v.by_code("temperature.inside-illumination")
    assert w.data["extra_s"] == 600.0 * 36 and "6 h 00 more waiting" in w.text
    assert levels(validate(canonical()), "temperature.inside-illumination") == ["ok"]


def test_the_chain_read_back_becomes_warnings_with_named_fixes():
    v = validate(canonical(), bench=bench_snapshot(led_pol="NORM"))
    [w] = v.by_code("chain.led-polarity")
    assert w.level == "warn" and w.data["fix"] == "set-33220a-pol-inv"
    assert "NORM" in w.text and "during illumination" in w.text
    assert v.valid                                               # warn never blocks

    v = validate(canonical(), bench=bench_snapshot(arm="IMM", slope="NEG"))
    [w] = v.by_code("chain.bias-arm")
    assert w.level == "warn" and w.data["fix"] == "arm-81150a-ext"
    assert "IMM" in w.text and "EXT" in w.text

    v = validate(canonical(inverted_output=True), bench=bench_snapshot(bias_pol="NORM"))
    [w] = v.by_code("chain.bias-polarity")
    assert w.level == "warn" and w.data["expected"] == "INV"
    v = validate(canonical(output_polarity="leave"), bench=bench_snapshot(bias_pol="NORM"))
    [i] = v.by_code("chain.bias-polarity")
    assert i.level == "info" and "leaves" in i.text

    # a J-V-only tree does not use the Sync edge
    v = validate(module("jv"), bench=bench_snapshot(led_pol="NORM"))
    assert levels(v, "chain.led-polarity") == ["info"]
    assert levels(v, "chain.bias-arm") == ["ok"]


def test_without_a_read_back_the_bench_checks_say_so_instead_of_guessing():
    v = validate(canonical(), bench=None)
    for code in ("chain.led-polarity", "chain.bias-arm", "chain.bias-polarity",
                 "bench.live-at-start", "chain.stale", "relay.interlock", "power.console"):
        [c] = v.by_code(code)
        assert c.level == "info", code
        assert "not read" in c.text or "read at Start" in c.text, code
    assert v.valid


def test_live_outputs_and_a_missing_router_are_crit():
    v = validate(canonical(), bench=bench_snapshot(bias_on=True))
    assert not v.valid
    [c] = v.by_code("bench.live-at-start")
    assert c.level == "crit" and "81150A" in c.text and c.data["live"] == ["81150A bias"]
    [r] = v.by_code("relay.interlock")
    assert r.level == "crit" and "interlock refuses" in r.text

    v = validate(canonical(), bench=bench_snapshot(relay="unknown", relay_how="unavailable"))
    [r] = v.by_code("relay.interlock")
    assert r.level == "crit" and "no Router" in r.text and r.data["transitions"] == 89
    assert levels(v, "bench.live-at-start") == ["ok"]

    # a tree that never crosses the relay needs no router
    v = validate(module("bace", measure_dc=True),
                 bench=bench_snapshot(relay="unknown", relay_how="unavailable"))
    assert levels(v, "relay.interlock") == ["ok"]
    v = validate(canonical(), bench=bench_snapshot())
    [r] = v.by_code("relay.interlock")
    assert r.level == "ok" and r.data["transitions"] == 89 and r.data["position"] == "amplifier"


def test_the_power_console_and_the_calibration_factor():
    v = validate(canonical(), bench=bench_snapshot(power=False))
    [w] = v.by_code("power.console")
    assert w.level == "warn" and "NaN" in w.text and w.data["readers"] == 45
    v = validate(canonical(), bench=bench_snapshot(power=False), power_available=True)
    assert levels(v, "power.console") == ["ok"]                  # the caller just asked
    v = validate(canonical(read_intensity=False), bench=bench_snapshot(power=False))
    assert levels(v, "power.console") == ["ok"]
    assert levels(v, "intensity.factor") == ["ok"]
    v = validate(module("power"), bench=bench_snapshot(power=False))
    assert levels(v, "power.console") == ["warn"]
    assert levels(validate(canonical()), "intensity.factor") == ["info"]


def test_trigger_auto_is_information_and_trig_is_not():
    v = validate(module("bace", measure_dc=True))
    [i] = v.by_code("trigger.auto")
    assert i.level == "info" and "AUTO" in i.text and i.node_path == "bace"
    v = validate(module("bace", measure_dc=True, trigger_sweep="TRIG"))
    assert levels(v, "trigger.auto") == ["ok"]


def test_a_long_or_heavy_run_is_a_warning():
    v = validate(module("bace", measure_dc=True, n_loops=100_000))   # 80 000 s
    [w] = v.by_code("cost.long")
    assert w.level == "warn" and "over 8 h" in w.text
    v = validate(module("bace", measure_dc=True, n_loops=100, store_shots=True,
                        axis_start=-1.0, axis_stop=1.0, axis_step=0.001, centre_on_voc=False))
    [w] = v.by_code("cost.long")
    assert w.level == "warn" and "GB" in w.text and w.data["store_shots_bytes"] > 2e9
    assert levels(validate(module("bace", measure_dc=True)), "cost.long") == ["ok"]


def test_a_stale_chain_read_back_is_information():
    v = validate(module("bace", measure_dc=True), bench=bench_snapshot(read_at=NOW - 1200))
    [i] = v.by_code("chain.stale")
    assert i.level == "info" and "20 min" in i.text and "re-read at Start" in i.text
    v = validate(module("bace", measure_dc=True), bench=bench_snapshot())
    assert levels(v, "chain.stale") == ["ok"]


def test_a_malformed_tree_is_one_invalid_and_the_rest_not_evaluated():
    v = validate({"kind": "loop", "loop": "repeat", "count": 1, "children": []},
                 bench=bench_snapshot())
    assert not v.valid and v.schedule is None and v.counters == {} and v.cost == {}
    [bad] = v.by_code("tree.shape")
    assert bad.level == "invalid" and bad.node_path == "repeat"
    assert [c.code for c in v.checks] == list(P.CHECKS)
    assert {c.level for c in v.checks if c.code != "tree.shape"} <= {"info", "ok"}
    assert "not evaluated" in v.by_code("voc.source")[0].text
    assert levels(v, "chain.led-polarity") == ["ok"]             # bench facts still stand
    v = validate(illumination([module("bake")]))
    assert v.by_code("tree.shape")[0].node_path == "led=1.020V/bake"


def test_per_node_verdicts_are_grouped_by_tree_node_not_iteration():
    tree = canonical(smu_current_compliance_a=0.2)
    v = validate(tree)
    [bad] = v.by_code("smu.ceiling")
    assert bad.node_path == "T=295K/led=1.010V/bace"
    assert bad.data["count"] == 45 and "44 more iterations" in bad.text
    assert len(bad.data["node_paths"]) == 10


# -- the cost model -------------------------------------------------------------
def test_the_cost_without_history_is_a_lower_bound_with_no_finish_time():
    v = validate(canonical())
    cost = v.cost
    assert cost["lower_bound"] is True and cost["finish_at"] is None
    assert cost["waiting_s"] is None
    assert cost["t_shot_s"] == 0.8 and cost["t_shot_source"] == "default"
    assert [p["setpoint_k"] for p in cost["per_temperature"]] == TEMPERATURES
    assert all(p["settle_s"] is None for p in cost["per_temperature"])
    # the known part: 5 x (jv_bace + bace + 2 s LED settle) per temperature,
    # plus the 60 s hold at each
    jv = v.schedule.modules[0].estimate_s
    bace = v.schedule.modules[1].estimate_s
    assert bace == 100 * 1 * 0.8
    per_t = 5 * (jv + bace + 2.0)
    assert cost["per_temperature"][0]["measure_s"] == pytest.approx(per_t)
    assert cost["per_temperature"][0]["hold_s"] == 60
    assert cost["measuring_s"] == pytest.approx(9 * per_t)
    assert cost["total_s"] == pytest.approx(9 * per_t + 9 * 60)


def test_the_cost_with_history_uses_the_median_settle_and_the_journal_shot_time():
    settle = {295.0: [30.0, 90.0, 60.0], 290.0: [600.0], 280.0: [1300.0, 1400.0]}
    settle.update({float(t): [900.0] for t in (270, 260, 250, 240, 230, 220)})
    history = FakeHistory(settle=settle, shot=1.1)
    v = validate(canonical(), history=history)
    cost = v.cost
    assert cost["lower_bound"] is False
    assert cost["t_shot_s"] == 1.1 and cost["t_shot_source"] == "journal"
    assert v.schedule.modules[1].estimate_s == pytest.approx(100 * 1.1)
    per = {p["setpoint_k"]: p["settle_s"] for p in cost["per_temperature"]}
    assert per[295] == 60.0 and per[290] == 600.0 and per[280] == 1350.0
    waiting = sum(statistics.median(x) for x in settle.values()) + 9 * 60
    assert cost["waiting_s"] == pytest.approx(waiting)
    assert cost["total_s"] == pytest.approx(cost["measuring_s"] + waiting)
    assert cost["finish_at"] == pytest.approx(NOW + cost["total_s"])
    # the schedule's temperature step says the same
    assert v.schedule.steps[0].detail["settle_s"] == 60.0
    assert v.schedule.steps[0].estimate_s == 120.0

    # one temperature without history is enough to make it a bound again
    del settle[250.0]
    v = validate(canonical(), history=FakeHistory(settle=settle, shot=1.1))
    assert v.cost["lower_bound"] is True and v.cost["finish_at"] is None
    per = {p["setpoint_k"]: p["settle_s"] for p in v.cost["per_temperature"]}
    assert per[250] is None and per[240] == 900.0


def test_settle_history_keys_are_matched_at_a_tenth_of_a_kelvin():
    history = FakeHistory(settle={250.0: [100.0], 249.9: [5.0]})
    assert P.settle_s(history, 250.04) == 100.0
    assert P.settle_s(history, 249.94) == 5.0
    assert P.settle_s(history, 251.0) is None
    assert P.settle_s(None, 250.0) is None


def test_estimate_takes_now_as_an_argument():
    sched = P.resolve(P.parse_tree(module("wait", seconds=5.0)), FakeCatalogue())
    cost = P.estimate(sched, None, now=1000.0)
    assert cost == {"total_s": 5.0, "measuring_s": 5.0, "waiting_s": 0.0,
                    "per_temperature": [], "lower_bound": False, "finish_at": 1005.0,
                    "t_shot_s": 0.8, "t_shot_source": "default",
                    "unestimated_modules": 0}


# -- the wire ---------------------------------------------------------------------
def test_everything_survives_json_dumps():
    voc = VocSource(1.0423, 1.020, "s-002", "jv_bace", "jv_bace", NOW)
    history = FakeHistory(settle={295.0: [60.0]}, shot=1.1)
    trees = [canonical(), module("bace"), illumination([module("bace", led_v=1.1)]),
             {"kind": "loop", "loop": "repeat", "count": 1, "children": []}]
    for tree in trees:
        v = validate(tree, bench=bench_snapshot(led_pol="NORM"), history=history,
                     session_voc=voc)
        wire = json.loads(json.dumps(v.as_wire()))
        assert set(wire) == {"valid", "checks", "schedule", "counters", "cost", "node_paths"}
        assert wire["valid"] == v.valid
        assert [c["code"] for c in wire["checks"]] == list(P.CHECKS)
        for c in wire["checks"]:
            assert set(c) == {"level", "code", "text", "node_path", "data"}
        if v.schedule is not None:
            steps = wire["schedule"]["steps"]
            assert len(steps) == len(v.schedule.steps)
            assert wire["schedule"]["counters"] == v.counters
            assert wire["node_paths"] == v.node_paths
            assert P.parse_tree(wire["schedule"]["tree"]) == v.schedule.tree
            mod = next(s for s in steps if s["module"] == "bace")
            pv = mod["params"]["led_v"]
            assert set(pv) == {"value", "source", "detail"}
            assert pv["source"] in ("inherited", "default", "edited")
    v = validate(module("bace"), session_voc=voc)
    wire = v.as_wire()
    assert wire["schedule"]["steps"][0]["params"]["voc"] == {
        "value": 1.0423, "source": "derived", "detail": "jv_bace jv_bace (this session)"}


# -- the round of 2026-09-02 --------------------------------------------------------------
def test_an_unreadable_33220a_polarity_is_a_warn_for_a_transient_and_info_for_a_jv():
    """The bench card calls a `?` a warn; the validator used to call the same
    read-back `info` for a bace tree. An unreadable polarity on a transient
    is as unproven as NORM, so the two agree now."""
    v = validate(canonical(), bench=bench_snapshot(led_pol="?"))
    [w] = v.by_code("chain.led-polarity")
    assert w.level == "warn" and w.data["fix"] == "set-33220a-pol-inv"
    assert "unproven" in w.text and w.data["value"] == "?"
    assert v.valid
    v = validate(module("jv"), bench=bench_snapshot(led_pol="?"))
    assert levels(v, "chain.led-polarity") == ["info"]


def test_a_module_whose_instrument_is_unplugged_is_refused_at_validate():
    """The real catalogue knows what each module cannot run without; with a
    read-back that lists the Keithley as unavailable a jv is refused
    with a check, not accepted, queued and failed at preflight."""
    from bace.service.modules import Catalogue

    cat = Catalogue(rig_config=RIG, run_toml={}, history=None)
    snap = bench_snapshot()
    snap["unavailable"] = {"smu": "GPIB0::24::INSTR: VisaIOError: VI_ERROR_RSRC_NFOUND"}
    v = validate(module("jv"), catalogue=cat, bench=snap)
    assert not v.valid
    [c] = v.by_code("bench.instrument")
    assert c.level == "invalid" and c.data["missing"] == ["smu"]
    assert "VI_ERROR_RSRC_NFOUND" in c.text and c.node_path == "jv"

    # bace inside the canonical tree: the same read-back says the scope is gone
    snap["unavailable"] = {"scope": "TCPIP0::PwM-DSO9054H.local::inst0::INSTR: timeout"}
    v = validate(canonical(centre_on_voc=True), catalogue=cat, bench=snap)
    [c] = v.by_code("bench.instrument")
    assert c.level == "invalid" and c.data["missing"] == ["scope"]
    assert c.data["count"] == 45, "one verdict per tree node, not per iteration"

    # nothing missing: ok, and a bench not read yet is information
    snap["unavailable"] = {}
    assert levels(validate(module("jv"), catalogue=cat, bench=snap), "bench.instrument") == ["ok"]
    assert levels(validate(module("jv"), catalogue=cat, bench=None), "bench.instrument") == ["info"]
    # the power meter is advisory for a bace (intensity NaN) and required by `power`
    snap["unavailable"] = {"power": "the 1918-C console is not answering"}
    assert levels(validate(module("bace", measure_dc=True), catalogue=cat, bench=snap),
                  "bench.instrument") == ["ok"]
    [c] = validate(module("power"), catalogue=cat, bench=snap).by_code("bench.instrument")
    assert c.level == "invalid" and c.data["missing"] == ["power"]
    assert "bench.instrument" in P.CHECKS


def test_a_pinned_prebias_on_voc_needs_a_source_and_a_pinned_vpre():
    """The design's inherited "vpre = V_oc + 0.000 V" when delay_ns is the
    axis: `vpre_on_voc` makes V_oc mandatory the way `centre_on_voc` does,
    and is meaningless when vpre is the swept axis."""
    from bace.service.modules import Catalogue

    cat = Catalogue(rig_config=RIG, run_toml={}, history=None)
    delay = dict(axis_name="delay_ns", axis_start=50.0, axis_stop=250.0, axis_step=50.0,
                 centre_on_voc=False, vpre_on_voc=True, vpre=0.0)
    v = validate(module("bace", **delay), catalogue=cat)
    assert not v.valid
    [c] = v.by_code("voc.source")
    assert c.level == "invalid" and "vpre_on_voc" in c.text
    assert c.data["needed_by"] == ["vpre_on_voc"]

    voc = VocSource(value=0.906, led_v=1.0, run_id="r", node_path="jv_bace", how="jv_bace", ts=1.0)
    v = validate(module("bace", **delay), catalogue=cat, session_voc=voc)
    assert v.valid, [c.text for c in v.checks if c.level != "ok"]
    step = v.schedule.modules[0]
    assert step.params["voc"].source is Source.DERIVED and step.params["voc"].value == 0.906
    assert step.detail["voc_needed_by"] == ["vpre_on_voc"]

    swept = dict(axis_name="vpre", axis_start=-0.1, axis_stop=0.1, axis_step=0.1,
                 centre_on_voc=True, vpre_on_voc=True)
    v = validate(module("bace", **swept), catalogue=cat, session_voc=voc)
    [c] = v.by_code("axis.geometry")
    assert c.level == "invalid" and "vpre_on_voc" in c.text and "swept axis" in c.text


def test_the_temperature_check_tells_a_console_gone_since_start_from_an_instrument_silent():
    """A read-back block with `error` (the read itself raised) is the
    console not answering, not the instrument: the text says start the
    console and never mentions the instrument. A controller attached and not
    read back yet (the bench stub before the first read-back, `connected`
    null) is a pause until the read-back says otherwise."""
    named = RigConfig(temperature_console="http://127.0.0.1:8331")
    snap = with_temperature(wired=True, connected=False)
    snap["instruments"]["temperature"].update({
        "kelvin": None, "setpoint_k": None, "status_text": None,
        "error": "TemperatureError: cannot reach the 331 console at http://127.0.0.1:8331"})
    v = validate(canonical(), bench=snap, rig=named)
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn"
    assert w.text.startswith("331 console attached at Start and not answering now -- "
                             "http://127.0.0.1:8331 did not answer the read-back")
    assert "instrument is not answering" not in w.text, (
        "a console that stopped answering is not a console with a silent 331")
    assert w.data["error"].startswith("TemperatureError: cannot reach")
    assert v.schedule.steps[0].needs_operator and v.valid

    stub = {"wired": True, "kelvin": None, "setpoint_k": None, "in_band": None,
            "source": "console", "connected": None}
    snap = with_temperature(wired=True)
    snap["instruments"]["temperature"] = dict(stub)
    v = validate(canonical(), bench=snap, rig=named)
    [w] = v.by_code("temperature.not-wired")
    assert w.level == "warn" and w.text.startswith("331 console attached and not read back yet")
    assert v.schedule.steps[0].needs_operator, "a pause until the read-back says otherwise"
    assert P.temperature_automatic(snap) is None
    snap["instruments"]["temperature"]["source"] = "simulated"
    [w] = validate(canonical(), bench=snap, rig=named).by_code("temperature.not-wired")
    assert w.text.startswith("simulated 331 attached and not read back yet")

    assert P.temperature_automatic(with_temperature(wired=True)) is True
    assert P.temperature_automatic(with_temperature(wired=True, connected=False)) is False
    assert P.temperature_automatic(with_temperature(wired=False)) is False
    assert P.temperature_automatic(None) is None


def test_a_run_that_only_sets_the_light_is_refused_because_park_would_undo_it():
    """Every run ends parked -- the executor's `finally` and then the worker's
    -- so a light-only run hands the bench back exactly as dark as it found
    it. The operator would watch a run succeed and change nothing, which is
    the failure `ui-rules` §9 is about wearing a green tick."""
    v = validate(module("light", shutter="open", led_mode="dc", led_v=1.02))
    assert levels(v, "light.undone-by-park") == ["invalid"] and not v.valid
    c = v.by_code("light.undone-by-park")[0]
    assert "bench actions" in c.text and "shutter-open" in c.text

    # Inside a tree the node is the point: it sets the light for the steps
    # after it, and park at the end of the run is where the bench should end.
    seq = {"kind": "loop", "loop": "repeat", "count": 1,
           "children": [module("light", shutter="open", led_mode="dc", led_v=1.02),
                        module("jv")]}
    v = validate(seq)
    assert levels(v, "light.undone-by-park") == ["ok"]


def test_a_jv_sweep_has_geometry_too_and_it_is_checked():
    """Until this, `axis.geometry` skipped every module without an
    `axis_name`, which is every J-V module — so `step_v = 0` validated clean,
    was queued, and died at build time with `jv: step_v: step_v must be
    positive`. The card's own estimate already read "cannot estimate" while
    the Run button beside it stayed enabled.

    Pre-existing (it applied to `jv_dark` just the same); named here because
    the split is what made it visible."""
    ok = validate(module("jv"))
    assert levels(ok, "axis.geometry") == ["ok"]

    bad = validate(module("jv", step_v=0))
    assert levels(bad, "axis.geometry") == ["invalid"] and not bad.valid
    assert "step_v must be positive" in bad.by_code("axis.geometry")[0].text

    # A range of LED levels with no step is the same shape of refusal, and the
    # one `_jv_levels` makes in the builder.
    span = validate(module("jv_bace", led_start_v=1.02, led_stop_v=1.06, led_step_v=0))
    assert levels(span, "axis.geometry") == ["invalid"]
    assert "led_step_v" in span.by_code("axis.geometry")[0].text

    # One level is not a range: start = stop needs no step.
    one = validate(module("jv_bace", led_start_v=1.02, led_stop_v=1.02, led_step_v=0))
    assert levels(one, "axis.geometry") == ["ok"]
