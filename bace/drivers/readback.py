"""Parse read-only instrument replies for the real drivers.

The console and relay interlock need instrument state, not an initial cached
flag. A generator enabled outside this driver can otherwise appear off and
allow a relay move under load. Helpers return a parsed value, `None` or `?`
for an unavailable reply; a plausible default conceals missing evidence.

The service calls driver read-back methods through `getattr` and uses cached
properties for implementations without those methods. The experiment
protocols do not require these service queries."""
from __future__ import annotations

from typing import Any


def ask(io: Any, query: str) -> str | None:
    """Query the instrument and return stripped text, or `None` on failure.

    Failure must not mask a pending exception during cleanup. Read-back also
    needs to render when one instrument does not answer."""
    try:
        reply = io.query(query)
    except Exception:                                       # noqa: BLE001
        return None
    text = str(reply).strip()
    return text if text else None


def on_off(reply: str | None) -> bool | None:
    """Parse `1` or `ON` as `True`, `0` or `OFF` as `False`, else `None`."""
    if reply is None:
        return None
    up = reply.strip().upper().rstrip(";")
    if up in ("1", "ON", "+1"):
        return True
    if up in ("0", "OFF", "+0"):
        return False
    return None


def number(reply: str | None) -> float | None:
    """Parse a SCPI number as a float, or return `None`."""
    if reply is None:
        return None
    try:
        return float(reply.strip().rstrip(";"))
    except ValueError:
        return None


def word(reply: str | None, known: tuple[str, ...]) -> str:
    """Return the first matching prefix in `known`, or the reply itself.

    An unknown word remains useful read-back information. No reply gives `?`."""
    if reply is None:
        return "?"
    up = reply.strip().upper().rstrip(";")
    if not up:
        return "?"
    for token in known:
        if up.startswith(token):
            return token
    return up
