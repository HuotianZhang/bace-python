"""Number formatting matches LabVIEW `.dat` files byte for byte.

LabVIEW uses an unpadded exponent: 0.905 becomes `9.05000E-1`.
Python's `%E` pads the exponent to two digits, changing almost every cell.
`tests/test_storage.py` checks byte-exact round trips through this shared
formatter.
"""
from __future__ import annotations

import math

SIG_FIGS = 6
"""The export stores six significant figures, one before the decimal point.

The seventh figure is lost, so HDF5 holds the primary full-precision data.
"""


FIELD_WIDTH = 10
"""The export's minimum field width is ten characters.

Finite values already fill `%10.5e`: `9.05000E-1` has ten characters,
and a sign or two-digit exponent requires eleven. Non-finite values expose
the padding: `NaN` must be right-aligned as `'       NaN'` to match the
LabVIEW files, including unmeasured loops.
"""


def lv_float(x: float, decimals: int = 5, width: int = FIELD_WIDTH) -> str:
    """Formats a number as `%10.5E` with LabVIEW's unpadded exponent.

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
    """Formats V_pre column labels in `1_allLoopsQ` as plain `%f`.

    LabVIEW compatibility requires scientific data and decimal header labels
    in the same file. Changing the header format breaks its parsers.
    """
    return f"{float(x):.{decimals}f}"
