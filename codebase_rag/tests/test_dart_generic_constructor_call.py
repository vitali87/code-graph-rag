"""A Dart constructor call with explicit type arguments, or with `new` or
`const`, takes INSTANTIATES plus the constructor CALLS the bare `Box(1)`
form takes (issue #2010)."""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SOURCE = """\
class Box<T> {
  Box(T value);
  Box.of(T value);
  String describe() => 'box';
  int width = 1;
}

class Pair<K, V> {
  Pair(K key, V value);
}

void helperFn() {}

void genericBare() {
  final a = Box<int>(1);
}

void genericNew() {
  final b = new Box<int>(1);
}

void plainNew() {
  final c = new Box(1);
}

void genericConst() {
  final d = const Box<int>(1);
}

void genericNamed() {
  final e = Box<int>.of(1);
}

void newNamed() {
  final f = new Box.of(1);
}

void twoTypeArgs() {
  final g = Pair<int, String>(1, 'a');
}

void nestedTypeArg() {
  final h = Box<List<int>>([1]);
}

void newChained() {
  new Box<int>(1).describe();
}

void newTearoff() {
  final r = new Box(helperFn);
}

void genericTearoff() {
  final r = Box<Function>(helperFn);
}

int genericRead() {
  return Box<int>(1).width;
}

void namedStatement() {
  Box<int>.of(1);
}

bool compare(int lo, int hi) {
  return lo < hi;
}
"""

MODULE = "proj.lib.box"
EDGE_KINDS = ("CALLS", "INSTANTIATES", "REFERENCES")


@pytest.fixture(scope="module")
def edges(tmp_path_factory: pytest.TempPathFactory) -> set[tuple[str, str, str]]:
    root = tmp_path_factory.mktemp("dart") / "proj"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "box.dart").write_text(SOURCE, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.DART not in parsers:
        # A module-scoped fixture runs before the per-test grammar skip
        # hook is installed, so a base install must skip here.
        pytest.skip("dart parser not available")
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    return {
        (str(source), kind, str(target))
        for _sl, source, kind, _tl, target in store.edges
        if kind in EDGE_KINDS and str(source).startswith(MODULE + ".")
    }


@pytest.mark.parametrize(
    "caller",
    ["genericBare", "genericNew", "plainNew", "genericConst", "genericRead"],
)
def test_a_construction_takes_instantiates_and_the_constructor_call(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    source = f"{MODULE}.{caller}"
    assert (source, "INSTANTIATES", f"{MODULE}.Box") in edges, sorted(edges)
    assert (source, "CALLS", f"{MODULE}.Box.Box") in edges, sorted(edges)


@pytest.mark.parametrize("caller", ["genericNamed", "newNamed", "namedStatement"])
def test_a_named_construction_calls_the_named_constructor(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert (f"{MODULE}.{caller}", "CALLS", f"{MODULE}.Box.of") in edges, sorted(edges)


def test_several_or_nested_type_arguments_still_construct(
    edges: set[tuple[str, str, str]],
) -> None:
    assert (f"{MODULE}.twoTypeArgs", "INSTANTIATES", f"{MODULE}.Pair") in edges
    assert (f"{MODULE}.twoTypeArgs", "CALLS", f"{MODULE}.Pair.Pair") in edges
    assert (f"{MODULE}.nestedTypeArg", "INSTANTIATES", f"{MODULE}.Box") in edges
    assert (f"{MODULE}.nestedTypeArg", "CALLS", f"{MODULE}.Box.Box") in edges


def test_a_construction_receiver_types_the_chained_call(
    edges: set[tuple[str, str, str]],
) -> None:
    assert (f"{MODULE}.newChained", "CALLS", f"{MODULE}.Box.describe") in edges, sorted(
        edges
    )


@pytest.mark.parametrize("caller", ["newTearoff", "genericTearoff"])
def test_a_tear_off_passed_to_a_construction_is_referenced(
    edges: set[tuple[str, str, str]], caller: str
) -> None:
    assert (f"{MODULE}.{caller}", "REFERENCES", f"{MODULE}.helperFn") in edges, sorted(
        edges
    )


def test_a_comparison_is_not_a_call(edges: set[tuple[str, str, str]]) -> None:
    assert not {edge for edge in edges if edge[0] == f"{MODULE}.compare"}
