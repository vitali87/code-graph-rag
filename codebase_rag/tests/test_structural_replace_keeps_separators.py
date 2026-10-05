"""Issue #2669: a `$$$NAME` capture is rewritten as its source text.

`_interpolate` joined the captured nodes' texts with "", dropping whatever
the source had between them. Statements lost their newline, so a JavaScript
body without semicolons became `...i.price)return ...` (a SyntaxError) even
when the rewrite equalled the pattern, and every argument list lost the
space after each comma. A capture is now the source from its first node to
its last, as the ast-grep CLI takes it; the template's own text around it is
still the template's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ast_grep_py")

from ast_grep_py import SgRoot  # noqa: E402

from codebase_rag.tools.ast_grep_service import AstGrepService  # noqa: E402

UTIL_JS = """function total(items) {
  const prices = items.map((i) => i.price)
  return prices.reduce((a, b) => a + b, 0)
}
"""
UTIL_JS_REWRITTEN = """function total(items) { const prices = items.map((i) => i.price)
  return prices.reduce((a, b) => a + b, 0) }
"""

FETCH_PY = """resp = fetch(
    "https://example.com",
    timeout=5,
)
"""


def _replace(root: Path, pattern: str, rewrite: str, language: str) -> int:
    changes = AstGrepService(str(root)).replace(
        pattern, rewrite, language, dry_run=False
    )
    return len(changes)


def _parses(text: str, language: str) -> bool:
    return SgRoot(text, language).root().find(kind="ERROR") is None


def test_a_js_body_without_semicolons_still_parses_after_an_identity_rewrite(
    tmp_path: Path,
) -> None:
    target = tmp_path / "util.js"
    target.write_text(UTIL_JS, encoding="utf-8")

    _replace(
        tmp_path,
        "function $F($$$P) { $$$B }",
        "function $F($$$P) { $$$B }",
        "javascript",
    )

    result = target.read_text(encoding="utf-8")
    assert result == UTIL_JS_REWRITTEN
    assert _parses(result, "javascript")


def test_statements_keep_their_newline_in_a_new_template(tmp_path: Path) -> None:
    target = tmp_path / "util.js"
    target.write_text(UTIL_JS, encoding="utf-8")

    _replace(
        tmp_path,
        "function $F($$$P) { $$$B }",
        "export function $F($$$P) { $$$B }",
        "javascript",
    )

    result = target.read_text(encoding="utf-8")
    assert result == f"export {UTIL_JS_REWRITTEN}"
    assert _parses(result, "javascript")


@pytest.mark.parametrize(
    ("name", "text", "pattern", "language"),
    [
        pytest.param(
            "enc.py",
            'want_bytes(string, encoding="ascii", errors="ignore")\n',
            "want_bytes($$$A)",
            "python",
            id="py-args",
        ),
        pytest.param(
            "add.js",
            "function add(a, b) { return a + b; }\n",
            "function $F($$$P) { $$$B }",
            "javascript",
            id="js-one-line",
        ),
    ],
)
def test_a_rewrite_equal_to_the_source_around_its_captures_changes_nothing(
    tmp_path: Path, name: str, text: str, pattern: str, language: str
) -> None:
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")

    assert _replace(tmp_path, pattern, pattern, language) == 0
    assert target.read_text(encoding="utf-8") == text


def test_arguments_keep_their_separators(tmp_path: Path) -> None:
    target = tmp_path / "enc.py"
    target.write_text(
        'want_bytes(string, encoding="ascii", errors="ignore")\n', encoding="utf-8"
    )

    _replace(tmp_path, "want_bytes($$$A)", "to_bytes($$$A)", "python")

    assert target.read_text(encoding="utf-8") == (
        'to_bytes(string, encoding="ascii", errors="ignore")\n'
    )


def test_a_multi_line_call_keeps_the_text_between_its_arguments(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fetch.py"
    target.write_text(FETCH_PY, encoding="utf-8")

    _replace(tmp_path, "fetch($$$A)", "http_get($$$A)", "python")

    result = target.read_text(encoding="utf-8")
    assert result == 'resp = http_get("https://example.com",\n    timeout=5,)\n'
    assert _parses(result, "python")


def test_a_capture_after_non_ascii_text_is_sliced_in_place(tmp_path: Path) -> None:
    target = tmp_path / "enc.py"
    target.write_text(
        'label = "日本語"\nwant_bytes(label, errors="ignore")\n', encoding="utf-8"
    )

    _replace(tmp_path, "want_bytes($$$A)", "to_bytes($$$A)", "python")

    assert target.read_text(encoding="utf-8") == (
        'label = "日本語"\nto_bytes(label, errors="ignore")\n'
    )


# Negative: what must not change.


def test_an_empty_capture_is_still_empty(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("ping()\n", encoding="utf-8")

    _replace(tmp_path, "ping($$$A)", "pong($$$A)", "python")

    assert target.read_text(encoding="utf-8") == "pong()\n"


def test_a_single_capture_is_still_its_node_text(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x + 1)\n", encoding="utf-8")

    _replace(tmp_path, "print($A)", "log($A)", "python")

    assert target.read_text(encoding="utf-8") == "log(x + 1)\n"


def test_an_unknown_single_metavariable_is_still_left_literal(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("print(x)\n", encoding="utf-8")

    _replace(tmp_path, "print($$$A)", "log($$$A, $NOPE)", "python")

    assert target.read_text(encoding="utf-8") == "log(x, $NOPE)\n"


def test_a_one_argument_call_is_rewritten_as_before(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("want_bytes(value)\n", encoding="utf-8")

    _replace(tmp_path, "want_bytes($$$A)", "to_bytes($$$A)", "python")

    assert target.read_text(encoding="utf-8") == "to_bytes(value)\n"
