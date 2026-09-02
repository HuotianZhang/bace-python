"""The console's own suite, and the fixtures it develops against.

`ui/` is plain ES modules with no build step, so its unit tests are Node's
(`node --test ui/tests/`) and this file is how they reach the one command the
project runs. Node is a desk-machine tool: the lab PC has WinPython and no
Node at all, so the JavaScript tests **skip** there rather than fail, and what
is left is what Python can assert on its own — that the fixtures exist and are
the shapes `docs/ui-plan.md` M0 named.

The live proofs (a `jv_dark` reaching `done`, and a client dropped at 1008
coming back without missing a numbered frame) need a running service and are
not in this file. They are `ui/tests/live.test.mjs`, run against one:

    python -m bace.service --sim --fast --port 8900
    BACE_SERVICE=http://127.0.0.1:8900 node --test ui/tests/live.test.mjs
"""
from __future__ import annotations

import json
import os
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


def test_the_transient_fixture_is_the_data_endpoints_shape():
    """The rig day's HDF5, rendered as `GET /runs/{id}/data` answers."""
    data = _fixture("transient_20260902_153722.json")
    for key in ("axis", "values", "q_mean", "q_std", "q_all", "time_s", "light", "dark", "photo",
                "last_shot", "kept", "requested"):
        assert key in data
    assert len(data["time_s"]) == len(data["light"][0]) == len(data["photo"][0])
    assert data["last_shot"]["t0_int_record_s"] > 0, "the integration window has to be drawable"
    assert data["_fixture"]["from"].startswith("acceptance/"), "and it says where it came from"


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


@pytest.mark.skipif(shutil.which("node") is None, reason="no Node on this machine (the lab PC has none)")
def test_the_console_suite_passes():
    """`node --test ui/tests/` — the fold, the socket, and the number rules."""
    result = subprocess.run(
        ["node", "--test", "ui/tests/store.test.mjs", "ui/tests/stream.test.mjs",
         "ui/tests/format.test.mjs"],
        cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
