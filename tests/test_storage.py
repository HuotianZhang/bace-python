"""Storage, verified against the archive it has to stay compatible with.

The strongest test here is not that the writer produces something readable — it
is that reading the 2026-08-07 archive and writing it straight back out
reproduces the original **bytes**. That catches the exponent padding, the field
width, the blank separator line, the CRLF, and every trailing space in a header,
none of which a "looks right" test would notice.
"""
from __future__ import annotations

import os

import numpy as np
import pytest
from dataclasses import replace

from bace.core.axis import bace_sweep
from bace.drivers.simulated import make_bench
from bace.experiment.rig import Rig, RigConfig
from bace.experiment.transient import RunConfig, run_transient_scan
from bace.storage import legacy_dat as L
from bace.storage.naming import RunMetadata
from bace.storage.numbers import lv_fixed, lv_float
from bace.storage.recorder import RunRecorder, record
from tests.test_regression_20260807 import find_archive

NO_SLEEP = lambda s: None


# -- the number format ----------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (0.905, "9.05000E-1"),
    (-1.0, "-1.00000E+0"),
    (88.0, "8.80000E+1"),
    (3.65257e-10, "3.65257E-10"),
    (4.01451e-12, "4.01451E-12"),
    (0.0, "0.00000E+0"),
    (1.9995e-6, "1.99950E-6"),
    (float("nan"), "       NaN"),          # right-aligned in a 10-wide field
])
def test_labview_number_format(value, expected):
    assert lv_float(value) == expected


def test_the_vpre_header_row_uses_plain_fixed_point():
    """`1_allLoopsQ` mixes formats — scientific data, fixed header. Reproduced,
    not tidied, because the parsers on the other side match on it."""
    assert lv_fixed(0.905) == "0.905000"


# -- byte-exact round trip -----------------------------------------------
def _archive():
    folder = find_archive()
    if folder is None:
        pytest.skip("no measurement archive found; set BACE_ARCHIVE")
    return folder


@pytest.mark.parametrize("which,column_format", [
    ("averages_photocurrent", "sci"),
    ("averages_light", "sci"),
    ("averages_dark", "sci"),
    ("all_loops_q", "fixed"),
])
def test_matrix_files_round_trip_byte_for_byte(which, column_format):
    path = L.find(_archive(), which)
    assert path, f"{which} not in the archive"
    corner, columns, labels, data = L.read_matrix(path)
    rendered = L.render_matrix(corner, columns, labels, data,
                               column_format=column_format)
    assert rendered == open(path, "rb").read().decode("ascii")


def test_averages_q_round_trips_byte_for_byte():
    path = L.find(_archive(), "averages_q")
    header, rows = L.read_table(path)
    assert L.render_table(header, rows) == open(path, "rb").read().decode("ascii")


def test_the_archive_is_a_partial_run_and_the_mean_ignores_the_gap():
    """100 loops planned, 20 run, the rest NaN — and `1_averagesQ` is the
    nan-aware mean over the 20. Partial runs are the normal case, not an edge
    case, so the writer has to produce them."""
    folder = _archive()
    _, _, _, q_all = L.read_matrix(L.find(folder, "all_loops_q"))
    _, rows = L.read_table(L.find(folder, "averages_q"))

    completed = (~np.isnan(q_all)).all(axis=1).sum()
    assert q_all.shape[0] == 100 and completed == 20

    q_mean, q_std = np.nanmean(q_all, axis=0), np.nanstd(q_all, axis=0, ddof=1)
    np.testing.assert_allclose(q_mean, rows[:, 3], rtol=2e-6)

    # The std tolerance has to be derived, not chosen. Each Q in the file is
    # rounded to six significant figures, so it carries a half-ulp of about
    # 5e-16 -- negligible against Q itself (3.7e-10) but not against a spread of
    # 3.6e-12, which is ~100x smaller. Rounding therefore propagates into the
    # std about a hundred times harder than into the mean.
    half_ulp = 0.5 * 10.0 ** (np.floor(np.log10(np.abs(q_mean))) - 5)
    bound = half_ulp / q_std
    assert np.all(bound < 2e-4)                  # sanity: the bound is not vacuous
    np.testing.assert_allclose(q_std, rows[:, 4], rtol=bound.max())


# -- the recorder end to end ---------------------------------------------
def _run(tmp_path, spec, cfg_kw=None, meta_kw=None, **rec_kw):
    sim = make_bench(seed=3)
    sim.led.set_pulse(1.020, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter,
              config=RigConfig(), power=sim.power)
    meta = RunMetadata(sample="sim", material="TEST", pixel="pxa",
                       temperature_k=290, led_drive_v=1.020, voc_v=0.906,
                       **(meta_kw or {}))
    rec = RunRecorder(str(tmp_path), meta, **rec_kw)
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0, record_length=500,
                    **(cfg_kw or {}))
    events = list(record(run_transient_scan(rig, spec, cfg, sleep=NO_SLEEP), rec))
    return rec, events


def _h5(rec):
    from bace.storage.hdf5 import read_run
    return read_run([p for p in rec.written if p.endswith(".h5")][0])


@pytest.mark.parametrize("asked, expect_pol, expect_source", [
    ("INV", "INV", "set by this run"),
    ("NORM", "NORM", "set by this run"),
    ("leave", "NORM", "left as found"),   # the simulator starts NORM
])
def test_the_file_records_the_polarity_that_ran_not_the_one_that_was_asked_for(
        tmp_path, asked, expect_pol, expect_source):
    """`output_polarity = "leave"` writes nothing into the run config -- that is
    the point of it. Without the readback the file could not say which
    convention produced its own numbers, which is exactly the gap that made the
    first delay scan un-interpretable."""
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.90, 0.02, n_loops=1),
                  cfg_kw={"output_polarity": asked})
    h = _h5(rec)
    assert h["resolved"]["bias_output_polarity"] == expect_pol
    assert h["resolved"]["bias_polarity_source"] == expect_source
    # and the request is still there, so the two can be compared
    assert h["run_config"]["output_polarity"] == asked


def test_a_file_written_before_the_resolved_group_still_reads(tmp_path):
    """Schema 1 files have no /config/resolved. `read_run` must not fail on the
    2026-08 archive."""
    import h5py
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.90, 0.02, n_loops=1))
    path = [p for p in rec.written if p.endswith(".h5")][0]
    with h5py.File(path, "a") as f:
        del f["config/resolved"]
    from bace.storage.hdf5 import read_run
    assert read_run(path)["resolved"] == {}


def test_a_recorded_run_writes_the_whole_legacy_set_plus_hdf5(tmp_path):
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.92, 0.02, n_loops=3))
    names = sorted(os.path.basename(p) for p in rec.written)
    stems = [n.split("2")[0] for n in names]
    assert any(n.startswith("1_averagesQ") for n in names)
    assert any(n.startswith("1_allLoopsQ") for n in names)
    assert any(n.startswith("2_averagesPhotoCurrent") for n in names)
    assert any(n.startswith("4_averagesLightCurrent") for n in names)
    assert any(n.startswith("4_averagesDarkCurrent") for n in names)
    assert any(n.startswith("1b_averagesIntensity") for n in names)
    assert any(n.endswith(".h5") for n in names)
    _ = stems


def test_the_written_files_read_back_as_what_was_measured(tmp_path):
    rec, events = _run(tmp_path, bace_sweep(0.88, 0.92, 0.02, n_loops=3))
    from bace.experiment.events import RunFinished
    fin = next(e for e in events if isinstance(e, RunFinished))

    _, rows = L.read_table(L.find(rec.folder, "averages_q"))
    np.testing.assert_allclose(rows[:, 0], fin.values, rtol=1e-5)
    np.testing.assert_allclose(rows[:, 3], fin.q_mean, rtol=1e-5)

    from bace.storage.hdf5 import read_run
    h = read_run([p for p in rec.written if p.endswith(".h5")][0])
    np.testing.assert_allclose(h["q_mean"], fin.q_mean)
    assert h["attrs"]["loops_completed"] == 3
    assert bool(h["attrs"]["complete"]) is True
    assert h["axis"]["name"] == "vpre"
    assert h["rig_config"]["sense_resistor_ohm"] == pytest.approx(5.192)
    assert h["run_config"]["n_averages"] == 64
    assert h["metadata"]["temperature_k"] == pytest.approx(290.0)


def test_the_file_carries_the_sign_convention_its_currents_were_written_in(tmp_path):
    """`current_sign` is multiplied into every trace once, in the digitizer
    fetch. A file that did not say which sign it used could not be compared
    with the LabVIEW engine's: the port read every current positive and the
    original every one negative for the same physics (2026-09-02). The value
    rides into `/config/rig` with the rest of `RigConfig`, so the recorder did
    not have to change -- this pins that it actually arrives, next to numbers
    that carry it."""
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.90, 0.02, n_loops=1))
    h = _h5(rec)
    assert h["rig_config"]["current_sign"] == -1.0
    assert np.all(h["q_mean"] < 0), "extraction reads negative in this convention"


def test_an_aborted_run_still_leaves_a_readable_folder(tmp_path):
    """The failure mode that matters: hours of loops, a fault at loop 7, and
    nothing on disk. Here loop 3 of 5 is interrupted and everything measured
    survives, with NaN for the loops that never ran."""
    sim = make_bench(seed=5)
    sim.led.set_pulse(1.020, 0.4)
    rig = Rig(bias=sim.bias, scope=sim.scope, shutter=sim.shutter, config=RigConfig())
    meta = RunMetadata(sample="sim", material="ABORT", started=__import__(
        "datetime").datetime(2026, 8, 31, 12, 0, 0))
    rec = RunRecorder(str(tmp_path), meta)
    cfg = RunConfig(n_averages=64, settle_s=0.0, dark_settle_s=0.0, record_length=500)

    gen = record(run_transient_scan(rig, bace_sweep(0.88, 0.92, 0.02, n_loops=5),
                                    cfg, sleep=NO_SLEEP), rec)
    seen = 0
    for ev in gen:
        from bace.experiment.events import StepDone
        if isinstance(ev, StepDone):
            seen += 1
            if seen == 7:                       # two full loops plus one step
                gen.close()
                break

    assert rec.written, "an aborted run wrote nothing"
    _, _, _, q_all = L.read_matrix(L.find(rec.folder, "all_loops_q"))
    assert q_all.shape == (5, 3)
    completed = (~np.isnan(q_all)).all(axis=1).sum()
    assert completed == 2
    assert np.isnan(q_all[4]).all()
    assert not np.isnan(q_all[2, 0])             # the partial third loop is kept
    assert sim.bench.bias_output is False        # and the bias was still parked


def test_shot_level_traces_are_opt_in(tmp_path):
    from bace.storage.hdf5 import read_run
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.92, 0.02, n_loops=2))
    h = read_run([p for p in rec.written if p.endswith(".h5")][0])
    assert "shots" not in h

    rec2, _ = _run(tmp_path / "big", bace_sweep(0.88, 0.92, 0.02, n_loops=2),
                   store_shots=True)
    h2 = read_run([p for p in rec2.written if p.endswith(".h5")][0])
    assert h2["shots"].shape[:2] == (2, 3)


def test_folder_name_matches_the_archive_convention():
    import datetime
    m = RunMetadata(sample="s4", material="PTQ10IT4F", pixel="pxa",
                    temperature_k=290, led_drive_v=1.020, voc_v=0.906,
                    offset_corrected=True,
                    started=datetime.datetime(2026, 8, 7, 11, 15, 21))
    assert m.folder_name() == (
        "s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521")


@pytest.mark.parametrize("field, typed, filed", [
    # `naming-plan.md` §2's table, which was written as the list of what these
    # three fields could do to a path and is now the list of what they cannot.
    ("sample", "a_b", "a-b"),                       # `_` separates fields; a name may not forge one
    ("sample", "s4 pixel a", "s4-pixel-a"),         # the 2026-09-01 bug: a space in a directory name
    ("material", "PTQ10:IT-4F", "PTQ10IT-4F"),      # the contract's own example; `:` fails on the lab PC
    ("sample", "a/b", "ab"),                        # two directories, not one
    ("sample", "../../etc", "etc"),                 # and not a walk out of `runs/`
    ("pixel", "px" + chr(92) + "a", "pxa"),
    ("sample", "///", ""),                          # reduced to nothing: the field leaves the name
    ("material", "PTQ10IT4F-batch-2026-08-A", "PTQ10IT4F-batch-2026-08"),   # NAME_MAX = 24
    ("sample", "образец4", "образец4"),               # not-ASCII is not the same as not-safe
])
def test_an_identity_field_is_one_path_segment(field, typed, filed):
    """Until 2026-09-05 these three went into the name as typed, which
    `docs/history/naming-plan.md` §2 carried as a live defect: on the lab PC a colon
    fails at folder creation, on Linux and the simulator it succeeds, so no
    test caught it. They are slugged now, at `NAME_MAX = 24`."""
    import datetime
    m = RunMetadata(temperature_k=290, started=datetime.datetime(2026, 8, 7, 11, 15, 21),
                    **{field: typed})
    name = m.folder_name()
    assert m.identity_in_name()[("sample", "material", "pixel").index(field)] == filed
    assert name == (f"{filed}_" if filed else "") + "290K_offsetcorr_20260807_111521"
    assert m.as_dict()[field] == typed, "the name is reduced; the record is not"


def test_temperature_provenance_stays_out_of_the_name():
    """`290K` in a folder name says nothing about where 290 came from, and the
    name cannot be made to: it is the 2026-08-07 convention, and every
    reference to that archive -- HANDOVER, the journals, the regression test --
    reads it. So the provenance is stored beside the number and the name does
    not move by so much as a character."""
    import datetime
    plain = RunMetadata(sample="s4", material="PTQ10IT4F", pixel="pxa",
                        temperature_k=290, led_drive_v=1.020, voc_v=0.906,
                        started=datetime.datetime(2026, 8, 7, 11, 15, 21))
    settled = replace(plain, temperature_how="settled", temperature_source="console")
    typed = replace(plain, temperature_how="typed")
    guessed = replace(plain, temperature_how="setpoint")
    assert plain.folder_name() == settled.folder_name() == typed.folder_name() \
        == guessed.folder_name()

    d = settled.as_dict()
    assert (d["temperature_k"], d["temperature_how"], d["temperature_source"]) == \
        (290, "settled", "console")
    # An unset pair is empty, not None: it goes into HDF5 attrs, which have no
    # null, and `""` is how the rest of `as_dict` spells "nothing here".
    assert plain.as_dict()["temperature_how"] == plain.as_dict()["temperature_source"] == ""


@pytest.mark.parametrize("comment, expect", [
    ("snake_case_note", "snake-case-note"),           # `_` separates fields: a comment may not forge one
    ("a/b:c*d?e", "abcde"),                           # not allowed in a Windows path segment
    ("up" + chr(92) + "down|<x>", "updownx"),         # the backslash above all: a path separator
    ("tab\there", "tab-here"),                        # control codes go, they do not become dashes
    ("  spaced  out  ", "spaced-out"),                # no spaces in a directory name
    ("сравнение LabVIEW", "сравнение-LabVIEW"),       # not-ASCII is not the same as not-safe
    ("   ", ""),                                      # nothing to say
    ("x" * 90, "x" * 64),                             # one long token: truncated as it stands
    ("a-" * 60, "a-" * 31 + "a"),                     # many words: cut on a word boundary
])
def test_a_comment_is_reduced_to_one_path_segment(comment, expect):
    from bace.storage.naming import slug
    assert slug(comment) == expect


def test_the_name_gets_the_slug_and_the_record_keeps_the_sentence():
    """`290K_1000mVLED_offsetcorr_LabVIEW panel replica - combination 4 -
    shutter only dark_20260902_012223` is a real directory in `runs/`: spaces
    in a path, and a claim -- "combination 4" -- that was overturned the next
    day and cannot be corrected now without renaming the folder. The name is
    reduced from here on. The sentence is not: it stays in the metadata, in
    full, where nothing depends on its shape."""
    import datetime
    said = "LabVIEW panel replica - combination 4 - shutter only dark"
    m = RunMetadata(sample="s4", temperature_k=290, offset_corrected=False,
                    comment=said, started=datetime.datetime(2026, 9, 2, 1, 22, 23))
    assert m.folder_name() == (
        "s4_290K_LabVIEW-panel-replica-combination-4-shutter-only-dark_20260902_012223")
    assert " " not in m.folder_name()
    assert m.as_dict()["comment"] == said, "the name is reduced; the record is not"

    # A comment with nothing left after the reduction drops the field rather
    # than leaving an empty one -- `s4_290K__20260902_012223` helps nobody.
    blank = replace(m, comment=" :: ")
    assert blank.folder_name() == "s4_290K_20260902_012223"
    assert blank.as_dict()["comment"] == " :: "


def test_the_file_says_whether_its_temperature_was_measured(tmp_path):
    """The 2026-08-07 defect in miniature: `output_polarity = "leave"` wrote
    nothing, so the file could not say which convention produced its numbers,
    and `/config/resolved` was the answer. A temperature is the same shape of
    problem -- 290 K typed into a recipe and 290 K settled by the 331 are the
    same float -- so the pair travels into `/metadata` with it."""
    from bace.storage.hdf5 import read_run
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.92, 0.02, n_loops=2),
                  meta_kw={"temperature_how": "settled",
                           "temperature_source": "simulated"})
    meta = read_run([p for p in rec.written if p.endswith(".h5")][0])["metadata"]
    assert meta["temperature_k"] == pytest.approx(290.0)
    assert (meta["temperature_how"], meta["temperature_source"]) == ("settled", "simulated"), (
        "a file written against the stand-in must not read as a measured one")


def test_a_run_survives_a_missing_h5py(tmp_path, monkeypatch):
    """An hour of measurement must not be lost because an optional dependency is
    absent. The first bench session hit exactly this on the lab PC: the .dat
    files were already written, and the ImportError threw them away."""
    import bace.storage.hdf5 as h5

    def no_h5py():
        raise ImportError("HDF5 storage needs h5py")

    monkeypatch.setattr(h5, "_require_h5py", no_h5py)
    rec, _ = _run(tmp_path, bace_sweep(0.88, 0.92, 0.02, n_loops=2))

    assert rec.hdf5_error and "h5py" in rec.hdf5_error
    names = [os.path.basename(p) for p in rec.written]
    assert any(n.startswith("1_averagesQ") for n in names)
    assert any(n.startswith("4_averagesLightCurrent") for n in names)
    assert not any(n.endswith(".h5") for n in names)
