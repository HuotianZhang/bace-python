"""The journal: append-only on disk, and the history queries read back off it.

The queries are tested on hand-written files, because what they have to
survive is not a tidy run but the files a session actually leaves: a run
that was aborted, a pause nobody answered, a torn last line from a crash,
and twenty-five sessions of history of which only twenty are read.
"""
from __future__ import annotations

import json
import os

import pytest

from bace.service.journal import MAX_HISTORY_FILES, Journal, node_record, run_queued

T0 = 1_788_390_000.0
HEADER = {"mode": "sim", "rig_toml": "rig.toml", "run_toml": "run.toml", "out": "runs",
          "fingerprint": "d7daadac0cc1", "python": "3.13", "version": "0.2.0"}


def line(seq, ts, typ, data, run_id=None, node_path=""):
    return {"seq": seq, "ts": ts, "run_id": run_id, "node_path": node_path,
            "type": typ, "data": data, "decimated": {}}


def write_session(out, session_id, lines, *, torn=False):
    d = os.path.join(out, "journal")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{session_id}.jsonl"), "w", encoding="utf-8") as fh:
        for l in lines:
            fh.write(json.dumps(l) + "\n")
        if torn:
            fh.write('{"seq": 999, "ts": 1.0, "type": "StepDo')


def read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def bace_run(run_id, t0, seq, params, *, gaps=(0.8, 0.9, 0.7), end="done",
             node_path="bace", folder="runs/s4_290K_20260902_120000"):
    """A manual bace run as the session would journal it: queued, states,
    one StepDone + Progress per shot, then the ending and parked."""
    total = 20
    ls = [line(seq, t0, "RunQueued", {"kind": "manual", "module": "bace",
                                       "tree": {"kind": "module", "module": "bace",
                                                "params": params},
                                       "params": params, "name": ""}, run_id),
          line(seq + 1, t0 + 0.1, "RunStateChanged",
               {"state": "preflight", "reason": "building"}, run_id),
          line(seq + 2, t0 + 0.2, "RunStateChanged",
               {"state": "running", "reason": "started"}, run_id)]
    t, n = t0 + 1.0, seq + 3
    for i, gap in enumerate((0.0,) + tuple(gaps)):
        t += gap
        ls.append(line(n, t, "StepDone", {"index": i, "loop": i + 1, "step": 1,
                                          "q": -3.5e-10, "q_mean": -3.5e-10,
                                          "q_std": 0.0, "clipped": False},
                       run_id, node_path))
        ls.append(line(n + 1, t, "Progress", {"done": i + 1, "total": total,
                                              "elapsed_s": t - t0, "eta_s": 1.0},
                       run_id, node_path))
        n += 2
    done = len(gaps) + 1
    if end == "done":
        ls.append(line(n, t + 0.1, "RunFinished",
                       {"values": [0.906], "q_mean": [-3.52e-10], "q_std": [1.8e-12],
                        "q_all": [[-3.5e-10]] * done, "dt": 5e-10, "elapsed_s": t - t0},
                       run_id, node_path))
        ls.append(line(n + 1, t + 0.1, "NodeDone",
                       {"node_path": node_path, "outcome": "ok",
                        "detail": {"kept": done, "requested": total, "folder": folder}},
                       run_id, node_path))
        ls.append(line(n + 2, t + 0.2, "RunStateChanged",
                       {"state": "done", "reason": "finished"}, run_id))
    elif end in ("stopped", "aborted"):
        reason = "requested" if end == "stopped" else "aborted"
        ls.append(line(n, t + 0.1, "RunAborted",
                       {"reason": reason, "done": done, "total": total}, run_id, node_path))
        ls.append(line(n + 1, t + 0.2, "RunStateChanged",
                       {"state": end, "reason": reason}, run_id))
    elif end == "failed":
        ls.append(line(n, t + 0.1, "RunFailed",
                       {"error": "RuntimeError: scope fell over", "where": node_path},
                       run_id, node_path))
        ls.append(line(n + 1, t + 0.2, "RunStateChanged",
                       {"state": "failed", "reason": "scope fell over"}, run_id))
    ls.append(line(n + 3, t + 0.3, "RunStateChanged", {"state": "parked", "reason": end},
                   run_id))
    return ls


def pipeline_run(run_id, t0, seq):
    tree = {"kind": "loop", "loop": "temperature", "values_k": [250, 240, 230],
            "children": [{"kind": "loop", "loop": "illumination",
                          "led_start_v": 1.01, "led_stop_v": 1.03, "led_step_v": 0.01,
                          "children": [{"kind": "module", "module": "jv_bace"},
                                       {"kind": "module", "module": "bace"}]}]}

    def pause(s, t, path, k):
        return line(s, t, "NeedsOperator", {"what": "temperature", "node_path": path,
                                            "detail": {"setpoint_k": k}}, run_id, path)

    def resume(s, t, path):
        return line(s, t, "OperatorResumed", {"node_path": path, "note": "by hand",
                                              "detail": {"temperature_k": 250.1}},
                    run_id, path)

    return [
        line(seq, t0, "RunQueued", {"kind": "pipeline", "module": None, "tree": tree,
                                    "params": None, "name": "cooldown",
                                    "folder": "runs/cooldown_20260902_130000"}, run_id),
        line(seq + 1, t0 + 1, "RunStateChanged", {"state": "running", "reason": ""}, run_id),
        pause(seq + 2, t0 + 2, "T=250K", 250.0),
        resume(seq + 3, t0 + 2 + 1200.0, "T=250K"),
        pause(seq + 4, t0 + 5000, "T=240K", 240.04),
        resume(seq + 5, t0 + 5000 + 900.0, "T=240K"),
        pause(seq + 6, t0 + 9000, "T=230K", 230.0),           # never answered
        line(seq + 7, t0 + 9001, "RunStateChanged", {"state": "aborted", "reason": ""},
             run_id),
        line(seq + 8, t0 + 9002, "RunStateChanged", {"state": "parked", "reason": "aborted"},
             run_id),
    ]


# -- writing ------------------------------------------------------------------
def test_the_first_line_is_session_started_with_the_header(tmp_path):
    j = Journal(str(tmp_path), "20260902_120000", header=HEADER)
    assert j.path == os.path.join(str(tmp_path), "journal", "20260902_120000.jsonl")
    first = read_lines(j.path)
    assert len(first) == 1
    assert first[0]["type"] == "SessionStarted" and first[0]["seq"] == 0
    assert first[0]["data"] == {**HEADER, "session_id": "20260902_120000"}
    assert first[0]["run_id"] is None and first[0]["node_path"] == ""
    assert j.seq == 0 and j.resumed is False
    j.close()


def test_append_flushes_every_line_and_a_second_journal_appends(tmp_path):
    j = Journal(str(tmp_path), "20260902_120000", header=HEADER)
    j.append(line(1, T0, "RunStateChanged", {"state": "queued", "reason": ""}, "r-001"))
    j.append(line(2, T0, "Notice", {"level": "info", "text": "x"}))
    assert [l["seq"] for l in read_lines(j.path)] == [0, 1, 2], "flushed before close"
    assert j.seq == 2
    j.close()
    with pytest.raises(RuntimeError, match="closed"):
        j.append(line(3, T0, "Notice", {"level": "info", "text": "late"}))

    again = Journal(str(tmp_path), "20260902_120000", header=HEADER)
    assert again.resumed is True and again.seq == 2, "picks up where the file ends"
    again.append(line(3, T0, "Notice", {"level": "info", "text": "y"}))
    again.close()
    lines = read_lines(again.path)
    assert [l["type"] for l in lines] == ["SessionStarted", "RunStateChanged", "Notice",
                                          "Notice"], "one header, nothing truncated"


def test_append_refuses_what_a_reader_could_not_parse(tmp_path):
    with Journal(str(tmp_path), "20260902_120000", header=HEADER) as j:
        with pytest.raises(ValueError):
            j.append(line(1, T0, "StepDone", {"q": float("nan")}))
        with pytest.raises(TypeError, match="string 'type'"):
            j.append({"seq": 1})
        with pytest.raises(TypeError):
            j.append(["not", "a", "dict"])
    assert len(read_lines(j.path)) == 1, "a refused line leaves the file untouched"


def test_a_header_naming_another_session_is_refused(tmp_path):
    with pytest.raises(ValueError, match="names session"):
        Journal(str(tmp_path), "20260902_120000",
                header={**HEADER, "session_id": "20260901_000000"})


def test_run_queued_is_the_line_the_queries_key_on():
    d = run_queued(seq=4, ts=T0, run_id="20260902_120000-001", kind="manual",
                   module="bace", tree={"kind": "module", "module": "bace"},
                   params={"n_loops": 20}, name="quick")
    assert d["type"] == "RunQueued" and d["seq"] == 4 and d["node_path"] == ""
    assert d["data"] == {"kind": "manual", "module": "bace",
                         "tree": {"kind": "module", "module": "bace"},
                         "params": {"n_loops": 20}, "name": "quick",
                         "resolved": None, "folder": None, "sample": None}, (
        "the experiment.events.RunQueued dataclass, every field, as the one event path writes it")
    json.dumps(d)
    assert run_queued(seq=1, ts=None, run_id="r", kind="pipeline", module=None,
                      tree={}, params=None, folder="runs/x")["data"]["folder"] == "runs/x"
    with pytest.raises(ValueError, match="names its module"):
        run_queued(seq=1, ts=T0, run_id="r", kind="manual", module=None, tree={},
                   params={})
    with pytest.raises(ValueError, match="kind"):
        run_queued(seq=1, ts=T0, run_id="r", kind="run", module="bace", tree={}, params={})


# -- the queries, on a hand-written session ------------------------------------
@pytest.fixture
def history(tmp_path):
    out = str(tmp_path)
    older = (bace_run("20260901_100000-001", T0 - 86400, 1, {"n_loops": 7, "n_averages": 20})
             + pipeline_run("20260901_100000-002", T0 - 80000, 100))
    write_session(out, "20260901_100000", older)
    this = (bace_run("20260902_120000-001", T0, 1, {"n_loops": 20, "n_averages": 20},
                     gaps=(0.8, 0.9, 0.7))
            + bace_run("20260902_120000-002", T0 + 100, 50, {"n_loops": 5, "n_averages": 8},
                       end="aborted")
            + pipeline_run("20260902_120000-003", T0 + 200, 100)
            + bace_run("20260902_120000-004", T0 + 20000, 200, {"n_loops": 3},
                       end="failed"))
    write_session(out, "20260902_120000", this, torn=True)
    j = Journal(out, "20260902_120000", header=HEADER)
    yield j
    j.close()


def test_last_used_params_wants_a_run_that_finished_or_was_stopped(history):
    assert history.last_used_params("bace") == {"n_loops": 20, "n_averages": 20}, (
        "the aborted and failed runs after it do not count")
    assert history.last_used_params("jv") is None


def test_a_stopped_run_counts_and_an_aborted_one_does_not(tmp_path):
    out = str(tmp_path)
    write_session(out, "20260902_120000",
                  bace_run("a", T0, 1, {"n_loops": 9}, end="stopped")
                  + bace_run("b", T0 + 50, 50, {"n_loops": 4}, end="aborted"))
    with Journal(out, "20260902_120000", header=HEADER) as j:
        assert j.last_used_params("bace") == {"n_loops": 9}


def test_run_index_reconstructs_every_run_newest_first(history):
    idx = history.run_index("this")
    assert [r["run_id"][-3:] for r in idx] == ["004", "003", "002", "001"]
    done = idx[-1]
    assert done["kind"] == "manual" and done["module"] == "bace"
    assert done["state"] == "done" and done["parked"] is True
    assert done["queued_at"] == T0 and done["started_at"] == T0 + 0.1
    assert done["finished_at"] == pytest.approx(T0 + 1.0 + 2.4 + 0.2)
    assert done["kept"] == 20 and done["requested"] == 20
    assert done["outcome_text"] == "Q -3.520e-10 ± 1.8e-12 C · 20/20"
    assert done["folder"] == "runs/s4_290K_20260902_120000"
    assert done["tree_summary"] == "bace" and done["node_count"] == 1

    aborted = idx[2]
    assert aborted["state"] == "aborted"
    assert aborted["kept"] == 4 and aborted["requested"] == 20
    assert aborted["outcome_text"] == "aborted at 4/20"

    pipe = idx[1]
    assert pipe["kind"] == "pipeline" and pipe["name"] == "cooldown"
    assert pipe["module"] is None
    assert pipe["tree_summary"] == "T×3/led×3/jv_bace+bace" and pipe["node_count"] == 2
    assert pipe["folder"] == "runs/cooldown_20260902_130000"
    assert pipe["state"] == "aborted"

    failed = idx[0]
    assert failed["state"] == "failed"
    assert failed["error"] == "RuntimeError: scope fell over"
    assert failed["outcome_text"].startswith("failed: RuntimeError")

    everything = history.run_index("all")
    assert len(everything) == 6
    assert [r["session_id"] for r in everything] == ["20260902_120000"] * 4 + \
        ["20260901_100000"] * 2, "this session first, then older"
    assert [r["run_id"] for r in history.run_index("20260901_100000")] == \
        ["20260901_100000-002", "20260901_100000-001"]
    assert history.run_index("20250101_000000") == []


def test_settle_history_pairs_each_pause_with_its_answer(history):
    settle = history.settle_history()
    assert settle == {250.0: [1200.0, 1200.0], 240.0: [900.0, 900.0]}, (
        "keyed to 0.1 K, both sessions read, the unanswered 230 K absent")


def test_settle_history_reads_the_331_consoles_own_settles_too(tmp_path):
    """A temperature the console settled leaves a `temperature.settled`
    verdict and no pause pair; the cost model learns its `settle_s` beside the
    operator's waits. A timeout verdict and a malformed line teach nothing."""
    out = str(tmp_path)
    run_id = "20260902_120000-001"

    def settled(seq, ts, setpoint, settle, path):
        return line(seq, ts, "Verdict",
                    {"level": "ok", "code": "temperature.settled",
                     "text": f"{setpoint:g} K reached", "node_path": path,
                     "data": {"setpoint_k": setpoint, "kelvin": setpoint - 0.03,
                              "settle_s": settle, "hold_s": 60.0, "polls": 3,
                              "source": "console"}},
                    run_id, path)

    write_session(out, "20260902_120000", [
        settled(1, T0 + 100, 250.0, 300.0, "T=250K"),
        settled(2, T0 + 900, 240.04, 480.0, "T=240K"),
        line(3, T0 + 950, "Verdict",
             {"level": "warn", "code": "temperature.timeout", "text": "230 K not reached",
              "node_path": "T=230K", "data": {"setpoint_k": 230.0, "elapsed_s": 1800.0}},
             run_id, "T=230K"),
        line(4, T0 + 960, "Verdict",
             {"level": "ok", "code": "temperature.settled", "text": "", "node_path": "T=220K",
              "data": {"setpoint_k": 220.0}}, run_id, "T=220K"),
        line(5, T0 + 2000, "NeedsOperator",
             {"what": "temperature timeout", "node_path": "T=230K",
              "detail": {"setpoint_k": 230.0}}, run_id, "T=230K"),
        line(6, T0 + 2000, "NeedsOperator",
             {"what": "temperature", "node_path": "T=250K", "detail": {"setpoint_k": 250.0}},
             run_id, "T=250K"),
        line(7, T0 + 3200, "OperatorResumed",
             {"node_path": "T=250K", "note": "", "detail": {}}, run_id, "T=250K"),
    ])
    with Journal(out, "20260902_120000", header=HEADER) as j:
        assert j.settle_history() == {250.0: [300.0, 1200.0], 240.0: [480.0]}


def test_shot_time_is_the_median_gap_of_the_last_completed_run(history):
    assert history.shot_time_s("bace") == pytest.approx(0.8)
    assert history.shot_time_s("jv_bace") is None


def test_shot_time_matches_the_leaf_of_a_pipeline_node_path(tmp_path):
    out = str(tmp_path)
    write_session(out, "20260902_120000",
                  bace_run("p", T0, 1, {}, gaps=(1.5, 1.5, 3.0),
                           node_path="T=250K/led=1.020V/bace#2"))
    with Journal(out, "20260902_120000", header=HEADER) as j:
        assert j.shot_time_s("bace") == pytest.approx(1.5)


def _started(sid, mode, fast):
    return line(0, T0, "SessionStarted",
                {"session_id": sid, "mode": mode, "fast": fast}, run_id=None)


def test_the_cost_model_ignores_a_different_bench(tmp_path):
    """`shot_time_s`/`settle_history` read only files whose bench matches this
    session's: a --sim --fast session settles a cryostat in a click and a shot
    in a millisecond, and the lab PC's Dry run must not learn either. But what
    the operator typed (`last_used_params`) is real on any bench."""
    out = str(tmp_path)
    # an older session on the real rig: a slow shot and a real settle
    write_session(out, "20260901_090000",
                  [_started("20260901_090000", "rig", False)]
                  + bace_run("20260901_090000-001", T0 - 8000, 1, {"n_loops": 9},
                             gaps=(2.0, 2.0, 2.0))
                  + pipeline_run("20260901_090000-002", T0 - 6000, 100))
    # this session, --sim --fast: a millisecond shot
    write_session(out, "20260902_120000",
                  [_started("20260902_120000", "sim", True)]
                  + bace_run("20260902_120000-001", T0, 1, {"n_loops": 3},
                             gaps=(0.001, 0.001, 0.001)))
    with Journal(out, "20260902_120000",
                 header={**HEADER, "mode": "sim", "fast": True}) as j:
        # this session's own sim run teaches it (same bench), so it reads sim
        assert j.shot_time_s("bace") == pytest.approx(0.001, abs=1e-5)
        # the real rig's 2 s shots and 20 min settles are not mixed in
        assert j.settle_history() == {}, "the real session's settle is a different bench"
        # but the operator's typed n_loops on the real rig is still last-used
        assert j.last_used_params("bace") == {"n_loops": 3}, "this session's is newest"

    # a real-rig session ignores the sim file and learns from the rig
    with Journal(out, "20260901_090000",
                 header={**HEADER, "mode": "rig", "fast": False, "session_id": "x"}) as jr:
        # (opened on the existing real file, so it resumes it)
        assert jr.shot_time_s("bace") == pytest.approx(2.0)
        assert 250.0 in jr.settle_history(), "the real session's settle is read"


def test_a_torn_last_line_is_skipped_and_the_rest_read(history):
    with open(history.path, encoding="utf-8") as fh:
        assert fh.read().rstrip().endswith("StepDo"), "the fixture really is torn"
    assert len(history.run_index("this")) == 4, "the torn line did not take the file down"
    assert history.seq == 200 + 3 + 2 * 4 + 3, "seq comes from the last whole line"


def test_older_sessions_are_read_newest_first_and_capped(tmp_path):
    out = str(tmp_path)
    for k in range(25):                                   # 25 older sessions
        sid = f"202608{1 + k // 24:02d}_{k % 24:02d}0000"
        write_session(out, sid, bace_run(f"{sid}-001", T0 - (30 - k) * 3600, 1,
                                         {"n_loops": k}))
    with Journal(out, "20260902_120000", header=HEADER) as j:
        files = j._files("all")
        assert len(files) == MAX_HISTORY_FILES == 20
        assert files[0] == j.path, "this session first"
        names = [os.path.basename(f) for f in files[1:]]
        assert names == sorted(names, reverse=True), "then newest first"
        assert j.last_used_params("bace") == {"n_loops": 24}, "the newest older session"
        assert len(j.run_index("all")) == 19, "this session has no runs; 19 older files"
        opened = {os.path.basename(f) for f in files}
        assert "20260801_050000.jsonl" not in opened and "20260801_060000.jsonl" in opened


def test_the_queries_see_this_sessions_own_lines_as_they_are_written(tmp_path):
    with Journal(str(tmp_path), "20260902_120000", header=HEADER) as j:
        assert j.run_index("this") == [] and j.last_used_params("bace") is None
        for l in bace_run("20260902_120000-001", T0, 1, {"n_loops": 2}, gaps=(0.5,)):
            j.append(l)
        assert j.last_used_params("bace") == {"n_loops": 2}
        assert j.shot_time_s("bace") == pytest.approx(0.5)
        assert j.run_index("this")[0]["state"] == "done"


# -- the round of 2026-09-02 --------------------------------------------------------------
def jv_run(run_id, t0, seq, vocs, *, node_path="jv_bace"):
    """A jv_bace as the session journals it: one light curve per level."""
    ls = [line(seq, t0, "RunQueued", {"kind": "manual", "module": "jv_bace",
                                       "tree": {"kind": "module", "module": "jv_bace"},
                                       "params": {}, "name": ""}, run_id),
          line(seq + 1, t0 + 0.2, "RunStateChanged", {"state": "running", "reason": ""}, run_id),
          line(seq + 2, t0 + 0.3, "JVCurveDone", {"index": 0, "label": "dark", "dark": True,
                                                  "led_level_v": None, "metrics": {"voc": None}},
               run_id, node_path)]
    n = seq + 3
    for i, v in enumerate(vocs, 1):
        ls.append(line(n, t0 + i, "JVCurveDone", {"index": i, "label": f"{1.0 + i / 100:.3f} V",
                                                   "dark": False, "led_level_v": 1.0 + i / 100,
                                                   "metrics": {"voc": v}}, run_id, node_path))
        n += 1
    ls += [line(n, t0 + 9, "JVFinished", {"n_curves": len(vocs) + 1, "curves": []}, run_id, node_path),
           line(n + 1, t0 + 9.1, "NodeDone", {"node_path": node_path, "outcome": "ok",
                                              "detail": {"module": "jv_bace", "kept": len(vocs) + 1,
                                                         "requested": len(vocs) + 1,
                                                         "folder": "runs/jv", "led_v": None,
                                                         "summary": "x"}}, run_id, node_path),
           line(n + 2, t0 + 9.2, "RunStateChanged", {"state": "done", "reason": ""}, run_id),
           line(n + 3, t0 + 9.3, "RunStateChanged", {"state": "parked", "reason": "done"}, run_id)]
    return ls


def test_the_jv_bace_line_gives_the_voc_range_over_its_levels(tmp_path):
    out = str(tmp_path)
    write_session(out, "20260902_120000",
                  jv_run("20260902_120000-001", T0, 1, [1.0269, 1.0330, 1.0479])
                  + jv_run("20260902_120000-002", T0 + 100, 50, [0.906]))
    with Journal(out, "20260902_120000", header=HEADER) as j:
        many, one = j.run_index("this")[1], j.run_index("this")[0]
        assert many["outcome_text"] == "4 curves · V_oc 1.0269 … 1.0479 V"
        assert (many["voc_min"], many["voc_max"], many["light_curves"]) == (1.0269, 1.0479, 3)
        assert one["outcome_text"] == "2 curves · V_oc 0.906 V" and one["light_curves"] == 1


def test_run_record_finds_a_run_in_any_session_file_with_its_nodes(tmp_path):
    out = str(tmp_path)
    older = bace_run("20260901_100000-001", T0 - 86400, 1, {"n_loops": 7})
    older[-3]["data"]["detail"].update({"module": "bace", "voc": {"value": 0.906, "how": "jv_bace"},
                                        "led_v": 1.02, "temperature_k": 250.1,
                                        "temperature_how": "settled",
                                        "temperature_source": "console",
                                        "summary": "Q 1e-10 C · 4/20"})
    write_session(out, "20260901_100000", older)
    write_session(out, "20260902_120000", bace_run("20260902_120000-001", T0, 1, {"n_loops": 3}))
    with Journal(out, "20260902_120000", header=HEADER) as j:
        rec = j.run_record("20260901_100000-001")
        assert rec is not None and rec["state"] == "done" and rec["session_id"] == "20260901_100000"
        assert rec["tree"] == {"kind": "module", "module": "bace", "params": {"n_loops": 7}}
        node = rec["nodes"]["bace"]
        assert {k: node[k] for k in ("module", "outcome", "kept", "requested", "voc", "voc_how",
                                     "led_v", "temperature_k", "temperature_how",
                                     "temperature_source", "summary", "folder")} == {
            "module": "bace", "outcome": "ok", "kept": 4, "requested": 20, "voc": 0.906,
            "voc_how": "jv_bace", "led_v": 1.02, "temperature_k": 250.1,
            # Beside the number, as voc_how sits beside the V_oc: 250.1 K
            # that the console settled at is not 250.1 K that a loop asked for.
            "temperature_how": "settled", "temperature_source": "console",
            "summary": "Q 1e-10 C · 4/20", "folder": "runs/s4_290K_20260902_120000"}
        assert node["finished_at"] is not None and node["node_path"] == "bace"
        # What the node measured travels with it, so a results grid needs no
        # HDF5: the axis from RunFinished, and the shots counted.
        assert node["values"] == [0.906] and node["q_mean"] == [-3.52e-10]
        assert node["q_std"] == [1.8e-12] and node["shots"] == 4
        assert node["intensity_recorded"] == 0 and node["shots_flagged"] == 0
        assert rec["verdicts"] == []
        assert list(rec["nodes"]["bace"]) == list(node_record("", None, {}, None,
                                                              started_at=None, finished_at=None)), (
            "one key order from both sources -- the session's record builds through the same function")
        assert rec["node_count_done"] == 1
        assert j.run_record("20260902_120000-001")["nodes"] == {}, "a NodeDone naming no module"
        assert j.run_record("20260902_120000-009") is None
        assert j.run_record("20250101_000000-001") is None
        assert "nodes" not in j.run_index("all")[0], "the index stays lean; the record carries them"


def test_settle_history_counts_an_answered_timeout_pause_with_the_consoles_time_before_it(tmp_path):
    """The console took `elapsed_s` before the timeout pause and the operator
    another stretch: the run's own ETA learned the sum, and so does the Dry
    run. A refused setpoint's pause is not a settle and teaches nothing."""
    out = str(tmp_path)
    run_id = "20260902_130000-001"
    write_session(out, "20260902_130000", [
        line(1, T0 + 1800, "Verdict",
             {"level": "warn", "code": "temperature.timeout", "text": "230 K not reached",
              "node_path": "T=230K", "data": {"setpoint_k": 230.0, "elapsed_s": 1800.0,
                                              "reason": "timeout"}},
             run_id, "T=230K"),
        line(2, T0 + 1800, "NeedsOperator",
             {"what": "temperature timeout", "node_path": "T=230K",
              "detail": {"setpoint_k": 230.0, "elapsed_s": 1800.0, "reason": "timeout"}},
             run_id, "T=230K"),
        line(3, T0 + 2400, "OperatorResumed",
             {"node_path": "T=230K", "note": "close enough", "detail": {}}, run_id, "T=230K"),
        line(4, T0 + 2500, "NeedsOperator",
             {"what": "temperature refused", "node_path": "T=400K",
              "detail": {"setpoint_k": 400.0, "error": "exceeds the 350.0 K limit"}},
             run_id, "T=400K"),
        line(5, T0 + 2600, "OperatorResumed",
             {"node_path": "T=400K", "note": "", "detail": {}}, run_id, "T=400K"),
    ])
    with Journal(out, "20260902_130000", header=HEADER) as j:
        assert j.settle_history() == {230.0: [2400.0]}, "1800 s of console plus 600 s of operator"
