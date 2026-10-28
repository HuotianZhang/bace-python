"""Read the Newport 1918-C through the service's own USB session.

`DirectPowerMeter` wraps `meter.PowerMeter` in the experiment's `PowerMeter`
interface. The low-level driver serialises exchanges and the service
serialises monitor reads against runs. Exactly one process holds the USB
handle. A separate console must use the console adapter instead; opening a
second owner produces a device-not-found error that can resemble a cable fault.

`meter` handles bench firmware v2.1.7, SN10259: DLL bitness, command echo,
CR termination, `PM:DS:INT` in ms, store wrap and `PM:PWS?` status bits."""
from __future__ import annotations

from .console import WATTS, PowerMeterError, Reading

__all__ = ["DirectPowerMeter", "AVERAGING_DIGITAL_SAMPLES", "ANALOG_FILTER_5HZ",
           "MODE_DC_CONTINUOUS"]

MODE_DC_CONTINUOUS = 0
"""Select DC-continuous power with `PM:MODE 0`.

The other seven modes in `meter.MODE_NAMES` measure pulse energy,
peak-to-peak or RMS quantities rather than mean power of a 500 Hz LED."""

ANALOG_FILTER_5HZ = 4
"""Select the 5 Hz analog filter with `PM:ANALOGFILTER 4`.

It is the slowest filter and averages a 500 Hz square wave on this bench.
The 1 kHz filter, code 3, still follows the edges."""

AVERAGING_DIGITAL_SAMPLES = 100
"""Set light digital smoothing above the analog averaging filter.

The response stays fast enough for the service's 0.5 s LED-settle poll.
`[power_meter] digital_filter` in `rig.toml` overrides this count."""


class DirectPowerMeter:
    """Implement power measurement through this process's USB session.

    Construction opens and identifies the meter. Failure raises; `last`
    caches the most recent reading for bench read-back."""

    def __init__(self, *, dll: str | None = None, simulate: bool = False,
                 wavelength_nm: float | None = None):
        from .meter import MeterError, PowerMeter, python_bitness
        try:
            self._meter = PowerMeter.open(dll=dll, simulate=simulate)
        except MeterError as exc:
            raise PowerMeterError(
                f"{exc} (this process could not open the 1918-C. If the "
                f"meter's own console is running it holds the USB handle and "
                f"nothing else can -- close it; {python_bitness()}-bit Python "
                "needs the matching usbdll.dll, which the Newport driver "
                "package installs in both builds)") from exc
        except OSError as exc:
            raise PowerMeterError(
                f"loading the Newport USB driver failed ({exc}); install "
                "Newport Computer Interface Software v3.0.2, or set "
                "[power_meter] dll in rig.toml") from exc
        self.info: dict = dict(getattr(self._meter, "info", {}) or {})
        self.last: Reading | None = None
        self.averaged: bool | None = None
        """Record whether mode and filter read-back confirm averaging.

        `set_averaging` updates this state and each `Reading` carries it."""
        self.averaging: dict = {}
        """Hold mode and filter read-back for bench checks.

        Fields describe mode, filter selection, analog bandwidth and digital count."""
        self.wavelength_nm: float | None = None
        """Keep wavelength read-back in nm for each `Reading`.

        Power depends on responsivity at that wavelength.
        `bace.service.rigs.power_reading` uses this cached value when a
        reading does not carry its own."""
        # The exclusive USB handle is open. Any configuration failure must close
        # it because a failed constructor gives the caller no object to close.
        # Wavelength validation can raise outside the detector calibration range.
        try:
            if wavelength_nm is not None:
                self.set_wavelength(wavelength_nm)
            else:
                self._read_wavelength()
        except BaseException:
            self.close()
            raise

    @property
    def source(self) -> str:
        """Return `usb` or `simulated` for the reading's source.

        `bace.service.rigs.power_reading` uses this before considering a console
        URL. A real meter must not be labelled simulated, or a simulation
        presented as measured."""
        return "simulated" if self.info.get("simulated") else "usb"

    # -- liveness ---------------------------------------------------------
    def available(self) -> bool:
        """Request a reading and return whether the meter answers."""
        try:
            self.read()
        except Exception:                                   # noqa: BLE001
            return False
        return True

    # -- configuration ----------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        """Send the wavelength in nm and cache instrument read-back; return `None`.

        Responsivity depends on wavelength. Values outside detector calibration
        are refused to avoid extrapolation. The meter rounds to whole nm on
        this bench, so read-back determines the cached value."""
        from .meter import MeterError
        try:
            answered = self._meter.set_wavelength(float(nm))
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc
        got = (answered or {}).get("wavelength")
        self.wavelength_nm = float(got) if got is not None else float(nm)

    def _read_wavelength(self) -> None:
        """Query and cache the held wavelength in nm when no value is requested.

        A missing read-back does not make the meter unusable."""
        try:
            got = self._meter.settings().get("wavelength")
        except Exception:                                   # noqa: BLE001
            return          # a meter that will not say is not a reason to fail
        if got is not None:
            self.wavelength_nm = float(got)

    def set_units_watts(self) -> None:
        """Send `PM:UNITS` and return `None`; `read` verifies W from status.

        A meter in A or dBm gives plausible values that downstream analysis
        cannot recognise as the wrong unit."""
        from .meter import MeterError
        try:
            self._meter.set_units(WATTS)
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc

    def set_averaging(self, on: bool = True, *,
                      digital_samples: int = AVERAGING_DIGITAL_SAMPLES) -> dict:
        """Set DC-continuous mode and optional filtering; return confirmed filter state.

        On this bench a 500 Hz LED at 50 % duty reads full level, zero or a
        flicker when sampled without filtering, including in DC-continuous mode.
        Other modes measure pulse energy, peak-to-peak or RMS quantities.
        `PM:MODE 0` with the analog 5 Hz filter averages about a hundred cycles
        on this bench. At 50 % duty the mean is half the DC power
        (manual 1.2, `docs/manual/1.2-bench.md`).
        The mean supports intensity scans and LED settling.

        `on=False` selects DC-continuous without filtering for instantaneous
        readings. Mode is written in both cases to override front-panel pulse
        or RMS settings. Read-back confirms the mode and filter; `averaged` is
        true only for mode 0 with the 5 Hz analog filter selected."""
        from .meter import MeterError
        digital = max(0, int(digital_samples))
        try:
            self._meter.set_mode(MODE_DC_CONTINUOUS)
            if on:
                self._meter.set_analog_filter(ANALOG_FILTER_5HZ)
                self._meter.set_filter(3 if digital > 0 else 1, digital)
            else:
                self._meter.set_filter(0, 0)
                self._meter.set_analog_filter(0)
            s = self._meter.settings()
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc
        mode = s.get("mode")
        filt = s.get("filter")
        analog = s.get("analogFilter")
        self.averaging = {
            "mode": mode, "mode_name": s.get("modeName"),
            "filter": filt, "filter_name": s.get("filterName"),
            "analog_filter": analog, "analog_filter_name": s.get("analogFilterName"),
            "digital_filter": s.get("digitalFilter"),
        }
        self.averaged = (mode is not None and int(mode) == MODE_DC_CONTINUOUS
                         and filt is not None and int(filt) in (1, 3)
                         and analog is not None and int(analog) == ANALOG_FILTER_5HZ)
        if on and not self.averaged:
            raise PowerMeterError(
                f"the 1918-C did not take the averaging settings: it reports mode "
                f"{s.get('modeName')}, filter {s.get('filterName')}, analog filter "
                f"{s.get('analogFilterName')} -- a pulsed LED will not read as its "
                "average")
        return dict(self.averaging)

    # -- reading ----------------------------------------------------------
    def read(self) -> Reading:
        from .meter import MeterError
        try:
            d = self._meter.read()
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc
        st = d.get("status") or {}
        r = Reading(watts=float(d["value"]),
                    saturated=bool(st.get("saturated")),
                    overrange=bool(st.get("overrange")),
                    units=st.get("units"),
                    wavelength_nm=self.wavelength_nm,
                    averaged=self.averaged)
        self.last = r
        return r

    def read_power(self) -> float:
        return self.read().watts

    def read_statistics(self, n: int, *, interval_ms: int = 10) -> tuple[float, float]:
        """Capture `n` samples and return mean and sample standard deviation in W.

        `interval_ms` is in ms. Sampling uses the meter clock; USB polling
        cannot guarantee a rate. Reject a short capture rather than average
        data taken at an unconfirmed rate."""
        from .meter import MeterError
        n = max(2, int(n))
        try:
            d = self._meter.capture(n, int(interval_ms), fetch=False)
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc
        collected = int(d.get("collected", 0))
        if collected < n:
            raise PowerMeterError(
                f"capture collected {collected} of {n} samples "
                f"({d.get('measuredMsPerSample')} ms/sample measured) — the "
                "meter is not sampling at the requested rate")
        return float(d["mean"]), float(d["sdev"])

    def errors(self) -> str:
        try:
            return str(self._meter.errors())
        except Exception:                                   # noqa: BLE001
            return ""

    def close(self) -> None:
        try:
            self._meter.close()
        except Exception:                                   # noqa: BLE001
            pass
