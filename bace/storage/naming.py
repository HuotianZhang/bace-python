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

The controller since arrived, and with it the question the name cannot answer:
a folder called `290K` looks the same whether someone typed 290 into
`[sample]`, a loop asked for it and nothing ever read back, the 331 console
settled there, or an operator typed it at a pause. `temperature_how` and
`temperature_source` are that answer, stored beside the number and never in
the name — the name keeps the 2026-08-07 convention exactly. They are the
`how` and `source` of `service.temperature.Settled`, carried through
unchanged, which is where every value but `typed` comes from.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime


def _mv(volts: float) -> str:
    """0.906 -> '906'. Rounds, because the original wrote whole millivolts."""
    return f"{round(volts * 1000):d}"


_PATH_UNSAFE = '<>:"/|?*' + chr(92)
"""What a path segment may not hold on Windows. `chr(92)` is the backslash,
spelled that way so neither this string nor the class built from it needs an
escape that a later edit could quietly drop -- one surviving backslash in a
comment would put a path separator inside a folder name."""
_UNSAFE = re.compile("[\x00-\x1f" + re.escape(_PATH_UNSAFE) + "]+")
_TO_DASH = re.compile(r"[\s_]+")
"""Whitespace, and `_` -- the field separator, which a comment must not forge."""

COMMENT_MAX = 64
"""Characters of comment the folder name will carry. The rest of a name runs
to about 66 (`s4_PTQ10IT4F_pxa_290K_1020mVLED_906mVVOC_offsetcorr_<stamp>`)
and the longest file inside to about 40, which leaves this much before a run
under a `runs/` directory a few levels deep approaches Windows' 260-character
path limit."""


def slug(text: str, limit: int = COMMENT_MAX) -> str:
    """A comment reduced to one path segment.

    The original typed comments straight into the folder name and the
    2026-09-01 runs show the bill: `290K_1000mVLED_offsetcorr_LabVIEW panel
    replica - combination 4 - shutter only dark_20260902_012223` puts spaces
    into a directory name, and `_` in a comment would forge a field boundary
    in a name whose fields are separated by `_`. Only the *name* is reduced --
    `as_dict` keeps the comment verbatim, so the file still has the sentence
    the operator wrote, the same split as `temperature_how`.

    Anything that is merely not ASCII is kept: a Chinese comment is a
    perfectly good directory name and dropping it would lose the operator's
    note from the one place they look first.
    """
    # Whitespace first, then the rest: a tab between two words is a word
    # break like a space, and removing it before it becomes a dash would
    # glue the words together.
    out = _UNSAFE.sub("", _TO_DASH.sub("-", text))
    out = re.sub(r"-{2,}", "-", out).strip("-. ")
    if len(out) > limit:
        cut = out[:limit].rstrip("-. ")
        if len(cut) == limit and out[limit] != "-":
            # The cut landed inside a word: back off to the last whole one,
            # but not at the cost of half the comment -- a single very long
            # token is truncated as it stands.
            head, sep, _tail = cut.rpartition("-")
            if sep and len(head) >= limit // 2:
                cut = head
        out = cut
    return out


@dataclass(frozen=True)
class RunMetadata:
    """Everything about a run that is not an instrument setting."""

    sample: str = ""
    material: str = ""
    pixel: str = ""
    temperature_k: float | None = None
    temperature_how: str = ""
    """Where `temperature_k` came from. `typed` -- `[sample]` in the recipe or
    the console's metadata field, nobody read an instrument; `setpoint` -- a
    temperature loop asked for it and the settle never got a reading, so this
    is what was requested, not what was reached; `settled` -- the controller
    held it inside the band; `operator` -- a person resumed a pause (they
    typed the number when `temperature_source` is `operator`, otherwise it is
    the last reading polled while they were deciding). Empty in files written
    before this field existed, and when `temperature_k` is None."""
    temperature_source: str = ""
    """Which instrument produced it: `console` (the 331 through its console),
    `simulated` (the stand-in, so a `--sim` file says so), `operator` (typed),
    or empty when nothing was read."""
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
            # The name gets a slug, `as_dict` keeps the sentence: the same
            # split as the temperature above -- the name is a convenience,
            # the file is the record.
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
