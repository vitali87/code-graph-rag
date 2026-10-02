"""Issue #2769: a Dart call site spans its callee, so `rename` can find it.

tree-sitter-dart has no call-expression node: `dhelper(1)` is an identifier
followed by a `selector(argument_part)`, and the call processor uses that
selector as the call node. The edge's site therefore covered only `(1)`.
`rename` found no name inside it, took it for an aliased call and dropped it:
the plan listed the definition alone, and every applied rename of a Dart
symbol with callers was rolled back for the dangling caller.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.editing.rename import QueryFn, RenameRefused, rename
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

DART = """int dhelper(int x) {
  return x + 1;
}

int dcaller() {
  return dhelper(1);
}

class Box {
  int v = 0;

  int grow(int n) {
    v += n;
    return v;
  }

  int twice() {
    grow(2);
    return this.grow(3) + dhelper(4);
  }
}
"""

PY = "def helper(x):\n    return x\n\n\ndef caller():\n    return helper(1)\n"


def _fetch(graph: RecordedGraph) -> QueryFn:
    def fetch(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return graph.fetch_all(query, dict(params) if params is not None else None)

    return fetch


@pytest.fixture
def dart_repo(tmp_path: Path) -> tuple[Path, RecordedGraph]:
    root = tmp_path / "dartren"
    _write(root, "dmod.dart", DART)
    _write(root, "pymod.py", PY)
    return root, _index(root, MagicMock())


def _call_sites(graph: RecordedGraph, callee_suffix: str) -> set[tuple[int, int, int]]:
    return {
        (int(props["line"]), int(props["col"]), int(props["end_col"]))
        for _src, rel, dst, props in graph.edges
        if rel == "CALLS" and dst.endswith(callee_suffix) and "line" in props
    }


def test_a_dart_function_call_site_starts_at_the_callee(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    _root, graph = dart_repo

    assert _call_sites(graph, ".dmod.dhelper") == {(6, 9, 19), (19, 26, 36)}


def test_a_dart_method_call_site_starts_at_the_receiver_chain(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    _root, graph = dart_repo

    assert _call_sites(graph, ".dmod.Box.grow") == {(18, 4, 11), (19, 11, 23)}


@pytest.mark.parametrize(
    ("target", "calls"),
    [("dmod.dhelper", {6, 19}), ("dmod.Box.grow", {18, 19})],
)
def test_the_rename_plan_lists_every_dart_call_site(
    dart_repo: tuple[Path, RecordedGraph], target: str, calls: set[int]
) -> None:
    root, graph = dart_repo

    report = rename(
        root,
        _fetch(graph),
        graph.project,
        f"{graph.project}.{target}",
        "bump",
        dry_run=True,
    )

    assert {s.line for s in report.sites if s.kind == "call"} == calls
    assert report.unlocatable == ()


def test_an_applied_dart_rename_rewrites_the_callers(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = dart_repo

    report = rename(
        root, _fetch(graph), graph.project, f"{graph.project}.dmod.dhelper", "bump"
    )

    assert report.applied, report.message
    text = (root / "dmod.dart").read_text()
    assert "dhelper" not in text
    assert "return bump(1);" in text
    assert "this.grow(3) + bump(4);" in text


def test_a_call_site_that_names_nothing_is_unlocatable_not_dropped(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    # A span holding no name at all (the old Dart `(1)`) is a mislocated
    # site, not an alias: the plan must refuse rather than leave the caller.
    root, graph = dart_repo
    graph.edges = [
        (src, rel, dst, {**props, "col": 16} if props.get("line") == 6 else props)
        for src, rel, dst, props in graph.edges
    ]

    with pytest.raises(RenameRefused, match="1 graph-known site"):
        rename(
            root,
            _fetch(graph),
            graph.project,
            f"{graph.project}.dmod.dhelper",
            "bump",
            dry_run=True,
        )


# Negative: what must not change.


def test_a_python_call_site_keeps_its_span(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    _root, graph = dart_repo

    assert _call_sites(graph, ".pymod.helper") == {(6, 11, 20)}


def test_the_dart_definition_is_still_in_the_plan(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    root, graph = dart_repo

    report = rename(
        root,
        _fetch(graph),
        graph.project,
        f"{graph.project}.dmod.dhelper",
        "bump",
        dry_run=True,
    )

    assert [s for s in report.sites if s.kind == "definition" and s.line == 1]
