r"""The `.dat` export matches LabVIEW run files byte for byte.

Existing analysis depends on the exact format. `tests/test_storage.py`
checks byte-exact round trips, including these constraints:

* CRLF line endings, including the final row; tab separators; ASCII.
* Six significant figures with an unpadded exponent, from
  `bace.storage.numbers.lv_float`.
* Tables contain a header followed by data rows, as in `1_averagesQ`::

      % Vpre/V \tVcoll/V \tDelay / ns\tQ/C\tstdDevQ/C
      9.05000E-1\t-1.00000E+0\t8.80000E+1\t3.65257E-10\t4.01451E-12

* Matrices contain column coordinates, a blank separator line, and rows
  prefixed by their coordinates::

      % Vpre/V  //  time/s\t0.00000E+0\t5.00000E-10\t...
      <blank>
      9.05000E-1\t-3.31157E-5\t...

  The blank line is part of the format. Readers must account for it rather
  than skip only one header line and treat the separator as a data row.
* Header spaces are significant. `'% Vpre/V '` has a trailing space,
  `'% Vpre/V  //  time/s'` has two spaces around the slashes, and
  `'% Loop // Vpre / V '` has one. Parsers match this text exactly.
* `1_allLoopsQ` uses scientific data and plain `%f` V_pre column labels.

Filenames use `<stem><YYYYMMDD>_<HHMMSS>.dat`, with one timestamp per run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from .numbers import lv_fixed, lv_float

EOL = "\r\n"
SEP = "\t"

# LabVIEW header matching requires these trailing spaces.
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
    """Renders a matrix with a header, blank separator line and data rows.

    `data` has shape `(n_rows, n_columns)` and must match both label arrays.
    """
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
    # `newline=""` preserves the explicit CRLF bytes required by LabVIEW files.
    with open(path, "w", newline="", encoding="ascii") as fh:
        fh.write(text)
    return path


@dataclass
class LegacyWriter:
    """Writes the LabVIEW-compatible run file set into one folder.

    Only requested files are written. A run without intensity or per-shot
    trace export can therefore contain only the five summary and trace files.
    """

    folder: str
    stamp: str          # 'YYYYMMDD_HHMMSS'

    def averages_q(self, vpre, vcoll, delay_ns, q_mean, q_std) -> str:
        rows = [(a, b, c, d, e) for a, b, c, d, e
                in zip(vpre, vcoll, delay_ns, q_mean, q_std)]
        return _write(self.folder, STEMS["averages_q"], self.stamp,
                      render_table(H_AVERAGES_Q, rows))

    def all_loops_q(self, vpre, q_all) -> str:
        """Writes per-shot Q in C with 1-based loop labels.

        `q_all` has shape `(n_loops, n_steps)`; `vpre` supplies column labels in V.
        """
        q_all = np.asarray(q_all, dtype=float)
        loops = np.arange(1, q_all.shape[0] + 1, dtype=float)
        return _write(self.folder, STEMS["all_loops_q"], self.stamp,
                      render_matrix(H_ALL_LOOPS_Q, vpre, loops, q_all,
                                    column_format="fixed"))

    def trace_matrix(self, which: str, time_s, vpre, data) -> str:
        """Writes loop-averaged traces with time and V_pre coordinates.

        `which` selects `averages_photocurrent`, `averages_light` or
        `averages_dark`. `time_s` is in s, `vpre` in V and `data` in A.
        """
        return _write(self.folder, STEMS[which], self.stamp,
                      render_matrix(H_TRACE_MATRIX, time_s, vpre, data))

    def averages_intensity(self, vpre, mean_w, std_w) -> str:
        rows = list(zip(vpre, mean_w, std_w))
        return _write(self.folder, STEMS["averages_intensity"], self.stamp,
                      render_table(H_INTENSITY, rows))


# -- reading and byte-exact round trips ---------------------------------
def read_table(path: str) -> tuple[list[str], np.ndarray]:
    raw = open(path, "rb").read().decode("ascii")
    lines = raw.split(EOL)
    header = lines[0].split(SEP)
    rows = [[float(c) for c in ln.split(SEP)] for ln in lines[1:] if ln.strip()]
    return header, np.array(rows)


def read_matrix(path: str) -> tuple[str, np.ndarray, np.ndarray, np.ndarray]:
    """Returns `(corner, columns, row_labels, data)` from a `.dat` matrix."""
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
