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

# A callee whose arguments name the same symbol, and a repeated selector
# (bot review on PR #2782): each site must start at its own callee name.
CHAIN = """int grow(int n) {
  return n;
}

class Box {
  Box grow(int n) {
    return this;
  }
}

int f(int x) {
  return x;
}

class Other {
  int f(int x) {
    return x;
  }
}

void use(Box box, Other other) {
  box.grow(grow(1));
  box.grow(1).grow(2);
  f(other.f(1));
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
    _write(root, "chain.dart", CHAIN)
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


def test_a_dart_method_call_site_starts_at_the_method_name(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    _root, graph = dart_repo

    assert _call_sites(graph, ".dmod.Box.grow") == {(18, 4, 11), (19, 16, 23)}


def test_a_repeated_dart_selector_keeps_a_site_per_call(
    dart_repo: tuple[Path, RecordedGraph],
) -> None:
    _root, graph = dart_repo

    assert _call_sites(graph, ".chain.Box.grow") == {
        (22, 6, 19),
        (23, 6, 13),
        (23, 14, 21),
    }


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


@pytest.mark.parametrize(
    ("target", "new", "lines"),
    [
        (
            "chain.Box.grow",
            "expand",
            ["  box.expand(grow(1));", "  box.expand(1).expand(2);"],
        ),
        ("chain.f", "g", ["  g(other.f(1));"]),
    ],
    ids=["method-named-in-its-argument", "function-named-in-its-argument"],
)
def test_an_applied_dart_rename_rewrites_the_callee_not_its_arguments(
    dart_repo: tuple[Path, RecordedGraph], target: str, new: str, lines: list[str]
) -> None:
    root, graph = dart_repo

    report = rename(
        root,
        _fetch(graph),
        graph.project,
        f"{graph.project}.{target}",
        new,
        allow_heuristic=True,
    )

    assert report.applied, report.message
    text = (root / "chain.dart").read_text().splitlines()
    for line in lines:
        assert line in text, text


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
