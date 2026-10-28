"""The manual's generated chapters match the code, and the book builds.

4.1, 4.2 and 4.3 are written by `tools/build_manual.py` from the parameter
specs, the check catalogue and the bench actions. A change to any of those
without running the tool leaves the manual describing code that is gone.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("build_manual", REPO / "tools" / "build_manual.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_chapters_are_current():
    stale = _tool().stale()
    assert stale == [], (f"{', '.join(stale)} out of date with the code: "
                         "run python tools/build_manual.py")


def test_every_check_and_action_is_documented():
    from bace.service.pipeline import CHECK_DOCS, CHECKS
    from bace.service.rigs import ACTION_DOCS, ACTIONS

    assert tuple(CHECK_DOCS) == CHECKS
    assert tuple(ACTION_DOCS) == ACTIONS
    levels = {"ok", "info", "warn", "crit", "invalid"}
    assert all(level in levels and when and remedy
               for level, when, remedy in CHECK_DOCS.values())


def test_the_book_holds_every_chapter_and_its_links_resolve():
    tool = _tool()
    page = tool.book()
    names = tool.chapters()
    assert len(names) == len(list((REPO / "docs" / "manual").glob("[1-4].*.md")))
    for name in names:
        assert f'id="ch-{name[:-3]}"' in page, name
    ids = set(re.findall(r'id="([^"]+)"', page))
    targets = re.findall(r'href="#([^"]+)"', page)
    assert targets and all(t in ids for t in targets)
    assert "**" not in re.sub(r"<pre>.*?</pre>|<code>.*?</code>", "", page, flags=re.S)
