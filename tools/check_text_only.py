#!/usr/bin/env python3
"""Check that a change touched only comments and docstrings.

    python tools/check_text_only.py                 # against main, every changed file
    python tools/check_text_only.py --base HEAD~1 bace/core

A rewrite of the project's prose (`docs/style.md`, "Review") must leave the
behaviour alone. For each changed file this compares the version at `--base`
with the working tree:

| File | Compared |
|---|---|
| `.py` | The syntax tree with every docstring and every bare string statement removed; comments are not in the tree |
| `.toml` | The parsed tables |

Any other changed file is listed as not checked. Exit 1 if a checked file
differs in more than its text.
"""
from __future__ import annotations

import argparse
import ast
import os
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:                         # Python 3.10
    tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _StripText(ast.NodeTransformer):
    """Removes string statements: docstrings and attribute docstrings."""

    def generic_visit(self, node):
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            body = getattr(node, field, None)
            if isinstance(body, list):
                kept = [s for s in body
                        if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
                                and isinstance(s.value.value, str))]
                setattr(node, field, kept or [ast.Pass()])
        return node


def code_of(source: str) -> str:
    return ast.dump(_StripText().visit(ast.parse(source)), include_attributes=False)


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def changed(base: str, paths: list[str]) -> list[str]:
    out = _git("diff", "--name-only", "--diff-filter=M", base, "--", *paths)
    return [p for p in out.splitlines() if p]


def compare(path: str, base: str) -> str | None:
    """None when only text changed; otherwise why not. Raises for unchecked kinds."""
    before = _git("show", f"{base}:{path}")
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        after = fh.read()
    if path.endswith(".py"):
        try:
            same = code_of(before) == code_of(after)
        except SyntaxError as exc:
            return f"does not parse: {exc}"
        return None if same else "the code differs, not only its text"
    if path.endswith(".toml"):
        if tomllib is None:
            raise ValueError("tomllib needs Python 3.11")
        try:
            same = tomllib.loads(before) == tomllib.loads(after)
        except tomllib.TOMLDecodeError as exc:
            return f"does not parse: {exc}"
        return None if same else "the tables differ, not only the comments"
    raise ValueError("not checked")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="main", help="the version to compare with (default main)")
    ap.add_argument("paths", nargs="*", help="limit to these paths")
    a = ap.parse_args(argv)
    bad = 0
    for path in changed(a.base, a.paths):
        try:
            why = compare(path, a.base)
        except ValueError:
            print(f"  not checked  {path}")
            continue
        if why:
            bad += 1
            print(f"  DIFFERS      {path}: {why}")
        else:
            print(f"  text only    {path}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
