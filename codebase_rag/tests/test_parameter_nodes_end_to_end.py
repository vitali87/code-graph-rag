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
    "models.py": "class Widget:\n    pass\n\nclass Gadget:\n    pass\n",
    # No import: `Gadget` resolves by unique suffix. That makes consumer.py NOT
    # a dependent of models.py, so a re-parse of models.py alone never
    # re-parses it -- the one shape in which OF_TYPE has to be rebuilt from
    # the graph rather than re-emitted by ingest.
    "consumer.py": "def use(gadget: Gadget) -> int:\n    return 0\n",
    # A second module with its OWN Widget: the deferred pass memoises resolution
    # per (annotation, module), and this is the fixture that can tell a
    # per-module key from a global one.
    "other.py": (
        "class Widget:\n    pass\n\ndef use(widget: Widget) -> int:\n    return 0\n"
    ),
    "app.py": (
        "from .models import Widget\n"
        "\n"
        "def build(name: str, widget: Widget, *rest, flag: bool = False) -> int:\n"
        "    return 1\n"
        "\n"
        "class Factory:\n"
        "    def make(self, widget: Widget) -> Widget:\n"
        "        return widget\n"
        "    @staticmethod\n"
        "    def static(self, value: int) -> int:\n"
        "        return value\n"
        "\n"
        "def callback(self, value: int) -> int:\n"
        "    return value\n"
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
        ("proj.other.use.0", "proj.other.Widget"),
        ("proj.consumer.use.0", "proj.models.Gadget"),
    }


def test_a_parameter_node_is_never_left_without_its_edge(tmp_path: Path) -> None:
    """Whatever the capture selection, no orphan: the gate that drops the
    edge must drop the node too. Checked under both selections."""
    for tokens in ([], ["+parameters"]):
        store = _index(tmp_path / ("on" if tokens else "off"), tokens)
        owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_PARAMETER.value)}
        assert set(_nodes(store, cs.NodeLabel.PARAMETER.value)) == owned


def test_index_offsets_against_param_types_as_documented(tmp_path: Path) -> None:
    """The owner keeps the receiver in `param_types`; a Parameter does not.

    So on a method `param_types[index + 1]` is this parameter's annotation and
    on a function `param_types[index]` is. The docstring says exactly that;
    this pins it against real nodes (local review P1: an earlier docstring
    claimed the two agreed).
    """
    store = _index(tmp_path, ["+parameters"])
    owners = {
        str(p[cs.KEY_QUALIFIED_NAME]): p
        for (label, _uid), p in store.nodes.items()
        if label in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
    }
    params = _nodes(store, cs.NodeLabel.PARAMETER.value)
    make = params["proj.app.Factory.make.0"]
    build = params["proj.app.build.1"]
    assert owners["proj.app.Factory.make"][cs.KEY_PARAM_TYPES][0] == ""
    assert (
        owners["proj.app.Factory.make"][cs.KEY_PARAM_TYPES][make[cs.KEY_INDEX] + 1]
        == make[cs.KEY_TYPE_NAME]
        == "Widget"
    )
    assert (
        owners["proj.app.build"][cs.KEY_PARAM_TYPES][build[cs.KEY_INDEX]]
        == build[cs.KEY_TYPE_NAME]
        == "Widget"
    )


def _reindex(store: _StatefulIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["+parameters"]),
    ).run(force=False)


def test_a_reparse_takes_stale_parameters_with_their_owner(tmp_path: Path) -> None:
    """Drop a parameter and delete a function; nothing of theirs survives.

    `CYPHER_DELETE_MODULE` walks what the module DEFINES; a Parameter hangs
    off a Function by HAS_PARAMETER, which was outside the walk, so a removed
    parameter or a deleted function left its nodes with no owner (local
    review P1, the shape of #1828 with the opposite remedy).
    """
    store = _index(tmp_path, ["+parameters"])
    repo = tmp_path / "proj"
    (repo / "app.py").write_text(
        "from .models import Widget\n\n"
        "def build(name: str, widget: Widget) -> int:\n    return 1\n"
    )
    _reindex(store, repo)

    params = set(_nodes(store, cs.NodeLabel.PARAMETER.value))
    owned = {t for _s, t in _edges(store, cs.RelationshipType.HAS_PARAMETER.value)}
    assert params == owned, params - owned
    assert {qn for qn in params if qn.startswith("proj.app.")} == {
        "proj.app.build.0",
        "proj.app.build.1",
    }


def test_of_type_survives_a_reparse_of_only_the_type_file(tmp_path: Path) -> None:
    """Touch models.py alone: app.py is not re-parsed, so its Parameter nodes
    are not re-emitted and their OF_TYPE has to be rebuilt from the graph,
    the way RETURNS/ACCEPTS are (local review P1)."""
    store = _index(tmp_path, ["+parameters"])
    repo = tmp_path / "proj"
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    _reindex(store, repo)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    # app.py imports models, so it is a dependent and is re-parsed: its edges
    # come back through ingest. consumer.py is not, and is the real test.
    assert ("proj.app.build.1", "proj.models.Widget") in of_type, of_type
    assert ("proj.consumer.use.0", "proj.models.Gadget") in of_type, of_type


def test_an_explicit_self_survives_where_no_receiver_is_implied(tmp_path: Path) -> None:
    """A module-level `def callback(self, value)` and a `@staticmethod` both
    declare `self` explicitly; only an instance/class method has an implicit
    receiver. Classified at the call site, not by the name."""
    store = _index(tmp_path, ["+parameters"])
    params = _nodes(store, cs.NodeLabel.PARAMETER.value)
    assert {
        qn: p[cs.KEY_NAME]
        for qn, p in params.items()
        if qn.startswith("proj.app.callback.")
    } == {
        "proj.app.callback.0": "self",
        "proj.app.callback.1": "value",
    }
    assert {
        qn: p[cs.KEY_NAME]
        for qn, p in params.items()
        if qn.startswith("proj.app.Factory.static.")
    } == {
        "proj.app.Factory.static.0": "self",
        "proj.app.Factory.static.1": "value",
    }
    # ...and the instance method still drops its receiver.
    assert {qn for qn in params if qn.startswith("proj.app.Factory.make.")} == {
        "proj.app.Factory.make.0"
    }


def test_of_type_survives_on_a_reused_updater(tmp_path: Path) -> None:
    """Second `run()` on the SAME updater: its registry already holds every
    unchanged definition, so keying the requeue on registry membership skipped
    them all (CodeRabbit). Keyed on the file being re-parsed instead."""
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
        capture=resolve_capture(["+parameters"]),
    )
    updater.run(force=True)
    (repo / "models.py").write_text(_SRC["models.py"] + "# touched\n")
    updater.run(force=False)

    of_type = _edges(store, cs.RelationshipType.OF_TYPE.value)
    assert ("proj.consumer.use.0", "proj.models.Gadget") in of_type, of_type


def test_scoped_reingest_drops_the_old_annotation(tmp_path: Path) -> None:
    """`def f(x: Old)` -> `def f(x: New)` through `reingest`: the scoped
    prologue rehydrates the OLD annotation from the graph before the delete,
    so without filtering, OF_TYPE went to both (Greptile, executed)."""
    repo = tmp_path / "proj"
    repo.mkdir(parents=True)
    (repo / "__init__.py").touch()
    app = repo / "app.py"
    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\ndef f(x: Old) -> int:\n    return 0\n"
    )
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    capture = resolve_capture(["+parameters"])
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run(force=True)
    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.f.0", "proj.app.Old")
    }

    app.write_text(
        "class Old:\n    pass\n\nclass New:\n    pass\n\ndef f(x: New) -> int:\n    return 0\n"
    )
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).reingest([app])

    assert _edges(store, cs.RelationshipType.OF_TYPE.value) == {
        ("proj.app.f.0", "proj.app.New")
    }


def test_the_deferred_pass_runs_for_parameter_facts_alone(tmp_path: Path) -> None:
    """`emit_type_edges` used to return early when no RETURNS/ACCEPTS fact was
    queued, BEFORE the parameter pass -- a Parameter fact on its own was
    skipped and OF_TYPE silently absent. Today every annotated parameter also
    queues an ACCEPTS fact, so no ingest path reaches this; the guard is pinned
    directly (a peer's Field pass, which queues nothing else, hit it)."""
    from codebase_rag.parsers.parameter_nodes import PendingParameterType

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
        capture=resolve_capture(["+parameters"]),
    )
    updater.run(force=True)
    processor = updater.factory.definition_processor
    assert not processor.pending_type_facts and not processor.pending_parameter_types

    processor.pending_parameter_types.append(
        PendingParameterType("proj.app.build.1", "proj.app", "Widget")
    )
    emitted = processor.emit_type_edges()

    assert emitted == 1
    assert processor.pending_parameter_types == []
