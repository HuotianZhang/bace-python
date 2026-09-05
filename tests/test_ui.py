"""The console's own suite, and the fixtures it develops against.

`ui/` is plain ES modules with no build step, so its unit tests are Node's
(`node --test ui/tests/`) and this file is how they reach the one command the
project runs. Node is a desk-machine tool: the lab PC has WinPython and no
Node at all, so the JavaScript tests **skip** there rather than fail, and what
is left is what Python can assert on its own — that the fixtures exist and are
the shapes `docs/ui-plan.md` M0 named.

The live proofs (a `jv` reaching `done`, and a client dropped at 1008
coming back without missing a numbered frame) need a running service and are
not in this file. They are `ui/tests/live.test.mjs`, run against one:

    python -m bace.service --sim --fast --port 8900
    BACE_SERVICE=http://127.0.0.1:8900 node --test ui/tests/live.test.mjs
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(REPO, "ui")
FIXTURES = os.path.join(UI, "fixtures")


def _fixture(name: str):
    path = os.path.join(FIXTURES, name)
    if not os.path.exists(path):
        pytest.skip(f"{name} is not recorded; tools/record_ui_fixtures.py writes it")
    if name.endswith(".jsonl"):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def test_hello_carries_the_whole_bench():
    """The one fixture no journal can stand in for.

    A journal contains no bench: `Hello` is never journalled, and
    `InstrumentState` frames say what one step changed rather than what the
    bench is. M1 is the rail and the chain, so without this the milestone that
    needs it most would have nothing to develop against.
    """
    hello = _fixture("hello_sim.json")
    assert hello["type"] == "Hello"
    assert hello["seq"] is None, "Hello is exempt from the dedupe it configures"
    bench = hello["data"]["bench"]
    for key in ("session", "state", "queue", "instruments", "inferred", "chain", "rig", "verdicts"):
        assert key in bench, f"the rail needs {key}"
    assert {"relay", "bias", "smu", "shutter", "led", "voc", "power", "temperature"} <= set(bench["instruments"])
    assert bench["chain"]["items"], "the chain strip needs its items"


def test_the_jv_fixture_has_the_arrays_the_journal_drops():
    """The J-V chart's fixture: the journal reduces `JVCurveDone` to metrics."""
    data = _fixture("jv_sim.json")
    assert data["curves"], "no curves"
    for curve in data["curves"]:
        assert len(curve["voltage"]) == len(curve["current"])
        assert curve["metrics"], "the metrics are interpolated, and labelled derived on screen"
    assert any(not curve["dark"] for curve in data["curves"]), "a light curve, for the V_oc"

    # The density is **mA/cm²** on the wire (`experiment.jv.current_density`),
    # so the console converts nothing and picks no prefix. A fixture recorded
    # without a pixel area carries `density: null` and proves none of that --
    # which is what the first recording of this file did, `run.toml` having no
    # `[jv]` table.
    lit = [c for c in data["curves"] if not c["dark"]][0]
    assert lit["density"] is not None, "recorded with pixel_area_cm2 = 0.04"
    assert len(lit["density"]) == len(lit["current"])
    area_cm2 = 0.04
    for j, i in zip(lit["density"][:5], lit["current"][:5]):
        assert abs(j - i * 1e3 / area_cm2) <= abs(j) * 1e-9, "mA/cm², not A/cm²"
    # `metrics.jsc` is the exception, and stays amps: it is interpolated from
    # the current array, whatever area the run was given.
    assert abs(lit["metrics"]["jsc"]) < 1.0, "jsc is A, not a density"


def test_the_recorded_stream_is_the_wire_and_not_the_journal():
    """Decimated traces and `StepPhase` — the shape only a live socket has."""
    frames = _fixture("stream_bace_sim.jsonl")
    phases = [f for f in frames if f["type"] == "StepPhase"]
    steps = [f for f in frames if f["type"] == "StepDone"]
    assert phases and steps
    assert all(f["seq"] is None for f in phases), "StepPhase is live-only, never numbered"
    for frame in steps:
        assert frame["decimated"]["light.y"]["stride"] >= 1
        assert frame["data"]["verdict"]["level"] in ("ok", "warn")
        kept = len(frame["data"]["light"]["y"])
        n_full, stride = (frame["decimated"]["light.y"][k] for k in ("n_full", "stride"))
        # The last kept sample is the record's own final one, not one stride
        # after its predecessor: `t0 + (n-1)*dt`.
        expected = (n_full - 1) // stride + 1 + (1 if (n_full - 1) % stride else 0)
        assert kept == expected


def test_every_recorded_stream_runs_through_to_parked():
    """A recording that stopped at `done` would be a run the bench never made
    safe. Every terminal state is reached *through* `parked` (contract §2), and
    that transition is what tells a console the worker is free again -- so the
    fixtures have to carry it or the state the rail shows cannot be tested.
    """
    for name in ("stream_bace_sim.jsonl", "stream_jv_sim.jsonl", "stream_pipeline_sim.jsonl"):
        frames = _fixture(name)
        states = [f["data"]["state"] for f in frames if f["type"] == "RunStateChanged"]
        assert states[-1] == "parked", f"{name} stops at {states[-1]}"
        assert "done" in states


def test_the_pipeline_fixture_reuses_its_shot_numbers():
    """The fixture is only a test of node identity if the nodes collide.

    Two `bace` nodes under one `run_id`, each numbering `loop`/`index` from
    one, so `loop:index` alone is not an identity -- and the loop's `Progress`
    carries `data.node_path = "rep=1"` where the leaf's carries `""`, which is
    the three counters of `docs/ui-rules.md` §5 arriving as three events.
    """
    frames = _fixture("stream_pipeline_sim.jsonl")
    steps = [f for f in frames if f["type"] == "StepDone"]
    assert len(steps) == 4
    assert len({(f["data"]["loop"], f["data"]["index"]) for f in steps}) == 2
    assert len({f["node_path"] for f in steps}) == 2
    scopes = {f["data"]["node_path"] for f in frames if f["type"] == "Progress"}
    assert scopes == {"", "rep=1", "rep=2"}


def test_the_transient_fixture_is_the_data_endpoints_shape():
    """The rig day's HDF5, rendered as `GET /runs/{id}/data` answers."""
    data = _fixture("transient_20260902_153722.json")
    for key in ("axis", "values", "q_mean", "q_std", "q_all", "time_s", "light", "dark", "photo",
                "last_shot", "kept", "requested"):
        assert key in data
    assert len(data["time_s"]) == len(data["light"][0]) == len(data["photo"][0])
    assert data["last_shot"]["t0_int_record_s"] > 0, "the integration window has to be drawable"
    assert data["_fixture"]["from"].startswith("acceptance/"), "and it says where it came from"


def test_the_validate_fixtures_are_the_tree_m5_has_to_prove():
    """The canonical 9 T x 5 level tree, as `docs/ui-plan.md` M5 names it, and
    the endpoint's whole answer for it.

    `POST /pipelines/validate` touches nothing, so this is the one fixture in
    the set that can be recorded on a live bench at any time -- and the one
    the pipeline tab is entirely drawn from: the checks, the schedule in
    order, the counters and the cost all come out of a single answer.
    """
    v = _fixture("validate_txill_sim.json")
    assert v["valid"] is True
    assert v["counters"] == {"temperatures": 9, "levels": 5, "modules": 90, "shots": 4500}
    assert len(v["schedule"]) == 198, "9 x (enter + 5 x (enter + 2 modules + exit) + exit)"
    assert len(v["node_paths"]) == 90
    # The range the tree posted comes back as the levels it made: five, by the
    # rounded count `pipeline.range_values` settled once. The console reads
    # them off this rather than rounding a second time.
    assert v["tree"]["children"][0]["levels_v"] == [1.01, 1.015, 1.02, 1.025, 1.03]
    assert v["folder_pattern"].endswith("_YYYYMMDD_HHMMSS")
    # And the cost is a floor, because no settle has been measured on this
    # bench: the console renders "at least", and never a finish time.
    assert v["cost"]["lower_bound"] is True
    assert v["cost"]["finish_at"] is None
    assert v["cost"]["waiting_s"] is None
    assert all(t["settle_s"] is None for t in v["cost"]["per_temperature"])


def test_the_bound_fixture_is_what_the_canonical_tree_has_none_of():
    """A `temperature` **module**, and duplicate sibling modules.

    The module's setpoint binds the rest of the run rather than the rest of
    one iteration (`executor.ExecCtx`), and the resolver says nothing about it
    -- `Step.detail.temperature_k` is the enclosing *loop's* setpoint and is
    null on every one of these steps. That absence is the fixture's point:
    `ui/lib/tree.js` re-walks the schedule with the executor's rule, and
    `ui/tests/tree.test.mjs` pins the answer against this file.
    """
    v = _fixture("validate_bound_sim.json")
    steps = [s for s in v["schedule"] if s["kind"] == "module"]
    assert [s["node_path"] for s in steps] == [
        "rep=1/temperature", "rep=1/bace", "rep=1/bace#2", "rep=1/wait",
        "rep=2/temperature", "rep=2/bace", "rep=2/bace#2", "rep=2/wait",
    ], "duplicate siblings are numbered by the service, and only by the service"
    assert all(s["detail"]["temperature_k"] is None for s in steps)
    assert steps[0]["params"]["setpoint_k"]["value"] == 250.0
    # The counter-case for the cost: nothing settles, so nothing is unmeasured.
    assert v["cost"]["lower_bound"] is False
    assert v["cost"]["finish_at"] is not None


def test_the_nested_fixture_is_a_temperature_loop_inside_a_temperature_loop():
    """Legal, pathological, and the case the console's time bar got wrong.

    `pipeline.estimate` keeps `open_temperatures` as a *list*: a module's time
    is attributed to every temperature it is inside, and each temperature's
    own settle and hold are counted once. So the total covers the inner holds
    -- and a bar that drew one block per outer iteration lost them, reading
    62 s against a cost of 102 s. `ui/tests/tree.test.mjs` asserts the bar
    totals this file's `cost.total_s`.
    """
    v = _fixture("validate_nested_sim.json")
    assert v["valid"] is True
    enters = [s for s in v["schedule"] if s["kind"] == "loop-enter"]
    assert [s["node_path"] for s in enters] == [
        "T=290K", "T=290K/T=200K", "T=290K/T=180K",
        "T=250K", "T=250K/T=200K", "T=250K/T=180K",
    ]
    assert v["counters"]["temperatures"] == 4, "two values in each of the two loops"
    assert len(v["cost"]["per_temperature"]) == 6, "one per iteration, inner ones included"
    holds = sum(t["hold_s"] for t in v["cost"]["per_temperature"])
    assert holds == 2 * 30 + 4 * 10, "the inner holds are in the cost, so they must be in the bar"


def test_the_journals_have_no_seq_gaps():
    """`docs/ui-kickoff.md` says these carry "real seq gaps". They do not.

    A journal is one monotonic counter with `StepPhase` — the only unjournalled
    frame — consuming no number. Gaps belong to the *socket*, where a client
    falls behind and is dropped at 1008, and are tested against a live
    `--sim --fast` scan instead (`ui/tests/live.test.mjs`). This asserts the
    correction rather than leaving it as a claim in a document.
    """
    folder = os.path.join(REPO, "acceptance", "20260902_service-vs-labview", "journals")
    if not os.path.isdir(folder):
        pytest.skip("the acceptance journals are not in this checkout")
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            seqs = [json.loads(line)["seq"] for line in fh if line.strip()]
        assert seqs == list(range(len(seqs))), f"{name} is not 0…N"


def test_every_offline_fixture_is_registered_and_present():
    """`ui/lib/replay.js` lists what the offline page can load. A fixture that
    is in the repo but not in that list cannot be opened without a service, and
    an entry pointing at a file that is not there is a 404 in the browser --
    both are silent, so they are asserted here instead.
    """
    listed = re.findall(r"url:\s*'([^']+)'", open(os.path.join(UI, "lib", "replay.js"),
                                                  encoding="utf-8").read())
    assert listed, "the fixture list is empty"
    for url in listed:
        path = os.path.normpath(os.path.join(UI, url))
        assert os.path.exists(path), f"{url} is listed and not there"

    on_disk = {n for n in os.listdir(FIXTURES) if n.endswith((".jsonl", ".json"))}
    registered = {os.path.basename(u) for u in listed}
    assert on_disk <= registered, f"recorded but not loadable offline: {sorted(on_disk - registered)}"


@pytest.mark.skipif(shutil.which("node") is None, reason="no Node on this machine (the lab PC has none)")
def test_the_console_suite_passes():
    """`node --test ui/tests/` — the fold, the socket, the numbers, the rail,
    the card rows, the scales and the charts, the run monitor, and what the
    shell does per frame rather than what it draws.

    Every suite in `ui/tests/` but `live.test.mjs`, which needs a service. A
    suite that is written and not listed here does not run in CI or on the lab
    PC, which is how `fields.test.mjs` sat unrun after M2.
    """
    result = subprocess.run(
        ["node", "--test", "ui/tests/store.test.mjs", "ui/tests/stream.test.mjs",
         "ui/tests/format.test.mjs", "ui/tests/rail.test.mjs", "ui/tests/fields.test.mjs",
         "ui/tests/render.test.mjs", "ui/tests/scale.test.mjs", "ui/tests/charts.test.mjs",
         "ui/tests/monitor.test.mjs", "ui/tests/tree.test.mjs", "ui/tests/rig.test.mjs",
         "ui/tests/power.test.mjs"],
        cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_results_fixtures_carry_the_identity_and_a_partial_cell():
    """What the results tab draws from (`docs/ui-plan.md` M6), and the shape
    `docs/naming-plan.md` rule 1 promised it: every row of `GET /runs` names
    the device it ran on, every module node of `GET /runs/{id}` carries the
    temperature triple, its LED level, its V_oc and what it measured -- and
    the grid has a cell that was stopped early, because R2·3's "outlined,
    never averaged in silently, never dropped" needs one to be drawn at all.
    """
    rows = _fixture("runs_sim.json")
    assert rows and all(r["sample"]["sample"] == "s4" for r in rows), "the identity, per run"
    assert {r["kind"] for r in rows} == {"manual", "pipeline"}

    grid = _fixture("run_grid_sim.json")
    assert grid["sample"]["sample"] == "s4" and grid["from"] == "session"
    cells = {k: n for k, n in grid["nodes"].items() if n["module"] == "bace"}
    assert len(cells) == 4, "2 T x 2 levels"
    for node in grid["nodes"].values():
        assert (node["temperature_how"], node["temperature_source"]) == ("operator", "operator"), (
            "the recorder answered the pauses by hand, and every node says so")
        assert node["led_v"] in (1.01, 1.02)
    partial = [n for n in cells.values() if n["kept"] < n["requested"]]
    assert len(partial) == 1 and partial[0]["outcome"] == "stopped"
    assert partial[0]["q_mean"] and partial[0]["q_std"], (
        "a stopped node keeps the statistics of the loops that ran (LoopDone), not nothing")
    assert all(n["q_mean"] and n["values"] == [n["voc"]] for n in cells.values()), (
        "one point per cell, at the V_oc it was centred on")

    manual = _fixture("run_bace_sim.json")
    node = manual["nodes"]["bace"]
    assert (node["temperature_k"], node["temperature_how"], node["temperature_source"]) == (290.0, "typed", ""), (
        "a manual run has no temperature node above it: the typed 290 K, said to be typed")
    assert node["voc_how"] == "jv_bace" and node["offset_corrected"] is True
    assert manual["params_as_executed"]["bace"]["voc"]["source"] == "derived"
