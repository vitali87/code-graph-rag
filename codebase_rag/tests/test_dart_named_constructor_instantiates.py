"""A Dart named-constructor call (`Box.of(1)`) records INSTANTIATES on the
class beside the CALLS edge to the constructor; a static factory method
that merely returns the class does not (issue #2012)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

SOURCE = """\
class Box<T> {
  Box(T value);
  Box.of(T value);
  const Box.fixed(T value);
  factory Box.build(T value) => Box(value);
  static Box<int> make() => Box(0);
}

void named() {
  final a = Box.of(1);
}

void constNamed() {
  final b = Box.fixed(1);
}

void factoryNamed() {
  final c = Box.build(1);
}

void staticFactory() {
  final d = Box.make();
}
"""

MODULE = "proj.lib.box"


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
        if kind in ("CALLS", "INSTANTIATES") and str(source).startswith(MODULE + ".")
    }


@pytest.mark.parametrize(
    ("caller", "ctor"),
    [("named", "of"), ("constNamed", "fixed"), ("factoryNamed", "build")],
)
def test_a_named_constructor_call_instantiates_the_class(
    edges: set[tuple[str, str, str]], caller: str, ctor: str
) -> None:
    source = f"{MODULE}.{caller}"
    assert (source, "CALLS", f"{MODULE}.Box.{ctor}") in edges, sorted(edges)
    assert (source, "INSTANTIATES", f"{MODULE}.Box") in edges, sorted(edges)


def test_a_static_factory_method_does_not_instantiate(
    edges: set[tuple[str, str, str]],
) -> None:
    source = f"{MODULE}.staticFactory"
    assert (source, "CALLS", f"{MODULE}.Box.make") in edges, sorted(edges)
    assert (source, "INSTANTIATES", f"{MODULE}.Box") not in edges, sorted(edges)


# After the local review: the constructor set is per-file state, so a
# re-parse that turns the named constructor into a static factory must not
# leave a stale qn stamping INSTANTIATES; and twin classes take the
# `overload` stamp the class branch gives a two-candidate construction.
V1 = "class Box {\n  Box(int v);\n  Box.of(int v);\n}\n\nvoid caller() {\n  final a = Box.of(1);\n}\n"
V2 = (
    "class Box {\n  Box(int v);\n  static Box of(int v) => Box(v);\n}\n\n"
    "void caller() {\n  final a = Box.of(1);\n}\n"
)
TWINS = (
    "class Box {\n  Box(int v);\n  Box.of(int v);\n}\n\n"
    "class Box {\n  Box(int v);\n  Box.of(int v);\n}\n\n"
    "void viaNamed() {\n  final a = Box.of(1);\n}\n"
)


def _index(root: Path, source: str) -> tuple[GraphUpdater, _StatefulIngestor]:
    (root / "lib").mkdir(parents=True, exist_ok=True)
    (root / "lib" / "box.dart").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    )
    updater.run()
    return updater, store


def test_a_reparsed_file_forgets_a_constructor_that_became_a_factory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    updater, store = _index(root, V1)
    (root / "lib" / "box.dart").write_text(V2, encoding="utf-8")
    store.edges.clear()
    updater.reingest([root / "lib" / "box.dart"])
    assert (
        "proj.lib.box.Box.of" not in updater.factory.type_inference.dart_constructor_qns
    )
    assert ("proj.lib.box.caller", "INSTANTIATES", "proj.lib.box.Box") not in {
        (str(s), k, str(t)) for _sl, s, k, _tl, t in store.edges
    }


def test_twin_classes_take_the_overload_stamp(tmp_path: Path) -> None:
    _updater, store = _index(tmp_path / "proj", TWINS)
    stamps = {
        str(t): props.get(cs.KEY_RESOLUTION)
        for (_sl, s, k, _tl, t), props in store.edge_props.items()
        if k == "INSTANTIATES" and str(s) == "proj.lib.box.viaNamed"
    }
    assert len(stamps) == 2, stamps
    assert set(stamps.values()) == {cs.EdgeResolution.OVERLOAD.value}, stamps
