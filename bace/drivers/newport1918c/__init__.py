"""Provide direct and console-backed Newport 1918-C power-meter drivers.

`[power_meter]` in `rig.toml` selects the owner. `direct.DirectPowerMeter`
opens USB in the service by default. `console.ConsolePowerMeter` uses a
configured console URL when that program holds the handle.

Only one process can hold the meter. A second open gives a device-not-found
error that can resemble a cable fault; the direct adapter explains the
possible competing owner."""
from __future__ import annotations

import warnings
from typing import Any

from .console import (DEFAULT_CONSOLE, WATTS, ConsolePowerMeter,
                      PowerMeterError, Reading)
from .direct import DirectPowerMeter

__all__ = ["DEFAULT_CONSOLE", "WATTS", "ConsolePowerMeter", "DirectPowerMeter",
           "PowerMeterError", "Reading", "open_power_meter"]


def open_power_meter(rig_config: Any, *, simulate: bool = False):
    """Open the power-meter backend selected by `[power_meter]` in `rig.toml`.

    Direct USB is the default. A nonempty `console` uses HTTP to the program
    that holds the USB handle, avoiding a competing open."""
    console = getattr(rig_config, "power_meter_console", "") or ""
    wavelength = getattr(rig_config, "power_meter_wavelength_nm", None)
    averaging = bool(getattr(rig_config, "power_meter_averaging", True))
    digital = int(getattr(rig_config, "power_meter_digital_filter", 100))
    held = getattr(rig_config, "power_meter_range", None)
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
    # Units must be W; A or dBm can look plausible but are wrong for power.
    # Wavelength selects the responsivity used to convert detector current.
    #
    # Configuration runs under cleanup because direct opening owns the
    # exclusive handle. A rejected wavelength must release it; the bench
    # registers a closer only after this factory returns successfully.
    try:
        meter.set_units_watts()
        if wavelength is not None:
            meter.set_wavelength(wavelength)
        # A 500 Hz pulsed LED requires averaging to read mean power rather than
        # a sampled phase. `[power_meter] averaging = false` selects instantaneous
        # readings; both paths explicitly set the measurement mode.
        took = meter.set_averaging(averaging, digital_samples=digital)
        if console and took is False:
            warnings.warn(
                f"the 1918-C console at {console} has no /api/filter route, so the "
                "meter's averaging could not be set from here. Under a pulsed LED "
                "its readings are one instant of the square wave; set DC-continuous "
                "mode and the 5 Hz analog filter in that console, or clear "
                "[power_meter] console so the service owns the meter and sets them.",
                stacklevel=2)
        # Written either way: the meter keeps the last owner's range across a restart.
        meter.set_range(held)
    except BaseException:
        close = getattr(meter, "close", None)
        if close is not None:
            try:
                close()
            except Exception:                               # noqa: BLE001
                pass    # the configuration failure is the one to report
        raise
    return meter
