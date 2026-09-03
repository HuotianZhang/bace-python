r"""Newport 1918-C optical power meter.

Two ways to reach the same instrument, and the choice is `[power_meter]` in
rig.toml:

* `direct.DirectPowerMeter` -- **the default.** This process opens the USB
  device through `meter.py`, the driver vendored from the
  `D:\1918cPowerMeter` console project (real 1918-C v2.1.7, SN10259).
* `console.ConsolePowerMeter` -- the escape hatch, used only when
  `[power_meter] console` names a URL. That console holds the USB handle
  when it runs, and then the service must ask it rather than fight for the
  device.

Only one process can hold the meter, so exactly one of the two may be live.
Opening the device while the console has it fails with a bare "no Newport
device found" that looks like a cable problem; `DirectPowerMeter` catches
that and says what it actually means.
"""
from __future__ import annotations

from typing import Any

from .console import (DEFAULT_CONSOLE, WATTS, ConsolePowerMeter,
                      PowerMeterError, Reading)
from .direct import DirectPowerMeter

__all__ = ["DEFAULT_CONSOLE", "WATTS", "ConsolePowerMeter", "DirectPowerMeter",
           "PowerMeterError", "Reading", "open_power_meter"]


def open_power_meter(rig_config: Any, *, simulate: bool = False):
    """The meter a rig should use, given `[power_meter]` in rig.toml.

    Direct by default. A non-empty `console` is the explicit escape hatch for
    the case the direct path exists to remove: somebody is running the 1918-C
    console and the service must go through it instead of failing to open a
    device that program already holds.
    """
    console = getattr(rig_config, "power_meter_console", "") or ""
    wavelength = getattr(rig_config, "power_meter_wavelength_nm", None)
    if console:
        meter = ConsolePowerMeter(console, timeout_s=5.0)
        if not meter.available():
            raise PowerMeterError(
                f"the 1918-C console is not answering at {console}. It is named "
                "in rig.toml, so the service will not open the meter itself -- "
                "start the console, or clear [power_meter] console to let the "
                "service own the device")
    else:
        meter = DirectPowerMeter(dll=getattr(rig_config, "power_meter_dll", "") or None,
                                 simulate=simulate)
    # Both paths, in this order: a meter in amps or dBm reads plausibly and
    # wrongly, and the wavelength decides the responsivity that turns the
    # detector current into those watts.
    #
    # Under try/finally because the direct meter already holds the exclusive
    # USB device by now. `set_wavelength` refuses a value outside the head's
    # calibrated range, and `Bench.build_real` registers its closer only on
    # the value this returns -- so a rejected rig.toml wavelength would have
    # left the meter held, with the service running and reporting it missing.
    try:
        meter.set_units_watts()
        if wavelength is not None:
            meter.set_wavelength(wavelength)
    except BaseException:
        close = getattr(meter, "close", None)
        if close is not None:
            try:
                close()
            except Exception:                               # noqa: BLE001
                pass    # the configuration failure is the one to report
        raise
    return meter
