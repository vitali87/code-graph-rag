# (H) Unit tests for the ast-grep structural search/replace service (#415).
# (H) No database or LLM: they drive AstGrepService directly over tmp files and
# (H) assert match locations, language filtering, unsupported-language skipping,
# (H) dry-run safety, and metavariable interpolation on rewrite.
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ast_grep_py")

from codebase_rag.tools.ast_grep_service import AstGrepService


def test_search_finds_pattern_with_1_based_line(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    print(x)\n    print(y)\n")
    svc = AstGrepService(str(tmp_path))
    matches = svc.search("print($A)")
    assert len(matches) == 2
    assert matches[0]["file"] == "a.py"
    # (H) line is reported 1-based (editor convention), so the first print is 2.
    assert matches[0]["line"] == 2
    assert matches[0]["text"] == "print(x)"


def test_search_language_filter_restricts_to_one_language(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("print(1)\n")
    (tmp_path / "b.js").write_text("print(1)\n")
    svc = AstGrepService(str(tmp_path))
    py_only = svc.search("print($A)", language="python")
    assert {m["file"] for m in py_only} == {"a.py"}


def test_unsupported_language_file_is_skipped_not_crashed(tmp_path: Path) -> None:
    # (H) ast-grep ships no dart grammar; the dart file must be skipped rather
    # (H) than panicking the Rust binding.
    (tmp_path / "a.dart").write_text("void main() { print(1); }\n")
    (tmp_path / "b.py").write_text("print(1)\n")
    svc = AstGrepService(str(tmp_path))
    res = svc.search("print($A)")
    assert {m["file"] for m in res} == {"b.py"}


def test_replace_dry_run_produces_diff_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x)\n")
    svc = AstGrepService(str(tmp_path))
    changes = svc.replace("print($A)", "log($A)", dry_run=True)
    assert changes and changes[0]["applied"] is False
    assert "log(x)" in changes[0]["diff"]
    # (H) file left untouched in dry-run.
    assert target.read_text() == "print(x)\n"


def test_replace_applies_and_interpolates_single_metavar(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x)\nprint(y)\n")
    svc = AstGrepService(str(tmp_path))
    changes = svc.replace("print($A)", "log($A)", dry_run=False)
    assert changes[0]["applied"] is True
    txt = target.read_text()
    assert "log(x)" in txt and "log(y)" in txt
    assert "print(" not in txt


def test_replace_interpolates_multi_metavar(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("foo(a, b)\n")
    svc = AstGrepService(str(tmp_path))
    svc.replace("foo($$$ARGS)", "bar($$$ARGS)", dry_run=False)
    result = target.read_text()
    assert result.startswith("bar(a")
    assert "b)" in result


def test_no_matches_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n")
    svc = AstGrepService(str(tmp_path))
    assert svc.search("print($A)") == []
    assert svc.replace("print($A)", "log($A)", dry_run=True) == []


def test_csharp_language_alias_is_accepted(tmp_path: Path) -> None:
    # (H) callers naturally pass ast-grep's id "csharp"; the repo enum value is
    # (H) "c_sharp". Both must select the C# grammar, or C# scans silently fail.
    (tmp_path / "a.cs").write_text(
        "class A { void M() { System.Console.WriteLine(1); } }\n"
    )
    svc = AstGrepService(str(tmp_path))
    for lang in ("c_sharp", "csharp"):
        res = svc.search("System.Console.WriteLine($A)", language=lang)
        assert {m["file"] for m in res} == {"a.cs"}, lang


def test_invalid_pattern_raises_value_error(tmp_path: Path) -> None:
    # (H) an empty / matcher-less pattern makes ast-grep raise RuntimeError; the
    # (H) service must convert that to a ValueError the tool layer reports, not
    # (H) let it crash the turn.
    (tmp_path / "a.py").write_text("print(x)\n")
    svc = AstGrepService(str(tmp_path))
    with pytest.raises(ValueError):
        svc.search("")
    with pytest.raises(ValueError):
        svc.replace("", "log($A)", dry_run=True)
