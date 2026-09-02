"""LED drive levels, and the invariant that couples them to the V_oc measurement.

The LED is driven by a 33220A through a fixed-gain amplifier. Two modes are
used within one intensity point, and the whole experiment depends on them
agreeing:

    V_oc measurement    FUNC:SHAP DC;   :VOLT:OFFS  <level>
    transient           FUNC:SHAP PULSE; :FREQ 1000; :VOLT:HIGH <level>
                                         :VOLT:LOW <sub-threshold>
                                         FUNC:PULS:DCYC 50

`level` must be the **same number** in both. The prebias for the transient is
centred on the V_oc measured under DC at that level, so if the pulse high level
ever drifts from the DC level, the sweep is centred on the wrong V_oc and every
extracted charge is biased — silently, with no symptom in the data.

There is no duty-cycle dimming to correct for: the low level sits below the
LED's turn-on threshold (~1 V at the generator), so the diode is fully off, not
dim. The on-phase intensity is therefore exactly the DC intensity, and the
on-phase (1 ms at the rig's measured 500 Hz, 50 % duty) is long enough for the
device to equilibrate at its light V_oc.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_FREQUENCY_HZ = 1000.0
DEFAULT_DUTY_PERCENT = 50.0
DEFAULT_THRESHOLD_V = 1.0
"""Approximate generator voltage at which the LED turns on. Rig-specific — it is
the LED's turn-on threshold referred back through the fixed amplifier gain, and
on this rig it sits around 1 V. Drive levels at the threshold are legitimate
(the working point in use is exactly 1 V); only levels clearly below it are
rejected."""


class IlluminationError(ValueError):
    pass


@dataclass(frozen=True)
class LedDrive:
    """One illumination point, in volts at the 33220A output.

    `level` is the drive that defines this point; it is used unchanged for both
    the DC V_oc measurement and the high level of the pulse.
    """

    level: float
    low_level: float = 0.4
    frequency_hz: float = DEFAULT_FREQUENCY_HZ
    duty_percent: float = DEFAULT_DUTY_PERCENT
    threshold_v: float = DEFAULT_THRESHOLD_V

    def __post_init__(self) -> None:
        if self.low_level >= self.threshold_v:
            raise IlluminationError(
                f"low level {self.low_level} V is not below the LED threshold "
                f"{self.threshold_v} V — the 'dark' half of the cycle would still emit, "
                "and carriers would keep generating during extraction"
            )
        if self.level < self.threshold_v:
            raise IlluminationError(
                f"drive level {self.level} V is below the LED threshold "
                f"{self.threshold_v} V — no illumination during the on-phase"
            )
        if self.level <= self.low_level:
            raise IlluminationError(
                f"drive level {self.level} V is not above the low level "
                f"{self.low_level} V — the LED would never switch"
            )
        if not 0.0 < self.duty_percent < 100.0:
            raise IlluminationError(f"duty cycle {self.duty_percent} % out of range")

    # -- the two generator states ----------------------------------------
    def dc_settings(self) -> dict[str, float | str]:
        """For the V_oc / J_sc / J_sat measurement: steady illumination."""
        return {"shape": "DC", "offset": self.level}

    def pulse_settings(self) -> dict[str, float | str]:
        """For the transient: on/off square at the same on-level."""
        return {
            "shape": "PULSE",
            "frequency": self.frequency_hz,
            "high": self.level,
            "low": self.low_level,
            "duty": self.duty_percent,
        }

    # -- derived timings --------------------------------------------------
    @property
    def period_s(self) -> float:
        return 1.0 / self.frequency_hz

    @property
    def on_time_s(self) -> float:
        return self.period_s * self.duty_percent / 100.0

    def check_equilibration(self, tau_s: float) -> None:
        """Raise if the on-phase is too short for the device to reach light V_oc.

        `tau_s` is the slowest relevant settling time of the device under
        illumination. The prebias is only meaningful once the device has
        equilibrated, so this is worth asserting rather than assuming.
        """
        if self.on_time_s < 5.0 * tau_s:
            raise IlluminationError(
                f"on-phase {self.on_time_s * 1e6:.0f} µs is under 5 τ for τ = "
                f"{tau_s * 1e6:.0f} µs — the device may not reach its light V_oc "
                "before extraction, so the prebias would be centred on a "
                "transient value"
            )


def assert_axis_centre(drive: LedDrive, voc_measured_at: float) -> None:
    """Guard the coupling: V_oc must have been measured at this drive level.

    Call this where the prebias axis is built. It is the one check that catches
    a stale V_oc — measured at a previous intensity, or at a DC level that no
    longer matches the pulse high level.
    """
    if abs(drive.level - voc_measured_at) > 1e-9:
        raise IlluminationError(
            f"V_oc was measured at {voc_measured_at} V drive but the transient "
            f"will run at {drive.level} V — the prebias axis would be centred on "
            "the wrong V_oc. These must be the same level."
        )
