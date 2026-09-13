"""Parameter nodes through a real index (issue #1804).

Opt-in: the `parameters` capture group is not in the defaults, so the first
test is that the default index emits NOTHING for it -- a Parameter node with
no HAS_PARAMETER edge would be an orphan, and the gate has to hold both.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_SRC = {
    "models.py": "class Widget:\n    pass\n",
    "app.py": (
        "from .models import Widget\n"
        "\n"
        "def build(name: str, widget: Widget, *rest, flag: bool = False) -> int:\n"
        "    return 1\n"
        "\n"
        "class Factory:\n"
        "    def make(self, widget: Widget) -> Widget:\n"
        "        return widget\n"
    ),
}


def _index(tmp_path: Path, tokens: list[str]) -> _StatefulIngestor:
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    for name, src in _SRC.items():
        (repo / name).write_text(src)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run(force=True)
    return store


def _nodes(store: _StatefulIngestor, label: str) -> dict[str, dict]:
    return {
        str(props[cs.KEY_QUALIFIED_NAME]): props
        for (node_label, _uid), props in store.nodes.items()
        if node_label == label
    }


def _edges(store: _StatefulIngestor, rel: str) -> set[tuple[str, str]]:
    return {(str(src), str(tgt)) for _sl, src, r, _tl, tgt in store.edges if r == rel}


def test_the_default_index_emits_no_parameter_and_no_edge(tmp_path: Path) -> None:
    store = _index(tmp_path, [])
    assert _nodes(store, cs.NodeLabel.PARAMETER.value) == {}
    assert _edges(store, cs.RelationshipType.HAS_PARAMETER.value) == set()
    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == set()


def test_every_declared_parameter_becomes_a_node_in_order(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+parameters"])
    params = _nodes(store, cs.NodeLabel.PARAMETER.value)

    build = {qn: p for qn, p in params.items() if qn.startswith("proj.app.build.")}
    assert [(p[cs.KEY_NAME], p[cs.KEY_INDEX]) for _, p in sorted(build.items())] == [
        ("name", 0),
        ("widget", 1),
        ("rest", 2),
        ("flag", 3),
    ]
    assert build["proj.app.build.2"][cs.KEY_IS_VARIADIC] is True
    assert build["proj.app.build.3"][cs.KEY_HAS_DEFAULT] is True
    assert build["proj.app.build.1"][cs.KEY_TYPE_NAME] == "Widget"
    assert build["proj.app.build.0"][cs.KEY_PATH] == "app.py"

    # The method's `self` is the receiver, not a parameter.
    make = {qn for qn in params if qn.startswith("proj.app.Factory.make.")}
    assert make == {"proj.app.Factory.make.0"}


def test_has_parameter_links_the_owner_to_each_node(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+parameters"])
    has = _edges(store, cs.RelationshipType.HAS_PARAMETER.value)
    assert {t for s, t in has if s == "proj.app.build"} == {
        f"proj.app.build.{i}" for i in range(4)
    }
    assert ("proj.app.Factory.make", "proj.app.Factory.make.0") in has


def test_of_type_resolves_an_annotation_to_the_project_class(tmp_path: Path) -> None:
    """`widget: Widget` on both callables; `str`/`bool` are not project types."""
    store = _index(tmp_path, ["+parameters"])
    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    assert of_type == {
        ("proj.app.build.1", "proj.models.Widget"),
        ("proj.app.Factory.make.0", "proj.models.Widget"),
    }


def test_a_parameter_node_is_never_left_without_its_edge(tmp_path: Path) -> None:
    """Whatever the capture selection, no orphan: the gate that drops the
    edge must drop the node too. Checked under both selections."""
    for tokens in ([], ["+parameters"]):
        store = _index(tmp_path / ("on" if tokens else "off"), tokens)
        owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_PARAMETER.value)}
        assert set(_nodes(store, cs.NodeLabel.PARAMETER.value)) == owned
