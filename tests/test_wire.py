"""Every event goes over the wire, and comes back a JSON document.

The serialiser is reflection over dataclasses, so the thing to test is not
each event's shape but that *no* event is left out: every `Event` subclass in
`events.py`, `jv.py` and `intensity_series.py` is instantiated here with
realistic values -- including a 4000-point `Trace` -- and has to survive
`json.dumps`. A new event class that this file does not know about fails the
coverage test, which is the point of enumerating the modules rather than
listing the classes by hand.
"""
from __future__ import annotations

import inspect
import json

import numpy as np
import pytest

from bace.core.axis import Axis, Setpoint
from bace.drivers.protocols import DCPoint, Trace
from bace.experiment import events as E
from bace.experiment import intensity_series as S
from bace.experiment import jv as J
from bace.experiment.rig import RigConfig
from bace.experiment.transient import RunConfig
from bace.experiment.wire import envelope_to_wire, to_wire

N = 4000
DT, T0 = 5e-10, -1.995e-7


def _trace(seed: int, scale: float = 1e-3) -> Trace:
    rng = np.random.default_rng(seed)
    t = np.arange(N) * DT
    y = -scale * np.exp(-(t - 3.3e-7).clip(0) / 7e-8) * (t > 3.3e-7)
    return Trace(y=y + rng.normal(0, 1e-5, N), dt=DT, t0=T0)


def _axis() -> Axis:
    return Axis("vpre", 0.905, 0.907, 0.001)


def _dc() -> DCPoint:
    return DCPoint(voc=0.906, jsc=-2.0e-4, jsat=-6.0e-4, v_sat=-1.0)


def _curve(index: int, dark: bool) -> J.JVCurveDone:
    v = np.linspace(-0.2, 1.2, 71)
    i = 1e-9 * (np.exp(v / 0.05) - 1) - (0.0 if dark else 2e-4)
    return J.JVCurveDone(index=index, label="dark" if dark else "1.02 V",
                         dark=dark, led_level_v=None if dark else 1.02,
                         direction="forward", voltage=v, current=i,
                         density=J.current_density(i, 0.0 if dark else 0.04),
                         metrics=J.metrics(v, i, dark=dark),
                         intensity_w=None if dark else 2.07e-5)


def _point() -> S.SeriesPointDone:
    return S.SeriesPointDone(index=0, level_v=1.02, dc=_dc(), voc=0.906,
                             axis_values=np.array([0.905, 0.906, 0.907]),
                             q_mean=np.array([-3.4e-10, -3.5e-10, -3.6e-10]),
                             q_std=np.array([1e-12, 1.2e-12, 0.9e-12]),
                             intensity_w=2.07e-5, vcoll=-1.0, delay_ns=88.0)


def realistic_events() -> dict[type, E.Event]:
    """One instance per event class, with the values a real run would carry."""
    light, dark = _trace(1), _trace(2, scale=0.0)
    photo = light.y - dark.y
    sp = Setpoint(vpre=0.906, vcoll=-1.0, delay_ns=88.0)
    cfg = {"run": RunConfig().as_dict(), "rig": RigConfig().as_dict()}
    step = E.StepDone(index=0, loop=1, step=1, setpoint=sp, axis_value=0.906,
                      light=light, dark=dark, photo=photo, photo_averaged=photo,
                      q=-3.47e-10, q_mean=-3.47e-10, q_std=0.0,
                      intensity_w=2.07e-5, clipped=False)
    fin = E.RunFinished(axis=_axis(), values=np.array([0.905, 0.906, 0.907]),
                        q_mean=np.array([-3.4e-10, -3.5e-10, -3.6e-10]),
                        q_std=np.array([1e-12, 1.2e-12, 0.9e-12]),
                        q_all=np.array([[-3.4e-10, -3.5e-10, -3.6e-10]] * 3),
                        photo_averaged=np.tile(photo, (3, 1)),
                        dt=DT, elapsed_s=12.5)
    events: list[E.Event] = [
        E.RunStarted(description="vpre: 0.905 -> 0.907 step 0.001 V (3 pts) x 3",
                     n_shots=9, n_steps=3, n_loops=3, config=cfg),
        E.DCMeasured(led_drive_v=1.02, dc=_dc()),
        E.AxisResolved(axis=_axis(), values=np.array([0.905, 0.906, 0.907]),
                       voc=0.906),
        E.InstrumentState({"bias_output_polarity": "NORM",
                           "bias_arm_source": "EXT"}),
        E.StepStarted(index=0, loop=1, step=1, setpoint=sp, axis_value=0.906),
        E.StepPhase(index=0, phase="acquire light", k=3, of=7),
        step,
        E.LoopDone(loop=1, q_mean=fin.q_mean, q_std=fin.q_std),
        E.Progress(done=1, total=9, elapsed_s=2.5, eta_s=None, node_path="T=250K"),
        fin,
        E.RunAborted(reason="requested", done=3, total=9),
        E.RunFailed(error="scope fell over", where="acquire"),
        E.Notice("warning", "the tail of light - dark is not a baseline"),
        E.NeedsOperator(what="set temperature", node_path="T=250K",
                        detail={"target_k": 250.0}),
        E.OperatorResumed(node_path="T=250K", note="reached 250.1 K",
                          detail={"kelvin": 250.1}),
        E.NodeStarted(node_path="T=250K", kind="temperature", label="T=250K"),
        E.NodeDone(node_path="T=250K/led=1.020V/jv", outcome="ok",
                   detail={"voc": 0.906}),
        E.RunStateChanged(state="running", reason="start"),
        E.RunQueued(kind="manual", module="bace",
                    tree={"kind": "module", "module": "bace", "params": {"n_loops": 20}},
                    params={"n_loops": 20}, resolved={"n_loops": 20, "vpre": 0.0},
                    name="quick", folder=None),
        E.BenchAction(name="set-33220a-pol-inv", args={},
                      result={"polarity": "INV"}, by="operator"),
        E.Verdict(level="warn", code="led-polarity-norm",
                  text="the 33220A is NORM, so the Sync edge means light on",
                  data={"polarity": "NORM"}),
        E.PowerReading(watts=2.07e-5, trustworthy=True, wavelength_nm=530.0,
                       source="http://127.0.0.1:8918"),
        E.TemperatureRead(kelvin=250.1, setpoint_k=250.0, in_band=True,
                          source="operator"),
        J.JVStarted(n_curves=2, n_points=71,
                    config={"jv": J.JVConfig().as_dict(), "rig": cfg["rig"]}),
        J.JVPoint(index=1, k=12, of=71, label="1.02 V", dark=False, led_level_v=1.02,
                  direction="forward", voltage=0.24, current=-9.1e-5, density=-9.1),
        _curve(0, dark=True),
        J.JVFinished(curves=(_curve(0, True), _curve(1, False)), elapsed_s=3.0),
        S.SeriesStarted(levels=np.array([1.02, 1.06]), n_levels=2,
                        config={"series": S.SeriesConfig().as_dict(), **cfg}),
        S.IlluminationSet(index=0, level_v=1.02, mode="dc"),
        S.IntensityMeasured(level_v=1.02, watts=2.07e-5, watts_std=None,
                            trustworthy=True),
        _point(),
        S.SeriesFinished(points=(_point(),), elapsed_s=100.0),
    ]
    return {type(e): e for e in events}


def _event_classes(module) -> set[type]:
    return {obj for _, obj in inspect.getmembers(module, inspect.isclass)
            if issubclass(obj, E.Event) and obj is not E.Event
            and obj.__module__ == module.__name__}


# -- coverage -------------------------------------------------------------
def test_every_event_class_is_exercised_here():
    """A new event that this file does not instantiate is a new event the
    service has never serialised."""
    declared = _event_classes(E) | _event_classes(J) | _event_classes(S)
    missing = declared - set(realistic_events())
    assert not missing, sorted(c.__name__ for c in missing)


@pytest.mark.parametrize("cls", sorted(
    _event_classes(E) | _event_classes(J) | _event_classes(S), key=lambda c: c.__name__),
    ids=lambda c: c.__name__)
def test_every_event_survives_json(cls):
    ev = realistic_events()[cls]
    data, info = to_wire(ev)
    text = json.dumps(data)                    # raises on anything not JSON
    assert json.loads(text) == data
    assert info == {}, "nothing is decimated unless asked"
    assert "NaN" not in text and "Infinity" not in text


# -- shapes -----------------------------------------------------------------
def test_a_trace_carries_its_full_length_and_arrays_become_lists():
    data, _ = to_wire(realistic_events()[E.StepDone])
    light = data["light"]
    assert set(light) == {"y", "dt", "t0", "n", "count"}
    assert light["n"] == N and len(light["y"]) == N
    assert isinstance(light["y"], list) and isinstance(light["y"][0], float)
    assert light["dt"] == DT and light["t0"] == T0
    assert data["setpoint"] == {"vpre": 0.906, "vcoll": -1.0, "delay_ns": 88.0}
    assert data["clipped"] is False


def test_nested_dataclasses_and_tuples_become_dicts_and_lists():
    fin = realistic_events()[J.JVFinished]
    data, _ = to_wire(fin)
    assert isinstance(data["curves"], list) and len(data["curves"]) == 2
    assert data["curves"][1]["metrics"] == fin.curves[1].metrics.as_dict()
    assert isinstance(data["curves"][1]["metrics"]["voc"], float)
    assert data["curves"][0]["metrics"]["voc"] is None        # dark: no V_oc
    assert data["curves"][0]["density"] is None

    data, _ = to_wire(realistic_events()[S.SeriesPointDone])
    assert data["dc"] == {"voc": 0.906, "jsc": -2.0e-4, "jsat": -6.0e-4, "v_sat": -1.0}

    data, _ = to_wire(realistic_events()[E.RunStarted])
    assert data["config"]["rig"]["current_sign"] == -1.0
    assert isinstance(data["config"]["run"]["n_averages"], int)


def test_nan_and_infinity_become_null():
    photo = np.zeros(N)
    photo[7], photo[8] = np.nan, np.inf
    light = _trace(3)
    ev = E.StepDone(index=0, loop=1, step=1,
                    setpoint=Setpoint(0.906, -1.0, 88.0), axis_value=0.906,
                    light=light, dark=light, photo=photo, photo_averaged=photo,
                    q=float("nan"), q_mean=np.float64("inf"), q_std=0.0)
    data, _ = to_wire(ev)
    assert data["photo"][7] is None and data["photo"][8] is None
    assert data["q"] is None and data["q_mean"] is None
    text = json.dumps(data)
    assert "NaN" not in text and "Infinity" not in text
    # and the run-level `dt = nan` of a run that never took a shot
    fin = E.RunFinished(axis=_axis(), values=np.empty(0), q_mean=np.empty(0),
                        q_std=np.empty(0), q_all=np.empty((0, 0)),
                        photo_averaged=np.empty((0, 0)), dt=float("nan"),
                        elapsed_s=0.0)
    assert to_wire(fin)[0]["dt"] is None


def test_numpy_scalars_become_python_scalars():
    ev = E.Progress(done=np.int64(3), total=np.int32(9), elapsed_s=np.float64(1.5),
                    eta_s=np.float32(3.0))
    data, _ = to_wire(ev)
    assert type(data["done"]) is int and type(data["elapsed_s"]) is float
    assert json.loads(json.dumps(data))["node_path"] == ""


# -- decimation -------------------------------------------------------------
def test_decimation_keeps_first_and_last_and_reports_the_stride():
    ev = realistic_events()[E.StepDone]
    data, info = to_wire(ev, max_points=1000)
    for name, full in (("light", ev.light.y), ("dark", ev.dark.y)):
        y = data[name]["y"]
        assert len(y) <= 1000
        assert y[0] == full[0] and y[-1] == full[-1]
        assert data[name]["n"] == N, "the record length survives decimation"
        assert info[f"{name}.y"] == {"n_full": N, "stride": 5}
        # every kept sample is a real sample at a stride-multiple index
        np.testing.assert_array_equal(y[:-1], full[::5][:len(y) - 1])
    assert info["photo"] == {"n_full": N, "stride": 5}
    assert info["photo_averaged"] == {"n_full": N, "stride": 5}
    assert set(info) == {"light.y", "dark.y", "photo", "photo_averaged"}
    json.dumps(data)


def test_decimation_never_exceeds_max_points():
    for n, m in ((4000, 1000), (4001, 1000), (3999, 1000), (11, 2), (12, 5), (1000, 1000)):
        ev = E.LoopDone(loop=1, q_mean=np.arange(n, dtype=float), q_std=np.zeros(n))
        data, info = to_wire(ev, max_points=m)
        assert len(data["q_mean"]) <= m, (n, m, len(data["q_mean"]))
        assert data["q_mean"][0] == 0.0 and data["q_mean"][-1] == n - 1
        if n > m:
            assert info["q_mean"]["n_full"] == n
        else:
            assert "q_mean" not in info, "an array that fits is sent whole"


def test_short_arrays_are_untouched_and_two_d_arrays_are_never_decimated():
    ev = realistic_events()[E.RunFinished]
    data, info = to_wire(ev, max_points=1000)
    assert data["values"] == [0.905, 0.906, 0.907]
    assert data["q_all"] == ev.q_all.tolist(), "small and 2-D: goes whole"
    assert data["photo_averaged"] is None
    assert info == {"photo_averaged": {"omitted": True}}

    full, info_full = to_wire(ev)
    assert len(full["photo_averaged"]) == 3 and len(full["photo_averaged"][0]) == N
    assert info_full == {}


def test_one_point_cannot_hold_both_ends():
    with pytest.raises(ValueError, match="at least 2"):
        to_wire(realistic_events()[E.Notice], max_points=1)


# -- the envelope -------------------------------------------------------------
def test_the_envelope_flattens_and_names_the_type():
    ev = realistic_events()[E.StepDone]
    env = E.Envelope(seq=42, ts=1_756_800_000.5, run_id="20260902_020827",
                     node_path="T=250K/led=1.020V/bace", event=ev)
    frame = envelope_to_wire(env, max_points=500)
    assert set(frame) == {"seq", "ts", "run_id", "node_path", "type", "data", "decimated"}
    assert frame["type"] == "StepDone"
    assert frame["seq"] == 42 and frame["node_path"] == "T=250K/led=1.020V/bace"
    assert frame["decimated"]["light.y"]["n_full"] == N
    back = json.loads(json.dumps(frame))
    assert back["data"]["light"]["n"] == N
    assert len(back["data"]["light"]["y"]) <= 500

    bare = envelope_to_wire(E.Envelope(seq=0, ts=0.0, run_id=None, node_path="",
                                       event=E.RunStateChanged("running", "start")))
    assert bare["run_id"] is None and bare["decimated"] == {}
    assert json.loads(json.dumps(bare))["data"] == {"state": "running", "reason": "start"}
