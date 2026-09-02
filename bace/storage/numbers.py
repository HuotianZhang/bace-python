"""LabVIEW's number formatting, reproduced exactly.

The engine wrote every value with what looks like `%.5E` but is not: LabVIEW
emits the **minimum-width exponent**, so 0.905 becomes `9.05000E-1`, not
`9.05000E-01`. Python's `%E` always pads to two digits, so a naive port
produces files that differ from the originals in almost every cell.

This is one function because it is the single place that difference lives, and
because a byte-exact round-trip test of the 2026-08-07 archive is what proves
it right.
"""
from __future__ import annotations

import math

SIG_FIGS = 6
"""Digits actually stored: one before the point plus five after. Everything
written through here is lossy at the seventh figure, which is why HDF5 is the
primary store and these files are an export."""


FIELD_WIDTH = 10
"""The format is `%10.5e`, not `%.5e`. The width never shows on a real number —
`9.05000E-1` is already 10 characters and anything with a sign or a two-digit
exponent is 11 — so it is invisible until a value is not finite. `NaN` is three
characters and comes out right-aligned as `'       NaN'`, which is exactly what
the archive contains for loops that were never run."""


def lv_float(x: float, decimals: int = 5, width: int = FIELD_WIDTH) -> str:
    """`%10.5E` with LabVIEW's unpadded exponent.

        0.905        -> '9.05000E-1'
        -1.0         -> '-1.00000E+0'
        3.65257e-10  -> '3.65257E-10'
        0.0          -> '0.00000E+0'
        nan          -> '       NaN'
    """
    x = float(x)
    if math.isnan(x):
        return "NaN".rjust(width)
    if math.isinf(x):
        return ("Inf" if x > 0 else "-Inf").rjust(width)
    mantissa, exponent = f"{x:.{decimals}E}".split("E")
    sign, digits = exponent[0], exponent[1:].lstrip("0")
    return f"{mantissa}E{sign}{digits or '0'}".rjust(width)


def lv_fixed(x: float, decimals: int = 6) -> str:
    """Plain `%f`, used for the V_pre column labels of `1_allLoopsQ`.

    The original mixes the two formats in one file: scientific for data,
    fixed for that header row. Reproduced rather than tidied, because tidying
    it would break every script that parses the header.
    """
    return f"{float(x):.{decimals}f}"
