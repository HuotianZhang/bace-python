"""A VISA session that records everything and checks the error queue after
every command.

This is the single most useful thing a bench session can produce. Every SCPI
string in this port was recovered from a binary and has never been sent to an
instrument; sending each one and immediately asking `:SYST:ERR?` turns "does my
SCPI work" from a guess into a table of exactly which commands the instrument
rejected and what it said about them.

`RecordingResource` wraps a pyvisa resource and presents the same `write` /
`query` / `query_binary_values` interface, so the real drivers can be pointed at
it **unmodified**. Nothing in `bace.drivers` knows it is being watched, which
means the transcript is of the code that will actually run, not of a test
double.

The error query itself is configurable because not every instrument spells it
the same way, and it can be turned off for the inner loop of an acquisition
where an extra round trip per command would distort the timing.
"""
from __future__ import annotations

import time
from typing import Any

from .report import Exchange

def is_no_error(reply: str) -> bool:
    """Is this error-queue reply the instrument saying 'nothing wrong'?

    Instruments disagree on the spelling. A Keithley or a 33220A answers
    `0,"No error"`; the DSO9054H, with `:SYST:HEAD OFF`, answers a bare `0`.
    Matching on the prefix `"0,"` therefore counted every scope reply as an
    error and filled the first bench report with 28 phantom rejections.

    So: parse the leading integer and compare it to zero. Anything unparsable is
    treated as an error, because an unexpected reply is worth seeing.
    """
    text = str(reply).strip().strip('"')
    if not text:
        return True
    head = text.split(",")[0].strip()
    try:
        return int(float(head)) == 0
    except ValueError:
        return False


class RecordingResource:
    """Wraps a pyvisa resource; records every exchange into a list."""

    def __init__(self, resource, exchanges: list[Exchange], *,
                 error_query: str | None = ":SYST:ERR?", label: str = "",
                 max_errors: int = 8):
        self._io = resource
        self.exchanges = exchanges
        self.error_query = error_query
        self.label = label
        self.max_errors = max_errors
        self.check_errors = error_query is not None

    # pyvisa attributes the drivers set or read
    @property
    def timeout(self):
        return self._io.timeout

    @timeout.setter
    def timeout(self, value):
        self._io.timeout = value

    def __getattr__(self, name):          # anything else goes straight through
        return getattr(self._io, name)

    # -- the recorded operations ------------------------------------------
    def _drain_errors(self) -> list[str]:
        if not self.check_errors or not self.error_query:
            return []
        out: list[str] = []
        try:
            for _ in range(self.max_errors):
                reply = str(self._io.query(self.error_query)).strip()
                if is_no_error(reply):
                    break
                out.append(reply)
        except Exception as exc:          # an instrument with no error queue
            self.check_errors = False
            out.append(f"<error query failed, disabled: {exc}>")
        return out

    def write(self, command: str) -> Any:
        ex = Exchange(command=command)
        t0 = time.perf_counter()
        try:
            result = self._io.write(command)
        except Exception as exc:
            ex.exception = f"{type(exc).__name__}: {exc}"
            ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
            self.exchanges.append(ex)
            raise
        ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
        ex.errors = self._drain_errors()
        self.exchanges.append(ex)
        return result

    def query(self, command: str) -> str:
        ex = Exchange(command=command)
        t0 = time.perf_counter()
        try:
            reply = self._io.query(command)
        except Exception as exc:
            ex.exception = f"{type(exc).__name__}: {exc}"
            ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
            self.exchanges.append(ex)
            raise
        ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
        ex.reply = str(reply).strip()
        # Never ask the error queue about the error query itself.
        if command.strip() != (self.error_query or "").strip():
            ex.errors = self._drain_errors()
        self.exchanges.append(ex)
        return reply

    def query_binary_values(self, command: str, **kwargs):
        ex = Exchange(command=command + "  <binary>")
        t0 = time.perf_counter()
        try:
            data = self._io.query_binary_values(command, **kwargs)
        except Exception as exc:
            ex.exception = f"{type(exc).__name__}: {exc}"
            ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
            self.exchanges.append(ex)
            raise
        ex.ms = round((time.perf_counter() - t0) * 1e3, 2)
        try:
            ex.reply = f"<{len(data)} values, first={data[0]}, last={data[-1]}>"
        except Exception:
            ex.reply = "<binary>"
        ex.errors = self._drain_errors()
        self.exchanges.append(ex)
        return data

    def close(self):
        try:
            return self._io.close()
        except Exception:
            return None


class quiet:
    """`with quiet(res):` — suspend the per-command error query.

    For the inner loop of an acquisition, where an extra round trip after every
    write would change the timing being measured.
    """

    def __init__(self, resource: RecordingResource):
        self.resource = resource

    def __enter__(self):
        self._was = self.resource.check_errors
        self.resource.check_errors = False
        return self.resource

    def __exit__(self, *exc):
        self.resource.check_errors = self._was
        return False
