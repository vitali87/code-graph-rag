"""Go's ignored-error smells need the blank identifier, not a trailing `_`.

Both rules tested the left side's text with `_\\s*$`, which an ordinary
identifier ending in an underscore (`result_`, `type_`, protobuf's `XXX_`)
matches as well as `_` does, so `result_ := 42` was reported as "Return value
discarded with blank identifier" (issue #2871).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_ast_grep_analyzer import _fire

pytest.importorskip("ast_grep_py")

SHORTVAR = "ignored_error_shortvar"
ASSIGN = "ignored_error_assign"


def _flagged(tmp_path: Path, body: str) -> dict[str, list[int]]:
    src = f"package main\n\nfunc main() {{\n{body}}}\n"
    found: dict[str, list[int]] = {SHORTVAR: [], ASSIGN: []}
    for p in _fire(tmp_path, "m.go", src):
        if p[cs.KEY_NAME] in found:
            # Lines of the body: the function opens on line 3.
            found[p[cs.KEY_NAME]].append(int(p[cs.KEY_START_LINE]) - 3)
    return {name: sorted(lines) for name, lines in found.items()}


def test_the_issue_flags_only_the_ignored_error(tmp_path: Path) -> None:
    body = (
        "\tresult_ := 42\n"
        '\ttype_ := "widget"\n'
        "\tresult_ = result_ + 1\n"
        "\tx, _ := compute()\n"
        "\tfmt.Println(result_, type_, x)\n"
    )
    assert _flagged(tmp_path, body) == {SHORTVAR: [4], ASSIGN: []}


def test_a_target_ending_in_underscore_is_not_blank(tmp_path: Path) -> None:
    body = (
        "\tXXX_unrecognized := 1\n"
        "\tval, err_ := compute()\n"
        "\tval, err_ = compute()\n"
        "\tmsg.XXX_ = nil\n"
        "\tid_, ok := lookup()\n"
    )
    assert _flagged(tmp_path, body) == {SHORTVAR: [], ASSIGN: []}


def test_the_blank_identifier_is_still_flagged(tmp_path: Path) -> None:
    # Negatives: every spelling of a discarded last value still fires.
    body = (
        "\tx, _ := compute()\n"
        "\tx, _ = compute()\n"
        "\t_ = compute()\n"
        "\tx,\n\t\t_ := compute()\n"
        "\tx,  _  := compute()\n"
    )
    assert _flagged(tmp_path, body) == {SHORTVAR: [1, 4, 6], ASSIGN: [2, 3]}


def test_a_blank_that_is_not_last_keeps_the_error(tmp_path: Path) -> None:
    # Negative: `_, err := f()` keeps the conventionally-last error.
    body = "\t_, err := compute()\n\t_, err = compute()\n"
    assert _flagged(tmp_path, body) == {SHORTVAR: [], ASSIGN: []}
