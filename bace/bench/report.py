"""Accumulating a bench report.

The report is the deliverable of a bench session, so it is built to be read by
someone who was not in the room. Three rules shape it:

* **Record the traffic, not a summary of it.** Every command sent, the reply,
  how long it took, and what the instrument's error queue said immediately
  afterwards. A failing SCPI string is then a line in a table rather than a
  guess.
* **Never let one failure end the session.** A check that raises records the
  traceback and the session continues. Half a report from a rig with one broken
  instrument is worth far more than no report.
* **Say what was not done.** Skipped stages are listed with their reason, so an
  absent result is never mistaken for a passing one.
"""
from __future__ import annotations

import json
import platform
import struct
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

SCHEMA = "bace-bench/1"

OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"
WARNED = "warned"


@dataclass
class Exchange:
    """One command and what came back."""

    command: str
    reply: str | None = None
    ms: float = 0.0
    errors: list[str] = field(default_factory=list)
    exception: str | None = None

    @property
    def clean(self) -> bool:
        return not self.errors and self.exception is None


@dataclass
class Check:
    """One named thing that was tried."""

    name: str
    stage: str
    status: str = OK
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    exchanges: list[Exchange] = field(default_factory=list)
    traceback: str | None = None
    seconds: float = 0.0

    def fail(self, detail: str, exc: BaseException | None = None) -> "Check":
        self.status = FAILED
        self.detail = detail
        if exc is not None:
            self.traceback = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))
        return self

    def warn(self, detail: str) -> "Check":
        if self.status == OK:
            self.status = WARNED
        self.detail = (self.detail + "; " if self.detail else "") + detail
        return self

    def skip(self, reason: str) -> "Check":
        # A warn recorded before the skip is the cause, not noise: `_wrap`
        # says why an instrument could not be opened, and the stage then
        # skipped with a bare "not reachable" that hid it (2026-09-04).
        self.status = SKIPPED
        self.detail = f"{reason} ({self.detail})" if self.detail else reason
        return self


def environment() -> dict:
    """What the report needs to be interpretable somewhere else."""
    mods = {}
    for name in ("numpy", "scipy", "h5py", "pyvisa", "pyvisa_py", "serial"):
        try:
            mods[name] = __import__(name).__version__
        except Exception:
            mods[name] = None
    return {
        "python": sys.version.split()[0],
        "bitness": struct.calcsize("P") * 8,
        "executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "node": platform.node(),
        "modules": mods,
    }


@dataclass
class Report:
    title: str = "BACE bench check"
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    env: dict = field(default_factory=environment)
    stages_requested: list[str] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- building ---------------------------------------------------------
    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    def note(self, text: str) -> None:
        self.notes.append(text)

    def run(self, name: str, stage: str, fn, *args, **kwargs) -> Check:
        """Run one check, catching everything."""
        check = Check(name=name, stage=stage)
        t0 = time.monotonic()
        try:
            fn(check, *args, **kwargs)
        except Exception as exc:                      # noqa: BLE001 - the point
            check.fail(f"{type(exc).__name__}: {exc}", exc)
        finally:
            check.seconds = round(time.monotonic() - t0, 3)
        return self.add(check)

    # -- reading ----------------------------------------------------------
    @property
    def counts(self) -> dict[str, int]:
        out = {OK: 0, WARNED: 0, FAILED: 0, SKIPPED: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    def failed_commands(self) -> list[tuple[str, Exchange]]:
        """The debug list: every command the instrument complained about."""
        out = []
        for c in self.checks:
            for x in c.exchanges:
                if not x.clean:
                    out.append((c.name, x))
        return out

    # -- output -----------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "title": self.title,
            "started": self.started,
            "finished": datetime.now().isoformat(timespec="seconds"),
            "env": self.env,
            "stages_requested": self.stages_requested,
            "counts": self.counts,
            "notes": self.notes,
            "checks": [asdict(c) for c in self.checks],
        }

    def write_json(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1, default=_jsonable)
        return path

    def write_html(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_html(self))
        return path

    def write_text(self, path: str) -> str:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(render_text(self))
        return path


def _jsonable(o):
    try:
        import numpy as np
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (np.floating, np.integer, np.bool_)):
            return o.item()
    except Exception:
        pass
    return str(o)


# -- rendering ------------------------------------------------------------
_BADGE = {OK: "ok", WARNED: "warn", FAILED: "fail", SKIPPED: "skip"}


def render_text(r: Report) -> str:
    lines = [f"{r.title}", "=" * len(r.title), ""]
    lines.append(f"started   {r.started}")
    lines.append(f"python    {r.env['python']} ({r.env['bitness']}-bit) on "
                 f"{r.env['platform']}")
    mods = ", ".join(f"{k} {v}" for k, v in r.env["modules"].items() if v)
    lines.append(f"modules   {mods or 'none found'}")
    missing = [k for k, v in r.env["modules"].items() if not v]
    if missing:
        lines.append(f"absent    {', '.join(missing)}")
    lines.append(f"stages    {', '.join(r.stages_requested)}")
    c = r.counts
    lines += ["", f"{c[OK]} ok, {c[WARNED]} warned, {c[FAILED]} failed, "
                  f"{c[SKIPPED]} skipped", ""]

    stage = None
    for chk in r.checks:
        if chk.stage != stage:
            stage = chk.stage
            lines += [f"-- {stage} " + "-" * max(0, 60 - len(stage)), ""]
        lines.append(f"[{_BADGE[chk.status]:>4}] {chk.name}"
                     + (f"  ({chk.seconds:g}s)" if chk.seconds >= 0.05 else ""))
        if chk.detail:
            lines.append(f"        {chk.detail}")
        for k, v in chk.data.items():
            lines.append(f"        {k} = {_short(v)}")
        for x in chk.exchanges:
            if not x.clean:
                lines.append(f"        ! {x.command!r} -> "
                             f"{'; '.join(x.errors) or x.exception}")
        lines.append("")

    bad = r.failed_commands()
    if bad:
        lines += ["", "COMMANDS THE INSTRUMENTS REJECTED", "-" * 34]
        for name, x in bad:
            lines.append(f"  {name}: {x.command!r}")
            lines.append(f"      {'; '.join(x.errors) or x.exception}")
    if r.notes:
        lines += ["", "NOTES", "-" * 5] + [f"  - {n}" for n in r.notes]
    return "\n".join(lines) + "\n"


def _short(v, limit: int = 120) -> str:
    s = str(v)
    return s if len(s) <= limit else s[:limit] + f" ... ({len(s)} chars)"


def render_html(r: Report) -> str:
    c = r.counts
    rows = []
    stage = None
    for chk in r.checks:
        if chk.stage != stage:
            stage = chk.stage
            rows.append(f'<tr class="stage"><td colspan="3">{_esc(stage)}</td></tr>')
        data = "".join(
            f"<div class='kv'><span>{_esc(k)}</span><code>{_esc(_short(v, 400))}</code></div>"
            for k, v in chk.data.items())
        ex = ""
        if chk.exchanges:
            ex = "<details><summary>%d commands</summary><table class='ex'>%s</table></details>" % (
                len(chk.exchanges),
                "".join(
                    "<tr class='%s'><td><code>%s</code></td><td><code>%s</code></td>"
                    "<td>%s</td></tr>" % (
                        "" if x.clean else "bad", _esc(x.command),
                        _esc(_short(x.reply or "", 200)),
                        _esc("; ".join(x.errors) or x.exception or ""))
                    for x in chk.exchanges))
        tb = (f"<details><summary>traceback</summary><pre>{_esc(chk.traceback)}</pre></details>"
              if chk.traceback else "")
        rows.append(
            f"<tr><td><span class='b {_BADGE[chk.status]}'>{_BADGE[chk.status]}</span></td>"
            f"<td><strong>{_esc(chk.name)}</strong>{data}{ex}{tb}</td>"
            f"<td class='d'>{_esc(chk.detail)}</td></tr>")

    bad = r.failed_commands()
    badblock = ""
    if bad:
        badblock = ("<h2>Commands the instruments rejected</h2><table class='ex'>"
                    + "".join(f"<tr class='bad'><td><code>{_esc(n)}</code></td>"
                              f"<td><code>{_esc(x.command)}</code></td>"
                              f"<td>{_esc('; '.join(x.errors) or x.exception or '')}</td></tr>"
                              for n, x in bad) + "</table>")

    mods = ", ".join(f"{k}&nbsp;{v}" for k, v in r.env["modules"].items() if v)
    return f"""<!doctype html><meta charset="utf-8">
<title>{_esc(r.title)}</title>
<style>
:root{{color-scheme:light dark}}
body{{font:14px/1.55 -apple-system,Segoe UI,system-ui,sans-serif;margin:0;
 background:#f6f7f8;color:#16202a}}
@media(prefers-color-scheme:dark){{body{{background:#11161b;color:#e6ecf1}}}}
.wrap{{max-width:1100px;margin:0 auto;padding:28px 22px 80px}}
h1{{font-size:26px;margin:0 0 6px}} h2{{font-size:18px;margin:32px 0 8px}}
.meta{{color:#6b7a86;font-size:13px;margin:0 0 18px}}
table{{border-collapse:collapse;width:100%;background:#fff;border:1px solid #dfe4e8;
 border-radius:4px;overflow:hidden}}
@media(prefers-color-scheme:dark){{table{{background:#182028;border-color:#2b3640}}}}
td{{padding:8px 11px;border-bottom:1px solid #e8ecef;vertical-align:top}}
@media(prefers-color-scheme:dark){{td{{border-color:#232d36}}}}
tr.stage td{{background:#eef2f5;font-weight:600;font-size:12px;letter-spacing:.06em;
 text-transform:uppercase;color:#516170}}
@media(prefers-color-scheme:dark){{tr.stage td{{background:#1e2831;color:#8ea1b0}}}}
td.d{{color:#6b7a86;max-width:32ch}}
.b{{display:inline-block;font-size:10px;font-weight:700;letter-spacing:.06em;
 text-transform:uppercase;padding:2px 7px;border-radius:3px}}
.ok{{background:#e2efdc;color:#3f6633}} .warn{{background:#f8ecd6;color:#8a6116}}
.fail{{background:#f8dfd8;color:#95371c}} .skip{{background:#e6eaee;color:#6b7a86}}
code{{font:12px ui-monospace,Consolas,monospace;background:#eef1f4;padding:1px 4px;
 border-radius:2px;word-break:break-all}}
@media(prefers-color-scheme:dark){{code{{background:#222c35}}}}
.kv{{margin-top:4px;font-size:12.5px}} .kv span{{color:#6b7a86;margin-right:6px}}
table.ex{{margin-top:6px;border:none;background:none}}
table.ex td{{padding:3px 6px;border:none;font-size:12px}}
tr.bad td{{background:#fdeee9}}
@media(prefers-color-scheme:dark){{tr.bad td{{background:#2e1d18}}}}
details{{margin-top:6px}} summary{{cursor:pointer;color:#436b86;font-size:12.5px}}
pre{{background:#eef1f4;padding:10px;border-radius:3px;overflow-x:auto;font-size:12px}}
@media(prefers-color-scheme:dark){{pre{{background:#222c35}}}}
ul{{margin:6px 0}}
</style>
<div class="wrap">
<h1>{_esc(r.title)}</h1>
<p class="meta">{_esc(r.started)} &middot; Python {_esc(r.env['python'])}
 ({r.env['bitness']}-bit) &middot; {_esc(r.env['platform'])}<br>{mods}<br>
 stages: {_esc(', '.join(r.stages_requested))}</p>
<p><span class="b ok">{c[OK]} ok</span> <span class="b warn">{c[WARNED]} warned</span>
 <span class="b fail">{c[FAILED]} failed</span>
 <span class="b skip">{c[SKIPPED]} skipped</span></p>
{badblock}
<h2>Checks</h2>
<table>{''.join(rows)}</table>
{'<h2>Notes</h2><ul>' + ''.join(f'<li>{_esc(n)}</li>' for n in r.notes) + '</ul>' if r.notes else ''}
</div>
"""


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
