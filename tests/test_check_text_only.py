"""`tools/check_text_only.py`: a prose rewrite passes, a code change does not."""
from __future__ import annotations

import importlib.util
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("check_text_only",
                                                  REPO / "tools" / "check_text_only.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BEFORE = '''
"""Module."""
X = 1  # one
"""What X is."""


def f(a):
    """Old text."""
    # old comment
    return a + X
'''


def test_docstrings_comments_and_attribute_docstrings_may_change():
    code_of = _tool().code_of
    after = BEFORE.replace("Old text.", "New text, longer.").replace("# old comment", "# new") \
                  .replace("What X is.", "X, the count.").replace('"""Module."""', "")
    assert code_of(BEFORE) == code_of(after)


def test_code_and_message_strings_may_not():
    code_of = _tool().code_of
    assert code_of(BEFORE) != code_of(BEFORE.replace("a + X", "a - X"))
    message = 'def g():\n    raise ValueError("old")\n'
    assert code_of(message) != code_of(message.replace("old", "new"))


def test_a_toml_value_keeps_its_type():
    typed = _tool()._typed
    import tomllib
    assert typed(tomllib.loads("n = 1")) != typed(tomllib.loads("n = 1.0"))
    assert typed(tomllib.loads("n = 1  # one")) == typed(tomllib.loads("n = 1\n# one"))


def test_added_and_deleted_files_fail(tmp_path, monkeypatch, capsys):
    tool = _tool()
    monkeypatch.setattr(tool, "changed", lambda base, paths: [
        ("D", "bace/gone.py"), ("A", "bace/added.toml"), ("A", "docs/x.md")])
    assert tool.main(["--base", "HEAD"]) == 1
    out = capsys.readouterr().out
    assert "gone.py: D" in out and "added.toml: A" in out and "not checked  docs/x.md" in out


JS = r"""// a comment
const root = base.replace(/\/$/, '');   // trailing slash
/* block
   comment */
const half = width / 2 / scale;
const s = `a ${x / 2} // not a comment`;
const q = "say // nothing";
if (!/^\d+$/.test(v)) return a / b;
"""


def test_js_comments_may_change():
    tokens = _tool().js_tokens
    after = JS.replace("// a comment", "// another comment, longer") \
              .replace("/* block\n   comment */", "") \
              .replace("// trailing slash", "")
    assert tokens(JS) == tokens(after)


def test_js_code_strings_and_regexes_may_not():
    tokens = _tool().js_tokens
    for old, new in (("width / 2", "width / 3"), ("/\\/$/", "/\\/+$/"),
                     ("// not a comment", "// changed"), ("say // nothing", "say"),
                     ("/^\\d+$/", "/^\\d*$/")):
        assert old in JS, old
        assert tokens(JS) != tokens(JS.replace(old, new)), old


def test_no_console_code_is_lost_to_a_misread_comment_string_or_regex():
    """A lexer that took a regular expression or a string for the start of a
    comment would drop the code after it. Every function the console
    declares must survive as a token, and a trailing comment changes nothing."""
    import re
    tokens = _tool().js_tokens
    for path in sorted((REPO / "ui").rglob("*.js")) + sorted((REPO / "ui").rglob("*.mjs")):
        if "node_modules" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        found = tokens(source)
        names = set(found)
        uncommented = re.sub(r"^\s*//.*$", "", source, flags=re.M)
        for name in re.findall(r"^\s*(?:export\s+)?(?:async\s+)?function\*?\s+([A-Za-z_$][\w$]*)",
                               uncommented, flags=re.M):
            assert name in names, (path, name)
        assert found == tokens(source + "\n// trailing comment\n"), path


def test_a_comment_may_not_add_or_remove_a_line_break_between_tokens():
    tokens = _tool().js_tokens
    one = "function f() { return /* reason */ 42; }"
    assert tokens(one) != tokens("function f() { return /* reason\n */ 42; }")
    assert tokens(one) == tokens("function f() { return /* a longer reason */ 42; }")
    wrapped = "a();\n// one\n// two\nb();"
    assert tokens(wrapped) == tokens("a();\n// one, two, three\nb();")


def test_a_regular_expression_after_a_control_condition():
    tokens = _tool().js_tokens
    run = "if (ok) /[//]/.test(x); run();"
    assert tokens(run) != tokens(run.replace("run()", "destroy()"))
    assert "/[//]/" in tokens(run)
    assert tokens("f(a) / 2") == ["f", "(", "a", ")", "/", "2"]


def test_css_comments_may_change_and_rules_may_not():
    css_text = _tool().css_text
    before = '/* bar */\n.bar { color: red; /* why */ }\n.x::after { content: "/* kept */"; }\n'
    after = '/* the bar,\n   rewritten */\n.bar { color: red; }\n.x::after { content: "/* kept */"; }\n'
    assert css_text(before) == css_text(after)
    assert css_text(before) != css_text(before.replace("red", "blue"))
    assert css_text(before) != css_text(before.replace('"/* kept */"', '"kept"'))
