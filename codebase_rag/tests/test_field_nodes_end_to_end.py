"""Field nodes through a real index (issue #1805).

Opt-in: the `fields` capture group is not in the defaults, so the first test
is that the default index emits NOTHING for it -- a Field node with no
HAS_FIELD edge would be an orphan, and the gate has to hold both. The shape
mirrors the Parameter tests (#1804) because the plumbing is the same, with
the declaring type as owner.
"""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_parameter_nodes_end_to_end import _edges, _nodes
from evals.cgr_graph import _StatefulIngestor

_SRC = {
    "models.py": "class Widget:\n    pass\n\nclass Gadget:\n    pass\n",
    # No import: `Gadget` resolves by unique suffix, so consumer.py is NOT a
    # dependent of models.py and a re-parse of models.py alone never re-parses
    # it -- the one shape in which OF_TYPE must be rebuilt from the graph.
    "consumer.py": "class Holder:\n    gadget: Gadget\n",
    "app.py": (
        "from .models import Widget\n"
        "\n"
        "class Box:\n"
        "    size: int = 1\n"
        "    widget: Widget\n"
        "    __slots__ = ('a',)\n"
        "\n"
        "    def __init__(self):\n"
        "        self.count = 0\n"
        "\n"
        "    def method(self):\n"
        "        pass\n"
    ),
    # A second language, so the emitter is proven to run off the enumerator
    # dispatch and not off a Python-only path.
    "Shape.java": "class Shape {\n  private static final int SIDES = 4;\n  String name;\n  void m() {}\n}\n",
    # The point of the exercise: 32% of Rust `///` blocks precede a field.
    "geo.rs": "pub struct Point {\n    /// Horizontal offset.\n    pub x: i32,\n    pub y: i32,\n}\n",
    # Owners other than Class: an Interface and an Enum both declare fields.
    "shapes.ts": "interface I {\n  a: string;\n}\n",
    "Colour.java": "enum Colour {\n  RED;\n  private final int code = 1;\n}\n",
    # A field and a method with one name share the `<owner>.<name>` key.
    "acc.py": "class Acc:\n    total = 0\n\n    def total(self):\n        return 0\n",
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


def test_the_default_index_emits_no_field_and_no_edge(tmp_path: Path) -> None:
    store = _index(tmp_path, [])
    assert _nodes(store, cs.NodeLabel.FIELD.value) == {}
    assert _edges(store, cs.RelationshipType.HAS_FIELD.value) == set()


def test_every_declared_field_becomes_a_node(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+fields"])
    fields = _nodes(store, cs.NodeLabel.FIELD.value)
    box = {qn.rsplit(".", 1)[-1]: p for qn, p in fields.items() if ".Box." in qn}
    assert set(box) == {"size", "widget", "a", "count"}, sorted(fields)
    assert (
        box["size"][cs.KEY_TYPE_NAME] == "int" and box["size"][cs.KEY_IS_STATIC] is True
    )
    assert box["widget"][cs.KEY_TYPE_NAME] == "Widget"
    assert (
        cs.KEY_TYPE_NAME not in box["count"] and box["count"][cs.KEY_IS_STATIC] is False
    )
    # Position is the NAME's, 1-based line: `size` on line 4, column 4.
    assert (box["size"][cs.KEY_START_LINE], box["size"][cs.KEY_START_COL]) == (4, 4)
    # Path and absolute_path come from the owner's props, as for Parameter.
    assert box["size"][cs.KEY_PATH].endswith("app.py")
    assert Path(box["size"][cs.KEY_ABSOLUTE_PATH]).is_absolute()
    # Methods are not fields.
    assert "method" not in box


def test_a_second_language_emits_through_the_same_path(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+fields"])
    fields = _nodes(store, cs.NodeLabel.FIELD.value)
    shape = {qn.rsplit(".", 1)[-1]: p for qn, p in fields.items() if ".Shape." in qn}
    assert set(shape) == {"SIDES", "name"}, sorted(fields)
    assert shape["SIDES"][cs.KEY_MODIFIERS] == ["private", "static", "final"]
    assert shape["SIDES"][cs.KEY_IS_STATIC] is True
    assert shape["name"][cs.KEY_TYPE_NAME] == "String"


def test_has_field_links_the_owner_to_each_node(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+fields"])
    has = _edges(store, cs.RelationshipType.HAS_FIELD.value)
    owners = {src for src, _t in has}
    assert any(o.endswith(".app.Box") for o in owners), owners
    assert any(o.endswith(".Shape") for o in owners), owners
    # Every Field node is the target of exactly one HAS_FIELD from its owner.
    assert {t for _s, t in has} == set(_nodes(store, cs.NodeLabel.FIELD.value))
    for src, tgt in has:
        assert tgt.startswith(src + ".")


def test_of_type_resolves_a_field_annotation_to_the_project_class(
    tmp_path: Path,
) -> None:
    store = _index(tmp_path, ["+fields"])
    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    widget_edges = {(s, t) for s, t in of_type if s.endswith(".app.Box.widget")}
    assert len(widget_edges) == 1, of_type
    assert next(iter(widget_edges))[1].endswith(".models.Widget")
    # `int` is not a project class: no OF_TYPE for `size`.
    assert not any(s.endswith(".app.Box.size") for s, _t in of_type)


def test_a_field_node_is_never_left_without_its_edge(tmp_path: Path) -> None:
    """Whatever the capture selection, no orphan: the gate that drops the edge
    must drop the node with it."""
    for tokens in ([], ["+fields"]):
        store = _index(tmp_path / ("on" if tokens else "off"), tokens)
        owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_FIELD.value)}
        assert set(_nodes(store, cs.NodeLabel.FIELD.value)) == owned


def _reindex(store: _StatefulIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+fields"]),
    ).run(force=False)


def test_a_reparse_takes_stale_fields_with_their_owner(tmp_path: Path) -> None:
    """Drop a field and delete a class; nothing of theirs survives.

    `CYPHER_DELETE_MODULE` walks what the module DEFINES; a Field hangs off
    its owner by HAS_FIELD, which had to join that walk or a removed field or a
    deleted class left its nodes with no owner -- the Parameter shape (#1804).
    """
    store = _index(tmp_path, ["+fields"])
    repo = tmp_path / "proj"
    (repo / "app.py").write_text(
        "from .models import Widget\n\nclass Box:\n    widget: Widget\n"
    )
    (repo / "Shape.java").write_text("class Other {\n  void m() {}\n}\n")
    _reindex(store, repo)

    fields = set(_nodes(store, cs.NodeLabel.FIELD.value))
    owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_FIELD.value)}
    assert fields == owned, fields - owned
    assert {qn for qn in fields if ".Box." in qn} == {"proj.app.Box.widget"}, fields
    assert not any(".Shape." in qn for qn in fields), fields


def test_of_type_survives_a_reparse_of_only_the_type_file(tmp_path: Path) -> None:
    """Touch models.py alone: consumer.py is not re-parsed, so its Field node
    is not re-emitted and its OF_TYPE has to be rebuilt from the graph."""
    store = _index(tmp_path, ["+fields"])
    repo = tmp_path / "proj"
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    _reindex(store, repo)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    # app.py imports models, so it is a dependent and is re-parsed: its edge
    # comes back through ingest. consumer.py is not, and is the real test.
    assert ("proj.app.Box.widget", "proj.models.Widget") in of_type, of_type
    assert ("proj.consumer.Holder.gadget", "proj.models.Gadget") in of_type, of_type


def test_of_type_survives_on_a_reused_updater(tmp_path: Path) -> None:
    """Second `run()` on the SAME updater: its registry already holds every
    unchanged definition, so a requeue keyed on registry membership skipped
    them all (#1804's CodeRabbit finding). Keyed on the file being re-parsed."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    for name, src in _SRC.items():
        (repo / name).write_text(src)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+fields"]),
    )
    updater.run(force=True)
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    # Rename a typed field on the same updater: the OLD fact for `Box.widget`
    # must not be re-emitted from a queue that was never emptied.
    (repo / "app.py").write_text(
        _SRC["app.py"].replace("widget: Widget", "gizmo: Widget")
    )
    updater.run(force=False)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    assert ("proj.consumer.Holder.gadget", "proj.models.Gadget") in of_type, of_type
    assert ("proj.app.Box.gizmo", "proj.models.Widget") in of_type, of_type
    # Every OF_TYPE source must be a live node: the field queue is emptied after
    # each run like the parameter one, or a reused updater re-emits an edge
    # from a Field that no longer exists (local review P1).
    live = set(_nodes(store, cs.NodeLabel.FIELD.value)) | set(
        _nodes(store, cs.NodeLabel.PARAMETER.value)
    )
    assert {s for s, _t in of_type} <= live, {s for s, _t in of_type} - live


def test_scoped_reingest_drops_the_old_field_annotation(tmp_path: Path) -> None:
    """`x: Old` -> `x: New` through `reingest`: the scoped prologue rehydrates
    the OLD annotation from the graph before the delete, so without the
    stale-module filter OF_TYPE went to both (#1804's Greptile finding)."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    app = repo / "app.py"
    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\nclass Box:\n    x: Old\n"
    )
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    capture = resolve_capture(["+fields"])
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run(force=True)
    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.Box.x", "proj.app.Old")
    }

    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\nclass Box:\n    x: New\n"
    )
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).reingest([app])

    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.Box.x", "proj.app.New")
    }


def test_scoped_reingest_keeps_a_colliding_modules_field_facts(tmp_path: Path) -> None:
    """`foo.py` and `foo/__init__.py` both derive `proj.foo` from their paths.
    Re-ingesting `foo.py` with the type's file detaches `foo/__init__.py`'s
    OF_TYPE (its target is recreated); the rehydrated facts for that UNCHANGED
    file must survive the stale filter, which keys on the file and not on the
    module qn (#1891 round 3, #1892)."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    (repo / "models.py").write_text("class Gadget:\n    pass\n")
    (repo / "foo.py").write_text("class Holder:\n    gadget: Gadget\n")
    (repo / "foo").mkdir()
    (repo / "foo" / "__init__.py").write_text("class Other:\n    gadget: Gadget\n")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    capture = resolve_capture(["+fields"])
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run(force=True)

    # Same-stem siblings get distinct module qns in the graph (one is renamed,
    # per #1569), so match by the owner's NAME; the stale filter derives its
    # module qn from the path, where both files give the same one.
    def of_type_for(owner: str) -> set[str]:
        return {
            tgt
            for src, tgt in _edges(store, cs.RelationshipType.OF_TYPE.value)
            if src.endswith(f".{owner}.gadget")
        }

    assert of_type_for("Other") == {"proj.models.Gadget"}

    (repo / "models.py").write_text("class Gadget:\n    pass\n# touched\n")
    (repo / "foo.py").write_text("class Holder:\n    gadget: Gadget\n    n = 2\n")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).reingest([repo / "models.py", repo / "foo.py"])

    assert of_type_for("Other") == {"proj.models.Gadget"}, (
        "the unchanged colliding file's OF_TYPE was not rebuilt"
    )
    assert of_type_for("Holder") == {"proj.models.Gadget"}


def test_a_documented_field_carries_its_doc_comment(tmp_path: Path) -> None:
    """The docstring goes through the same extractor definitions use (#1888),
    pointed at the declaring node; an undocumented sibling has no property."""
    store = _index(tmp_path, ["+fields"])
    fields = _nodes(store, cs.NodeLabel.FIELD.value)
    point = {qn.rsplit(".", 1)[-1]: p for qn, p in fields.items() if ".Point." in qn}
    assert set(point) == {"x", "y"}, sorted(fields)
    assert point["x"][cs.KEY_DOCSTRING] == "Horizontal offset."
    assert cs.KEY_DOCSTRING not in point["y"]
    # Python has no field-docstring convention: absent, not empty.
    box = {qn.rsplit(".", 1)[-1]: p for qn, p in fields.items() if ".Box." in qn}
    assert all(cs.KEY_DOCSTRING not in p for p in box.values())


def test_interface_and_enum_owners_declare_fields(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+fields"])
    fields = _nodes(store, cs.NodeLabel.FIELD.value)
    assert any(qn.endswith(".I.a") for qn in fields), sorted(fields)
    assert any(qn.endswith(".Colour.code") for qn in fields), sorted(fields)
    has = _edges(store, cs.RelationshipType.HAS_FIELD.value)
    assert any(src.endswith(".I") and tgt.endswith(".I.a") for src, tgt in has), has


def test_a_field_and_a_method_may_share_a_qualified_name(tmp_path: Path) -> None:
    """Both exist under `Acc.total`; the label-less definition lookup must
    therefore exclude Field (and Parameter) rows, which carry no `end`."""
    store = _index(tmp_path, ["+fields"])
    labels = {
        label
        for (label, _uid), props in store.nodes.items()
        if str(props.get(cs.KEY_QUALIFIED_NAME, "")).endswith(".Acc.total")
    }
    assert labels == {cs.NodeLabel.FIELD.value, cs.NodeLabel.METHOD.value}, labels
    from codebase_rag.cypher_queries import CYPHER_FIND_BY_QUALIFIED_NAME

    assert "NOT n:Field" in CYPHER_FIND_BY_QUALIFIED_NAME
    assert "NOT n:Parameter" in CYPHER_FIND_BY_QUALIFIED_NAME
