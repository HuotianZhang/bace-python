"""Run folders and files use the LabVIEW naming convention.

Folder fields identify sample, material, pixel, temperature in K, LED level
in mV, V_oc in mV, baseline correction, comment and start timestamp.
Metadata inside each file preserves the full values; the folder name is a
convenience rather than the sole record.

A `290K` name cannot distinguish typed temperature from a requested setpoint,
a settled controller reading or a value accepted at a pause. The stored
`temperature_how` and `temperature_source` distinguish these cases without
changing the naming convention. Except for `typed`, they carry the `how` and
`source` fields of `service.temperature.Settled` unchanged.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime


def _mv(volts: float) -> str:
    """Rounds V to whole mV for LabVIEW-compatible names, such as 0.906 to 906."""
    return f"{round(volts * 1000):d}"


_PATH_UNSAFE = '<>:"/|?*' + chr(92)
"""Characters prohibited in a Windows path segment.

`chr(92)` represents a backslash without a fragile escape sequence. Losing
that character from the filter lets metadata insert a path separator.
"""
_UNSAFE = re.compile("[\x00-\x1f" + re.escape(_PATH_UNSAFE) + "]+")
_TO_DASH = re.compile(r"[\s_]+")
"""Whitespace and `_` match here so metadata cannot forge field separators."""

NAME_MAX = 24
"""Each identity field contributes at most 24 characters to the folder name.

This exceeds the nine-character `PTQ10IT4F` identity and the sixteen-character
`s4_PTQ10IT4F_pxa` combination used to budget the rest of the name. The bound
keeps a pasted sentence from extending a Windows path beyond its limit.

`slug` maps multiple identities to one name. Two material names differing
only after character 24 can therefore collide. `RunMetadata.as_dict` keeps
each full value, and the console reads metadata rather than parsing names.
The console displays the reduced name while the operator edits it.

Directories are not guaranteed unique: matching metadata and a start time
within the same second can select one folder. The J–V writer opens HDF5 in
`w` mode, so a collision can overwrite data. A uniqueness guarantee requires
consistent handling across the BACE, J–V and series writers.
"""

COMMENT_MAX = 64
"""The folder name carries at most 64 characters of the comment.

The other fields take about 66 characters and the longest filename about
40. This budget allows a `runs/` directory a few levels deep to remain
within the Windows 260-character path limit.
"""


def slug(text: str, limit: int = COMMENT_MAX) -> str:
    """Reduces metadata text to one bounded Windows path segment.

    Whitespace and `_` become `-`; prohibited path characters are removed.
    This prevents spaces from splitting words inconsistently and underscores
    from forging field boundaries. `RunMetadata.as_dict` retains the full text.

    Non-ASCII characters remain valid path text, so comments in any language
    retain their meaning in the folder name.
    """
    # Whitespace replacement precedes character removal so a tab separates
    # words instead of joining them.
    out = _UNSAFE.sub("", _TO_DASH.sub("-", text))
    out = re.sub(r"-{2,}", "-", out).strip("-. ")
    if len(out) > limit:
        cut = out[:limit].rstrip("-. ")
        if len(cut) == limit and out[limit] != "-":
            # A whole-word boundary preserves readability if it retains at least
            # half the limit. A single long token still requires truncation.
            head, sep, _tail = cut.rpartition("-")
            if sep and len(head) >= limit // 2:
                cut = head
        out = cut
    return out


@dataclass(frozen=True)
class RunMetadata:
    """Run identity and context stored separately from instrument settings."""

    sample: str = ""
    material: str = ""
    pixel: str = ""
    temperature_k: float | None = None
    temperature_how: str = ""
    """The method that supplies `temperature_k`, in K.

    `typed` is recipe or console metadata without an instrument read-back.
    `setpoint` is a requested temperature without a reading during settling;
    it does not establish the reached temperature. `read` is one controller
    reading at node start without a requested setpoint. `settled` means the
    controller holds within the tolerance band. `operator` means a pause
    resumes: the value is typed if `temperature_source` is `operator`, or is
    the last controller reading polled during the pause.
    The field is empty when unavailable or when `temperature_k` is `None`.
    """
    temperature_source: str = ""
    """The source of the temperature value.

    `console` is the 331 through its console; `simulated` identifies a simulated
    reading; `operator` identifies a typed value. Empty means no read-back.
    """
    led_drive_v: float | None = None
    voc_v: float | None = None
    offset_corrected: bool = True
    operator: str = ""
    comment: str = ""
    started: datetime = field(default_factory=datetime.now)

    @property
    def stamp(self) -> str:
        """Returns `YYYYMMDD_HHMMSS`, shared by the folder and its files."""
        return self.started.strftime("%Y%m%d_%H%M%S")

    def identity_in_name(self) -> tuple[str, str, str]:
        """Returns the reduced sample, material and pixel fields for the folder.

        `slug` bounds each field to `NAME_MAX`, replaces whitespace and `_`,
        removes prohibited Windows path characters and keeps non-ASCII text.
        `RunMetadata.as_dict` retains all three full values, so a material such
        as `PTQ10:IT-4F` remains intact in metadata and legal in the folder name.

        The service exposes this result as `sample_in_name` in `GET /session`.
        The console can show the destination name during editing without a
        second implementation of the reduction rules.
        """
        return tuple(slug(p, NAME_MAX) for p in (self.sample, self.material, self.pixel))

    def folder_name(self) -> str:
        # Identity fields need reduction before joining. A colon in
        # `material = "PTQ10:IT-4F"` fails on Windows even if Linux accepts it;
        # `sample = "a/b"` would create two path segments.
        parts = [p for p in self.identity_in_name() if p]
        if self.temperature_k is not None:
            parts.append(f"{self.temperature_k:g}K")
        if self.led_drive_v is not None:
            parts.append(f"{_mv(self.led_drive_v)}mVLED")
        if self.voc_v is not None:
            parts.append(f"{_mv(self.voc_v)}mVVOC")
        if self.offset_corrected:
            parts.append("offsetcorr")
        if self.comment:
            # `as_dict` keeps the full comment. The reduced name is a convenience;
            # stored metadata is the record.
            reduced = slug(self.comment)
            if reduced:
                parts.append(reduced)
        parts.append(self.stamp)
        return "_".join(parts)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["started"] = self.started.isoformat()
        d["temperature_k"] = "" if self.temperature_k is None else self.temperature_k
        d["led_drive_v"] = "" if self.led_drive_v is None else self.led_drive_v
        d["voc_v"] = "" if self.voc_v is None else self.voc_v
        return d
