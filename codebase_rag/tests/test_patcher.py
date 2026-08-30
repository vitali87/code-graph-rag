# Concrete-syntax-preserving patchers (issue #1529): a rename touches only
# the identifier occurrences, a batch of edits to one file applies correctly
# in any order, and the result is re-parsed (and format-checked where a
# canonical formatter exists).
from __future__ import annotations

import difflib
import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import (
    Patcher,
    PatcherError,
    SpanEdit,
    apply_span_edits,
    byte_to_line_col,
    formatter_check,
    line_col_to_byte,
)
from codebase_rag.parser_loader import load_parsers

PY_SRC = (
    "# leading comment  \n"
    "def helper(a,   b,):   # odd spacing, trailing comma\n"
    "\tresult = helper(a, b)  # recursive, tab-indented\n"
    "\treturn result\n"
    "\n"
    "\n"
    "x = {'helper': helper,\n"
    "     'other': 1,   }\n"
)


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _changed_lines(before: str, after: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True), n=0
        )
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


def _grammar(name: str) -> None:
    parsers, _ = load_parsers()
    if name not in {str(lang) for lang in parsers}:
        pytest.skip(f"{name} parser not available")


# --- offsets ------------------------------------------------------------------


def test_offset_conversions_round_trip() -> None:
    src = b"ab\ncd\xc3\xa9f\n\nlast"
    for offset in range(len(src) + 1):
        line, col = byte_to_line_col(src, offset)
        assert line_col_to_byte(src, line, col) == offset
    assert line_col_to_byte(src, 1, 0) == 0
    assert line_col_to_byte(src, 4, 4) == len(src)
    with pytest.raises(PatcherError):
        line_col_to_byte(src, 0, 0)
    with pytest.raises(PatcherError):
        line_col_to_byte(src, 2, 99)
    with pytest.raises(PatcherError):
        line_col_to_byte(src, 9, 0)
    with pytest.raises(PatcherError):
        byte_to_line_col(src, len(src) + 1)


def test_span_edits_apply_in_any_order() -> None:
    src = b"aaa bbb ccc ddd"
    edits = [
        SpanEdit(8, 11, b"C"),
        SpanEdit(0, 3, b"AAAA"),
        SpanEdit(12, 15, b"D"),
        SpanEdit(4, 7, b"B"),
    ]
    expected = b"AAAA B C D"
    assert apply_span_edits(src, edits) == expected
    assert apply_span_edits(src, reversed(edits)) == expected
    assert apply_span_edits(src, sorted(edits, key=lambda e: e.text)) == expected


def test_span_edits_refuse_overlap_and_bad_spans() -> None:
    src = b"0123456789"
    with pytest.raises(PatcherError, match="overlaps"):
        apply_span_edits(src, [SpanEdit(0, 5, b"x"), SpanEdit(4, 6, b"y")])
    with pytest.raises(PatcherError, match="outside"):
        apply_span_edits(src, [SpanEdit(0, 11, b"x")])
    with pytest.raises(PatcherError, match="outside"):
        apply_span_edits(src, [SpanEdit(5, 2, b"x")])
    # Adjacent spans are fine, and an insertion at a boundary is kept.
    assert apply_span_edits(src, [SpanEdit(0, 5, b"a"), SpanEdit(5, 10, b"b")]) == b"ab"
    assert (
        apply_span_edits(src, [SpanEdit(5, 5, b"-"), SpanEdit(0, 5, b"a")])
        == b"a-56789"
    )


# --- rename preserving concrete syntax ----------------------------------------


def test_python_rename_touches_only_the_identifier_occurrences(tmp_path: Path) -> None:
    _grammar("python")
    _write(tmp_path, "mod.py", PY_SRC)
    patcher = Patcher(tmp_path)
    # Occurrences: definition (line 2), recursive call (line 3), dict value (line 7).
    patcher.replace_identifier_at("mod.py", 2, 4, "helper", "assist")
    patcher.replace_identifier_at("mod.py", 3, 10, "helper", "assist")
    patcher.replace_identifier_at("mod.py", 7, 15, "helper", "assist")
    (result,) = patcher.apply().values()

    after = result.content.decode()
    assert result.parses is True
    assert result.edits == 3
    changed = _changed_lines(PY_SRC, after)
    assert len(changed) == 6, changed
    assert all("helper" in line for line in changed if line.startswith("-"))
    assert all("assist" in line for line in changed if line.startswith("+"))
    # Everything around the identifiers survived byte for byte.
    assert after.replace("assist", "helper") == PY_SRC
    assert "'helper': assist" in after  # the string key is not an identifier


def test_identifier_position_must_hold_the_identifier(tmp_path: Path) -> None:
    _grammar("python")
    _write(tmp_path, "mod.py", PY_SRC)
    patcher = Patcher(tmp_path)
    with pytest.raises(PatcherError, match="not the identifier"):
        patcher.replace_identifier_at("mod.py", 2, 0, "helper", "x")
    # A prefix of the identifier: the bytes match, the tree says otherwise.
    with pytest.raises(PatcherError, match="not a whole identifier"):
        patcher.replace_identifier_at("mod.py", 2, 4, "help", "x")
    # The string literal 'helper' is not an identifier node.
    with pytest.raises(PatcherError, match="not a whole identifier"):
        patcher.replace_identifier_at("mod.py", 7, 6, "helper", "x")
    assert patcher.pending == {}


def test_replace_span_and_identifier_edits_mix_in_one_file(tmp_path: Path) -> None:
    _grammar("python")
    src = "def f(a, b):\n    return a + b\n"
    _write(tmp_path, "m.py", src)
    patcher = Patcher(tmp_path)
    patcher.replace_span("m.py", (src.index("a + b"), src.index("a + b") + 5), "a - b")
    patcher.replace_identifier_at("m.py", 1, 4, "f", "g")
    (result,) = patcher.apply().values()
    assert result.content == b"def g(a, b):\n    return a - b\n"
    assert result.parses is True
    assert result.message == cs.PATCH_OK.format(path="m.py", count=2)


def test_parse_failure_is_reported_and_not_staged(tmp_path: Path) -> None:
    _grammar("python")
    _write(tmp_path, "m.py", "def f():\n    return 1\n")
    patcher = Patcher(tmp_path)
    patcher.replace_span("m.py", (0, 3), "de")

    class Stub:
        staged: dict[str, bytes] = {}

        def stage(self, rel_path: str | Path, content: str | bytes | None) -> None:
            self.staged[str(rel_path)] = content  # type: ignore[assignment]

    stub = Stub()
    results = patcher.stage_into(stub)
    assert results["m.py"].parses is False
    assert results["m.py"].message == cs.PATCH_PARSE_FAILED.format(path="m.py")
    assert stub.staged == {}


def test_results_stage_into_a_transaction_like_object(tmp_path: Path) -> None:
    _grammar("python")
    _write(tmp_path, "a.py", "x = 1\n")
    _write(tmp_path, "b.py", "y = 2\n")
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("a.py", 1, 0, "x", "xx")
    patcher.replace_identifier_at("b.py", 1, 0, "y", "yy")
    staged: dict[str, bytes] = {}

    class Stub:
        def stage(self, rel_path: str | Path, content: str | bytes | None) -> None:
            assert isinstance(content, bytes)
            staged[str(rel_path)] = content

    results = patcher.stage_into(Stub())
    assert set(results) == {"a.py", "b.py"}
    assert staged == {"a.py": b"xx = 1\n", "b.py": b"yy = 2\n"}


def test_overlay_content_is_the_base_for_chained_batches(tmp_path: Path) -> None:
    _grammar("python")
    _write(tmp_path, "m.py", "old = 1\n")
    patcher = Patcher(tmp_path, overlay={"m.py": b"new = 1\n"})
    patcher.replace_identifier_at("m.py", 1, 0, "new", "newer")
    (result,) = patcher.apply().values()
    assert result.content == b"newer = 1\n"


def test_paths_outside_the_repo_and_missing_files_are_refused(tmp_path: Path) -> None:
    patcher = Patcher(tmp_path)
    with pytest.raises(PatcherError, match="outside"):
        patcher.replace_span("../x.py", (0, 0), "")
    with pytest.raises(PatcherError, match="No such file"):
        patcher.replace_span("missing.py", (0, 0), "")


def test_generic_fallback_for_a_language_without_a_grammar(tmp_path: Path) -> None:
    _write(tmp_path, "notes.txt", "alpha beta\n")
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("notes.txt", 1, 6, "beta", "gamma")
    (result,) = patcher.apply().values()
    assert result.content == b"alpha gamma\n"
    assert result.parses is None
    assert result.formatter is None


# --- other languages ----------------------------------------------------------


TS_SRC = (
    "export function helper(a: number, /* c */ b: number,): number {\n"
    "  return helper(a, b) + 1; // recursion\n"
    "}\n"
    "const obj = { helper, other: 2, };\n"
)


def test_typescript_rename_preserves_syntax(tmp_path: Path) -> None:
    _grammar("typescript")
    _write(tmp_path, "m.ts", TS_SRC)
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("m.ts", 1, 16, "helper", "assist")
    patcher.replace_identifier_at("m.ts", 2, 9, "helper", "assist")
    patcher.replace_identifier_at("m.ts", 4, 14, "helper", "assist")
    (result,) = patcher.apply().values()
    assert result.parses is True
    assert result.content.decode().replace("assist", "helper") == TS_SRC
    assert result.content.count(b"assist") == 3


JAVA_SRC = "class A {\n    int helper(int a,  int b) { return helper(a, b); } // r\n}\n"


def test_java_rename_preserves_syntax(tmp_path: Path) -> None:
    _grammar("java")
    _write(tmp_path, "A.java", JAVA_SRC)
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("A.java", 2, 8, "helper", "assist")
    patcher.replace_identifier_at("A.java", 2, 39, "helper", "assist")
    (result,) = patcher.apply().values()
    assert result.parses is True
    assert result.content.decode().replace("assist", "helper") == JAVA_SRC


GO_SRC = "package main\n\nfunc helper(a, b int) int { return helper(a, b) }\n"


def test_go_rename_runs_the_gofmt_check(tmp_path: Path) -> None:
    _grammar("go")
    _write(tmp_path, "m.go", GO_SRC)
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("m.go", 3, 5, "helper", "assist")
    patcher.replace_identifier_at("m.go", 3, 35, "helper", "assist")
    (result,) = patcher.apply().values()
    assert result.parses is True
    assert result.content.decode().replace("assist", "helper") == GO_SRC
    assert result.formatter == cs.PATCH_GOFMT
    if shutil.which(cs.PATCH_GOFMT):
        assert result.formatted is True
    else:
        assert result.formatted is None


RUST_SRC = "fn helper(a: u32) -> u32 {\n    helper(a) // r\n}\n"


def test_rust_rename_runs_the_rustfmt_check(tmp_path: Path) -> None:
    _grammar("rust")
    _write(tmp_path, "m.rs", RUST_SRC)
    patcher = Patcher(tmp_path)
    patcher.replace_identifier_at("m.rs", 1, 3, "helper", "assist")
    patcher.replace_identifier_at("m.rs", 2, 4, "helper", "assist")
    (result,) = patcher.apply().values()
    assert result.parses is True
    assert result.content.decode().replace("assist", "helper") == RUST_SRC
    assert result.formatter == cs.PATCH_RUSTFMT
    if shutil.which(cs.PATCH_RUSTFMT):
        assert result.formatted is True
    else:
        assert result.formatted is None


def test_formatter_drift_is_reported_not_rewritten(tmp_path: Path) -> None:
    if not shutil.which(cs.PATCH_GOFMT):
        pytest.skip("gofmt not installed")
    _grammar("go")
    _write(tmp_path, "m.go", GO_SRC)
    patcher = Patcher(tmp_path)
    # Two spaces after `func` still parses but is not gofmt-canonical.
    patcher.replace_span(
        "m.go", (GO_SRC.index("func "), GO_SRC.index("func ") + 5), "func  "
    )
    (result,) = patcher.apply().values()
    assert result.parses is True
    assert result.formatted is False
    assert result.message == cs.PATCH_FORMAT_DRIFT.format(
        path="m.go", tool=cs.PATCH_GOFMT
    )
    assert b"func  helper" in result.content


def test_formatter_check_without_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert formatter_check(cs.SupportedLanguage.GO, b"package main\n", ".go") == (
        cs.PATCH_GOFMT,
        None,
    )
    assert formatter_check(cs.SupportedLanguage.PYTHON, b"x = 1\n", ".py") == (
        None,
        None,
    )
