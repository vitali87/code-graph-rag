"""`rename` accepts the identifiers the target language accepts (issue #3225).

The new name was checked against `[A-Za-z_]\\w*`: an ASCII-only first
character refused `Ünit` and `数据` while `aünï` passed, `$` was refused
though JS/TS, Java, Scala and Dart allow it, and a reserved word passed the
check only to fail the post-rewrite parse, if that parse failed at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import RenameRefused, rename
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

_JS = (
    "function fetch(url) { return url; }\n"
    "function $fetch(url) { return fetch(url) + $fetch.count; }\n"
    "$fetch.count = 0;\n"
    "module.exports = { fetch, $fetch };\n"
)
_PY = "def helper(a):\n    return a\n\n\ndef run():\n    return helper(1)\n"
_GO = "package main\n\nfunc helper() int { return 1 }\n\nfunc main() { _ = helper() }\n"


def _plan(root: Path, graph: RecordedGraph, member: str, new: str):
    return rename(
        root,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.{member}",
        new,
        dry_run=True,
    )


@pytest.fixture
def js_graph(temp_repo: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    _write(temp_repo, "api.js", _JS)
    return _index(temp_repo, mock_ingestor)


@pytest.fixture
def py_graph(temp_repo: Path, mock_ingestor: MagicMock) -> RecordedGraph:
    _write(temp_repo, "pkg/util.py", _PY)
    return _index(temp_repo, mock_ingestor)


@pytest.mark.parametrize("new", ["Ünit", "数据", "$get", "get$"])
def test_a_unicode_or_dollar_js_name_is_accepted(
    temp_repo: Path, js_graph: RecordedGraph, new: str
) -> None:
    report = _plan(temp_repo, js_graph, "api.fetch", new)
    assert f"function {new}(url)" in report.diff, report.message


@pytest.mark.parametrize("new", ["Größe", "数据", "match"])
def test_a_unicode_or_soft_keyword_python_name_is_accepted(
    temp_repo: Path, py_graph: RecordedGraph, new: str
) -> None:
    report = _plan(temp_repo, py_graph, "pkg.util.helper", new)
    assert f"def {new}(a):" in report.diff, report.message


@pytest.mark.parametrize(
    ("fixture", "member", "new", "language"),
    [
        ("py_graph", "pkg.util.helper", "class", "python"),
        ("py_graph", "pkg.util.helper", "None", "python"),
        ("js_graph", "api.fetch", "function", "javascript"),
    ],
    ids=["python-class", "python-None", "js-function"],
)
def test_a_reserved_word_is_refused_with_its_reason(
    request: pytest.FixtureRequest,
    temp_repo: Path,
    fixture: str,
    member: str,
    new: str,
    language: str,
) -> None:
    graph: RecordedGraph = request.getfixturevalue(fixture)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, member, new)
    assert str(refused.value) == cs.RENAME_RESERVED_WORD.format(
        name=new, language=language
    )


def test_a_go_keyword_is_refused(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    _write(temp_repo, "main.go", _GO)
    graph = _index(temp_repo, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, "main.helper", "func")
    assert "reserved word in go" in str(refused.value)


@pytest.mark.parametrize(
    ("fixture", "member", "new"),
    [
        ("py_graph", "pkg.util.helper", "1bad"),
        ("py_graph", "pkg.util.helper", "$get"),
        ("py_graph", "pkg.util.helper", "a-b"),
        ("js_graph", "api.fetch", "get url"),
    ],
    ids=["leading-digit", "dollar-in-python", "hyphen", "space"],
)
def test_names_no_language_allows_are_still_refused(
    request: pytest.FixtureRequest,
    temp_repo: Path,
    fixture: str,
    member: str,
    new: str,
) -> None:
    # Negatives: a leading digit, a `$` where the language has none, a
    # hyphen and a space are no identifier anywhere.
    graph: RecordedGraph = request.getfixturevalue(fixture)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, member, new)
    assert str(refused.value) == cs.RENAME_BAD_NAME.format(name=new)


_JS_CLASS = (
    "class Store {\n  save(x) { return x; }\n  run() { return this.save(1); }\n}\n"
    "module.exports = { Store };\n"
)


@pytest.mark.parametrize("new", ["delete", "new"])
def test_a_js_method_may_take_a_reserved_word(
    temp_repo: Path, mock_ingestor: MagicMock, new: str
) -> None:
    # A method's name is an IdentifierName, which a reserved word may be:
    # `class Store { delete() {} }` and `this.delete()` are valid.
    _write(temp_repo, "store.js", _JS_CLASS)
    graph = _index(temp_repo, mock_ingestor)
    report = _plan(temp_repo, graph, "store.Store.save", new)
    assert f"  {new}(x) {{" in report.diff, report.message


@pytest.mark.parametrize("new", ["a·b", "Ⅻfoo", "á"])
def test_a_name_go_does_not_allow_is_refused(
    temp_repo: Path, mock_ingestor: MagicMock, new: str
) -> None:
    # Negative: Go identifiers are Unicode letters, `_` and decimal digits
    # alone; Python's rule also admits the middle dot, a letter number (Ⅻ)
    # and a combining mark.
    _write(temp_repo, "main.go", _GO)
    graph = _index(temp_repo, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, "main.helper", new)
    assert str(refused.value) == cs.RENAME_BAD_NAME.format(name=new)


def test_a_unicode_letter_go_name_is_accepted(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "main.go", _GO)
    graph = _index(temp_repo, mock_ingestor)
    report = _plan(temp_repo, graph, "main.helper", "数据2")
    assert "func 数据2() int" in report.diff, report.message


_LUA = (
    "local function helper(a)\n  return a\nend\n\n"
    "local function run()\n  return helper(1)\nend\n\n"
    "return { run = run }\n"
)


@pytest.mark.parametrize("new", ["数据", "a数据", "Größe", "x٣"])
def test_a_name_lua_does_not_allow_is_refused(
    temp_repo: Path, mock_ingestor: MagicMock, new: str
) -> None:
    # Negative: a Lua name is ASCII letters, digits and `_` (Lua 5.4 manual
    # section 3.1, digits being 0-9 alone); `isidentifier` and the Lua
    # grammar admit the CJK, accented and Arabic-Indic digit names the Lua
    # lexer refuses, so the rename was written as code Lua cannot load.
    _write(temp_repo, "mod.lua", _LUA)
    graph = _index(temp_repo, mock_ingestor)
    with pytest.raises(RenameRefused) as refused:
        _plan(temp_repo, graph, "mod.helper", new)
    assert str(refused.value) == cs.RENAME_BAD_NAME.format(name=new)


def test_an_ascii_lua_name_is_accepted(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write(temp_repo, "mod.lua", _LUA)
    graph = _index(temp_repo, mock_ingestor)
    report = _plan(temp_repo, graph, "mod.helper", "_fetch2")
    assert "local function _fetch2(a)" in report.diff, report.message
