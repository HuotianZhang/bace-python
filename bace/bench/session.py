"""Record VISA exchanges and check the error queue after each command.

The bench checks LabVIEW SCPI sequences against the instruments. An immediate
error-queue query associates each rejection with the command that causes it.

`RecordingResource` exposes the resource's `write`, `query` and
`query_binary_values` interfaces. Drivers use it without modification, so the
transcript records the driver calls used for a run.

The error query is configurable because its spelling depends on the instrument.
It can be disabled during acquisition: an extra round trip per command changes
the timing under test.
"""
from __future__ import annotations

import time
from typing import Any

from .report import Exchange

def is_no_error(reply: str) -> bool:
    """Return whether an error-queue reply indicates no error.

    A Keithley or 33220A returns `0,"No error"`; the DSO9054H returns bare `0`
    with `:SYST:HEAD OFF` (docs/notes.md). The leading number therefore decides
    whether the queue is clear. A nonempty reply that cannot be parsed counts
    as an error so an unexpected response remains visible.
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
    """Wrap a PyVISA resource and record every exchange in a list."""

    def __init__(self, resource, exchanges: list[Exchange], *,
                 error_query: str | None = ":SYST:ERR?", label: str = "",
                 max_errors: int = 8):
        self._io = resource
        self.exchanges = exchanges
        self.error_query = error_query
        self.label = label
        self.max_errors = max_errors
        self.check_errors = error_query is not None

    # PyVISA attributes used by the drivers
    @property
    def timeout(self):
        return self._io.timeout

    @timeout.setter
    def timeout(self, value):
        self._io.timeout = value

    def __getattr__(self, name):          # other attributes pass through
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
    """Suspend per-command error queries within `with quiet(res):`.

    An extra round trip after each acquisition command changes the timing
    under test.
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
