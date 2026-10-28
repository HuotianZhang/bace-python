"""Read the Newport 1918-C through its owning console's HTTP API.

Only one process can hold the USB device. When the console holds it, direct
opening fails with a device-not-found error that can resemble a cable fault.
This adapter sends requests to the console's acquisition thread.

Bench behaviour on firmware v2.1.7, SN10259: the meter echoes commands before
replies and uses a CR terminator. `PM:DS:INT` uses ms, rather than the 0.1 ms
claimed by some drivers. `PM:DS:BUFFER 0` prevents store wrap. `PM:PWS?`
returns saturation and overrange status; `PM:UNITS` must be checked because
A or dBm can appear as plausible power values. Both 32-bit and 64-bit USB
DLLs are available; a matching build permits direct access without a bridge.
The low-level implementation is `bace.drivers.newport1918c.meter`."""
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
    """Hold optical power in W and the meter status reported with it."""

    watts: float
    saturated: bool
    overrange: bool
    units: int | None
    wavelength_nm: float | None
    averaged: bool | None = None
    """Report whether DC-continuous mode and the 5 Hz filter are enabled.

    Without the filter a pulsed LED reading samples a phase; with it, the
    reading is the mean, half the DC level at 50 % duty. `None` means the
    adapter cannot confirm filtering, including a console without `/api/filter`."""

    @property
    def trustworthy(self) -> bool:
        return not (self.saturated or self.overrange) and self.units == WATTS


class ConsolePowerMeter:
    """Implement `bace.drivers.protocols.PowerMeter` through the owning console.

    Requests run on its acquisition thread so polling and other scripts
    cannot interleave USB commands with replies."""

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
            # `HTTPError` subclasses `URLError` and must be caught first.
            # A rejected request proves the console answers; it is reachable.
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
        """Request `/api/state` and return whether the console answers."""
        try:
            self._get("/api/state")
            return True
        except PowerMeterError:
            return False

    # -- configuration ----------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        """Send `/api/wavelength` in nm and return `None`.

        The console rejects wavelengths outside the detector calibration range;
        extrapolated responsivity can give a plausible wrong power."""
        self._post("/api/wavelength", {"nm": float(nm)})

    def set_units_watts(self) -> None:
        # The console requires the `code` key. A `units` key leaves `code` absent
        # and the integer conversion produces HTTP 400 on this bench.
        self._post("/api/units", {"code": WATTS})

    def set_averaging(self, on: bool = True, *, digital_samples: int = 100) -> bool:
        """Request DC-continuous mode and a 5 Hz filter; return whether it succeeds.

        `digital_samples` sets digital smoothing. Filtering makes pulsed light
        read as mean power. This matches `direct.DirectPowerMeter.set_averaging`.
        The console's `/api/filter` route is not verified here. A console without
        it gives 404, reported as `False`; `averaged` stays `None` so readings
        do not claim a setting that cannot be confirmed."""
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
        """Request `/api/reading` and return power in W.

        Saturation and overrange remain in `last` for a visible run notice;
        one bad sample need not abort transients. `strict=True` raises instead."""
        return self.read().watts

    def read_statistics(self, n: int, *, interval_ms: int = 10) -> tuple[float, float]:
        """Request a store capture and return mean and sample standard deviation in W.

        `n` is the sample count. The meter clock sets the rate; USB polling
        cannot guarantee it. A short capture raises so it cannot silently
        become an average with the wrong count."""
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

