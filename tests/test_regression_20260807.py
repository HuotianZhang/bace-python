#!/usr/bin/env python3
"""Numerical regression against a real LabVIEW measurement.

Dataset
    s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521

Panel settings (from z_Parameters*.png in the same folder)
    Vpre 0.905 -> 0.907 step 0.001 V      Vcoll -1 V      Delay 88 ns
    20 averages, 100 loops                offset corr ON
    t0 Integration 3.18e-7 s              Pulse Width 5 us
    Timebase 200 (panel note: "timebase*10" -> 2 us full screen)
    Record Length 5000 -> 4000 points returned, dt 0.5 ns
    MeasResistor 5.192 ohm ("50 old big Amp / 5.192 new Amp")
    Pulse Amp 4, Probe Attenuation 1, ShutterModule nr 0 / ID 9

The engine wrote its own inputs (4_averagesLightCurrent, 4_averagesDarkCurrent)
and its own outputs (2_averagesPhotoCurrent, 1_averagesQ). This recomputes the
outputs from the inputs with bace.core and asserts they match.

Tolerances are derived from the archive, not chosen. The files are written with
`%10.5e` -- six significant figures -- so a written Q of 3.65316e-10 carries a
quantisation of 1e-15, and a half-ulp rounding of the reference alone permits
~1.4e-6 of apparent disagreement. The test computes that bound per value rather
than hard-coding a number, and allows a small multiple of it for the path that
goes through two rounded files instead of one.

    python tests/test_regression_20260807.py [path to the measurement folder]
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bace.core.process import photocurrent, charge   # noqa: E402

T0_INTEGRATION = 3.18e-7
FILE_SIG_FIGS = 6           # "%10.5e"
TOL_ULPS_FROM_PHOTO = 1.0   # one rounded file between us and the reference
TOL_ULPS_FROM_TRACES = 4.0  # three rounded files: light, dark, and Q


def rel_ulp(value: float, sig_figs: int = FILE_SIG_FIGS) -> float:
    """Relative size of one unit in the last written place of `value`."""
    exponent = np.floor(np.log10(abs(value)))
    ulp = 10.0 ** (exponent - (sig_figs - 1))
    return float(ulp / abs(value))


def read_matrix(folder: str, pattern: str):
    """Engine data files: header row of time values, then one row per Vpre."""
    path = glob.glob(os.path.join(folder, pattern))[0]
    rows = open(path).read().splitlines()
    t = np.array([float(x) for x in rows[0].split("\t")[1:] if x.strip()])
    labels, data = [], []
    for r in rows[1:]:
        if not r.strip():
            continue
        cells = r.split("\t")
        vals = [float(x) for x in cells[1:] if x.strip()]
        if vals:
            labels.append(float(cells[0]))
            data.append(vals)
    return t, np.array(labels), np.array(data)


def read_q(folder: str):
    path = glob.glob(os.path.join(folder, "1_averagesQ*.dat"))[0]
    rows = [r for r in open(path).read().splitlines()[1:] if r.strip()]
    vpre = np.array([float(r.split("\t")[0]) for r in rows])
    q = np.array([float(r.split("\t")[3]) for r in rows])
    return vpre, q


def main(folder: str) -> int:
    t, vpre, light = read_matrix(folder, "4_averagesLightCurrent*.dat")
    _, _, dark = read_matrix(folder, "4_averagesDarkCurrent*.dat")
    _, _, photo_ref = read_matrix(folder, "2_averagesPhotoCurrent*.dat")
    vpre_q, q_ref = read_q(folder)
    dt = float(t[1] - t[0])

    assert np.allclose(vpre, vpre_q), "Vpre grids disagree between files"
    print(f"  {light.shape[0]} prebias points, {light.shape[1]} samples, dt = {dt*1e9:g} ns")
    print(f"  integration from {T0_INTEGRATION*1e9:g} ns "
          f"({int((t > T0_INTEGRATION).sum())} samples)\n")

    ok = True
    print(f"  {'Vpre':>6} {'LabVIEW Q':>16} {'ported Q':>16} "
          f"{'traces/ulp':>10} {'photoC/ulp':>10}")
    print(f"  {'':6} {'':16} {'':16} "
          f"{'<%.1f' % TOL_ULPS_FROM_TRACES:>10} {'<%.1f' % TOL_ULPS_FROM_PHOTO:>10}")
    for i, v in enumerate(vpre):
        mine = photocurrent(light[i], dark[i], dt, offset_correct=True)
        q_traces = charge(mine, dt, T0_INTEGRATION)
        q_photo = charge(photo_ref[i], dt, T0_INTEGRATION)
        d1 = abs(q_traces - q_ref[i]) / abs(q_ref[i])
        d2 = abs(q_photo - q_ref[i]) / abs(q_ref[i])
        u = rel_ulp(q_ref[i])
        passed = d1 < TOL_ULPS_FROM_TRACES * u and d2 < TOL_ULPS_FROM_PHOTO * u
        ok &= passed
        print(f"  {v:6.3f} {q_ref[i]:16.8e} {q_traces:16.8e} "
              f"{d1/u:10.2f} {d2/u:10.2f}{'' if passed else '  <-- FAIL'}")

    # the baseline correction, checked directly
    raw_tail = (light[0] - dark[0])[t > t[-1] * 0.9].mean()
    ref_tail = photo_ref[0][t > t[-1] * 0.9].mean()
    print(f"\n  last-10% mean, raw light-dark : {raw_tail:.3e} A")
    print(f"  last-10% mean, engine output  : {ref_tail:.3e} A   (baseline removed)")

    print("\n  PASS" if ok else "\n  FAIL")
    return 0 if ok else 1


ARCHIVE_ENV = "BACE_ARCHIVE"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ARCHIVE_GLOBS = (
    os.environ.get(ARCHIVE_ENV, ""),
    "/mnt/user-data/uploads/s4_PTQ10*",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "s4_PTQ10*"),
    # The copy the repository carries. It was there all along and nothing
    # looked for it, so the numerical regression against the 2026-08-07 run and
    # the byte-exact `.dat` round trip -- seven tests, and two of the most
    # valuable in the suite -- skipped on every checkout that had not set
    # `BACE_ARCHIVE` by hand. Last, so a folder somebody named still wins.
    os.path.join(_REPO, "bench-archive", "s4_PTQ10*"),
)


def find_archive() -> str | None:
    for pattern in ARCHIVE_GLOBS:
        if not pattern:
            continue
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def test_ported_maths_reproduces_the_labview_output():
    """Run under pytest so the suite catches a numerical regression.

    Skips rather than fails when the archive is not mounted -- the data is the
    user's, not the repository's -- but point %s at a measurement folder and it
    runs anywhere.
    """ % ARCHIVE_ENV
    import pytest

    folder = find_archive()
    if folder is None:
        pytest.skip(
            f"no measurement archive found; set {ARCHIVE_ENV} to a folder "
            "containing 4_averagesLightCurrent*.dat and friends"
        )

    t, vpre, light = read_matrix(folder, "4_averagesLightCurrent*.dat")
    _, _, dark = read_matrix(folder, "4_averagesDarkCurrent*.dat")
    _, _, photo_ref = read_matrix(folder, "2_averagesPhotoCurrent*.dat")
    vpre_q, q_ref = read_q(folder)
    dt = float(t[1] - t[0])
    assert np.allclose(vpre, vpre_q)

    for i, v in enumerate(vpre):
        mine = photocurrent(light[i], dark[i], dt, offset_correct=True)
        u = rel_ulp(q_ref[i])
        d_traces = abs(charge(mine, dt, T0_INTEGRATION) - q_ref[i]) / abs(q_ref[i])
        d_photo = abs(charge(photo_ref[i], dt, T0_INTEGRATION) - q_ref[i]) / abs(q_ref[i])
        assert d_traces < TOL_ULPS_FROM_TRACES * u, (
            f"Vpre {v}: recomputed from raw traces differs by {d_traces/u:.2f} ulp"
        )
        assert d_photo < TOL_ULPS_FROM_PHOTO * u, (
            f"Vpre {v}: recomputed from the stored photocurrent differs by "
            f"{d_photo/u:.2f} ulp"
        )

    # the baseline correction actually happened
    tail = t > t[-1] * 0.9
    assert abs((light[0] - dark[0])[tail].mean()) > 1e-6
    assert abs(photo_ref[0][tail].mean()) < 1e-9


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else find_archive()
    if folder is None:
        print("usage: test_regression_20260807.py <measurement folder>")
        raise SystemExit(2)
    raise SystemExit(main(folder))
