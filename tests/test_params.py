"""Parameter provenance: which value is in force, and where it came from.

`bace.params` is what the service's `/modules` endpoint will stand on, so
these tests pin the contract the catalogue is written against: the precedence
of the six sources, that an operator's edit is reported as *overridden* when a
loop overrides it, that a bad form value is refused by name and leaves the set
untouched, and that the specs read off the real dataclasses say what the
dataclasses say.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from typing import Literal, Optional, get_type_hints

import numpy as np
import pytest

from bace.params import (LOCKED, PRECEDENCE, ParamError, ParamSet, ParamSpec,
                         ParamValue, Source, field_docs, first_sentence,
                         run_toml_layer, specs_from_dataclass, toml_layer)

REPO = pathlib.Path(__file__).resolve().parents[1]

RUN_CHOICES = {"t0_int_reference": ("record", "trigger", "pulse"),
               "dark_reference": ("translated", "same"),
               "output_polarity": ("auto", "NORM", "INV", "leave")}
"""What `RunConfig.__post_init__` accepts. The dataclass validates these in
code rather than in its annotations, so the catalogue has to say them."""


def small_set() -> ParamSet:
    return ParamSet([
        ParamSpec("n_averages", "int", 200, unit="", minimum=1),
        ParamSpec("led_v", "float", 1.0, unit="V"),
        ParamSpec("voc", "float", None, unit="V", nullable=True),
        ParamSpec("offset_correct", "bool", True),
        ParamSpec("t0_int_reference", "enum", "record",
                  choices=("record", "trigger", "pulse")),
        ParamSpec("led_levels_v", "list[float]", ()),
        ParamSpec("sample", "str", ""),
        ParamSpec("max_current_a", "float", 0.05, unit="A", editable=False),
    ])


# -- precedence -----------------------------------------------------------
def test_the_documented_precedence_is_the_one_in_force():
    """The module docstring promises `default < run.toml < last-used < edited
    < inherited < derived`. If the tuple ever disagrees with the prose the
    console shows a source that is not the one that decided the value."""
    assert PRECEDENCE == (Source.DEFAULT, Source.RUN_TOML, Source.LAST_USED,
                          Source.EDITED, Source.INHERITED, Source.DERIVED)
    assert LOCKED == {Source.INHERITED, Source.DERIVED}


def test_each_layer_beats_the_one_below_it():
    ps = small_set()
    assert ps.get("n_averages") == ParamValue(200, Source.DEFAULT, "")

    ps.set_layer(Source.RUN_TOML, {"n_averages": 20}, detail="run.toml [acquisition]")
    assert ps.get("n_averages") == ParamValue(20, Source.RUN_TOML, "run.toml [acquisition]")

    ps.set_layer(Source.LAST_USED, {"n_averages": 50}, detail="run 20260902_210300-003")
    assert ps.get("n_averages").value == 50
    assert ps.get("n_averages").source is Source.LAST_USED

    ps.set_edited("n_averages", 30)
    assert ps.get("n_averages") == ParamValue(30, Source.EDITED, "")

    ps.update_layer(Source.INHERITED, {"n_averages": 8}, detail="repeat loop")
    assert ps.get("n_averages") == ParamValue(8, Source.INHERITED, "repeat loop")

    ps.update_layer(Source.DERIVED, {"n_averages": 4}, detail="a measurement")
    assert ps.get("n_averages") == ParamValue(4, Source.DERIVED, "a measurement")


def test_an_edit_a_loop_overrides_is_reported_as_inherited_not_edited():
    """The failure the whole module exists for: the operator types led_v =
    1.02, the illumination loop injects 1.06, the run uses 1.06 -- and a
    console that still said "1.02, edited" would have every charge in the run
    read against the wrong illumination."""
    ps = small_set()
    ps.set_edited("led_v", 1.02)
    ps.update_layer(Source.INHERITED, {"led_v": 1.06}, detail="illumination loop")

    pv = ps.get("led_v")
    assert pv.value == 1.06
    assert pv.source is Source.INHERITED
    assert pv.detail == "illumination loop"
    wire = next(w for w in ps.as_wire() if w["name"] == "led_v")
    assert wire["source"] == "inherited" and wire["editable"] is False

    # the edit is still there underneath: when the loop lets go, it shows again
    ps.clear_layer(Source.INHERITED)
    assert ps.get("led_v") == ParamValue(1.02, Source.EDITED, "")


def test_an_edit_against_a_locked_value_is_refused_not_hidden():
    """The wire says `editable: false` for an inherited or derived value. A
    PUT that succeeded anyway would store an edit the screen never showed,
    and the first manual run after the loop let go would use it."""
    ps = small_set()
    ps.update_layer(Source.INHERITED, {"led_v": 1.06}, detail="illumination loop")
    with pytest.raises(ParamError, match=r"^led_v: not editable while inherited \(illumination loop\)"):
        ps.set_edited("led_v", 1.02)
    assert ps.layer(Source.EDITED) == {}
    ps.update_layer(Source.DERIVED, {"voc": 0.86}, detail="jv_bace")
    with pytest.raises(ParamError, match=r"^voc: not editable while derived \(jv_bace\)"):
        ps.update_layer(Source.EDITED, {"n_averages": 30, "voc": 0.9})
    assert ps.layer(Source.EDITED) == {}                 # the batch, not just the one
    # the other layers are not the operator's, so they are not locked out
    ps.update_layer(Source.LAST_USED, {"led_v": 1.04}, detail="run 003")
    ps.clear_layer(Source.INHERITED)
    ps.clear_layer(Source.DERIVED)
    ps.set_edited("led_v", 1.02)                        # released: editable again
    assert ps.get("led_v") == ParamValue(1.02, Source.EDITED, "")


def test_derived_beats_inherited():
    """A V_oc measured inside the loop is more recent than the loop's own
    binding. The order is documented; this keeps it from drifting."""
    ps = small_set()
    ps.update_layer(Source.INHERITED, {"voc": 0.90}, detail="pipeline")
    ps.update_layer(Source.DERIVED, {"voc": 0.86}, detail="jv_bace 20260902_2103")
    assert ps.get("voc").value == 0.86
    assert ps.get("voc").source is Source.DERIVED


def test_reset_drops_the_edit_and_the_layer_below_shows_again():
    """`reset` must not fall all the way to the default: with a recipe loaded,
    the value the operator un-edits is the recipe's, not the dataclass's."""
    ps = small_set()
    ps.set_layer(Source.RUN_TOML, {"n_averages": 20}, detail="run.toml [acquisition]")
    ps.set_layer(Source.LAST_USED, {"n_averages": 50}, detail="run 003")
    ps.set_edited("n_averages", 30)
    ps.reset("n_averages")
    assert ps.get("n_averages") == ParamValue(50, Source.LAST_USED, "run 003")
    ps.clear_layer(Source.LAST_USED)
    assert ps.get("n_averages") == ParamValue(20, Source.RUN_TOML, "run.toml [acquisition]")
    ps.clear_layer(Source.RUN_TOML)
    assert ps.get("n_averages") == ParamValue(200, Source.DEFAULT, "")


def test_reset_of_an_unedited_parameter_is_harmless():
    ps = small_set()
    ps.set_layer(Source.RUN_TOML, {"n_averages": 20})
    ps.reset("n_averages")
    assert ps.get("n_averages").source is Source.RUN_TOML


def test_set_layer_replaces_and_update_layer_merges():
    ps = small_set()
    ps.set_layer(Source.RUN_TOML, {"n_averages": 20, "led_v": 1.02})
    ps.set_layer(Source.RUN_TOML, {"n_averages": 40})
    assert ps.get("led_v").source is Source.DEFAULT      # replaced, so gone
    ps.update_layer(Source.RUN_TOML, {"led_v": 1.04})
    assert ps.get("n_averages").value == 40 and ps.get("led_v").value == 1.04
    assert ps.layer(Source.RUN_TOML) == {"n_averages": (40, ""), "led_v": (1.04, "")}


def test_the_default_layer_is_not_a_layer():
    """Two places to hold a default are two places for it to disagree."""
    ps = small_set()
    with pytest.raises(ParamError, match="default layer"):
        ps.set_layer(Source.DEFAULT, {"n_averages": 1})
    with pytest.raises(ParamError, match="default layer"):
        ps.clear_layer(Source.DEFAULT)


# -- names ----------------------------------------------------------------
def test_unknown_names_are_refused_everywhere():
    """A misspelt name that was quietly stored would be a value the console
    shows and the run never reads."""
    ps = small_set()
    for call in (lambda: ps.set_edited("n_average", 1),
                 lambda: ps.update_layer(Source.RUN_TOML, {"n_average": 1}),
                 lambda: ps.reset("n_average"),
                 lambda: ps.get("n_average"),
                 lambda: ps.spec("n_average")):
        with pytest.raises(ParamError, match="n_average: no such parameter"):
            call()
    assert "n_average" not in ps and "n_averages" in ps


def test_a_spec_declared_twice_is_refused():
    with pytest.raises(ParamError, match="declared twice"):
        ParamSet([ParamSpec("a", "int", 1), ParamSpec("a", "float", 1.0)])


def test_a_batch_that_is_not_a_mapping_is_refused_by_source():
    """A JSON body that is a list must come back as the same refusal a bad
    value does, not as an AttributeError from `.items()` that the service
    would turn into a 500."""
    ps = small_set()
    with pytest.raises(ParamError, match="^edited: expected a mapping"):
        ps.update_layer(Source.EDITED, [("n_averages", 1)])
    with pytest.raises(ParamError, match="^run.toml: expected a mapping"):
        ps.set_layer(Source.RUN_TOML, "n_averages=1")
    assert ps.layer(Source.EDITED) == {} and ps.layer(Source.RUN_TOML) == {}


# -- coercion -------------------------------------------------------------
def test_json_and_form_values_are_coerced_on_the_way_in():
    """What a browser actually sends: numbers as strings, flags as "false",
    lists as one comma-separated field."""
    ps = small_set()
    ps.set_edited("n_averages", "20")
    ps.set_edited("led_v", "1.02")
    ps.set_edited("offset_correct", "false")
    ps.set_edited("led_levels_v", "1.02, 1.06,1.10")
    ps.set_edited("t0_int_reference", "TRIGGER")
    v = ps.values()
    assert v["n_averages"] == 20 and isinstance(v["n_averages"], int)
    assert v["led_v"] == 1.02 and isinstance(v["led_v"], float)
    assert v["offset_correct"] is False
    assert v["led_levels_v"] == (1.02, 1.06, 1.10)
    assert v["t0_int_reference"] == "trigger"          # canonical spelling


def test_the_string_false_is_false():
    """`bool("false")` is True. A run whose offset correction the operator
    switched off would otherwise run with it on and say so nowhere."""
    assert ParamSpec("f", "bool", True).coerce("false") is False
    assert ParamSpec("f", "bool", True).coerce("0") is False
    assert ParamSpec("f", "bool", False).coerce("true") is True
    with pytest.raises(ParamError, match="^f: 'maybe'"):
        ParamSpec("f", "bool", True).coerce("maybe")


def test_coercion_errors_name_the_parameter():
    """The console attaches the message to a field by its name, so every
    refusal must start with one."""
    ps = small_set()
    cases = [
        ("n_averages", "abc"), ("n_averages", "20.5"), ("n_averages", True),
        ("n_averages", 0),                                  # below minimum 1
        ("led_v", "nan"), ("led_v", "1,02"), ("led_v", False),
        ("offset_correct", "maybe"), ("offset_correct", 2), ("offset_correct", 1.0),
        ("t0_int_reference", "record time"),
        ("led_levels_v", "1.02, abc"), ("led_levels_v", 1.02),
        ("sample", 7), ("n_averages", None),
    ]
    for name, value in cases:
        with pytest.raises(ParamError, match=f"^{name}: "):
            ps.set_edited(name, value)


def test_numpy_scalars_are_accepted_as_the_numbers_they_wrap():
    """A last-used layer seeded from a run file's /config group, or a V_oc
    handed over by measurement code, arrives as np.float64/np.int64/np.bool_.
    Refusing those with "is not a number" would be wrong twice over."""
    ps = small_set()
    ps.update_layer(Source.LAST_USED, {
        "n_averages": np.int64(20), "led_v": np.float32(1.02), "offset_correct": np.bool_(False),
        "led_levels_v": np.array([1.02, 1.06]), "sample": np.str_("px3"),
        "t0_int_reference": np.str_("trigger"), "voc": np.float64(0.86)},
        detail="run 003 /config")
    v = ps.values()
    assert v["n_averages"] == 20 and type(v["n_averages"]) is int
    assert abs(v["led_v"] - 1.02) < 1e-6 and type(v["led_v"]) is float
    assert v["offset_correct"] is False
    assert v["led_levels_v"] == (1.02, 1.06) and all(type(x) is float for x in v["led_levels_v"])
    assert v["sample"] == "px3" and type(v["sample"]) is str
    assert v["t0_int_reference"] == "trigger" and v["voc"] == 0.86
    json.dumps(ps.as_wire())                            # nothing numpy leaks out
    with pytest.raises(ParamError, match="^n_averages: .* not a whole number"):
        ps.set_edited("n_averages", np.float64(20.5))
    with pytest.raises(ParamError, match="^led_v: .* is a flag"):
        ps.set_edited("led_v", np.bool_(True))


def test_a_bad_batch_changes_nothing():
    """One bad value in a form submission must not leave the others applied:
    the operator would fix the one field and unknowingly resubmit the rest."""
    ps = small_set()
    ps.set_layer(Source.RUN_TOML, {"n_averages": 20})
    with pytest.raises(ParamError, match="^led_v: "):
        ps.update_layer(Source.EDITED, {"n_averages": 30, "led_v": "x"})
    assert ps.layer(Source.EDITED) == {}
    assert ps.get("n_averages").value == 20


def test_nullable_accepts_none_and_empty_and_others_do_not():
    """`SeriesConfig.dc_settle_s = None` means "use the driver's own settle
    times". A field that could not be set back to None would lose that."""
    ps = small_set()
    ps.set_edited("voc", 0.9)
    ps.set_edited("voc", None)
    assert ps.get("voc").value is None and ps.get("voc").source is Source.EDITED
    ps.set_edited("voc", "")
    assert ps.get("voc").value is None
    with pytest.raises(ParamError, match="^led_v: no value"):
        ps.set_edited("led_v", "")
    # an empty string is a legitimate string, and an empty list a legitimate list
    ps.set_edited("sample", "")
    ps.set_edited("led_levels_v", "")
    assert ps.values()["sample"] == "" and ps.values()["led_levels_v"] == ()


def test_bounds_apply_to_scalars_and_list_elements():
    spec = ParamSpec("lv", "list[float]", (), unit="V", minimum=1.0, maximum=1.2)
    assert spec.coerce([1.0, 1.2]) == (1.0, 1.2)
    with pytest.raises(ParamError, match="^lv: 0.4 is below the minimum 1.0 V"):
        spec.coerce("1.0, 0.4")
    with pytest.raises(ParamError, match="^n: 5 is above the maximum 4"):
        ParamSpec("n", "int", 1, maximum=4).coerce(5)


def test_a_spec_that_contradicts_itself_is_refused():
    with pytest.raises(ParamError, match="unknown type"):
        ParamSpec("x", "double", 1.0)
    with pytest.raises(ParamError, match="needs choices"):
        ParamSpec("x", "enum", "a")
    with pytest.raises(ParamError, match="not 'enum'"):
        ParamSpec("x", "str", "a", choices=("a", "b"))
    with pytest.raises(ParamError, match="minimum 2 is above maximum 1"):
        ParamSpec("x", "int", 1, minimum=2, maximum=1)
    with pytest.raises(ParamError, match="^x: a minimum or maximum is given but the type is 'str'"):
        ParamSpec("x", "str", "a", minimum=1)
    with pytest.raises(ParamError, match="type is 'bool'"):
        ParamSpec("x", "bool", True, maximum=1)


def test_a_default_the_spec_itself_would_refuse_is_refused():
    """The default is the one value that reaches `values()` without entering
    a layer, so it is the one road into the dataclass `coerce` does not guard.
    A catalogue that lists choices without the default would otherwise show a
    dropdown that cannot select its own value, and `reset` would restore a
    value the form refuses."""
    with pytest.raises(ParamError, match="^n: the default 'abc' would be refused .*not a valid int"):
        ParamSpec("n", "int", "abc")
    with pytest.raises(ParamError, match="^t0: the default 'nope' would be refused .*not one of 'a'"):
        ParamSpec("t0", "enum", "nope", choices=("a",))
    with pytest.raises(ParamError, match="^n: the default 0 would be refused .*below the minimum 1"):
        ParamSpec("n", "int", 0, minimum=1)
    with pytest.raises(ParamError, match="^x: the default is None but the parameter is not nullable"):
        ParamSpec("x", "float", None)
    assert ParamSpec("x", "float", None, nullable=True).default is None

    from bace.experiment.transient import RunConfig
    with pytest.raises(ParamError, match="^t0_int_reference: the default 'record' would be refused"):
        specs_from_dataclass(RunConfig, choices={"t0_int_reference": ("trigger", "pulse")})


def test_the_default_is_canonicalised_like_any_other_value():
    """One place to canonicalise: a list default is the tuple `coerce` would
    make of it, and an enum default takes its choice's spelling, so the wire
    never shows "norm" next to a dropdown that says "NORM"."""
    assert ParamSpec("lv", "list[float]", [1.0, 2]).default == (1.0, 2.0)
    assert ParamSpec("p", "enum", "norm", choices=("auto", "NORM")).default == "NORM"
    assert ParamSpec("n", "int", "20").default == 20
    assert ParamSpec("s", "str", "").default == ""


def test_a_read_only_spec_refuses_edits_but_takes_the_other_layers():
    """A bench ceiling is shown, and comes from rig.toml, and is never typed."""
    ps = small_set()
    with pytest.raises(ParamError, match="^max_current_a: not editable"):
        ps.set_edited("max_current_a", 0.1)
    ps.set_layer(Source.RUN_TOML, {"max_current_a": 0.02})
    assert ps.get("max_current_a").value == 0.02
    assert next(w for w in ps.as_wire() if w["name"] == "max_current_a")["editable"] is False


# -- the wire -------------------------------------------------------------
def test_as_wire_is_json_serialisable_and_in_spec_order():
    ps = small_set()
    ps.set_layer(Source.RUN_TOML, {"led_levels_v": (1.02, 1.06)}, detail="run.toml [jv]")
    wire = ps.as_wire()
    text = json.dumps(wire)                              # must not raise
    back = json.loads(text)
    assert [w["name"] for w in back] == list(ps.names)
    lv = next(w for w in back if w["name"] == "led_levels_v")
    assert lv["value"] == [1.02, 1.06] and lv["default"] == []
    assert lv["source"] == "run.toml" and lv["detail"] == "run.toml [jv]"
    expected_keys = {"name", "value", "source", "detail", "editable", "type", "unit",
                     "default", "doc", "doc_full", "choices", "group", "nullable",
                     "minimum", "maximum"}
    assert all(set(w) == expected_keys for w in back)
    enum = next(w for w in back if w["name"] == "t0_int_reference")
    assert enum["choices"] == ["record", "trigger", "pulse"]
    assert json.loads(json.dumps(ps.get("voc").as_dict())) == {
        "value": None, "source": "default", "detail": ""}


def test_a_typed_voc_is_editable_and_a_derived_one_is_not():
    """The J-V step of the pipeline produces V_oc; the operator may type one
    for a manual run. Both are shown, only one is a form field -- a value the
    pipeline will overwrite is not the operator's to type."""
    ps = small_set()
    ps.set_edited("voc", "0.90")
    typed = next(w for w in ps.as_wire() if w["name"] == "voc")
    assert typed["value"] == 0.90 and typed["source"] == "edited"
    assert typed["editable"] is True

    ps.update_layer(Source.DERIVED, {"voc": 0.86}, detail="jv_bace 20260902_210300")
    derived = next(w for w in ps.as_wire() if w["name"] == "voc")
    assert derived["value"] == 0.86 and derived["source"] == "derived"
    assert derived["detail"] == "jv_bace 20260902_210300"
    assert derived["editable"] is False

    ps.clear_layer(Source.DERIVED)                      # the manual run again
    assert next(w for w in ps.as_wire() if w["name"] == "voc")["editable"] is True


# -- specs from the real dataclasses --------------------------------------
def test_specs_from_runconfig_say_what_runconfig_says():
    """Types, defaults and choices read off `RunConfig`. If a default here
    drifts from the dataclass, the console shows one number and the run uses
    another."""
    from bace.experiment.transient import RunConfig

    specs = {s.name: s for s in specs_from_dataclass(RunConfig, group="acquisition",
                                                     choices=RUN_CHOICES)}
    assert list(specs) == [f for f in RunConfig.__dataclass_fields__]
    assert (specs["n_averages"].type, specs["n_averages"].default) == ("int", 200)
    assert (specs["timebase_ns_per_div"].type,
            specs["timebase_ns_per_div"].default) == ("float", 200.0)
    assert (specs["offset_correct"].type, specs["offset_correct"].default) == ("bool", True)
    assert specs["t0_int_reference"].type == "enum"
    assert specs["t0_int_reference"].choices == ("record", "trigger", "pulse")
    assert specs["t0_int_reference"].default == "record"
    assert specs["dark_reference"].choices == ("translated", "same")
    assert specs["output_polarity"].choices == ("auto", "NORM", "INV", "leave")
    assert specs["trigger_sweep"].type == "str"           # no choices given: text
    assert all(s.group == "acquisition" for s in specs.values())
    assert all(not s.nullable for s in specs.values())

    # the round trip: what the set resolves builds the dataclass
    ps = ParamSet(specs.values())
    ps.set_edited("output_polarity", "norm")
    ps.set_edited("n_averages", "20")
    cfg = RunConfig(**ps.values())
    assert cfg.n_averages == 20 and cfg.output_polarity == "NORM"


def test_specs_from_jvconfig_turn_the_level_tuple_into_a_float_list():
    from bace.experiment.jv import JVConfig

    specs = {s.name: s for s in specs_from_dataclass(JVConfig)}
    assert specs["led_levels_v"].type == "list[float]"
    assert specs["led_levels_v"].default == ()
    assert specs["both_directions"].type == "bool"
    assert specs["step_v"].type == "float" and specs["step_v"].default == 0.02
    assert specs["led_levels_v"].coerce("1.02, 1.06") == (1.02, 1.06)
    cfg = JVConfig(**ParamSet(specs.values()).values())
    assert cfg == JVConfig()


def test_specs_from_seriesconfig_see_the_nullable_float():
    """`dc_settle_s: float | None = None` -- the one field where None is a
    meaning, not an absence."""
    from bace.experiment.intensity_series import SeriesConfig

    specs = {s.name: s for s in specs_from_dataclass(SeriesConfig)}
    s = specs["dc_settle_s"]
    assert (s.type, s.default, s.nullable) == ("float", None, True)
    assert s.coerce(None) is None and s.coerce("0.5") == 0.5
    assert not specs["led_settle_s"].nullable


def test_specs_from_the_rig_and_sourcemeter_configs_build_without_help():
    from bace.drivers.keithley2400 import SourceMeterConfig
    from bace.experiment.rig import RigConfig

    rig = {s.name: s for s in specs_from_dataclass(RigConfig, group="bench")}
    smu = {s.name: s for s in specs_from_dataclass(SourceMeterConfig)}
    assert rig["sense_resistor_ohm"].type == "float"
    assert rig["scope_channel"].type == "int"
    assert rig["trigger_positive"].type == "bool"
    assert rig["scope_address"].type == "str"
    assert smu["terminals"].type == "str" and smu["four_wire"].type == "bool"
    assert smu["averaging"].type == "int"
    assert SourceMeterConfig(**ParamSet(smu.values()).values()) == SourceMeterConfig()


def test_specs_from_dataclass_overrides_are_keyed_by_field_and_checked():
    """A typo in an override must not be silently dropped -- `units={"n_average":
    ...}` would leave `n_averages` without its unit and no one would know."""
    from bace.experiment.transient import RunConfig

    specs = {s.name: s for s in specs_from_dataclass(
        RunConfig, exclude=("calibrate_trigger",), rename={"n_averages": "averages"},
        units={"settle_s": "s"}, editable={"record_length": False})}
    assert "averages" in specs and "n_averages" not in specs
    assert "calibrate_trigger" not in specs
    assert specs["settle_s"].unit == "s" and specs["averages"].unit == ""
    assert specs["record_length"].editable is False

    for bad in (dict(units={"n_average": "x"}), dict(choices={"nope": ("a",)}),
                dict(exclude=("nope",)), dict(rename={"nope": "x"}),
                dict(editable={"nope": False}), dict(types={"nope": "int"})):
        with pytest.raises(ParamError, match="names no such field"):
            specs_from_dataclass(RunConfig, **bad)


def test_specs_from_dataclass_refuses_to_guess_a_type():
    """A guessed type is a form that accepts the wrong thing."""

    @dataclass(frozen=True)
    class Odd:
        pairs: tuple[tuple[float, float], ...] = ()

    with pytest.raises(ParamError, match="Odd.pairs: cannot infer"):
        specs_from_dataclass(Odd)
    assert specs_from_dataclass(Odd, types={"pairs": "list[str]"})[0].type == "list[str]"
    with pytest.raises(ParamError, match="not a dataclass"):
        specs_from_dataclass(int)


def test_a_fixed_length_tuple_is_a_pair_not_a_list():
    """`tuple[float, float]` as `list[float]` would let a form submit three
    values for a window of two. The default `(0.0, 1.0)` looks exactly like a
    float list, so the refusal must hold against the default-value fallback
    as well, on both the resolved and the string road."""

    @dataclass(frozen=True)
    class Pair:
        window: tuple[float, float] = (0.0, 1.0)
        counts: tuple[int, ...] = ()

    with pytest.raises(ParamError, match="Pair.window: cannot infer .*fixed-length tuple"):
        specs_from_dataclass(Pair, exclude=("counts",))
    with pytest.raises(ParamError, match="Pair.counts: cannot infer .*only float and str"):
        specs_from_dataclass(Pair, exclude=("window",))
    with pytest.raises(ParamError, match="Unresolved.window: cannot infer .*is a sequence, but not"):
        specs_from_dataclass(Unresolved, exclude=("mode", "tag", "names", "other"))
    assert specs_from_dataclass(Pair, types={"window": "list[float]", "counts": "list[str]"})


def test_a_field_with_no_default_is_refused_unless_the_catalogue_gives_one():
    """`Axis.name/start/stop` -- the fields the service contract maps the
    axis_* parameters onto -- have no default. Inventing None would present
    them as optional on the wire and hand `Axis(name=None)` to the run."""
    from bace.core.axis import Axis

    @dataclass(frozen=True)
    class Req:
        vpre: float
        n: int = 1

    with pytest.raises(ParamError, match=r"^Req.vpre: has no default.*defaults=\{'vpre': \.\.\.\}"):
        specs_from_dataclass(Req)
    with pytest.raises(ParamError, match="^Axis.name: has no default"):
        specs_from_dataclass(Axis)
    assert [s.name for s in specs_from_dataclass(Req, exclude=("vpre",))] == ["n"]

    specs = {s.name: s for s in specs_from_dataclass(
        Axis, defaults={"name": "vpre", "start": -0.2, "stop": 0.2, "step": 0.05})}
    assert (specs["name"].type, specs["name"].default) == ("enum", "vpre")
    assert specs["name"].choices == ("vpre", "vcoll", "delay_ns")
    assert (specs["start"].type, specs["start"].default, specs["start"].nullable) == ("float", -0.2, False)
    assert specs["step"].default == 0.05                  # a supplied default wins
    assert Axis(**ParamSet(specs.values()).values()) == Axis("vpre", -0.2, 0.2, 0.05)
    with pytest.raises(ParamError, match="defaults names no such field"):
        specs_from_dataclass(Axis, defaults={"nope": 1})


def test_init_false_fields_are_not_parameters():
    """`values()` is meant to be splatted into the constructor; a field the
    constructor does not take would make that a TypeError."""

    @dataclass
    class B:
        a: int = 1
        b: int = field(default=2, init=False)

    specs = specs_from_dataclass(B)
    assert [s.name for s in specs] == ["a"]
    assert B(**ParamSet(specs).values()) == B(1)
    with pytest.raises(ParamError, match="names no such field"):
        specs_from_dataclass(B, units={"b": "x"})


@dataclass(frozen=True)
class Resolved:
    """Module level, so `get_type_hints` can see `Literal` and `Optional`
    in the module globals: the resolved road."""
    mode: Literal["dc", "pulse"] = "dc"
    tag: Optional[str] = None
    names: tuple[str, ...] = ("a",)


@dataclass(frozen=True)
class Unresolved:
    """One name the module cannot resolve makes `get_type_hints` fail for the
    whole class, so every field takes the string road."""
    mode: Literal["dc", "pulse"] = "dc"
    tag: Optional[str] = None
    names: tuple[str, ...] = ("a",)
    window: tuple[float, float] = (0.0, 1.0)
    other: NoSuchName = "x"                       # noqa: F821 -- deliberately undefined


def test_specs_from_dataclass_reads_literal_and_optional_annotations():
    """Both roads, pinned separately: `typing.get_type_hints` when the module
    resolves, the string parser when it does not. This file's own
    `from __future__ import annotations` is what makes the second road real."""
    assert get_type_hints(Resolved)["mode"] == Literal["dc", "pulse"]
    with pytest.raises(NameError):
        get_type_hints(Unresolved)

    for cls, skip in ((Resolved, ()), (Unresolved, ("window",))):
        specs = {s.name: s for s in specs_from_dataclass(cls, exclude=skip)}
        assert specs["mode"].type == "enum" and specs["mode"].choices == ("dc", "pulse")
        assert specs["tag"].type == "str" and specs["tag"].nullable
        assert specs["names"].type == "list[str]" and specs["names"].default == ("a",)
    assert specs["other"].type == "str"                  # the default's own type, last resort


# -- docs from the source -------------------------------------------------
def test_field_docs_returns_the_first_sentence():
    """`RunConfig` documents fields with a bare string on the next line. The
    tooltip wants the first sentence, not the four-paragraph rationale."""
    from bace.experiment.transient import RunConfig

    docs = field_docs(RunConfig)
    assert docs["n_averages"] == "Hardware averages per trace."
    assert docs["t0_int_reference"] == (
        "`record`, `trigger` or `pulse` — what `t0_int_s` is measured from.")
    # Absent, not "": a field with no string under it is missing from the map
    # rather than mapping to empty. `RunConfig` has none left -- every one of
    # its fields is documented -- so the case is made on a dataclass that does.
    from bace.core.axis import Axis
    assert "start" not in field_docs(Axis)
    full = field_docs(RunConfig, full=True)
    assert full["n_averages"].startswith("Hardware averages per trace. Noise falls")
    assert "**`record`** measures from the first sample." in full["t0_int_reference"]


def test_field_docs_works_on_every_config_and_never_raises():
    from bace.drivers.keithley2400 import SourceMeterConfig
    from bace.experiment.intensity_series import SeriesConfig
    from bace.experiment.jv import JVConfig
    from bace.experiment.rig import RigConfig
    from bace.experiment.transient import RunConfig

    assert field_docs(JVConfig)["led_levels_v"] == (
        "Drive levels for the light scans, at the 33220A output.")
    assert field_docs(SeriesConfig)["led_low_v"] == "Pulse low level."
    assert field_docs(RigConfig)["sense_resistor_ohm"] == (
        "The resistor between the device and the scope input.")
    assert field_docs(SourceMeterConfig)["current_compliance_a"] == (
        "Hard limit while sourcing voltage.")
    assert field_docs(RunConfig)

    @dataclass
    class Undocumented:
        a: int = 1
        b: float = 2.0

    assert field_docs(Undocumented) == {}
    assert field_docs(int) == {}                         # no source to read
    assert field_docs(type("Made", (), {})) == {}        # built at runtime


def test_first_sentence_does_not_split_on_abbreviations_or_numbers():
    assert first_sentence("Noise falls as 1/sqrt(n). Time rises.") == "Noise falls as 1/sqrt(n)."
    assert first_sentence("Wait 0.2 s, e.g. after the shutter. Then go.") == (
        "Wait 0.2 s, e.g. after the shutter.")
    assert first_sentence("One paragraph\nover two lines.\n\nSecond paragraph.") == (
        "One paragraph over two lines.")
    assert first_sentence("No terminator at all") == "No terminator at all"
    # a digit starts a sentence; a lowercase letter continues an abbreviation
    assert first_sentence("Zero by default. 200 is the panel value.") == "Zero by default."
    assert first_sentence("About approx. two divisions. Then go.") == "About approx. two divisions."


# -- TOML -----------------------------------------------------------------
def test_toml_layer_picks_dotted_paths_and_skips_what_is_missing():
    """A recipe that leaves a key out lets the lower layer show. Inventing a
    value for it would attribute the default to the file."""
    table = {"acquisition": {"n_averages": 20, "settle_s": 0.2},
             "illumination": {"level_v": 1.02}, "flat": 3}
    out = toml_layer(table, {"acquisition.n_averages": "n_averages",
                             "acquisition.missing": "x",
                             "illumination.level_v": "led_v",
                             "nowhere.at_all": "y",
                             "flat.deeper": "z",          # a scalar has no children
                             "flat": "flat"})
    assert out == {"n_averages": 20, "led_v": 1.02, "flat": 3}


def test_run_toml_layer_reads_the_repo_recipe_raw():
    """The raw tables, because provenance names the table: by the time a value
    is a `RunConfig` field it no longer knows it came from `[acquisition]`."""
    raw = run_toml_layer(REPO / "run.toml")
    assert raw["acquisition"]["n_averages"] == 200        # the validated recipe (2026-09-02)
    assert raw["illumination"]["level_v"] == 1.000

    ps = small_set()
    ps.set_layer(Source.RUN_TOML,
                 toml_layer(raw, {"acquisition.n_averages": "n_averages",
                                  "acquisition.t0_int_reference": "t0_int_reference",
                                  "acquisition.offset_correct": "offset_correct"}),
                 detail="run.toml [acquisition]")
    ps.update_layer(Source.RUN_TOML,
                    toml_layer(raw, {"illumination.level_v": "led_v"}),
                    detail="run.toml [illumination]")
    assert ps.get("n_averages") == ParamValue(200, Source.RUN_TOML, "run.toml [acquisition]")
    assert ps.get("led_v") == ParamValue(1.0, Source.RUN_TOML, "run.toml [illumination]")
    assert ps.get("t0_int_reference").value == "trigger"
    assert ps.get("offset_correct").value is True

    with pytest.raises(ParamError, match="no such run file"):
        run_toml_layer(REPO / "does-not-exist.toml")
