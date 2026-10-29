"""Parameter labels: what the console shows in place of a code name.

`modules.LABELS` gives every module parameter a label. The console draws the
label and keeps the code name as the identifier (`docs/ui-rules.md` §1), so a
parameter without one would appear as a bare code name, and a label that
repeats its unit would print the unit twice.
"""
from __future__ import annotations

import re

import pytest

from bace.experiment.rig import RigConfig
from bace.params import ParamSpec
from bace.service.modules import MODULE_NAMES, Catalogue, ModuleError, _labelled


@pytest.fixture(scope="module")
def cat():
    return Catalogue(rig_config=RigConfig(), run_toml={}, history=None)


def test_every_module_parameter_has_a_label(cat):
    for name in MODULE_NAMES:
        labels = [s.label for s in cat.param_specs(name)]
        assert all(labels), f"{name}: unlabelled {[s.name for s in cat.param_specs(name) if not s.label]}"
        assert len(set(labels)) == len(labels), f"{name}: two parameters share a label"


def test_a_label_is_a_short_sentence_case_noun_phrase_without_a_unit(cat):
    units = {"s", "ms", "ns", "v", "a", "k", "hz", "nm", "cm2", "%", "w"}
    for name in MODULE_NAMES:
        for s in cat.param_specs(name):
            words = s.label.split()
            assert 1 <= len(words) <= 3, f"{s.name}: {s.label!r} is longer than three words"
            assert s.label[0].isupper(), f"{s.name}: {s.label!r} is not sentence case"
            assert not re.search(r"\b[a-z]+_[a-z]", s.label), f"{s.name}: {s.label!r} is a code name"
            assert words[-1].lower().strip("()") not in units, f"{s.name}: {s.label!r} ends in a unit"


def test_the_wire_carries_the_label_and_the_run_toml_key(cat):
    row = {r["name"]: r for r in cat.as_wire("bace")["params"]}["n_averages"]
    assert row["label"] == "Hardware averages"
    assert row["recipe"] == ["acquisition.n_averages"]
    settle = {r["name"]: r for r in cat.as_wire("jv")["params"]}["settle_s"]
    assert settle["label"] == "Dwell per point"
    assert {r["name"]: r for r in cat.as_wire("bace")["params"]}["settle_s"]["label"] == "Light settle"


def test_a_parameter_without_a_label_is_refused():
    with pytest.raises(ModuleError, match="no label"):
        _labelled("bace", [ParamSpec("not_in_the_table", "int", 1)])


def test_the_estimate_comes_with_its_parts(cat):
    est = cat.as_wire("bace")["estimate"]
    assert est["seconds"] == cat.as_wire("bace")["estimate_s"]
    assert est["shot_source"] in ("journal", "default") and est["loops"] >= 1 and est["points"] >= 1
    assert set(cat.as_wire("jv")["estimate"]) == {"seconds", "curves", "points"}
    assert cat.as_wire("note")["estimate"] == {"seconds": 0.0}
