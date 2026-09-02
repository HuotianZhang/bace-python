"""Run folder and file names, in the convention the archives already use.

The 2026-08-07 folder is

    s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_20260807_111521
    │  │         │   │    │         │        │          └─ started
    │  │         │   │    │         │        └─ offset correction was on
    │  │         │   │    │         └─ V_oc measured, in mV
    │  │         │   │    └─ LED drive at the 33220A, in mV
    │  │         │   └─ temperature
    │  │         └─ pixel
    │  └─ material
    └─ sample

Most of that was typed by hand in the original — including the temperature,
because temperature was set manually. Here every field is a named piece of
metadata that also gets stored inside the file, so the name stays a convenience
rather than the only record. `temperature_k` is already a field for the same
reason: when the temperature controller is integrated it fills itself in, and
nothing about the naming or the stored schema has to change.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


def _mv(volts: float) -> str:
    """0.906 -> '906'. Rounds, because the original wrote whole millivolts."""
    return f"{round(volts * 1000):d}"


@dataclass(frozen=True)
class RunMetadata:
    """Everything about a run that is not an instrument setting."""

    sample: str = ""
    material: str = ""
    pixel: str = ""
    temperature_k: float | None = None
    led_drive_v: float | None = None
    voc_v: float | None = None
    offset_corrected: bool = True
    operator: str = ""
    comment: str = ""
    started: datetime = field(default_factory=datetime.now)

    @property
    def stamp(self) -> str:
        """`YYYYMMDD_HHMMSS`, shared by the folder and every file in it."""
        return self.started.strftime("%Y%m%d_%H%M%S")

    def folder_name(self) -> str:
        parts = [p for p in (self.sample, self.material, self.pixel) if p]
        if self.temperature_k is not None:
            parts.append(f"{self.temperature_k:g}K")
        if self.led_drive_v is not None:
            parts.append(f"{_mv(self.led_drive_v)}mVLED")
        if self.voc_v is not None:
            parts.append(f"{_mv(self.voc_v)}mVVOC")
        if self.offset_corrected:
            parts.append("offsetcorr")
        if self.comment:
            parts.append(self.comment)
        parts.append(self.stamp)
        return "_".join(parts)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["started"] = self.started.isoformat()
        d["temperature_k"] = "" if self.temperature_k is None else self.temperature_k
        d["led_drive_v"] = "" if self.led_drive_v is None else self.led_drive_v
        d["voc_v"] = "" if self.voc_v is None else self.voc_v
        return d
