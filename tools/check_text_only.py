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
| `.toml` | The parsed tables, each value with its type |
| `.js`, `.mjs` | The token sequence with comments removed: every identifier, number, string, template and regular expression |
| `.css` | The rules with comments removed and whitespace collapsed |

A checked file that was added, deleted or renamed fails: a rewrite changes
files, it does not create or remove them. Any other changed file is listed
as not checked. Exit 1 if a checked file differs in more than its text.
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


CHECKED = (".py", ".toml", ".js", ".mjs", ".css")


def css_text(source: str) -> str:
    """A stylesheet with its comments removed and every run of whitespace
    outside quoted strings reduced to one space.

    A comment counts as nothing, not as whitespace: `.a/**/.b` selects an
    element with both classes and `.a .b` a descendant, and the two must
    compare different. Quoted strings are kept exactly, whitespace included.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            out.append(source[i:j + 1])
            i = j + 1
            continue
        if c.isspace():
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out).strip()


_JS_PUNCT = sorted(("{ } ( ) [ ] ; , < > + - * % & | ^ ! ~ ? : = . @ / "
                    "... => == != === !== <= >= && || ?? ?. ++ -- << >> >>> "
                    "+= -= *= /= %= &= |= ^= &&= ||= ??= **= <<= >>= >>>= **").split(),
                   key=len, reverse=True)
_JS_REGEX_AFTER = {"(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";", "+", "-",
                   "*", "%", "<", ">", "~", "^", "=>", "==", "!=", "===", "!==", "<=",
                   ">=", "&&", "||", "??", "+=", "-=", "*=", "/=", "return", "typeof",
                   "case", "in", "of", "delete", "void", "throw", "new", "else", "do",
                   "yield", "await", "ctl)", None}
"""Tokens after which a `/` opens a regular expression rather than dividing.
`ctl)` stands for the `)` that closes the condition of `if`, `while`, `for` or
`with`, after which a statement, and so a regular expression, may begin."""

_JS_CONTROL = {"if", "while", "for", "with"}

LINE_BREAK = "\u23ce"
"""Stands between two tokens that a line terminator separates, in the source
or inside a block comment: automatic semicolon insertion depends on it, so a
comment rewrite may not add or remove one (`return /* a\n */ 42`)."""


def js_tokens(source: str) -> list[str]:
    """The tokens of a JavaScript module with its comments left out.

    Enough of a lexer for comparing two versions: strings, templates (with
    their `${}` expressions kept as text), regular expressions, numbers,
    identifiers and punctuators. A `/` opens a regular expression after an
    operator, an opening bracket, a keyword or the `)` of a control
    statement's condition, and divides otherwise. A line break between two
    tokens is kept as `LINE_BREAK`, however many there are.
    """
    out: list[str] = []
    i, n = 0, len(source)
    prev = last = None
    parens: list[bool] = []
    broken = False
    while i < n:
        c = source[i]
        if c.isspace():
            broken = broken or c in "\n\r\u2028\u2029"
            i += 1
            continue
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if source.startswith("/*", i):
            j = source.find("*/", i + 2)
            end = n if j < 0 else j + 2
            broken = broken or any(t in source[i:end] for t in "\n\r\u2028\u2029")
            i = end
            continue
        if c in "'\"":
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == "\\" else 1
            tok = source[i:j + 1]
            i = j + 1
        elif c == "`":
            j, depth = i + 1, 0
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if depth == 0 and ch == "`":
                    break
                if source.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if depth and ch == "{":
                    depth += 1
                elif depth and ch == "}":
                    depth -= 1
                j += 1
            tok = source[i:j + 1]
            i = j + 1
        elif c == "/" and prev in _JS_REGEX_AFTER:
            j, in_class = i + 1, False
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (source[j].isalnum()):
                j += 1
            tok = source[i:j]
            i = j
        elif c.isalnum() or c in "_$#":
            j = i + 1
            while j < n and (source[j].isalnum() or source[j] in "_$."
                             and not (source[j] == "." and not source[i].isdigit())):
                j += 1
            tok = source[i:j]
            i = j
        else:
            tok = next((p for p in _JS_PUNCT if source.startswith(p, i)), c)
            i += len(tok)
        if broken and out:
            out.append(LINE_BREAK)
        broken = False
        out.append(tok)
        prev = tok if not (tok[0].isalnum() or tok[0] in "_$#'\"`") or tok in _JS_REGEX_AFTER else "x"
        if tok == "(":
            parens.append(last in _JS_CONTROL)
        elif tok == ")" and parens and parens.pop():
            prev = "ctl)"
        last = tok
    return out


def changed(base: str, paths: list[str]) -> list[tuple[str, str]]:
    """`(status, path)` for every file that differs from `base`: `M` modified,
    `A` added, `D` deleted, `R…` renamed, as `git diff --name-status` gives."""
    out = _git("diff", "--name-status", base, "--", *paths)
    rows = []
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2:
            rows.append((fields[0], fields[-1]))
    return rows


def _typed(value):
    """A parsed TOML value with the type of every leaf kept: `1` and `1.0`
    compare equal as numbers and differ as settings."""
    if isinstance(value, dict):
        return {k: _typed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_typed(v) for v in value]
    return (type(value).__name__, value)


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
            same = _typed(tomllib.loads(before)) == _typed(tomllib.loads(after))
        except tomllib.TOMLDecodeError as exc:
            return f"does not parse: {exc}"
        return None if same else "the tables differ, not only the comments"
    if path.endswith(".css"):
        return None if css_text(before) == css_text(after) else \
            "the rules differ, not only the comments"
    if path.endswith((".js", ".mjs")):
        return None if js_tokens(before) == js_tokens(after) else \
            "the code differs, not only its comments"
    raise ValueError("not checked")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="main", help="the version to compare with (default main)")
    ap.add_argument("paths", nargs="*", help="limit to these paths")
    a = ap.parse_args(argv)
    bad = 0
    for status, path in changed(a.base, a.paths):
        if status != "M":
            if path.endswith(CHECKED):
                bad += 1
                print(f"  DIFFERS      {path}: {status[0]} -- added, deleted or renamed")
            else:
                print(f"  not checked  {path}")
            continue
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
