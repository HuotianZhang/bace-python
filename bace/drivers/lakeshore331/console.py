"""Control the Lake Shore 331 through its owning console's HTTP API.

The 331 answers only the last query received. Exactly one process must hold
its GPIB session; a second poller can consume the other owner's replies.
This adapter uses `127.0.0.1:8331` and opens no VISA session.

The console enforces its 350 K ceiling and returns HTTP 403 on refusal.
The adapter passes the message through `TemperatureError` without clamping.
The console owns heater range, PID, ramp and wiring. This adapter reads
`ramping` from `RAMPST?` and writes only setpoints and journal notes.

`/api/state` returns a cached poll: control temperature, sensor A,
connection status and `max_setpoint_k`. `available` uses the ceiling key
to recognise the temperature console rather than another HTTP server."""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Any

from ..protocols import TemperatureReading

DEFAULT_CONSOLE = "http://127.0.0.1:8331"

READ_TIMEOUT_S = 3.0
"""`GET /api/state` answers from the console's cached poll without touching
the bus, so a slow answer is a console in trouble, not a slow instrument."""

WRITE_TIMEOUT_S = 10.0
"""Allow the console's bus-owner thread to finish a setpoint job.

On this bench the console waits up to 8 s with 3 s per VISA query. The client
waits longer so a setpoint applied after a slow exchange is not reported as
a failed write."""

__all__ = ["DEFAULT_CONSOLE", "READ_TIMEOUT_S", "WRITE_TIMEOUT_S",
           "ConsoleTemperatureController", "TemperatureError", "TemperatureReading"]


class TemperatureError(RuntimeError):
    """Report a refused request or an unreachable temperature console.

    `refused=True` means an HTTP error reply; `console_message` holds its
    message and `status` its code. Otherwise the console does not answer.
    A run can distinguish a setpoint to reconsider from a console to start.
    Display the refusal message and retain transport details in the log."""

    def __init__(self, message: str, *, refused: bool = False,
                 status: int | None = None, console_message: str | None = None):
        super().__init__(message)
        self.refused = refused
        self.status = status
        self.console_message = console_message


def _float_or_none(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _bool_or_none(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _error_text(exc: urllib.error.HTTPError) -> str:
    """Return the JSON error message, or body text if no error key exists."""
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:                                       # noqa: BLE001
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        return body
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return body


class ConsoleTemperatureController:
    """Implement temperature control through the owning console's HTTP API.

    The console serialises requests with its poller on the bus-owner thread.
    `last` caches the most recent reading for bench read-back."""

    def __init__(self, base_url: str = DEFAULT_CONSOLE, *, timeout_s: float = READ_TIMEOUT_S,
                 write_timeout_s: float = WRITE_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.write_timeout_s = write_timeout_s
        self.last: TemperatureReading | None = None

    # -- transport --------------------------------------------------------
    def _get(self, path: str) -> Any:
        return self._request("GET", path, None)

    def _post(self, path: str, body: dict, *, timeout_s: float | None = None) -> Any:
        return self._request("POST", path, body, timeout_s=timeout_s)

    def _request(self, method: str, path: str, body: dict | None, *,
                 timeout_s: float | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        unreachable = (f"cannot reach the 331 console at {self.base_url}. rig.toml names it, "
                       "so this program will not open the instrument itself -- two owners on "
                       "GPIB0::7 read each other's replies. Either start that console, or "
                       "clear [temperature] console and let the service own the bus "
                       "(the default).")
        try:
            with urllib.request.urlopen(
                    req, timeout=self.timeout_s if timeout_s is None else timeout_s) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            # `HTTPError` subclasses `URLError` and must be caught first. A ceiling
            # refusal proves the console is reachable; it must not become a transport
            # failure.
            detail = _error_text(exc) or f"HTTP {exc.code}"
            raise TemperatureError(
                detail
                + f" (the 331 console at {self.base_url} refused {method} {path} with "
                  f"HTTP {exc.code}; it is running -- this is the request it did not "
                  "accept, not a connection problem)",
                refused=True, status=exc.code, console_message=detail) from exc
        except urllib.error.URLError as exc:
            raise TemperatureError(unreachable) from exc
        except (OSError, http.client.HTTPException) as exc:
            # Socket timeout or closure during `r.read` lies outside `urlopen`'s
            # error wrapping. Treat it as unreachable so boolean and reading methods
            # do not leak raw socket errors.
            raise TemperatureError(f"{unreachable} ({type(exc).__name__}: {exc})") from exc
        except json.JSONDecodeError as exc:
            raise TemperatureError(f"the 331 console returned non-JSON from {path}") from exc

    # -- liveness ---------------------------------------------------------
    def probe(self) -> dict | None:
        """Request `/api/state` and return a JSON object or `None`.

        `available` separately checks whether the response identifies the 331
        console rather than another server."""
        try:
            state = self._get("/api/state")
        except TemperatureError:
            return None
        return state if isinstance(state, dict) else None

    def available(self) -> bool:
        """Return whether `/api/state` identifies the temperature console.

        `max_setpoint_k` distinguishes it from another server. Before its first
        instrument poll the console can return only `connected=False`; this is
        not available yet. `probe` exposes that response for a fuller diagnosis."""
        state = self.probe()
        return state is not None and "max_setpoint_k" in state

    # -- reading ----------------------------------------------------------
    def read(self) -> TemperatureReading:
        """Request `/api/state` and return temperature and setpoint in K with status.

        Return retained values with `connected=False` when the console answers
        but the instrument does not. Temperature uses the control input, then
        sensor A if loop discovery is incomplete, then NaN if neither exists."""
        d = self._get("/api/state")
        if not isinstance(d, dict):
            raise TemperatureError(f"the 331 console answered /api/state with {type(d).__name__}, "
                                   "not a state")
        kelvin = _float_or_none(d.get("control_temperature"))
        if kelvin is None:
            kelvin = _float_or_none(d.get("temperature_a"))
        connected = bool(d.get("connected", False))
        status = d.get("status_text")
        if not status and not connected:
            status = d.get("last_error")
        r = TemperatureReading(
            kelvin=float("nan") if kelvin is None else kelvin,
            setpoint_k=_float_or_none(d.get("setpoint")),
            ramping=_bool_or_none(d.get("ramping")),
            heater_range=_int_or_none(d.get("heater_range")),
            connected=connected,
            status_text=str(status or ""),
            elapsed_s=_float_or_none(d.get("elapsed_s")),
            max_setpoint_k=_float_or_none(d.get("max_setpoint_k")))
        self.last = r
        return r

    # -- writing ----------------------------------------------------------
    def set_setpoint(self, kelvin: float) -> float:
        """Post the setpoint in K and return instrument read-back in K.

        A ceiling refusal arrives as HTTP 403 and raises `TemperatureError`
        with the console's message. No clamping or retry occurs. The client
        wait exceeds the console's bus-job wait. If it still times out, read
        state before assuming the write fails to reach the instrument."""
        d = self._post("/api/setpoint", {"kelvin": float(kelvin)},
                       timeout_s=self.write_timeout_s)
        confirmed = _float_or_none(d.get("setpoint")) if isinstance(d, dict) else None
        return float(kelvin) if confirmed is None else confirmed

    def note(self, text: str) -> bool:
        """Post `/api/note` and return whether the journal entry succeeds.

        An unavailable console or refused empty note returns `False` rather
        than interrupting the run."""
        try:
            self._post("/api/note", {"text": str(text)})
        except TemperatureError:
            return False
        return True
