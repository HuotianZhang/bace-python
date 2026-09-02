r"""The engine's `.dat` files, written byte-for-byte.

Huotian's analysis reads these directly, so the port writes them exactly as
LabVIEW did rather than approximately. "Exactly" here is not a figure of
speech: `tests/test_storage.py` reads the 2026-08-07 archive, writes it back
out through this module, and compares the bytes.

The conventions, recovered from that archive:

* **CRLF** line endings throughout, including after the final row.
* Tab separated, ASCII.
* Values as `%.5E` with LabVIEW's **unpadded exponent** (`bace.storage.numbers`).
* Two file shapes:

  *table* — one header line, then one line per row. Used by `1_averagesQ`::

      % Vpre/V \tVcoll/V \tDelay / ns\tQ/C\tstdDevQ/C
      9.05000E-1\t-1.00000E+0\t8.80000E+1\t3.65257E-10\t4.01451E-12

  *matrix* — a header line whose cells are the column coordinates, then a
  **blank line**, then one line per row prefixed by its own coordinate::

      % Vpre/V  //  time/s\t0.00000E+0\t5.00000E-10\t...
      <blank>
      9.05000E-1\t-3.31157E-5\t...

  The blank line is not decoration; scripts that skip exactly one header line
  would read it as a row. Reproduced.

* Header text is copied verbatim, trailing spaces included: `'% Vpre/V '` has a
  space before its tab, `'% Vpre/V  //  time/s'` has two spaces around the
  slashes, and `'% Loop // Vpre / V '` has one. These are not typos to fix —
  they are what the parsers on the other side match against.
* `1_allLoopsQ` mixes formats: scientific for data, plain `%f` for the V_pre
  column labels.

Filenames are `<stem><YYYYMMDD>_<HHMMSS>.dat`, all sharing one timestamp per
run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .numbers import lv_fixed, lv_float

EOL = "\r\n"
SEP = "\t"

# Header strings, verbatim from the archive. Trailing spaces are significant.
H_AVERAGES_Q = ("% Vpre/V ", "Vcoll/V ", "Delay / ns", "Q/C", "stdDevQ/C")
H_ALL_LOOPS_Q = "% Loop // Vpre / V "
H_TRACE_MATRIX = "% Vpre/V  //  time/s"
H_INTENSITY = ("% Vpre/V ", "Intensity/W", "stdDevIntensity/W")

STEMS = {
    "all_loops_q": "1_allLoopsQ",
    "averages_q": "1_averagesQ",
    "all_loops_intensity": "1b_allLoopsIntensity",
    "averages_intensity": "1b_averagesIntensity",
    "averages_photocurrent": "2_averagesPhotoCurrent",
    "all_loops_photocurrent": "3_allLoopsPhotoCurrent",
    "averages_light": "4_averagesLightCurrent",
    "averages_dark": "4_averagesDarkCurrent",
}


# -- writing --------------------------------------------------------------
def _line(cells: Sequence[str]) -> str:
    return SEP.join(cells) + EOL


def render_table(header: Sequence[str], rows: Sequence[Sequence[float]]) -> str:
    out = [_line(list(header))]
    for r in rows:
        out.append(_line([lv_float(v) for v in r]))
    return "".join(out)


def render_matrix(corner: str, columns: Sequence[float], row_labels: Sequence[float],
                  data: np.ndarray, *, column_format: str = "sci",
                  row_format: str = "sci") -> str:
    """The header/blank-line/rows shape. `data` is (n_rows, n_columns)."""
    data = np.asarray(data, dtype=float)
    if data.shape != (len(row_labels), len(columns)):
        raise ValueError(
            f"data is {data.shape} but the labels imply "
            f"({len(row_labels)}, {len(columns)})"
        )
    fc = lv_float if column_format == "sci" else lv_fixed
    fr = lv_float if row_format == "sci" else lv_fixed

    out = [_line([corner] + [fc(c) for c in columns]), EOL]
    for label, row in zip(row_labels, data):
        out.append(_line([fr(label)] + [lv_float(v) for v in row]))
    return "".join(out)


def _write(folder: str, stem: str, stamp: str, text: str) -> str:
    path = os.path.join(folder, f"{stem}{stamp}.dat")
    # newline="" so Python does not translate the CRLF we wrote deliberately
    with open(path, "w", newline="", encoding="ascii") as fh:
        fh.write(text)
    return path


@dataclass
class LegacyWriter:
    """Writes the engine's file set into one folder.

    Only the files a run actually produces are written — the 2026-08-07 archive
    has five, because the intensity files and `3_allLoopsPhotoCurrent` were not
    produced by that run.
    """

    folder: str
    stamp: str          # 'YYYYMMDD_HHMMSS'

    def averages_q(self, vpre, vcoll, delay_ns, q_mean, q_std) -> str:
        rows = [(a, b, c, d, e) for a, b, c, d, e
                in zip(vpre, vcoll, delay_ns, q_mean, q_std)]
        return _write(self.folder, STEMS["averages_q"], self.stamp,
                      render_table(H_AVERAGES_Q, rows))

    def all_loops_q(self, vpre, q_all) -> str:
        """`q_all` is (n_loops, n_steps); loop numbers are written 1-based."""
        q_all = np.asarray(q_all, dtype=float)
        loops = np.arange(1, q_all.shape[0] + 1, dtype=float)
        return _write(self.folder, STEMS["all_loops_q"], self.stamp,
                      render_matrix(H_ALL_LOOPS_Q, vpre, loops, q_all,
                                    column_format="fixed"))

    def trace_matrix(self, which: str, time_s, vpre, data) -> str:
        """`which` is one of averages_photocurrent / averages_light / averages_dark."""
        return _write(self.folder, STEMS[which], self.stamp,
                      render_matrix(H_TRACE_MATRIX, time_s, vpre, data))

    def averages_intensity(self, vpre, mean_w, std_w) -> str:
        rows = list(zip(vpre, mean_w, std_w))
        return _write(self.folder, STEMS["averages_intensity"], self.stamp,
                      render_table(H_INTENSITY, rows))


# -- reading (for round-tripping and for old runs) -----------------------
def read_table(path: str) -> tuple[list[str], np.ndarray]:
    raw = open(path, "rb").read().decode("ascii")
    lines = raw.split(EOL)
    header = lines[0].split(SEP)
    rows = [[float(c) for c in ln.split(SEP)] for ln in lines[1:] if ln.strip()]
    return header, np.array(rows)


def read_matrix(path: str) -> tuple[str, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (corner, columns, row_labels, data)."""
    raw = open(path, "rb").read().decode("ascii")
    lines = raw.split(EOL)
    head = lines[0].split(SEP)
    corner, columns = head[0], np.array([float(c) for c in head[1:]])
    labels, rows = [], []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        cells = ln.split(SEP)
        labels.append(float(cells[0]))
        rows.append([float(c) for c in cells[1:]])
    return corner, columns, np.array(labels), np.array(rows)


def find(folder: str, which: str) -> str | None:
    stem = STEMS[which]
    for name in sorted(os.listdir(folder)):
        if name.startswith(stem) and name.endswith(".dat"):
            return os.path.join(folder, name)
    return None
