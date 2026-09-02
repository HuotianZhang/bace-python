"""The executor on the simulator: the three bindings, the pause, the two stops.

Transcript-style throughout. Every event the worker hands out is recorded
together with what the simulated instruments held at that instant -- the
relay position, the 33220A's levels and mode, the shutter -- so a binding is
asserted as "the LED was pulsing at the loop's level when this shot was
taken" and "the device was on the amplifier side when this shot was taken",
never as "nothing raised". The V_oc binding is checked by value: the number
the bace centred on is the number the jv_bace at the same level measured.
"""
from __future__ import annotations

import os
import pathlib
import threading
import time

import numpy as np
import pytest

from bace.experiment import events as E
from bace.experiment.jv import JVCurveDone, JVFinished
from bace.experiment.rig import RigConfig
from bace.params import run_toml_layer
from bace.service import executor
from bace.service.executor import run_pipeline
from bace.service.modules import Catalogue, RunContext, VocSource
from bace.service.pipeline import parse_tree, resolve
from bace.service.rigs import Bench
from bace.service.worker import Job, RunWorker, StopMode
from bace.storage.naming import RunMetadata

REPO = pathlib.Path(__file__).resolve().parents[1]
TIMEOUT = 20.0
NO_SLEEP = lambda s: None                                      # noqa: E731
RUN_ID = "20260902_210000-001"

FAST = {"n_averages": 8, "settle_s": 0.0, "dark_settle_s": 0.0, "record_length": 400,
        "t0_int_s": 2.71e-7, "t0_int_reference": "record", "calibrate_trigger": False,
        "led_settle_s": 0.0}
"""The bace overrides every tree here types: the simulator's geometry (see
`test_transient_sim.run`) and no settling."""


def catalogue() -> Catalogue:
    return Catalogue(rig_config=RigConfig(), run_toml=run_toml_layer(REPO / "run.toml"),
                     history=None, sample={"sample": "s4", "material": "SIM", "pixel": "a"})


def bench(seed: int = 3, **kw) -> Bench:
    return Bench.build_simulated(RigConfig(**kw), seed=seed, fast=True)


def bace_node(n_loops: int = 2, **params) -> dict:
    return {"kind": "module", "module": "bace",
            "params": {**FAST, "n_loops": n_loops, "centre_on_voc": True,
                       "axis_start": 0.0, "axis_stop": 0.0, "axis_step": 0.0, **params}}


def jv_node(**params) -> dict:
    return {"kind": "module", "module": "jv_bace", "params": {"step_v": 0.05, **params}}


def tree(temperatures=(295, 250), levels=(1.02, 1.04), n_loops: int = 2) -> dict:
    """The design's tree at 2 x 2: temperature > illumination > [jv_bace, bace]."""
    inner = {"kind": "loop", "loop": "illumination", "levels_v": list(levels),
             "led_low_v": 0.4, "led_settle_s": 0.0,
             "children": [jv_node(), bace_node(n_loops)]}
    if temperatures is None:
        return inner
    return {"kind": "loop", "loop": "temperature", "values_k": list(temperatures),
            "tolerance_k": 0.2, "hold_s": 0.0, "timeout_s": 60.0, "children": [inner]}


def schedule_of(obj: dict, cat: Catalogue | None = None, **kw):
    return resolve(parse_tree(obj), cat or catalogue(), **kw)


def factory(job, out_folder: str, store: dict | None = None):
    """The session's `ctx_factory`, reduced to what the executor needs."""
    def make(step) -> RunContext:
        return RunContext(run_id=RUN_ID, node_path=step.node_path, out_folder=out_folder,
                          metadata=RunMetadata(sample="s4", material="SIM", pixel="a"),
                          sleep=NO_SLEEP,
                          abort=job.abort_check if job is not None else (lambda: False),
                          on_data=None if store is None else
                          (lambda path, d: store.__setitem__(path, d)),
                          wait_for_operator=None if job is None else
                          (lambda detail, on_poll=None: job.wait_for_operator(on_poll=on_poll)))
    return make


class Harness:
    """Runs one executor job on a real `RunWorker` and records, for every
    event, the state of the simulated instruments at that instant."""

    def __init__(self, b: Bench, hook=None):
        self.bench, self.sim, self.hook = b, b.sim, hook
        self.events: list[tuple[str, E.Event, dict]] = []
        self.states: list[tuple[str, str]] = []
        self.paused = threading.Event()
        self.worker = RunWorker(on_event=self._on_event, on_state=self._on_state)

    def _on_event(self, job, ev):
        sim = self.sim
        self.events.append((job.node_path, ev, {
            "router": sim.router.position, "led_levels": sim.led.last_levels,
            "led_mode": sim.bench.led_mode, "led_drive": sim.bench.led_drive_v,
            "led_on": sim.led.output_enabled, "shutter": sim.bench.shutter_open}))
        if self.hook is not None:
            self.hook(job, ev)

    def _on_state(self, job, state, reason):
        self.states.append((state, reason))
        if state == "paused":
            self.paused.set()

    def start(self, job: Job) -> "Harness":
        self.worker.submit(job)
        self.worker.start()
        return self

    def finish(self) -> "Harness":
        assert self.worker.wait_idle(TIMEOUT), "the worker never went idle"
        assert self.worker.shutdown(TIMEOUT)
        return self

    def of(self, cls):
        return [(p, ev, s) for p, ev, s in self.events if isinstance(ev, cls)]

    def types(self):
        return [type(ev).__name__ for _, ev, _ in self.events]

    def state_names(self):
        return [s for s, _ in self.states]

    def wait_paused(self, setpoint: float, what: str = "temperature") -> None:
        assert self.paused.wait(TIMEOUT), f"never paused for {setpoint} K"
        self.paused.clear()
        _, last, _ = self.events[-1]
        assert isinstance(last, E.NeedsOperator) and last.what == what
        assert last.detail["setpoint_k"] == setpoint, "paused on the wrong node"


def run_job(b: Bench, schedule, out: str, *, store=None, vocs=None, park=True,
            hook=None, emit=None) -> tuple[Harness, Job]:
    cat = catalogue()

    def make(job):
        return run_pipeline(b.rig, schedule, catalogue=cat, ctx_factory=factory(job, out, store),
                            job=job, on_voc=None if vocs is None else vocs.append, emit=emit)

    job = Job(RUN_ID, "run", make, park=b.rig.park if park else None)
    return Harness(b, hook).start(job), job


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


# -- the canonical tree, 2 x 2 ---------------------------------------------------
def test_the_canonical_tree_runs_with_the_three_bindings(tmp_path):
    b = bench()
    sim = b.sim
    out = str(tmp_path / "pipe")
    schedule = schedule_of(tree())
    assert len(schedule.modules) == 8
    store, vocs = {}, []
    h, job = run_job(b, schedule, out, store=store, vocs=vocs)

    # The temperature loop pauses the worker at each setpoint until the
    # operator answers; nothing below the node runs while it waits.
    for setpoint, reached in ((295.0, 294.8), (250.0, 250.0)):
        h.wait_paused(setpoint)
        assert job.state == "paused" and job.paused
        started_below = [p for p, ev, _ in h.events
                         if isinstance(ev, E.NodeStarted) and p.startswith(f"T={setpoint:g}K/")]
        assert started_below == [], "a child started before the operator answered"
        assert job.resume({"temperature_k": reached, "note": f"set to {reached} K by hand"})
    h.finish()

    # every module step ran, in schedule order, with the envelope stamped
    # with its own path
    modules = [(p, ev) for p, ev, _ in h.of(E.NodeStarted) if ev.kind not in ("temperature", "illumination")]
    assert [p for p, _ in modules] == [
        "T=295K/led=1.020V/jv_bace", "T=295K/led=1.020V/bace",
        "T=295K/led=1.040V/jv_bace", "T=295K/led=1.040V/bace",
        "T=250K/led=1.020V/jv_bace", "T=250K/led=1.020V/bace",
        "T=250K/led=1.040V/jv_bace", "T=250K/led=1.040V/bace"]
    assert all(p == ev.node_path for p, ev in modules)
    assert h.state_names() == ["preflight", "running", "paused", "running", "paused",
                               "running", "done", "parked"]
    assert job.state == "done" and job.error is None

    # binding 2: the bace centred on the V_oc the jv_bace at the same level
    # and temperature measured -- by value, and a different value per level
    measured = {p: ev.metrics.voc for p, ev, _ in h.of(JVCurveDone) if not ev.dark}
    assert len(measured) == 4 and all(v is not None for v in measured.values())
    axes = h.of(E.AxisResolved)
    assert len(axes) == 4
    for p, ev, _ in axes:
        source = measured[parent_of(p) + "/jv_bace"]
        assert ev.voc == source and ev.values[0] == pytest.approx(source)
    assert measured["T=295K/led=1.040V/jv_bace"] > measured["T=295K/led=1.020V/jv_bace"], (
        "the simulated device's V_oc rises with drive, so a bace that centred on "
        "the wrong level's V_oc would be visible here")
    assert [(v.how, v.led_v, v.node_path) for v in vocs] == [
        ("jv_bace", 1.02, "T=295K/led=1.020V/jv_bace"),
        ("jv_bace", 1.04, "T=295K/led=1.040V/jv_bace"),
        ("jv_bace", 1.02, "T=250K/led=1.020V/jv_bace"),
        ("jv_bace", 1.04, "T=250K/led=1.040V/jv_bace")]
    assert [v.value for v in vocs] == [measured[v.node_path] for v in vocs]

    # binding 1: the loop's level drove the LED, for the curve and the pulse
    for p, ev, s in h.of(JVCurveDone):
        level = float(p.split("/")[1][4:-1])
        if ev.dark:
            assert (s["led_mode"], s["led_drive"], s["shutter"]) == ("OFF", 0.0, False)
        else:
            assert ev.led_level_v == level
            assert (s["led_mode"], s["led_drive"], s["shutter"]) == ("DC", level, True)
    shots = h.of(E.StepDone)
    assert len(shots) == 8
    for p, ev, s in shots:
        level = float(p.split("/")[1][4:-1])
        assert s["led_mode"] == "PULSE" and s["led_on"] and s["led_levels"] == (level, 0.4)

    # binding 3: the relay was on the SourceMeter side for every curve and on
    # the amplifier side for every shot, and each move was announced
    assert {s["router"] for _, _, s in h.of(JVCurveDone)} == {"sourcemeter"}
    assert {s["router"] for _, _, s in shots} == {"amplifier"}
    relay = [ev.text for _, ev, _ in h.of(E.Notice) if ev.text.startswith("relay ")]
    assert relay == ["relay sourcemeter -> amplifier for bace",
                     "relay amplifier -> sourcemeter for jv_bace"] * 3 + [
                        "relay sourcemeter -> amplifier for bace"]

    # the operator's temperature reached the subtree: the readings, then the
    # folder names of everything measured under that node
    resumed = h.of(E.OperatorResumed)
    assert [(p, ev.detail["temperature_k"]) for p, ev, _ in resumed] == \
        [("T=295K", 294.8), ("T=250K", 250.0)]
    assert [(ev.kelvin, ev.in_band, ev.source) for _, ev, _ in h.of(E.TemperatureRead)] == \
        [(294.8, True, "operator"), (250.0, True, "operator")]
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    assert len(done) == 14 and {ev.outcome for ev in done.values()} == {"ok"}
    for p, ev in done.items():
        if p.count("/") == 2:                              # a module node
            folder = ev.detail["folder"]
            assert os.path.dirname(folder) == out and os.path.isdir(folder)
            name = os.path.basename(folder)
            assert ("294.8K" if p.startswith("T=295K") else "250K") in name
            assert ("1020mVLED" if "1.020" in p else "1040mVLED") in name
            assert ev.detail["temperature_k"] == (294.8 if p.startswith("T=295K") else 250.0)
    assert len(os.listdir(out)) == 8, "one parent folder, one child per module run"
    assert sum(1 for n in os.listdir(out) if "mVVOC" in n) == 4

    # what the node summaries and the data store say
    for p, ev in done.items():
        if p.endswith("/bace"):
            assert (ev.detail["kept"], ev.detail["requested"]) == (2, 2)
            assert ev.detail["summary"].startswith("Q ") and ev.detail["summary"].endswith("2/2")
            assert ev.detail["voc"]["how"] == "jv_bace"
            assert (store[p]["kept"], store[p]["requested"]) == (2, 2)
        elif p.endswith("/jv_bace"):
            assert ev.detail["summary"].startswith("2 curves · V_oc ")
            assert len(store[p]["curves"]) == 2
    loops = [(p, ev.done, ev.total) for p, ev, _ in h.of(E.Progress) if ev.node_path]
    assert loops[:3] == [("T=295K", 0, 2), ("T=295K/led=1.020V", 0, 2),
                         ("T=295K/led=1.020V", 1, 2)]
    assert loops[-1] == ("T=250K", 2, 2)

    # parked on the way out
    assert not sim.bench.bias_output and not sim.bench.smu_output
    assert not sim.led.output_enabled and not sim.bench.shutter_open
    assert job.parked


def test_a_manual_run_takes_the_sessions_voc_as_the_source_it_is(tmp_path):
    """A one-node tree, run without a worker: the session's jv_bace at the
    same level is handed to the builder as that source, not as a typed
    number, so the run records `how = jv_bace` and the folder names it."""
    b = bench()
    session_voc = VocSource(value=0.9, led_v=1.02, run_id="20260902_210000-000",
                            node_path="jv_bace", how="jv_bace", ts=1.0)
    schedule = schedule_of(bace_node(1), session_voc=session_voc)
    step = schedule.modules[0]
    assert step.params["voc"].source.value == "derived" and step.params["voc"].value == 0.9
    vocs, store = [], {}
    evs = list(run_pipeline(b.rig, schedule, catalogue=catalogue(),
                            ctx_factory=factory(None, str(tmp_path), store),
                            on_voc=vocs.append, session_voc=session_voc))
    assert [type(e).__name__ for e in evs[:3]] == ["NodeStarted", "RunStarted", "AxisResolved"]
    assert evs[2].voc == 0.9
    done = evs[-1]
    assert isinstance(done, E.NodeDone) and done.node_path == "bace" and done.outcome == "ok"
    assert done.detail["voc"] == {**session_voc.as_dict()}
    assert "900mVVOC" in os.path.basename(done.detail["folder"])
    assert vocs == [], "the session's own source is not announced back to it"
    assert store["bace"]["voc"] == 0.9
    assert not b.sim.led.output_enabled and not b.sim.bench.bias_output


def test_measure_dc_becomes_the_sessions_next_voc(tmp_path):
    b = bench()
    schedule = schedule_of(bace_node(1, measure_dc=True))
    vocs = []
    evs = list(run_pipeline(b.rig, schedule, catalogue=catalogue(),
                            ctx_factory=factory(None, str(tmp_path)), on_voc=vocs.append))
    dc = next(e for e in evs if isinstance(e, E.DCMeasured))
    assert [(v.how, v.led_v, v.value, v.node_path) for v in vocs] == \
        [("measure_dc", 1.02, dc.dc.voc, "bace")]
    assert evs[-1].detail["voc"]["how"] == "measure_dc"


# -- the two stops ------------------------------------------------------------------
def test_after_shot_keeps_the_shot_closes_the_open_nodes_and_runs_nothing_more(tmp_path):
    b = bench()
    sim = b.sim
    out = str(tmp_path / "pipe")
    schedule = schedule_of(tree(temperatures=None, n_loops=3))
    store = {}

    def stop_at_first_shot(job, ev):
        if isinstance(ev, E.StepDone) and ev.index == 0:
            job.request_stop(StopMode.AFTER_SHOT)

    h, job = run_job(b, schedule, out, store=store, hook=stop_at_first_shot)
    h.finish()

    shots = h.of(E.StepDone)
    assert len(shots) == 1 and shots[0][0] == "led=1.020V/bace"
    aborted = h.of(E.RunAborted)
    assert [(p, ev.reason, ev.done, ev.total) for p, ev, _ in aborted] == \
        [("led=1.020V/bace", "requested", 1, 3)], "the module reported the stop itself"
    done = [(p, ev.outcome) for p, ev, _ in h.of(E.NodeDone)]
    assert done == [("led=1.020V/jv_bace", "ok"), ("led=1.020V/bace", "stopped"),
                    ("led=1.020V", "stopped")]
    bace_done = next(ev for p, ev, _ in h.of(E.NodeDone) if p == "led=1.020V/bace")
    assert (bace_done.detail["kept"], bace_done.detail["requested"]) == (1, 3)
    assert bace_done.detail["summary"] == "stopped after 1/3"
    assert os.path.isdir(bace_done.detail["folder"]), "the shot in flight was written"
    assert (store["led=1.020V/bace"]["kept"], store["led=1.020V/bace"]["requested"]) == (1, 3)
    assert not any(p.startswith("led=1.040V") for p, _, _ in h.events), "later steps never ran"
    assert h.state_names() == ["preflight", "running", "stopping", "stopped", "parked"]
    assert not sim.led.output_enabled and not sim.bench.bias_output
    assert not sim.bench.shutter_open and job.state == "stopped"


def test_a_stop_during_a_jv_is_reported_by_the_executor_when_the_module_stays_quiet(tmp_path):
    """`run_jv` returns early on abort without a `RunAborted`; the run still
    has to end `stopped`, so the executor says it."""
    b = bench()
    schedule = schedule_of(tree(temperatures=None))

    def stop_after_the_dark_curve(job, ev):
        if isinstance(ev, JVCurveDone) and ev.dark:
            job.request_stop(StopMode.AFTER_SHOT)

    h, job = run_job(b, schedule, str(tmp_path), hook=stop_after_the_dark_curve)
    h.finish()

    curves = h.of(JVCurveDone)
    assert len(curves) == 1 and not h.of(JVFinished)
    assert [ev.text[:19] for _, ev, _ in h.of(E.Notice) if ev.level == "warning"] == \
        ["J-V run aborted bef"]
    assert [(p, ev.outcome) for p, ev, _ in h.of(E.NodeDone)] == \
        [("led=1.020V/jv_bace", "stopped"), ("led=1.020V", "stopped")]
    assert [(ev.reason, ev.done, ev.total) for _, ev, _ in h.of(E.RunAborted)] == \
        [("requested", 1, 4)]
    assert h.types()[-1] == "RunAborted" and job.state == "stopped"
    assert not h.of(E.StepDone), "the bace after it never started"


def test_abort_discards_the_shot_unwinds_and_parks(tmp_path):
    b = bench()
    sim = b.sim
    schedule = schedule_of(tree(temperatures=None, n_loops=3))

    def abort_after_the_first_shots_progress(job, ev):
        # At the bace's own Progress, not its StepDone: the worker
        # synthesizes the RunAborted from the last Progress it saw, and at
        # the StepDone that would still be the jv's.
        if isinstance(ev, E.Progress) and not ev.node_path and job.node_path.endswith("/bace"):
            job.request_stop(StopMode.ABORT)

    h, job = run_job(b, schedule, str(tmp_path), hook=abort_after_the_first_shots_progress)
    h.finish()

    assert len(h.of(E.StepDone)) == 1
    assert h.types()[-1] == "RunAborted"
    assert h.events[-1][1] == E.RunAborted(reason="aborted", done=1, total=3), (
        "synthesized by the worker from the last Progress; the generator was closed")
    assert [(p, ev.outcome) for p, ev, _ in h.of(E.NodeDone)] == [("led=1.020V/jv_bace", "ok")], (
        "the bace and the loop above it were unwound, not reported")
    assert h.state_names() == ["preflight", "running", "aborted", "parked"]
    assert job.state == "aborted" and job.parked
    assert not sim.led.output_enabled and sim.bench.led_mode == "OFF"
    assert not sim.bench.bias_output and not sim.bench.shutter_open


def test_stopping_while_paused_closes_the_temperature_node_and_aborting_unwinds_it(tmp_path):
    b = bench()
    schedule = schedule_of(tree(temperatures=(250,), levels=(1.02,)))
    h, job = run_job(b, schedule, str(tmp_path))
    h.wait_paused(250.0)
    job.request_stop(StopMode.AFTER_SHOT)
    h.finish()
    assert h.types() == ["NodeStarted", "Progress", "NeedsOperator", "NodeDone", "RunAborted"]
    assert h.events[3][1].outcome == "stopped" and h.events[3][1].node_path == "T=250K"
    assert h.events[4][1] == E.RunAborted(reason="requested", done=0, total=2)
    assert h.state_names() == ["preflight", "running", "paused", "stopping", "stopped",
                               "parked"], "stopping at the NodeDone, stopped at the RunAborted"

    b2 = bench()
    h, job = run_job(b2, schedule, str(tmp_path / "b"))
    h.wait_paused(250.0)
    job.request_stop(StopMode.ABORT)
    h.finish()
    assert h.types() == ["NodeStarted", "Progress", "NeedsOperator", "RunAborted"]
    assert h.events[-1][1].reason == "aborted"
    assert h.state_names() == ["preflight", "running", "paused", "aborted", "parked"]
    assert job.parked and not b2.sim.bench.bias_output


# -- failure ----------------------------------------------------------------------------
def test_a_module_the_builder_refuses_fails_the_run_with_the_node_named(tmp_path):
    """No V_oc source and `centre_on_voc`: `validate` would have refused the
    tree, and the builder refuses it again before touching anything."""
    b = bench()
    schedule = schedule_of(bace_node(1))            # resolve alone does not refuse
    h, job = run_job(b, schedule, str(tmp_path))
    h.finish()
    assert h.types() == ["NodeStarted", "NodeDone", "RunFailed"]
    _, done, _ = h.events[1]
    assert done.outcome == "failed" and "centre_on_voc" in done.detail["error"]
    _, failed, _ = h.events[2]
    assert failed.where == "bace" and failed.error.startswith("ModuleError: bace: centre_on_voc")
    assert job.state == "failed" and job.error == failed.error
    assert h.state_names() == ["preflight", "running", "failed", "parked"]
    assert b.sim.bench.shots == 0 and not os.listdir(tmp_path)


def test_a_failure_inside_a_loop_closes_the_loop_as_failed(tmp_path):
    b = bench()
    obj = {"kind": "loop", "loop": "repeat", "count": 2,
           "children": [bace_node(1), {"kind": "module", "module": "note",
                                       "params": {"text": "never"}}]}
    h, job = run_job(b, schedule_of(obj), str(tmp_path))
    h.finish()
    assert [(p, ev.outcome) for p, ev, _ in h.of(E.NodeDone)] == \
        [("rep=1/bace", "failed"), ("rep=1", "failed")]
    assert h.of(E.RunFailed)[0][1].where == "rep=1/bace"
    assert not any(isinstance(ev, E.Notice) for _, ev, _ in h.events)


# -- the temperature console ----------------------------------------------------------------
def wired(seed: int = 3, **kw) -> Bench:
    """A simulated bench with the 331 stand-in attached: under `--sim` any
    non-empty console name attaches it (the default rig.toml names none)."""
    return bench(seed, temperature_console="sim", **kw)


def verdicts(h: Harness, code: str) -> list[tuple[str, E.Verdict]]:
    return [(p, ev) for p, ev, _ in h.of(E.Verdict) if ev.code == code]


def test_with_the_331_attached_a_temperature_loop_settles_on_its_own(tmp_path):
    """2 T x 1 level: no pause, the readings' `in_band` flips as the stand-in
    approaches each setpoint, a `temperature.settled` verdict per T with the
    settle on the poll clock, the measured kelvin (not the setpoint) in every
    folder name and metadata below the node, and a mark per node in the
    console's audit log."""
    b = wired()
    t331 = b.sim.temperature
    assert b.rig.temperature is t331 and t331.kelvin == 294.8
    obj = tree(temperatures=(295, 250), levels=(1.02,), n_loops=1)
    obj["timeout_s"] = 600.0        # 294.8 -> 250 K takes the stand-in ~30 polls, 145 s
    schedule = schedule_of(obj)
    h, job = run_job(b, schedule, str(tmp_path))
    h.finish()

    assert job.state == "done" and job.error is None
    assert not h.of(E.NeedsOperator) and not h.of(E.OperatorResumed)
    assert h.state_names() == ["preflight", "running", "done", "parked"], "never paused"
    assert t331.setpoints == [295.0, 250.0], "written once per node, in order"

    reads = [(p, ev) for p, ev, _ in h.of(E.TemperatureRead)]
    assert {ev.source for _, ev in reads} == {"simulated"}
    at_295 = [ev for p, ev in reads if p == "T=295K"]
    at_250 = [ev for p, ev in reads if p == "T=250K"]
    assert len(at_295) == 1 and at_295[0].in_band, "0.2 K away: in band at the first poll"
    assert len(at_250) > 2 and at_250[0].in_band is False and at_250[-1].in_band is True
    assert all(ev.setpoint_k == 250.0 for ev in at_250)
    flips = [ev.in_band for ev in at_250]
    assert flips == sorted(flips) and flips.count(True) == 1, (
        "hold_s = 0: the poll that enters the band settles the node")
    assert abs(at_250[-1].kelvin - 250.0) <= 0.2 and at_250[-1].kelvin != 250.0

    settled = verdicts(h, "temperature.settled")
    assert [(p, ev.level, ev.node_path) for p, ev in settled] == \
        [("T=295K", "ok", "T=295K"), ("T=250K", "ok", "T=250K")]
    d = settled[1][1].data
    assert d["setpoint_k"] == 250.0 and d["kelvin"] == at_250[-1].kelvin
    assert d["settle_s"] == (len(at_250) - 1) * executor.TEMPERATURE_POLL_S
    assert (d["polls"], d["source"], d["hold_s"], d["held_s"]) == (len(at_250), "simulated", 0.0, 0.0)
    assert settled[1][1].text == f"250 K reached in {d['settle_s'] / 60:.0f} min, held 0 s"
    assert settled[0][1].data["settle_s"] == 0.0

    # what was measured, not what was asked for, names the subtree
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    assert {ev.outcome for ev in done.values()} == {"ok"}
    for path, last in (("T=295K", at_295[-1].kelvin), ("T=250K", at_250[-1].kelvin)):
        assert done[path].detail["temperature_k"] == last
        for p, ev in done.items():
            if p.startswith(path + "/") and p.count("/") == 2:
                assert ev.detail["temperature_k"] == last
                assert f"{last:g}K" in os.path.basename(ev.detail["folder"])
                assert "250K_" not in os.path.basename(ev.detail["folder"])

    # the console's audit log: a mark at the setpoint and one at the settle, per node
    assert [n for n in t331.notes if "T=250K" in n] == [
        f"bace {RUN_ID} T=250K: setpoint 250 K +/-0.2 K hold 0 s",
        f"bace {RUN_ID} T=250K: settled at {at_250[-1].kelvin:.3f} K"]
    assert len(t331.notes) == 4

    # the ETA learned the settle the console took
    progress = [(p, ev.done, ev.eta_s) for p, ev, _ in h.of(E.Progress) if ev.node_path]
    assert progress[-1] == ("T=250K", 2, 0.0)
    assert not b.sim.bench.bias_output and not b.sim.led.output_enabled


def test_a_settle_that_times_out_pauses_and_the_operator_resumes_or_stops(tmp_path):
    """A sluggish cryostat under a 1 mK band: after `timeout_s` on the poll
    clock the node warns, pauses with what it last read, and a resume
    continues with the last reading as the subtree's temperature."""
    b = wired()
    b.sim.temperature.time_constant_reads = 1000
    obj = tree(temperatures=(250,), levels=(1.02,), n_loops=1)
    obj["tolerance_k"], obj["timeout_s"] = 0.001, 60.0
    h, job = run_job(b, schedule_of(obj), str(tmp_path))
    h.wait_paused(250.0, what="temperature timeout")

    reads = [ev for _, ev, _ in h.of(E.TemperatureRead)]
    assert len(reads) == 13, "polls at 0, 5, ... 60 s on the poll clock, then the timeout"
    assert all(ev.in_band is False for ev in reads)
    [(p, warn)] = verdicts(h, "temperature.timeout")
    assert p == "T=250K" and warn.level == "warn"
    assert warn.text.startswith("250 K not reached in 1 min: last reading ")
    assert warn.data["elapsed_s"] == 60.0 and warn.data["kelvin"] == reads[-1].kelvin
    assert warn.data["reason"] == "timeout" and warn.data["polls"] == 13
    need = h.events[-1][1]
    assert need.detail["kelvin"] == reads[-1].kelvin and need.detail["elapsed_s"] == 60.0
    assert need.detail["reason"] == "timeout" and need.detail["setpoint_k"] == 250.0
    assert job.state == "paused"
    assert not any(p.startswith("T=250K/") for p, _, _ in h.events), "nothing measured yet"

    assert job.resume({"note": "close enough"})
    h.finish()
    assert job.state == "done"
    [(_, resumed)] = [(p, ev) for p, ev, _ in h.of(E.OperatorResumed)]
    assert resumed.note == "close enough"
    last = [ev for _, ev, _ in h.of(E.TemperatureRead)][-1]
    assert last.source == "simulated", "no temperature typed: the last reading stands"
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    bace = done["T=250K/led=1.020V/bace"]
    assert bace.detail["temperature_k"] == last.kelvin and bace.outcome == "ok"
    assert f"{last.kelvin:g}K" in os.path.basename(bace.detail["folder"])
    assert h.state_names() == ["preflight", "running", "paused", "running", "done", "parked"]
    assert any("operator resumed" in n for n in b.sim.temperature.notes)

    # the same timeout, and the operator stops instead
    b2 = wired()
    b2.sim.temperature.time_constant_reads = 1000
    h, job = run_job(b2, schedule_of(obj), str(tmp_path / "b"))
    h.wait_paused(250.0, what="temperature timeout")
    job.request_stop(StopMode.AFTER_SHOT)
    h.finish()
    assert h.types()[-2:] == ["NodeDone", "RunAborted"] and job.state == "stopped"
    assert h.events[-2][1].outcome == "stopped" and not h.of(E.StepDone)


def test_a_setpoint_the_console_refuses_is_a_crit_verdict_and_a_pause(tmp_path):
    """400 K is above the console's 350 K ceiling: the refusal is the
    console's, in its words, nothing is clamped, and the operator decides.
    The one reading before the write (the cryostat as found) is on the
    stream; nothing is polled after a setpoint that was never written."""
    b = wired()
    h, job = run_job(b, schedule_of(tree(temperatures=(400,), levels=(1.02,), n_loops=1)),
                     str(tmp_path))
    h.wait_paused(400.0, what="temperature refused")
    assert h.types() == ["NodeStarted", "Progress", "TemperatureRead", "Verdict", "NeedsOperator"]
    found = h.events[2][1]
    assert (found.kelvin, found.setpoint_k, found.in_band) == (294.8, 400.0, False)
    crit = h.events[3][1]
    assert (crit.level, crit.code, crit.node_path) == ("crit", "temperature.refused", "T=400K")
    assert crit.text == "setpoint 400.000 K exceeds the 350.0 K limit for this cryostat"
    assert crit.data["setpoint_k"] == 400.0 and crit.data["error"] == crit.text
    assert crit.data["status"] == 403
    assert b.sim.temperature.setpoints == [] and b.sim.temperature.setpoint_k == 294.8, (
        "refused, not clamped to 350 K")
    need = h.events[4][1]
    assert need.detail["error"] == crit.text and need.detail["setpoint_k"] == 400.0
    assert len(h.of(E.TemperatureRead)) == 1, "nothing to poll for a setpoint that was never written"
    job.request_stop(StopMode.AFTER_SHOT)
    h.finish()
    assert job.state == "stopped" and not h.of(E.StepDone)
    assert h.state_names() == ["preflight", "running", "paused", "stopping", "stopped", "parked"]


def test_a_stop_during_a_settle_is_honoured_at_the_next_poll(tmp_path):
    """Abort: the generator is closed at the next reading and the rig parked,
    nothing reported for the open node. After-shot: the tree is closed at
    the next poll, `NodeDone(stopped)` for the node and an honest
    `RunAborted`."""
    schedule = schedule_of(tree(temperatures=(250,), levels=(1.02,), n_loops=1))

    def stop_at_the_third_reading(mode):
        seen = []

        def hook(job, ev):
            if isinstance(ev, E.TemperatureRead):
                seen.append(ev)
                if len(seen) == 3:
                    job.request_stop(mode)
        return hook

    b = wired()
    b.sim.temperature.time_constant_reads = 1000
    h, job = run_job(b, schedule, str(tmp_path), hook=stop_at_the_third_reading(StopMode.ABORT))
    h.finish()
    assert len(h.of(E.TemperatureRead)) == 3 and job.state == "aborted" and job.parked
    assert h.types()[-1] == "RunAborted" and h.events[-1][1].reason == "aborted"
    assert not h.of(E.NodeDone), "unwound, not reported"
    assert h.state_names() == ["preflight", "running", "aborted", "parked"]
    assert not b.sim.bench.bias_output and not b.sim.bench.shutter_open
    assert b.sim.temperature.setpoint_k == 250.0, "the cryostat keeps its setpoint; it is not parked"

    b2 = wired()
    b2.sim.temperature.time_constant_reads = 1000
    h, job = run_job(b2, schedule, str(tmp_path / "b"),
                     hook=stop_at_the_third_reading(StopMode.AFTER_SHOT))
    h.finish()
    assert len(h.of(E.TemperatureRead)) == 3 and job.state == "stopped"
    assert h.types()[-3:] == ["TemperatureRead", "NodeDone", "RunAborted"]
    assert (h.events[-2][1].node_path, h.events[-2][1].outcome) == ("T=250K", "stopped")
    assert h.events[-1][1] == E.RunAborted(reason="requested", done=0, total=2)
    assert not h.of(E.NeedsOperator) and not h.of(E.Verdict)


def test_a_named_console_that_is_silent_pauses_as_an_unwired_one_does(tmp_path):
    """`build_real` leaves `rig.temperature` None when the console does not
    answer at start-up: the pause path, unchanged, with no reading from a
    console and the operator's number naming the subtree."""
    b = wired()
    b.rig.temperature = None
    schedule = schedule_of(tree(temperatures=(250,), levels=(1.02,)))
    h, job = run_job(b, schedule, str(tmp_path))
    h.wait_paused(250.0)
    job.resume({"temperature_k": 250.05})
    h.finish()
    assert h.types()[:5] == ["NodeStarted", "Progress", "NeedsOperator", "OperatorResumed",
                             "TemperatureRead"]
    operator = h.events[4][1]
    assert (operator.kelvin, operator.setpoint_k, operator.in_band, operator.source) == \
        (250.05, 250.0, True, "operator")
    assert job.state == "done" and b.sim.temperature.setpoints == [] and b.sim.temperature.notes == []
    folders = [ev.detail["folder"] for _, ev, _ in h.of(E.NodeDone) if "folder" in ev.detail]
    assert all("250.05K" in os.path.basename(f) for f in folders)


def test_without_a_console_nothing_is_polled(tmp_path):
    b = bench()
    assert b.rig_config.temperature_console == "" and b.rig.temperature is None
    h, job = run_job(b, schedule_of(tree(temperatures=(250,), levels=(1.02,))), str(tmp_path))
    h.wait_paused(250.0)
    job.resume({})
    h.finish()
    assert job.state == "done"
    assert not h.of(E.TemperatureRead), "no reading and no temperature typed: nothing to say"
    assert not h.of(E.Verdict)
    folders = [ev.detail["folder"] for _, ev, _ in h.of(E.NodeDone) if "folder" in ev.detail]
    assert folders and all("250K" in os.path.basename(f) for f in folders), \
        "the setpoint names the folder when the operator typed no reading"


# -- the arrays reach the data sink whichever way a node ends ---------------------------
def test_the_data_sink_sees_arrays_at_full_precision(tmp_path):
    b = bench()
    store = {}
    schedule = schedule_of(tree(temperatures=None, levels=(1.02,), n_loops=2))
    h, job = run_job(b, schedule, str(tmp_path), store=store)
    h.finish()
    bace = store["led=1.020V/bace"]
    assert isinstance(bace["light"], np.ndarray) and bace["light"].shape[0] == 1
    assert bace["light"].shape[1] == bace["time_s"].size == 320
    assert isinstance(bace["last_shot"]["cumulative_q"], np.ndarray)
    jv = store["led=1.020V/jv_bace"]
    assert [c["label"] for c in jv["curves"]] == ["dark", "1.02 V"]
    assert isinstance(jv["curves"][1]["voltage"], np.ndarray)


# -- the ETA, re-derived from what the run measured -----------------------------------------
def test_loop_progress_carries_an_eta_that_follows_the_measured_settle(tmp_path):
    """Before the first temperature settles, the ETA is the schedule's (no
    settle history, so hold only, plus the modules' estimates); after the
    operator took a quarter of a second, the next temperature's Progress
    says so, and the measured module durations replace their estimates."""
    import time as _time

    b = bench()
    schedule = schedule_of(tree(temperatures=(295, 250), levels=(1.02,), n_loops=1))
    h, job = run_job(b, schedule, str(tmp_path))
    h.wait_paused(295.0)
    _time.sleep(0.25)
    job.resume({"temperature_k": 295.0})
    h.wait_paused(250.0)
    _time.sleep(0.25)
    job.resume({"temperature_k": 250.0})
    h.finish()
    assert job.state == "done"

    loops = [(p, ev.done, ev.eta_s) for p, ev, _ in h.of(E.Progress) if ev.node_path]
    assert all(eta is not None for _, _, eta in loops)
    first = [eta for p, done, eta in loops if p == "T=295K"][0]        # its enter
    second = [eta for p, done, eta in loops if p == "T=250K"][0]
    estimates = sum(s.estimate_s or 0.0 for s in schedule.modules)
    assert first == pytest.approx(estimates, abs=1e-9), (
        "no settle known and no module measured yet: the schedule's own estimates")
    assert 0.25 <= second < first, (
        "the measured settle (0.25 s) is in, and the sim's millisecond modules replaced "
        "the 0.8 s-a-shot estimate")
    assert loops[-1] == ("T=250K", 2, 0.0), "nothing left at the last exit"
    assert all(eta >= 0.0 for _, _, eta in loops)


# -- a temperature module binds the nodes after it; readings go out live while paused ------
def test_a_temperature_module_binds_the_nodes_after_it(tmp_path):
    """A `temperature` module beside a `bace` under a repeat loop: the bace
    is measured at what the module settled at -- the console's reading, not
    the session's number and not the setpoint -- folder name and metadata
    alike, and the loop's own record carries it; without a console, at what
    the operator typed."""
    def obj(setpoint: float) -> dict:
        return {"kind": "loop", "loop": "repeat", "count": 1,
                "children": [{"kind": "module", "module": "temperature",
                              "params": {"setpoint_k": setpoint, "tolerance_k": 0.2,
                                         "hold_s": 0.0, "timeout_s": 600.0}},
                             bace_node(1, voc=0.9)]}

    b = wired()
    h, job = run_job(b, schedule_of(obj(250.0)), str(tmp_path))
    h.finish()
    assert job.state == "done" and job.error is None
    [(_, settled)] = verdicts(h, "temperature.settled")
    kelvin = settled.data["kelvin"]
    assert kelvin != 250.0 and abs(kelvin - 250.0) <= 0.2
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    assert set(done) == {"rep=1/temperature", "rep=1/bace", "rep=1"}
    assert done["rep=1/temperature"].detail["temperature_k"] == kelvin
    bace = done["rep=1/bace"]
    assert bace.outcome == "ok" and bace.detail["temperature_k"] == kelvin, (
        "the sibling is measured at the settled kelvin")
    assert f"{kelvin:g}K" in os.path.basename(bace.detail["folder"])
    assert done["rep=1"].detail["temperature_k"] == kelvin
    assert h.of(E.StepDone), "and it measured"

    # no console: the operator's number binds the sibling the same way
    b = bench()
    h, job = run_job(b, schedule_of(obj(250.0)), str(tmp_path / "b"))
    h.wait_paused(250.0)
    job.resume({"temperature_k": 250.1, "note": "set by hand"})
    h.finish()
    assert job.state == "done"
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    assert done["rep=1/bace"].detail["temperature_k"] == 250.1
    assert "250.1K" in os.path.basename(done["rep=1/bace"].detail["folder"])


def test_readings_taken_during_a_pause_go_out_live_through_emit(tmp_path):
    """Contract section 7: while a pause lasts the console is polled and each
    reading goes out through the out-of-band hook *while the run is still
    paused* -- and a resume with nothing typed measures the subtree at the
    last reading polled, not at the settle loop's last."""
    b = wired()
    b.sim.temperature.time_constant_reads = 1000
    obj = tree(temperatures=(250,), levels=(1.02,), n_loops=1)
    obj["tolerance_k"], obj["timeout_s"] = 0.001, 5.0
    live = []
    h, job = run_job(b, schedule_of(obj), str(tmp_path), emit=live.append)
    h.wait_paused(250.0, what="temperature timeout")
    deadline = time.monotonic() + TIMEOUT
    while not live and time.monotonic() < deadline:
        time.sleep(0.01)
    assert live and job.state == "paused", "a reading reached the hook while the run was paused"
    polled = live[0]
    assert isinstance(polled, E.TemperatureRead) and polled.source == "simulated"
    assert polled.setpoint_k == 250.0 and polled.in_band is False
    in_loop = [ev for _, ev, _ in h.of(E.TemperatureRead)]
    assert len(in_loop) == 2, "polls at 0 and 5 s on the poll clock, then the timeout"
    assert polled.kelvin < in_loop[-1].kelvin, "one step beyond the settle loop's last reading"

    assert job.resume({})
    h.finish()
    assert job.state == "done"
    assert all(ev.kelvin >= in_loop[-1].kelvin for _, ev, _ in h.of(E.TemperatureRead)), (
        "the polled readings went out through the hook, not the generator")
    done = {p: ev for p, ev, _ in h.of(E.NodeDone)}
    bace = done["T=250K/led=1.020V/bace"]
    assert bace.detail["temperature_k"] == live[-1].kelvin != in_loop[-1].kelvin
    assert f"{live[-1].kelvin:g}K" in os.path.basename(bace.detail["folder"])


def test_a_silent_instrument_pauses_at_the_node_before_the_setpoint_is_written(tmp_path):
    """Console up, instrument silent (`connected` false three polls running):
    no reading is worth yielding, nothing is written into the console, the
    node pauses as the Dry run said it would, and a resume with nothing
    typed measures the subtree at the setpoint -- the only number there is."""
    b = wired()
    b.sim.temperature.connected = False
    h, job = run_job(b, schedule_of(tree(temperatures=(250,), levels=(1.02,), n_loops=1)),
                     str(tmp_path))
    h.wait_paused(250.0, what="temperature timeout")
    assert not h.of(E.TemperatureRead), "an unusable reading is not a reading"
    [(p, warn)] = verdicts(h, "temperature.timeout")
    assert p == "T=250K" and warn.level == "warn"
    assert (warn.data["reason"], warn.data["polls"], warn.data["written"]) == ("silent", 3, False)
    assert warn.data["kelvin"] is None and "before the setpoint was written" in warn.text
    need = h.events[-1][1]
    assert (need.detail["reason"], need.detail["written"]) == ("silent", False)
    assert b.sim.temperature.setpoints == [], "nothing written into a console with no instrument behind it"
    assert job.resume({})
    h.finish()
    assert job.state == "done" and not h.of(E.TemperatureRead)
    folders = [ev.detail["folder"] for _, ev, _ in h.of(E.NodeDone) if "folder" in ev.detail]
    assert folders and all("250K" in os.path.basename(f) for f in folders), (
        "no usable reading: the setpoint names the folder")
