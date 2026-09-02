#!/usr/bin/env python3
"""Lift the web fonts out of the Round 3 mockup and into `ui/fonts/`.

`docs/bace-console-round3.html` is self-contained: its 33 assets are inlined as
base64 in one JSON blob, and 24 of them are the woff2 subsets of the three
faces the design uses — IBM Plex Sans for text, IBM Plex Mono for every number,
Archivo for the wordmark, card titles and buttons (`docs/ui-rules.md` §2). The
console needs the same files, and the lab PC has no network, so they are copied
into the repo rather than fetched.

    python3 tools/extract_ui_fonts.py            # writes ui/fonts/

It writes one `.woff2` per subset and a `fonts.css` of the `@font-face` rules
with the mockup's `unicode-range`s kept intact — those ranges are why 24 files
weigh 387 KB instead of megabytes, and dropping them would load every subset
for a page of ASCII. Nothing here is edited by hand; re-run it if the mockup's
fonts ever change.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "docs", "bace-console-round3.html")
DEFAULT_OUT = os.path.join(REPO, "ui", "fonts")

ASSET = re.compile(r'"([0-9a-f]{8}-[0-9a-f-]{27})":\s*\{"mime":"([^"]+)",'
                   r'"compressed":(true|false),"data":"([A-Za-z0-9+/=]+)"')
FACE = re.compile(r'@font-face\s*\{[^}]*\}')


def _field(block: str, pattern: str) -> str | None:
    m = re.search(pattern, block)
    return m.group(1).strip() if m else None


def _slug(family: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", family.lower()).strip("-")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", default=SOURCE, help="the self-contained mockup")
    ap.add_argument("--out", default=DEFAULT_OUT, help="where the fonts land")
    a = ap.parse_args(argv)

    html = open(a.source, encoding="utf-8", errors="replace").read()
    assets = {}
    for uuid, mime, compressed, data in ASSET.findall(html):
        if mime != "font/woff2":
            continue
        raw = base64.b64decode(data)
        assets[uuid] = gzip.decompress(raw) if compressed == "true" else raw

    os.makedirs(a.out, exist_ok=True)
    names: dict[str, str] = {}
    counter: dict[str, int] = {}
    css: list[str] = []
    seen: set[str] = set()
    for block in FACE.findall(html):
        family = _field(block, r"font-family:\s*'([^']+)'")
        uuid = _field(block, r'url\(\\?"([^"\\]+)\\?"\)')
        if not (family and uuid) or uuid not in assets:
            continue
        if uuid not in names:
            # These are variable fonts: one file per unicode subset serves
            # every weight of its family, so the name carries the subset and
            # not a weight it does not have.
            stem = _slug(family)
            counter[stem] = counter.get(stem, 0) + 1
            names[uuid] = f"{stem}-{counter[stem]}.woff2"
            with open(os.path.join(a.out, names[uuid]), "wb") as fh:
                fh.write(assets[uuid])
        # The rule itself is the mockup's, unescaped from the JS string it was
        # inlined in and pointed at the local file. It renders; a rewritten one
        # would only be a second opinion about font-stretch and unicode-range.
        rule = block.replace("\\n", "\n").replace('\\"', '"')
        rule = re.sub(r'url\("[^"]+"\)', f"url('{names[uuid]}')", rule)
        rule = "\n".join(line.rstrip() for line in rule.splitlines())
        if rule in seen:         # the mockup carries each face twice
            continue
        seen.add(rule)
        css.append(rule)

    header = ("/* Extracted from docs/bace-console-round3.html by\n"
              "   tools/extract_ui_fonts.py — do not edit by hand.\n"
              "   IBM Plex Sans and Mono (SIL Open Font License 1.1),\n"
              "   Archivo (SIL Open Font License 1.1). */\n\n")
    with open(os.path.join(a.out, "fonts.css"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header + "\n\n".join(css) + "\n")

    total = sum(len(v) for k, v in assets.items() if k in names)
    print(f"{len(names)} woff2 · {total/1024:.0f} KB · {len(css)} @font-face rules "
          f"-> {os.path.relpath(a.out, REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
