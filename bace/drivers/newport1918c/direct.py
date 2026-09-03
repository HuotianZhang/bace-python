r"""The 1918-C opened by this process: the service holds the USB handle.

This is the default path. The alternative, `console.ConsolePowerMeter`, needs
the meter's console (`D:\1918cPowerMeter`) to be running, and on this rig
nobody starts it -- so the intensity a run recorded was whatever "no console"
degrades to, which is nothing. The driver moved in instead (`meter.py`,
vendored from that project, written against a real 1918-C v2.1.7 SN10259) and
this class wraps it in the `PowerMeter` protocol the experiment code expects.

**Only one process can hold the USB device.** That has not changed -- it moved
inside, like the 331's GPIB session. `meter.PowerMeter` serialises every
exchange on its own lock, and `service.rigs._POWER_LOCK` serialises the
monitor against a run. What still cannot happen is the console being started
beside the service: whichever opens first gets the handle and the other gets
a bare "no Newport device found" that reads like a cable problem. `open()`
below says so when it fails, because that sentence is the whole difference
between a five-minute and a two-hour diagnosis.

The measured facts about this instrument are in `meter.py`'s docstrings, not
repeated here -- the 32/64-bit DLL choice, the command echo, the `\r`
terminator, `PM:DS:INT` in milliseconds, `PM:DS:BUFFER 0`, and `PM:PWS?`
carrying the saturation bits that `PM:P?` does not.
"""
from __future__ import annotations

from .console import WATTS, PowerMeterError, Reading

__all__ = ["DirectPowerMeter"]


class DirectPowerMeter:
    """`PowerMeter` backed by this process's own USB session.

    Construction opens the device and identifies it, so a meter that exists
    is one that answered. `last` keeps the most recent reading for the bench
    read-back, the same way the console client does.
    """

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
        if wavelength_nm is not None:
            self.set_wavelength(wavelength_nm)

    @property
    def source(self) -> str:
        """Where a reading came from, for `PowerReading.source`: `usb` when
        this process holds the device, `simulated` when it is the driver's
        own stand-in.

        Never a URL -- that is the console client's answer, and
        `service.rigs.power_reading` asks for this first. Never `simulated`
        for a real meter either: a simulated number rendered as a measured
        one is the failure the field exists to prevent.
        """
        return "simulated" if self.info.get("simulated") else "usb"

    # -- liveness ---------------------------------------------------------
    def available(self) -> bool:
        """True when the meter answers a reading. The console client asks the
        console the same question; the bench uses one call for both."""
        try:
            self.read()
        except Exception:                                   # noqa: BLE001
            return False
        return True

    # -- configuration ----------------------------------------------------
    def set_wavelength(self, nm: float) -> None:
        """Responsivity is wavelength dependent, and the driver refuses a
        value outside the head's calibrated range rather than extrapolating
        into a confident wrong number."""
        from .meter import MeterError
        try:
            self._meter.set_wavelength(float(nm))
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc

    def set_units_watts(self) -> None:
        """A meter left in amps or dBm returns a perfectly plausible number
        that is wrong, and nothing downstream can tell. Set, then verified on
        the next `read`, whose status word carries the units back."""
        from .meter import MeterError
        try:
            self._meter.set_units(WATTS)
        except MeterError as exc:
            raise PowerMeterError(str(exc)) from exc

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
                    wavelength_nm=None)
        self.last = r
        return r

    def read_power(self) -> float:
        return self.read().watts

    def read_statistics(self, n: int, *, interval_ms: int = 10) -> tuple[float, float]:
        """Mean and standard deviation of `n` samples taken on the *meter's*
        clock, via its data store -- a polled loop over USB cannot give a
        deterministic rate. A short capture is refused rather than averaged:
        a partial capture is a rate the meter did not keep, and that must not
        survive into a published number."""
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
