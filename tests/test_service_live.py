"""The bench card while a run holds the worker: what the running step
implies, overlaid on the Start read-back and marked `inferred`.

`GET /bench` cannot read the instruments while a run is on the bus, and the
design pins the rail in every view. The overlay is folded from the run's
own events, so the first test feeds `LiveState` a real simulated run event
by event and checks the overlay at the moments that matter; the second
holds a simulated scan inside its light acquisition and reads the session's
snapshot from the outside, the way the console would.
"""
from __future__ import annotations

import pathlib
import queue
import threading

import pytest

from bace.experiment import events as E
from bace.experiment.rig import RigConfig
from bace.params import run_toml_layer
from bace.service.executor import run_pipeline
import types

from bace.service.live import LiveState
from bace.service.modules import Catalogue, RunContext, VocSource
from bace.service.pipeline import parse_tree, resolve
from bace.service.rigs import Bench
from bace.service.session import Session, tree_for_module
from bace.storage.naming import RunMetadata

REPO = pathlib.Path(__file__).resolve().parents[1]
RECIPE = REPO / "tests" / "run-quickcheck.toml"   # the frozen quick-check recipe these tests pin
TIMEOUT = 20.0
FAST = {"n_averages": 8, "settle_s": 0.0, "dark_settle_s": 0.0, "record_length": 400,
        "t0_int_s": 2.71e-7, "t0_int_reference": "record", "calibrate_trigger": False,
        "led_settle_s": 0.0}


def _catalogue() -> Catalogue:
    return Catalogue(rig_config=RigConfig(), run_toml=run_toml_layer(RECIPE),
                     history=None, sample={"sample": "s4", "material": "SIM", "pixel": "a"})


def _events(tmp_path, tree: dict, session_voc=None):
    b = Bench.build_simulated(RigConfig(), seed=3, fast=True)
    schedule = resolve(parse_tree(tree), _catalogue(), session_voc=session_voc)

    def factory(step):
        return RunContext(run_id="r-001", node_path=step.node_path, out_folder=str(tmp_path),
                          metadata=RunMetadata(sample="s4", material="SIM", pixel="a"),
                          sleep=lambda s: None)

    evs = list(run_pipeline(b.rig, schedule, catalogue=_catalogue(), ctx_factory=factory,
                            session_voc=session_voc))
    return schedule, evs


def test_the_overlay_follows_a_simulated_scan_event_by_event(tmp_path):
    voc = VocSource(value=0.9, led_v=1.02, run_id="r-000", node_path="jv_bace", how="jv_bace")
    tree = {"kind": "loop", "loop": "illumination", "levels_v": [1.02], "led_low_v": 0.4,
            "led_settle_s": 0.0,
            "children": [{"kind": "module", "module": "jv_bace", "params": {"step_v": 0.1}},
                         {"kind": "module", "module": "bace",
                          "params": {**FAST, "n_loops": 1, "centre_on_voc": True}}]}
    schedule, evs = _events(tmp_path, tree)
    live = LiveState(RigConfig())
    base = Bench.build_simulated(RigConfig()).read_back()["instruments"]
    assert base["bias"]["output"] is False and base["relay"]["position"] == "amplifier"

    seen: dict[str, dict] = {}
    for ev in evs:
        live.apply(ev, schedule)
        over = live.overlay(base)
        key = type(ev).__name__ + (f":{ev.phase}" if isinstance(ev, E.StepPhase) else "")
        seen.setdefault(key, over)
        if isinstance(ev, E.NodeStarted) and ev.node_path.endswith("/jv_bace"):
            assert over["relay"] == {"position": "sourcemeter", "how": "inferred"}
            assert over["bias"]["output"] is False, "nothing inferred about the bias yet"
        if isinstance(ev, E.InstrumentState) and ev.values.get("shutter") == "open":
            assert over["shutter"] == {"open": True, "how": "inferred"}
            assert (over["led"]["mode"], over["led"]["output"]) == ("DC", True)
        if isinstance(ev, E.NodeStarted) and ev.node_path.endswith("/bace"):
            assert over["relay"]["position"] == "amplifier" and over["relay"]["how"] == "inferred"
            assert (over["led"]["output"], over["led"]["mode"]) == (True, "DC"), (
                "the jv's unwind shut the shutter and left the LED at DC (2026-09-02)")
            assert over["shutter"]["open"] is False
        if isinstance(ev, E.RunStarted):
            assert over["bias"]["output"] is True and over["bias"]["how"] == "inferred"
            assert over["bias"]["frequency_hz"] == 500.0
            assert over["led"] == {**base["led"], "output": True, "mode": "PULSE",
                                   "high_v": 1.02, "low_v": 0.4, "frequency_hz": 500.0,
                                   "how": "inferred"}
        if isinstance(ev, E.InstrumentState) and "bias_arm_source" in ev.values:
            assert over["bias"]["arm_source"] == ev.values["bias_arm_source"]
            assert over["bias"]["polarity"] == ev.values["bias_output_polarity"]
        if isinstance(ev, E.StepStarted):
            assert over["bias"]["high_v"] == pytest.approx(ev.setpoint.vpre / 4.0)
            assert over["bias"]["low_v"] == pytest.approx(ev.setpoint.vcoll / 4.0)
    assert seen["StepPhase:acquire light"]["shutter"]["open"] is True
    assert seen["StepPhase:acquire dark"]["shutter"]["open"] is False
    assert seen["StepPhase:dark levels"]["bias"]["high_v"] == pytest.approx(0.0), (
        "the dark reference swings about 0 V (translated), and the overlay says so")
    assert seen["RunFinished"]["bias"]["output"] is False, "the transient's finally: bias off"
    done = [ev for ev in evs if isinstance(ev, E.NodeDone) and ev.node_path.endswith("/bace")]
    live2 = LiveState(RigConfig())
    for ev in evs:
        live2.apply(ev, schedule)
        if ev is done[0]:
            over = live2.overlay(base)
            assert over["shutter"]["open"] is False
            assert (over["led"]["output"], over["led"]["mode"]) == (True, "PULSE"), (
                "the bace's unwind leaves the LED pulsing; the shutter is the light switch")
            assert over["bias"]["output"] is False and over["smu"]["output"] is False
            assert over["relay"]["position"] == "amplifier", "the relay stays where it is"
    live2.apply(E.RunStateChanged("done", "finished"), schedule)
    assert live2.inferred == [] and live2.overlay(base) == base


def test_the_session_snapshot_says_live_while_a_scan_is_inside_its_acquisition(tmp_path):
    """The console's view: a bace held inside `scope.acquire` (a shot in
    flight), and `GET /bench` says relay amplifier, bias LIVE, LED pulsing,
    shutter open -- every one `inferred` -- with `state: running`, while
    `read_at` is still the Start read-back's. `StepPhase` frames reach the
    subscriber with `seq` null and are in neither the ring nor the journal."""
    s = Session(RigConfig(), run_toml_layer(RECIPE), out=str(tmp_path / "runs"),
                mode="sim", fast=True, seed=5, session_id="20260902_230000",
                sample={"sample": "s4", "material": "SIM", "pixel": "a"})
    with s:
        gate, entered = threading.Event(), threading.Event()
        real = s.bench.sim.scope.acquire

        def held(*args, **kw):
            entered.set()
            assert gate.wait(TIMEOUT)
            return real(*args, **kw)

        s.bench.sim.scope.acquire = held
        live = s.subscribe(queue.SimpleQueue())
        s.bench_read()
        before = s.bench_snapshot()["read_at"]
        run_id, _ = s.submit(tree_for_module("bace", {**FAST, "n_loops": 2, "voc": 0.9}))
        assert entered.wait(TIMEOUT)

        snap = s.bench_snapshot()
        assert snap["state"] == "running" and snap["run"]["run_id"] == run_id
        assert snap["read_at"] > before, "the read-back itself is the one Start took"
        inst = snap["instruments"]
        assert inst["relay"] == {"position": "amplifier", "how": "inferred"}
        assert inst["bias"]["output"] is True and inst["bias"]["how"] == "inferred"
        assert inst["bias"]["arm_source"] == "EXT", "what the run's InstrumentState read back"
        assert inst["led"]["output"] is True and inst["led"]["mode"] == "PULSE"
        assert (inst["led"]["high_v"], inst["led"]["low_v"]) == (1.02, 0.4), "the recipe's level"
        assert inst["shutter"] == {"open": True, "how": "inferred"}, "acquire light: lit"
        assert set(snap["inferred"]) >= {"relay", "bias", "led", "shutter"}
        assert inst["voc"] == {"value": None}, "a typed V_oc is the run's, not the session's rail"

        gate.set()
        assert s.wait_run(run_id, TIMEOUT)
        after = s.bench_snapshot()
        assert after["state"] == "idle" and after["inferred"] == []
        assert after["read_at"] == snap["read_at"], "no read-back since Start; nothing inferred now"
        assert after["instruments"]["bias"]["output"] is False
        assert "how" not in after["instruments"]["bias"], "nothing inferred once it ended"

        got = []
        while not live.empty():
            got.append(live.get_nowait())
        phases = [f for f in got if f["type"] == "StepPhase"]
        assert len(phases) == 2 * 7 and {f["seq"] for f in phases} == {None}
        assert [f["data"]["phase"] for f in phases[:3]] == ["levels", "light settle",
                                                             "acquire light"]
        assert phases[0]["run_id"] == run_id and phases[0]["node_path"] == "bace"
        assert not any(f["type"] == "StepPhase" for f in s.events_since(0)), "not in the ring"
        with open(s.journal.path, encoding="utf-8") as fh:
            assert "StepPhase" not in fh.read(), "not in the journal"
        seqs = [f["seq"] for f in got if f["seq"] is not None]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), "the numbered frames are intact"
        assert s.errors == []


def test_a_light_node_puts_its_led_read_back_on_the_rail(tmp_path):
    """A `light` node switching the generator on from parked is exactly what
    the snapshot Start took cannot know: it was taken before the node ran, and
    while the run holds the worker nothing re-reads the bench. Without the
    output flag on the event, the overlay set mode and level onto the old
    `output: False` and the rail showed the LED off through an illuminated
    `jv` sweep -- and `jv` is the module whose *curve label* comes from that
    same light."""
    live = LiveState(RigConfig())
    base = Bench.build_simulated(RigConfig()).read_back()["instruments"]
    assert base["led"]["output"] is False, "parked: the generator is off"

    live.apply(E.InstrumentState({"shutter": "open", "illumination": "light",
                                  "led_mode": "DC", "led_level_v": 1.02,
                                  "led_output": True}), None)
    over = live.overlay(base)
    assert over["led"]["output"] is True and over["led"]["how"] == "inferred"
    assert (over["led"]["mode"], over["led"]["high_v"]) == ("DC", 1.02)
    assert over["shutter"] == {"open": True, "how": "inferred"}

    # And the other direction: `led_mode=off` has to be able to turn it back.
    live.apply(E.InstrumentState({"shutter": "shut", "illumination": "dark",
                                  "led_mode": "OFF", "led_output": False}), None)
    assert live.overlay(base)["led"]["output"] is False


def test_an_unreadable_light_reads_as_unknown_not_as_the_snapshots_stale_value():
    """The run asked and the bench could not say, and *that is newer* than the
    snapshot Start took.

    The first attempt at this skipped the unread `?` so a good read-back could
    not be overwritten with "unknown" — but leaving Start's stale values in
    place is worse: `ui/lib/fields.js` combines the LED's mode and output with
    the shutter to decide whether the card says lit or dark, so a stale pair
    makes the screen claim a definite illumination for a curve the file is
    recording as `as found unknown`. The screen disagreeing with the file is
    the one thing this read-back exists to prevent."""
    live = LiveState(RigConfig())
    base = Bench.build_simulated(RigConfig()).read_back()["instruments"]
    base = {**base, "led": {**base["led"], "output": True, "mode": "DC"},
            "shutter": {**base["shutter"], "open": True}}
    live.apply(E.InstrumentState({"shutter": "?", "illumination": "unknown",
                                  "led_mode": "?", "led_level_v": None,
                                  "led_output": None}), None)
    over = live.overlay(base)
    assert over["led"]["output"] is None, "unread, and not the stale True"
    assert over["led"]["mode"] == "?", "unread, and not the stale DC"
    assert over["shutter"]["open"] is None, "unread, and not the stale open"

    # Which is exactly what the card needs to say unknown: `fields.js` reads
    # `null`/`?` on any of the three as "cannot tell", the same rule
    # `illumination_state` applies.
    assert over["led"]["how"] == "inferred" and over["shutter"]["how"] == "inferred"


def test_park_shuts_the_shutter_and_the_overlay_says_so():
    """`LIGHT_UNWOUND` names the modules that leave the bench dark. It was
    written to keep `jv` and `light` *out* — they do not touch the light — and
    left `park` out with them, though `Rig.park()` shuts the shutter outright.
    So `light(shutter=open)` then `park` then something long left the overlay
    saying open for the whole of it. The mistake runs both ways."""
    from bace.service.live import LIGHT_UNWOUND

    assert "park" in LIGHT_UNWOUND
    assert not {"jv", "light"} & LIGHT_UNWOUND, "these still must not imply it"

    live = LiveState(RigConfig())
    base = Bench.build_simulated(RigConfig()).read_back()["instruments"]
    base = {**base, "shutter": {**base["shutter"], "open": True}}
    live.step = types.SimpleNamespace(module="park", node_path="park", values=lambda: {})
    live.apply(E.NodeDone(node_path="park", outcome="ok", detail={}), None)
    assert live.overlay(base)["shutter"]["open"] is False
