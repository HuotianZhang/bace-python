"""Read-only queries for the real drivers, spelt once.

Three instruments answer the same three shapes of question -- is the output
on, what is this level, what is this word -- and each driver used to keep a
cached flag instead of asking. That cache is exactly what the service's bench
card and the relay interlock must not trust: the 81150A the LabVIEW VI left
ON shows as off in a driver that was constructed a minute ago, and the relay
would be thrown under it. These helpers ask the instrument and turn the reply
into a Python value, or into `None` / `"?"` when it will not answer, on the
principle the whole read-back path follows: a plausible default is a lie in
the file, an honest blank is not.

Not protocol members. The experiment layer never calls them; the service's
`rigs.Bench` reaches for them with `getattr` and falls back to the cached
properties on a driver (the simulator) that has none.
"""
from __future__ import annotations

from typing import Any


def ask(io: Any, query: str) -> str | None:
    """The instrument's reply, stripped, or None when the query fails.

    Never raises: a read-back that raised inside a run's unwind would mask
    the exception already propagating, and a bench card must render on a
    bench where one instrument is dead.
    """
    try:
        reply = io.query(query)
    except Exception:                                       # noqa: BLE001
        return None
    text = str(reply).strip()
    return text if text else None


def on_off(reply: str | None) -> bool | None:
    """`:OUTP?` and friends: `1`/`ON` -> True, `0`/`OFF` -> False, else None."""
    if reply is None:
        return None
    up = reply.strip().upper().rstrip(";")
    if up in ("1", "ON", "+1"):
        return True
    if up in ("0", "OFF", "+0"):
        return False
    return None


def number(reply: str | None) -> float | None:
    """A SCPI number (`+1.02000E+00`) as a float, or None."""
    if reply is None:
        return None
    try:
        return float(reply.strip().rstrip(";"))
    except ValueError:
        return None


def word(reply: str | None, known: tuple[str, ...]) -> str:
    """The first of `known` that the reply starts with, the reply itself when
    it is none of them (information, not to be hidden), `?` when there is
    no reply."""
    if reply is None:
        return "?"
    up = reply.strip().upper().rstrip(";")
    if not up:
        return "?"
    for token in known:
        if up.startswith(token):
            return token
    return up
