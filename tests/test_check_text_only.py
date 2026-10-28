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
