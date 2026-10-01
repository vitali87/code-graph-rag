"""Issue #2393: reading `Point.row` / `Point.column` must not free the int.

py-tree-sitter 0.26.0's `Point.row` and `Point.column` getters return
`PyTuple_GetItem(self, i)`, a BORROWED reference, where a getter must return
a new one. Every read therefore drops a reference the tuple still owns. Ints
below 257 are immortal in CPython 3.12, so small files never notice; a row
or column of 257 or more frees the int while the Point still holds it, and
the heap corruption surfaced as a SIGSEGV indexing spf13/cobra (fixed
upstream after 0.26.0, not yet released).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tree_sitter import Node, Point

import codebase_rag
from codebase_rag import constants as cs
from codebase_rag import tree_sitter_point
from codebase_rag.config import settings
from codebase_rag.parser_loader import load_parsers

FAR = 300


def _far_node() -> Node:
    parsers, _queries = load_parsers()
    parser = parsers[cs.SupportedLanguage.PYTHON]
    source = "x = 0\n" * FAR + " " * FAR + "y = 1\n"
    tree = parser.parse(source.encode())
    node = tree.root_node.children[-1]
    assert node.start_point[0] >= 257
    assert node.start_point[1] >= 257
    return node


def test_reading_a_far_row_keeps_the_int_alive() -> None:
    point = _far_node().start_point
    row = point[0]
    before = sys.getrefcount(row)

    point.row  # noqa: B018

    assert sys.getrefcount(row) == before


def test_reading_a_far_column_keeps_the_int_alive() -> None:
    point = _far_node().start_point
    column = point[1]
    before = sys.getrefcount(column)

    point.column  # noqa: B018

    assert sys.getrefcount(column) == before


def test_row_and_column_still_read_the_point() -> None:
    # Negative: the accessors answer exactly what the tuple holds, near and far.
    node = _far_node()
    # No `Point(r, c)` here: 0.26.0's constructor also mishandles the type's
    # refcount, and cgr never builds one.
    for point in (node.start_point, node.end_point, node.children[0].start_point):
        assert (point.row, point.column) == (point[0], point[1])
    assert isinstance(node.start_point, tuple)


def test_importing_the_package_installs_the_accessors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cgr pytest plugin imports `codebase_rag` before any test runs, so
    # the accessors are already replaced here. Take them away and forget the
    # module, then import the package again: the package import alone must
    # put them back, or a caller that never names the module stays exposed.
    monkeypatch.delattr(Point, "row")
    monkeypatch.delattr(Point, "column")
    monkeypatch.delitem(sys.modules, tree_sitter_point.__name__)
    monkeypatch.delattr(codebase_rag, "tree_sitter_point")
    monkeypatch.delattr(codebase_rag, "_tree_sitter_point")
    assert not hasattr(Point, "row")

    importlib.reload(codebase_rag)

    assert isinstance(Point.__dict__["row"], property)
    assert isinstance(Point.__dict__["column"], property)
    point = _far_node().start_point
    row = point[0]
    before = sys.getrefcount(row)
    assert (point.row, point.column) == (point[0], point[1])
    assert sys.getrefcount(row) == before


def test_install_makes_row_and_column_read_the_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StalePoint(tuple):
        # A tuple Point whose getters are still the binding's own.
        @property
        def row(self) -> str:
            return "stale"

        @property
        def column(self) -> str:
            return "stale"

    monkeypatch.setattr(tree_sitter_point, "Point", StalePoint)

    tree_sitter_point.install()

    point = StalePoint((FAR, FAR + 1))
    assert (point.row, point.column) == (FAR, FAR + 1)


def test_install_leaves_a_point_that_is_not_a_tuple_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: a Point that is not a tuple has no items to read, so its own
    # accessors are the only correct ones.
    class OpaquePoint:
        row = "own row"
        column = "own column"

    monkeypatch.setattr(tree_sitter_point, "Point", OpaquePoint)

    tree_sitter_point.install()

    assert OpaquePoint.__dict__["row"] == "own row"
    assert OpaquePoint.__dict__["column"] == "own column"


def test_install_gives_up_quietly_on_an_immutable_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: a binding that rebuilt Point as an immutable type is one that
    # fixed the getters, and importing cgr must not fail on it.
    attempts: list[str] = []

    class ImmutableType(type):
        # What CPython raises on assigning to a static or immutable type.
        def __setattr__(cls, name: str, value: property) -> None:
            attempts.append(name)
            raise TypeError(name)

    class ImmutablePoint(tuple, metaclass=ImmutableType):
        pass

    monkeypatch.setattr(tree_sitter_point, "Point", ImmutablePoint)

    tree_sitter_point.install()

    assert attempts == ["row"]
    assert "row" not in ImmutablePoint.__dict__
    assert "column" not in ImmutablePoint.__dict__


def _go_file_with_a_shadowed_type_past_row_257() -> str:
    # A package type and a function-local twin register as two variants, so
    # every `Local{...}` literal goes through the scope choice that reads each
    # literal's point; the literals sit past row 257, where the ints are
    # heap-allocated and the borrowed reference frees them.
    lines = ["package demo", "", "type Local struct{ n int }", ""]
    lines += [f"func pad{i}() int {{ return {i} }}" for i in range(FAR)]
    lines += [
        "",
        "func shadow() Local {",
        "\ttype Local struct{ m int }",
        "\t_ = Local{m: 1}",
        "\treturn Local{}",
        "}",
        "",
    ]
    lines += [f"func use{i}() Local {{ return Local{{n: {i}}} }}" for i in range(200)]
    return "\n".join(lines) + "\n"


def test_a_large_go_file_indexes_without_crashing(tmp_path: Path) -> None:
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "demo.go").write_text(_go_file_with_a_shadowed_type_past_row_257())
    script = (
        "from pathlib import Path\n"
        "from codebase_rag.graph_updater import GraphUpdater\n"
        "from codebase_rag.parser_loader import load_parsers\n"
        "from evals.cgr_graph import _CapturingIngestor\n"
        "parsers, queries = load_parsers()\n"
        f"GraphUpdater(ingestor=_CapturingIngestor(), repo_path=Path({str(repo)!r}),"
        " parsers=parsers, queries=queries, project_name='demo').run(force=True)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
        errors="replace",
        env={
            **os.environ,
            "GO_FRONTEND": "treesitter",
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        },
        check=False,
        timeout=600,
    )

    assert result.returncode == 0, result.stderr[-2000:]


def test_a_files_type_declarations_are_walked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every composite literal of a shadowed type asked for the declaration
    # scopes, and each ask walked the whole file: O(literals x file size),
    # which is also what made the call pass allocate enough to expose the
    # binding bug. Rows stay below 257 here so the old code cannot crash.
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parsers import call_processor as cp
    from evals.cgr_graph import _CapturingIngestor

    parsers, queries = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    lines = ["package demo", "", "type Local struct{ n int }", ""]
    lines += ["func shadow() {", "\ttype Local struct{ m int }", "\t_ = Local{}", "}"]
    lines += [f"func use{i}() Local {{ return Local{{n: {i}}} }}" for i in range(50)]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "demo.go").write_text("\n".join(lines) + "\n")
    walks: list[object] = []
    walk = cp._go_type_declarations_by_name

    def _counting(root: Node) -> dict[str, list[tuple[int, cp._Span | None]]]:
        walks.append(root)
        return walk(root)

    monkeypatch.setattr(cp, "_go_type_declarations_by_name", _counting)
    monkeypatch.setattr(settings, "GO_FRONTEND", cs.GoFrontend.TREESITTER)

    GraphUpdater(
        ingestor=_CapturingIngestor(),
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="demo",
    ).run(force=True)

    assert len(walks) == 1
