"""Newport 1918-C optical power meter — through the console that owns it.

**Only one process can hold the USB device.** The 1918-C console in
`D:\\1918cPowerMeter` is that process on this rig, so the default backend here
is an HTTP client to its localhost API rather than a second driver fighting for
the handle. Opening the meter directly while the console is running fails, and
it fails in the confusing way — a bare "no Newport device found" that looks like
a cable problem.

This module deliberately contains **no transport code**. The measured facts
about this instrument live in `newport1918c.py` in that project, written against
a real 1918-C v2.1.7 (SN10259), and several of them are the kind that only
appear on hardware:

* the driver package installs both 32- and 64-bit `usbdll.dll`, and the build
  matching the interpreter has to be picked deliberately — so, contrary to the
  earlier plan here, **the meter does not need the 32-bit bridge**; only
  `delib.dll` does;
* the meter **echoes the command back** before its reply, so a naive query
  returns the command;
* the line terminator is `\\r`, not `\\r\\n`;
* `PM:DS:INT` is in **milliseconds** on this firmware, not the 0.1 ms a widely
  copied driver claims;
* `PM:DS:BUFFER 0` is needed or the store wraps and you get a rolling window
  instead of one capture;
* `PM:PWS?` carries a status bitfield with saturation and overrange — and
  silently clipped data is the classic way to ruin a long log, so it is what
  gets read, not `PM:P?`;
* `PM:UNITS` must be checked: a meter left in amps or dBm returns a perfectly
  plausible wrong number.

Duplicating any of that here would create a second copy to drift. Use the
console.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_CONSOLE = "http://127.0.0.1:8918"
WATTS = 2          # PM:UNITS code


class PowerMeterError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reading:
    """One reading and what the meter said about it."""

    watts: float
    saturated: bool
    overrange: bool
    units: int | None
    wavelength_nm: float | None
    averaged: bool | None = None
    """Whether the meter was averaging when this was read -- DC-continuous
    mode with its 5 Hz analog filter on (`set_averaging`). A pulsed LED read
    without it is one instant of a square wave; with it, the time average,
    which at 50 % duty is half the DC level. None when the driver could not
    say (the console path, until that console answers `/api/filter`)."""

    @property
    def trustworthy(self) -> bool:
        return not (self.saturated or self.overrange) and self.units == WATTS


class ConsolePowerMeter:
    """`PowerMeter` backed by the 1918-C console's HTTP API.

    Satisfies `drivers.protocols.PowerMeter`. Every call goes to the console's
    acquisition thread, so it never collides with the console's own polling or
    with a second measurement script.
    """

    def __init__(self, base_url: str = DEFAULT_CONSOLE, *, timeout_s: float = 10.0,
                 strict: bool = False):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.strict = strict
        self.last: Reading | None = None
        self.averaged: bool | None = None
        """What `set_averaging` last managed to set, or None when this console
        has never been asked -- or has no route for it."""

    # -- transport --------------------------------------------------------
    def _get(self, path: str) -> dict:
        return self._request("GET", path, None)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body)

    def _request(self, method: str, path: str, body: dict | None) -> dict:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            # HTTPError is a *subclass* of URLError, so it has to be caught
            # first. Until 2026-09-01 it was not, and every rejected request --
            # a wrong body key, a value the meter refused -- was reported as
            # "cannot reach the console", which is the one thing it proves is
            # false. The console answered; it said no.
            try:
                detail = exc.read().decode("utf-8", "replace").strip()
            except Exception:                                   # noqa: BLE001
                detail = ""
            raise PowerMeterError(
                f"the 1918-C console at {self.base_url} refused {method} {path} "
                f"with HTTP {exc.code}"
                + (f": {detail}" if detail else "")
                + ". The console is running -- this is the request it did not "
                  "accept, not a connection problem."
            ) from exc
        except urllib.error.URLError as exc:
            raise PowerMeterError(
                f"cannot reach the 1918-C console at {self.base_url}. rig.toml names "
                "it, so this program will not open the meter itself — only one "
                "process can hold the USB device. Either start that console, or "
                "clear [power_meter] console and let the service own the meter "
                "(the default)."
            ) from exc
        except json.JSONDecodeError as exc:
            raise PowerMeterError(f"console returned non-JSON from {path}") from exc

    def available(self) -> bool:
        """True if the console answers. Cheap enough to call at rig setup."""
        try:
            self._get("/api/state")
            return True
        except PowerMeterError:
            return False

    # -- configuration ----------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        """The console refuses wavelengths outside the head's calibrated range,
        because outside it the responsivity is an extrapolation and the meter
        returns a confident, wrong number."""
        self._post("/api/wavelength", {"nm": float(nm)})

    def set_units_watts(self) -> None:
        # The console's key is "code", not "units" -- it does
        # `int(body.get("code"))`, so {"units": 2} arrived as int(None) and came
        # back as HTTP 400. Found on the rig 2026-09-01; nothing had ever called
        # this against the real console before.
        self._post("/api/units", {"code": WATTS})

    def set_averaging(self, on: bool = True, *, digital_samples: int = 100) -> bool:
        """Ask the console to put the meter in DC-continuous mode with the
        analog 5 Hz filter (and `digital_samples` of digital filter) so a
        pulsed LED reads as its time average -- the same setting
        `DirectPowerMeter.set_averaging` writes itself.

        The console's `/api/filter` route is assumed, not verified against
        that project (which this repository cannot see): a console that has
        no such route answers 404, and that is reported as *False*, never
        raised -- an escape hatch that cannot average is still a meter. The
        caller says so once; `averaged` stays None so no reading claims what
        was not set.
        """
        body = {"filter": 3 if on else 0, "analogFilter": 4 if on else 0,
                "digitalFilter": int(digital_samples) if on else 0, "mode": 0}
        try:
            self._post("/api/filter", body)
        except PowerMeterError as exc:
            if "HTTP 404" in str(exc) or "HTTP 405" in str(exc):
                self.averaged = None
                return False
            raise
        self.averaged = bool(on)
        return True

    # -- reading ----------------------------------------------------------
    def read(self) -> Reading:
        d = self._get("/api/reading")
        st = d.get("status") or {}
        r = Reading(watts=float(d["value"]),
                    saturated=bool(st.get("saturated")),
                    overrange=bool(st.get("overrange")),
                    units=int(d["units"]) if d.get("units") is not None else None,
                    wavelength_nm=d.get("wavelength"),
                    averaged=self.averaged)
        self.last = r
        if self.strict and not r.trustworthy:
            raise PowerMeterError(_why(r))
        return r

    def read_power(self) -> float:
        """W. Satisfies the protocol.

        A saturated or overrange reading is still returned rather than raising,
        because one bad intensity sample should not abort an hour of transients
        — but `last` carries the status and the experiment layer turns it into a
        visible notice. Construct with `strict=True` to raise instead.
        """
        return self.read().watts

    def read_statistics(self, n: int, *, interval_ms: int = 10) -> tuple[float, float]:
        """(mean, sample std) over `n` samples, in W.

        Sampling runs on the meter's own clock via its data store, which is the
        point: a polled loop cannot give a deterministic rate over USB. The
        console returns how many samples it actually collected, and a short
        capture raises — a silently short average is exactly the error that
        survives into a published number.
        """
        n = max(2, int(n))
        d = self._post("/api/capture", {"size": n, "intervalMs": int(interval_ms)})
        collected = int(d.get("collected", 0))
        if collected < n:
            raise PowerMeterError(
                f"capture collected {collected} of {n} samples "
                f"({d.get('measuredMsPerSample')} ms/sample measured) — the meter "
                "is not sampling at the requested rate"
            )
        return float(d["mean"]), float(d["sdev"])

    def errors(self) -> str:
        return str(self._get("/api/errors").get("errstr", ""))


def _why(r: Reading) -> str:
    if r.saturated:
        return "detector saturated — the reading is clipped, not low"
    if r.overrange:
        return "reading is overrange"
    return f"meter is in units code {r.units}, not watts ({WATTS})"

