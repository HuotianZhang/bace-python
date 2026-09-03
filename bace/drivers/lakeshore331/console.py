"""Lake Shore 331 temperature controller -- through the console that owns it.

**One owner.** The 331 answers only the last query it received and cannot
arbitrate between callers, so the console in `D:\\TemperatureController`
holds the only GPIB session to it (its README, "One owner"), exactly as the
1918-C console holds the USB meter. This module is therefore an HTTP client
to that console's localhost API (`127.0.0.1:8331`) and contains **no VISA
code**: a second session on `GPIB0::7` would interleave with the console's
own poller on the bus, and the two would read each other's replies.

What stays the console's, deliberately:

* the **350 K ceiling** (`ls331/config.py`): a setpoint above it is refused
  with HTTP 403 and the console's own message, never clamped -- quietly
  giving 350 K for 400 K would hide the mistake. This driver re-implements
  no limit and clamps nothing; it passes the refusal on as
  `TemperatureError` with the console's words;
* the **heater range, PID, ramp and loop wiring**: the console exposes no
  route the service may use for them (range and PID are locked behind its
  own confirm-and-unlock flags), so the ramp rate in force is whatever the
  console holds, and `ramping` (RAMPST?) is read here, never driven;
* its **audit log**: `POST /api/note` writes a line into it, the hook the
  console README reserves for the measurement program, so every temperature
  node the service runs is marked there beside the setpoint writes.

`/api/state` is one snapshot of the console's poller (`ls331/service.py`):
`control_temperature` is the reading on the input the loop controls,
`temperature_a` the A sensor, `connected` whether the instrument answered
the last poll, `max_setpoint_k` the ceiling -- and the last key is also how
the console recognises itself (`console_server._already_running`), so it is
what `available()` looks for.
"""
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
"""`POST /api/setpoint` is served on the console's bus-owner thread between
polls and waits up to 8 s for the job (`ls331/service.py`, `submit`), with a
3 s VISA timeout per query inside it. A client timeout shorter than that
turns a setpoint the console then applies into a reported failure, so the
write waits longer than the console does -- the same margin the 1918-C
client keeps."""

__all__ = ["DEFAULT_CONSOLE", "READ_TIMEOUT_S", "WRITE_TIMEOUT_S",
           "ConsoleTemperatureController", "TemperatureError", "TemperatureReading"]


class TemperatureError(RuntimeError):
    """The console refused a request, or could not be reached.

    `refused` is True when the console answered with an HTTP error -- its
    own message is `console_message` and the start of `str(exc)` -- and
    False when nothing answered at all; `status` is the HTTP code when there
    was one. The two are different facts to a run: a refusal is a setpoint
    to reconsider, an unreachable console is a program to start. A verdict
    about a refusal should show `console_message`, the sentence the console
    wrote, and keep the transport explanation in `str(exc)` for the log.
    """

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
    """The console's own message out of an error reply: the `error` key of
    its JSON body (`{"ok": false, "error": "..."}`), else the body text."""
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
    """`TemperatureController` backed by the 331 console's HTTP API.

    Every call is one request to the console, which serialises it onto its
    bus-owner thread, so nothing here collides with the console's polling or
    with the page someone has open beside it. `last` keeps the most recent
    reading for the bench read-back.
    """

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
            # HTTPError is a *subclass* of URLError, so it has to be caught
            # first (the 2026-09-01 fix in newport1918c): otherwise a 403 for
            # a setpoint above the ceiling reads as "cannot reach the
            # console", which is the one thing it proves is false.
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
            # A socket that timed out or closed while the reply was being
            # read (`r.read()` is outside urlopen's own wrapping): the same
            # fact as an unreachable console, and callers that promise a
            # bool or a reading must not see a raw socket error.
            raise TemperatureError(f"{unreachable} ({type(exc).__name__}: {exc})") from exc
        except json.JSONDecodeError as exc:
            raise TemperatureError(f"the 331 console returned non-JSON from {path}") from exc

    # -- liveness ---------------------------------------------------------
    def probe(self) -> dict | None:
        """`/api/state` as a dict when something answered with JSON, None
        when nothing did (or not with an object). What answered is the
        caller's question: `available` asks whether it is the console."""
        try:
            state = self._get("/api/state")
        except TemperatureError:
            return None
        return state if isinstance(state, dict) else None

    def available(self) -> bool:
        """True when the console answers `/api/state` with a state of its
        own: `max_setpoint_k` is the key the console itself checks to
        recognise a running instance on its port, so something else
        answering there is not mistaken for it. A console that is up but has
        not completed one poll of its instrument yet answers `{"connected":
        false}` without the key and is not "available" either -- `probe`
        lets the bench tell that case apart. Cheap enough for rig setup.
        """
        state = self.probe()
        return state is not None and "max_setpoint_k" in state

    # -- reading ----------------------------------------------------------
    def read(self) -> TemperatureReading:
        """The state as the console's poller last saw it.

        A state with `connected` False is still returned, with the flag
        False and whatever values the console still holds, so the caller can
        say "console up, instrument silent" rather than "no reading".
        `kelvin` is the control input's temperature, falling back to sensor A
        (a console that has not discovered its loop yet), NaN when neither
        is there yet.
        """
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
        """`POST /api/setpoint {"kelvin"}`; returns the setpoint the console
        read back from the instrument. A 403 (above the console's ceiling)
        arrives as `TemperatureError` carrying the console's own message;
        nothing is clamped or retried here -- the ceiling is the console's
        rule and a request it refused is a request to reconsider. The write
        waits `write_timeout_s`, longer than the console's own bus-job wait,
        so a slow bus is not reported as a failed write; a caller that still
        gets an unreachable error should read the state back before deciding
        the setpoint did not land."""
        d = self._post("/api/setpoint", {"kelvin": float(kelvin)},
                       timeout_s=self.write_timeout_s)
        confirmed = _float_or_none(d.get("setpoint")) if isinstance(d, dict) else None
        return float(kelvin) if confirmed is None else confirmed

    def note(self, text: str) -> bool:
        """`POST /api/note`: a line into the console's audit log, the hook
        its README keeps for the measurement program. A failure -- console
        gone, empty text refused -- is swallowed into False: a log mark must
        never stop a run."""
        try:
            self._post("/api/note", {"text": str(text)})
        except TemperatureError:
            return False
        return True
