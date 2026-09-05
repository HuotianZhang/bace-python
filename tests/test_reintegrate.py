"""The offline window: the file's own window reproduces the file's own Q, and a
moved window is a different, explainable number. This is how the *integration*
zero is found -- on stored traces, not by re-measuring."""
from __future__ import annotations

import numpy as np
import pytest

from bace.core.axis import bace_sweep, tdcf_delay
from bace.core.reintegrate import reintegrate, sweep
from bace.drivers.simulated import make_bench
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig, run_transient_scan
from bace.storage.hdf5 import read_run
from bace.storage.recorder import RunRecorder, record
from bace.storage.naming import RunMetadata

NO_SLEEP = lambda s: None  # noqa: E731


def _run(tmp_path, spec, *, latency=47e-9, store_shots=False, **cfg_kw):
    sim = make_bench(seed=5)
    sim.led.set_pulse(1.020, 0.4)
    sim.bench.field_latency_s = latency
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(trigger_offset_s=latency), power=sim.power)
    meta = RunMetadata(sample="sim", material="TEST", pixel="pxa", led_drive_v=1.020)
    rec = RunRecorder(str(tmp_path), meta, store_shots=store_shots)
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0, record_length=500,
                    t0_int_s=-2e-9, invert_polarity=True, **cfg_kw)
    list(record(run_transient_scan(rig, spec, cfg, sleep=NO_SLEEP), rec))
    return read_run([p for p in rec.written if p.endswith(".h5")][0])


def test_the_files_own_window_reproduces_the_files_own_charge(tmp_path):
    run = _run(tmp_path, tdcf_delay(0, 200, 100, vpre=0.9, n_loops=2))
    r = reintegrate(run, float(run["run_config"]["t0_int_s"]),
                    float(run["run_config"]["t_int_width_s"]))
    np.testing.assert_allclose(r.q_mean, run["q_mean"], rtol=1e-9)
    np.testing.assert_allclose(r.window_s, run["window"], atol=1e-15)
    assert r.trace_t0_source == "the file's /traces/trace_t0"
    assert r.trigger_offset_s == pytest.approx(47e-9), "the rig's latency, from the file"
    # The window travelled with the delay: 100 ns apart per step, start and end.
    np.testing.assert_allclose(np.diff(r.window_s[:, 0]), 100e-9, atol=1e-15)
    np.testing.assert_allclose(np.diff(r.window_s[:, 1]), 100e-9, atol=1e-15)


def test_per_loop_charges_come_from_the_stored_shots(tmp_path):
    run = _run(tmp_path, bace_sweep(0.88, 0.90, 0.02, n_loops=3), store_shots=True)
    r = reintegrate(run, -2e-9, 1.5e-6)
    assert r.q_loops.shape == (3, 2)
    np.testing.assert_allclose(np.nanmean(r.q_loops, axis=0), r.q_mean, rtol=1e-9)
    np.testing.assert_allclose(r.q_loops, run["q_all"], rtol=1e-9)
    assert r.q_std is not None and np.all(np.isfinite(r.q_std))


def test_a_window_started_after_the_transient_loses_charge_and_one_before_it_does_not(tmp_path):
    """The integration zero: moving the start earlier than the field's arrival
    changes nothing (the baseline is flat), moving it later cuts the peak."""
    run = _run(tmp_path, bace_sweep(0.90, 0.90, 0.02, n_loops=1))
    q0 = reintegrate(run, -2e-9, 1.5e-6).q_mean[0]
    early = reintegrate(run, -60e-9, 1.5e-6).q_mean[0]
    late = reintegrate(run, +60e-9, 1.5e-6).q_mean[0]
    assert early == pytest.approx(q0, rel=0.05)
    assert abs(late) < 0.9 * abs(q0)
    t0s, q = sweep(run, np.arange(-40e-9, 41e-9, 20e-9), 1.5e-6)
    assert q.shape == (5, 1)
    assert abs(q[0, 0]) > abs(q[-1, 0])


def test_a_window_past_the_record_is_flagged_and_an_empty_one_refused(tmp_path):
    run = _run(tmp_path, bace_sweep(0.90, 0.90, 0.02, n_loops=1))
    assert reintegrate(run, -2e-9, 5e-6).clipped.all()
    assert not reintegrate(run, -2e-9, 1.0e-6).clipped.any()
    with pytest.raises(ValueError, match="t_int_width_s must be positive"):
        reintegrate(run, -2e-9, 0.0)


def test_a_file_without_trace_t0_falls_back_to_the_timebase_geometry(tmp_path):
    run = _run(tmp_path, bace_sweep(0.90, 0.90, 0.02, n_loops=1))
    stored = run.pop("trace_t0")
    r = reintegrate(run, -2e-9, 1.5e-6)
    assert "timebase geometry" in r.trace_t0_source
    # The simulator's record begins one division before the trigger, which is
    # exactly what the geometry says, so the two agree here to the sample.
    dt = float(run["time_s"][1] - run["time_s"][0])
    assert abs(r.window_s[0, 0] - (-2e-9 + 90e-9 + 47e-9 - stored[0])) <= dt


def test_the_cli_prints_and_writes_the_table(tmp_path, capsys):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "reintegrate_cli", pathlib.Path(__file__).resolve().parents[1] / "tools" / "reintegrate.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    sim = make_bench(seed=5)
    sim.led.set_pulse(1.020, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig(), power=sim.power)
    rec = RunRecorder(str(tmp_path), RunMetadata(sample="sim", material="T", pixel="p", led_drive_v=1.02))
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0, record_length=500, t0_int_s=-2e-9)
    list(record(run_transient_scan(rig, tdcf_delay(0, 100, 100, vpre=0.9, n_loops=1), cfg,
                                   sleep=NO_SLEEP), rec))
    h5 = [p for p in rec.written if p.endswith(".h5")][0]

    assert cli.main([h5]) == 0
    out = capsys.readouterr().out
    assert "Q_stored" in out and "window_start_ns" in out
    csv_path = str(tmp_path / "sweep.csv")
    assert cli.main([h5, "--sweep=-10:10:10", "--csv", csv_path]) == 0
    lines = open(csv_path).read().splitlines()
    assert lines[0].startswith("t0_int_ns,delay_ns=0,delay_ns=100")
    assert len(lines) == 4
