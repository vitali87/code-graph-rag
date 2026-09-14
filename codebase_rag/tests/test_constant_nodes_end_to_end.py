"""Constant nodes through a real index (issue #1806).

Opt-in: the `constants` capture group is not in the defaults, so the first test
is that the default index emits NOTHING for it -- a Constant node with no
DEFINES_CONSTANT edge would be an orphan, and the gate has to hold both. The
shape mirrors the Field tests (#1805) because the plumbing is the same, with
the Module as owner.

`OF_TYPE` is one relationship and lives in the `parameters` group (the capture
contract puts every relationship in exactly one group), so the tests that
assert a constant's type edge enable both groups; `constants` alone yields the
nodes and DEFINES_CONSTANT.
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
    "consumer.py": "SPARE: Gadget = make()\n",
    "app.py": (
        "from .models import Widget\n"
        "from typing import Final\n"
        "\n"
        "MAX_SIZE: int = 10\n"
        "NAME = 'widget'\n"
        "DEFAULT: Widget = Widget()\n"
        "timeout: Final = 30\n"
        "logger = object()\n"
        "__all__ = ['MAX_SIZE']\n"
        "\n"
        "class Box:\n"
        "    INNER = 1\n"
        "\n"
        "def f():\n"
        "    LOCAL = 2\n"
        "    return LOCAL\n"
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


def test_the_default_index_emits_no_constant_and_no_edge(tmp_path: Path) -> None:
    """Opt-in: nothing for Constant unless the group is asked for.

    Both assertions below pass just as well if the index produced NOTHING --
    a broken fixture, a parser that never loaded -- so an empty result would
    read as suppression working (a peer session found 11 of its own absence
    assertions in this shape). The control pins that the index really ran:
    the same source yields Modules and a Class either way, and those are not
    gated by any capture group.
    """
    store = _index(tmp_path, [])
    assert _nodes(store, cs.NodeLabel.CONSTANT.value) == {}
    assert _edges(store, cs.RelationshipType.DEFINES_CONSTANT.value) == set()
    # The control: absence is only evidence if presence was possible.
    assert _nodes(store, cs.NodeLabel.MODULE.value), "the index produced nothing"
    assert _nodes(store, cs.NodeLabel.CLASS.value), "the index produced nothing"


def test_the_constant_label_is_owned_by_its_capture_group() -> None:
    """The label must be REGISTERED to the group, not merely absent from it.

    `_node_labels_for` treats a label no group claims as always enabled, so
    dropping `NodeLabel.CONSTANT` from `CAPTURE_GROUP_NODE_LABELS` does not
    disable it -- it enables it unconditionally, while `DEFINES_CONSTANT`
    stays off with the group. The default index would then be free to emit a
    node whose edge is disabled.

    The tests above cannot see that: they observe the emitter, which is gated
    separately, so they stay green through exactly this misregistration (a
    peer session hit the same shape on a shared label set, where every
    set-equality test stayed green through a dropped label). This one drives
    the capture resolution itself, which is where the defect would live.
    """
    default = resolve_capture([])
    assert cs.NodeLabel.CONSTANT not in default.enabled_node_labels
    assert cs.RelationshipType.DEFINES_CONSTANT not in default.enabled_rels

    opted_in = resolve_capture(["+constants"])
    assert cs.NodeLabel.CONSTANT in opted_in.enabled_node_labels
    assert cs.RelationshipType.DEFINES_CONSTANT in opted_in.enabled_rels


def test_every_module_level_constant_becomes_a_node(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+constants"])
    constants = _nodes(store, cs.NodeLabel.CONSTANT.value)
    app = {qn.rsplit(".", 1)[-1]: p for qn, p in constants.items() if ".app." in qn}
    assert set(app) == {"MAX_SIZE", "NAME", "DEFAULT", "timeout"}, sorted(constants)
    assert app["MAX_SIZE"][cs.KEY_TYPE_NAME] == "int"
    assert app["MAX_SIZE"][cs.KEY_VALUE] == "10"
    assert app["NAME"][cs.KEY_VALUE] == "'widget'"
    assert cs.KEY_TYPE_NAME not in app["NAME"]
    # `Final` bare: a constant, but it names no type.
    assert cs.KEY_TYPE_NAME not in app["timeout"]
    # Position is the NAME's, 1-based line: `MAX_SIZE` on line 4, column 0.
    assert (app["MAX_SIZE"][cs.KEY_START_LINE], app["MAX_SIZE"][cs.KEY_START_COL]) == (
        4,
        0,
    )
    # Path and absolute_path come from the module's props.
    assert app["MAX_SIZE"][cs.KEY_PATH].endswith("app.py")
    assert Path(app["MAX_SIZE"][cs.KEY_ABSOLUTE_PATH]).is_absolute()


def test_the_scope_and_naming_rules_hold_through_a_real_index(
    tmp_path: Path,
) -> None:
    """The negatives, through the whole pipeline rather than the enumerator.

    `logger` is lowercase, `__all__` is a dunder, `Box.INNER` is a class
    member (already a Field), and `f.LOCAL` is a function local.

    The Java half of the scope rule -- a `static final` is a Field and must
    not ALSO become a Constant -- is pinned by the enumerator dispatch test
    rather than here: a `.java` fixture makes conftest's autouse grammar
    guard skip the whole test on a base install, which is how this file came
    to skip entirely on its first run.
    """
    store = _index(tmp_path, ["+constants"])
    names = {qn.rsplit(".", 1)[-1] for qn in _nodes(store, cs.NodeLabel.CONSTANT.value)}
    assert "logger" not in names
    assert "__all__" not in names
    assert "INNER" not in names, "a class member is a Field, not a Constant"
    assert "LOCAL" not in names, "a function local is not a module constant"


def test_defines_constant_links_the_module_to_each_node(tmp_path: Path) -> None:
    store = _index(tmp_path, ["+constants"])
    defines = _edges(store, cs.RelationshipType.DEFINES_CONSTANT.value)
    assert any(src.endswith(".app") for src, _t in defines), defines
    # Every Constant node is the target of exactly one edge from its module.
    assert {t for _s, t in defines} == set(_nodes(store, cs.NodeLabel.CONSTANT.value))
    for src, tgt in defines:
        assert tgt.startswith(src + ".")


def test_of_type_resolves_a_constant_annotation_to_the_project_class(
    tmp_path: Path,
) -> None:
    store = _index(tmp_path, ["+constants", "+parameters"])
    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    default_edges = {(s, t) for s, t in of_type if s.endswith(".app.DEFAULT")}
    assert len(default_edges) == 1, of_type
    assert next(iter(default_edges))[1].endswith(".models.Widget")
    # `int` is not a project class: no OF_TYPE for MAX_SIZE.
    assert not any(s.endswith(".app.MAX_SIZE") for s, _t in of_type)


def test_a_constant_node_is_never_left_without_its_edge(tmp_path: Path) -> None:
    """Whatever the capture selection, no orphan: the gate that drops the edge
    must drop the node with it."""
    for tokens in ([], ["+constants"]):
        store = _index(tmp_path / ("on" if tokens else "off"), tokens)
        owned = {
            t for _s, t in _edges(store, cs.RelationshipType.DEFINES_CONSTANT.value)
        }
        assert set(_nodes(store, cs.NodeLabel.CONSTANT.value)) == owned


def _reindex(store: _StatefulIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+constants", "+parameters"]),
    ).run(force=False)


def test_a_reparse_takes_stale_constants_with_their_module(tmp_path: Path) -> None:
    """Drop a constant; nothing of it survives.

    `CYPHER_DELETE_MODULE` walks what the module DEFINES; a Constant hangs off
    it by DEFINES_CONSTANT, which had to join that walk or a removed constant
    left its node orphaned -- the Parameter shape (#1804) and the Field one.
    """
    store = _index(tmp_path, ["+constants"])
    repo = tmp_path / "proj"
    (repo / "app.py").write_text(
        "from .models import Widget\n\nDEFAULT: Widget = Widget()\n"
    )
    _reindex(store, repo)

    constants = set(_nodes(store, cs.NodeLabel.CONSTANT.value))
    owned = {t for _s, t in _edges(store, cs.RelationshipType.DEFINES_CONSTANT.value)}
    assert constants == owned, constants - owned
    assert {qn for qn in constants if ".app." in qn} == {"proj.app.DEFAULT"}, constants


def test_of_type_survives_a_reparse_of_only_the_type_file(tmp_path: Path) -> None:
    """Touch models.py alone: consumer.py is not re-parsed, so its Constant is
    not re-emitted and its OF_TYPE has to be rebuilt from the graph."""
    store = _index(tmp_path, ["+constants", "+parameters"])
    repo = tmp_path / "proj"
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    _reindex(store, repo)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    # app.py imports models, so it is a dependent and is re-parsed: its edge
    # comes back through ingest. consumer.py is not, and is the real test.
    assert ("proj.app.DEFAULT", "proj.models.Widget") in of_type, of_type
    assert ("proj.consumer.SPARE", "proj.models.Gadget") in of_type, of_type


def test_of_type_survives_on_a_reused_updater(tmp_path: Path) -> None:
    """Second `run()` on the SAME updater: its registry already holds every
    unchanged definition, so a requeue keyed on registry membership skipped
    them all (#1804's CodeRabbit finding). Keyed on the file being re-parsed.

    A constant is RENAMED between the runs, so the queue-emptying assertion
    below can observe a stale fact; without the rename, re-emitting an old
    edge would land on a node that still exists and look correct.
    """
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
        capture=resolve_capture(["+constants", "+parameters"]),
    )
    updater.run(force=True)
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    (repo / "app.py").write_text(
        _SRC["app.py"].replace("DEFAULT: Widget", "FALLBACK: Widget")
    )
    updater.run(force=False)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    assert ("proj.consumer.SPARE", "proj.models.Gadget") in of_type, of_type
    assert ("proj.app.FALLBACK", "proj.models.Widget") in of_type, of_type
    # Every OF_TYPE source must be a live node: the constant queue is emptied
    # after each run like the sibling ones, or a reused updater re-emits an
    # edge from a Constant that no longer exists (#1899's local review P1).
    live = (
        set(_nodes(store, cs.NodeLabel.CONSTANT.value))
        | set(_nodes(store, cs.NodeLabel.PARAMETER.value))
        | set(_nodes(store, cs.NodeLabel.FIELD.value))
    )
    sources = {s for s, _t in of_type}
    assert sources <= live, sources - live


def test_scoped_reingest_drops_the_old_constant_annotation(tmp_path: Path) -> None:
    """`X: Old` -> `X: New` through `reingest`: the scoped prologue rehydrates
    the OLD annotation from the graph before the delete, so without the
    stale-file filter OF_TYPE went to both (#1804's Greptile finding)."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    app = repo / "app.py"
    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\nTHING: Old = None\n"
    )
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    capture = resolve_capture(["+constants", "+parameters"])
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run(force=True)
    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.THING", "proj.app.Old")
    }

    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\nTHING: New = None\n"
    )
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).reingest([app])

    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.THING", "proj.app.New")
    }
