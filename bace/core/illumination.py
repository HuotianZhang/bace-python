"""LED drive levels keep the V_oc measurement and transient illumination equal.

The 33220A drives the LED through an amplifier with constant gain. At one LED
level, DC mode uses `:VOLT:OFFS` and pulse mode uses `:VOLT:HIGH` with the
same value. Otherwise V_pre centres on V_oc at the wrong illumination and
biases Q without an obvious symptom in the data.

The pulse low level stays below the LED threshold, about 1 V at the generator
as measured on this bench. The LED is off during that phase, so the pulse high
phase has the DC intensity without duty-cycle dimming. At the measured bench
frequency of 500 Hz and 50 % duty, the 1 ms high phase allows equilibration
at V_oc (manual 3.2, `docs/manual/3.2-correspondence.md` for pulse timing).
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_FREQUENCY_HZ = 1000.0
DEFAULT_DUTY_PERCENT = 50.0
DEFAULT_THRESHOLD_V = 1.0
"""The approximate LED turn-on voltage at the generator, in V.

The LED threshold referred through the amplifier gain is about 1 V,
measured on this bench. Levels at the threshold remain valid, including
the 1 V working level; only lower levels are rejected.
"""


class IlluminationError(ValueError):
    pass


@dataclass(frozen=True)
class LedDrive:
    """An LED level in V at the 33220A output, with pulse timing and threshold.

    `level` supplies both the DC V_oc measurement and pulse high level.
    `low_level` and `threshold_v` are in V, `frequency_hz` in Hz and
    `duty_percent` in %. The low level must be below the threshold and high
    level. The high level must reach the threshold.
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
        if self.frequency_hz <= 0.0:
            # The 33220A requires positive frequency; `period_s` also divides by it.
            # Validation here covers the `light` module, `set-led-pulse` bench
            # action and validator. An ignored `:FREQ 0` would leave the preceding
            # timing active while the console reports the requested pulse.
            raise IlluminationError(
                f"pulse frequency {self.frequency_hz} Hz must be above zero")

    # -- the two generator states ----------------------------------------
    def dc_settings(self) -> dict[str, float | str]:
        """Returns steady illumination settings for V_oc and DC current measurements."""
        return {"shape": "DC", "offset": self.level}

    def pulse_settings(self) -> dict[str, float | str]:
        """Returns pulse settings with the same high level as the DC illumination."""
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
        """Rejects a high phase shorter than five device settling times.

        `tau_s` is the slowest relevant device settling time under illumination,
        in s. V_pre represents steady state only after the device equilibrates.
        """
        if self.on_time_s < 5.0 * tau_s:
            raise IlluminationError(
                f"on-phase {self.on_time_s * 1e6:.0f} µs is under 5 τ for τ = "
                f"{tau_s * 1e6:.0f} µs — the device may not reach its light V_oc "
                "before extraction, so the prebias would be centred on a "
                "transient value"
            )


def assert_axis_centre(drive: LedDrive, voc_measured_at: float) -> None:
    """Checks that V_oc uses the transient's LED drive level.

    `voc_measured_at` is the generator level in V during the V_oc measurement.
    The axis must use V_oc at that illumination; a different DC or pulse high
    level centres V_pre on the wrong value.
    """
    if abs(drive.level - voc_measured_at) > 1e-9:
        raise IlluminationError(
            f"Voc was measured at {voc_measured_at} V drive but the transient "
            f"will run at {drive.level} V — the prebias axis would be centred on "
            "the wrong Voc. These must be the same level."
        )
