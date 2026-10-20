"""Constant emission, its OF_TYPE pass and the incremental requeue (issue #1806).

The enumerator (`declared_constants`) decides WHAT a module declares; these
tests cover what happens next: the node and its DEFINES_CONSTANT edge are
written together behind the capture gate, the annotation is queued and
resolved to OF_TYPE after Pass 2, and an incremental run rebuilds the queue
for files it does not re-parse -- from this project's rows only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.constant_nodes import (
    PendingConstantType,
    emit_constant_type_edges,
    emit_declared_constants,
)
from evals.cgr_graph import _StatefulIngestor

_CONSTANT = cs.NodeLabel.CONSTANT.value
_DEFINES = cs.RelationshipType.DEFINES_CONSTANT.value
_OF_TYPE = cs.RelationshipType.OF_TYPE.value

_MODELS = "class Limit:\n    pass\n\n\nclass Other:\n    pass\n"
_API = (
    "from models import Limit\n"
    "\n"
    "MAX: Limit = Limit()\n"
    "RETRIES = 3\n"
    "timeout = 5\n"
    "__all__ = ['MAX']\n"
)
_BOTH = ["+constants", "+parameters"]


def _updater(
    root: Path,
    store: _StatefulIngestor,
    project: str,
    tokens: list[str] | None = None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
        capture=resolve_capture(_BOTH if tokens is None else tokens),
    )


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, src in files.items():
        (root / name).write_text(src, encoding="utf-8")


def _nodes(store: _StatefulIngestor) -> dict[str, dict]:
    return {
        str(props[cs.KEY_QUALIFIED_NAME]): props
        for (label, _uid), props in store.nodes.items()
        if label == _CONSTANT
    }


def _edges(store: _StatefulIngestor, rel: str) -> set[tuple[str, str]]:
    return {(str(src), str(tgt)) for _sl, src, r, _tl, tgt in store.edges if r == rel}


def _index(tmp_path: Path, tokens: list[str]) -> _StatefulIngestor:
    root = tmp_path / "proj"
    _write(root, {"models.py": _MODELS, "api.py": _API})
    store = _StatefulIngestor()
    _updater(root, store, "proj", tokens).run(force=True)
    return store


# --- Through a real index ------------------------------------------------------


def test_the_default_index_emits_no_constant_and_no_edge(tmp_path: Path) -> None:
    store = _index(tmp_path, [])
    assert _nodes(store) == {}
    assert _edges(store, _DEFINES) == set()


def test_a_module_defines_its_constants_with_their_type(tmp_path: Path) -> None:
    store = _index(tmp_path, _BOTH)
    nodes = _nodes(store)
    # `timeout` is lowercase and un-`Final`; `__all__` is a dunder.
    assert set(nodes) == {"proj.api.MAX", "proj.api.RETRIES"}
    assert nodes["proj.api.MAX"][cs.KEY_TYPE_NAME] == "Limit"
    assert nodes["proj.api.MAX"][cs.KEY_VALUE] == "Limit()"
    assert nodes["proj.api.MAX"][cs.KEY_PATH] == "api.py"
    assert nodes["proj.api.MAX"][cs.KEY_START_LINE] == 3
    assert cs.KEY_TYPE_NAME not in nodes["proj.api.RETRIES"]
    assert _edges(store, _DEFINES) == {
        ("proj.api", "proj.api.MAX"),
        ("proj.api", "proj.api.RETRIES"),
    }
    assert {e for e in _edges(store, _OF_TYPE) if e[0].startswith("proj.api.")} == {
        ("proj.api.MAX", "proj.models.Limit")
    }


def test_constants_alone_emit_no_type_edge(tmp_path: Path) -> None:
    """OF_TYPE belongs to the `parameters` group, so `constants` alone yields
    the nodes and DEFINES_CONSTANT only."""
    store = _index(tmp_path, ["+constants"])
    assert set(_nodes(store)) == {"proj.api.MAX", "proj.api.RETRIES"}
    assert _edges(store, _OF_TYPE) == set()


def test_an_unchanged_file_keeps_its_type_edge_when_the_type_file_changes(
    tmp_path: Path,
) -> None:
    """Re-parsing only the TYPE's file detaches the OF_TYPE into it; the
    requeue rebuilds the unchanged constant's fact from the graph."""
    root = tmp_path / "proj"
    # No import: `Limit` resolves by unique suffix, so api.py is NOT a
    # dependent of models.py and re-parsing models.py alone leaves it alone.
    _write(root, {"models.py": _MODELS, "api.py": "MAX: Limit = None\n"})
    store = _StatefulIngestor()
    _updater(root, store, "proj").run(force=True)
    assert ("proj.api.MAX", "proj.models.Limit") in _edges(store, _OF_TYPE)

    (root / "models.py").write_text(_MODELS + "\n\nEXTRA = 1\n", encoding="utf-8")
    updater = _updater(root, store, "proj")
    updater.run()

    assert "api.py" not in updater._reparsed_file_keys
    assert ("proj.api.MAX", "proj.models.Limit") in _edges(store, _OF_TYPE)
    assert "proj.models.EXTRA" in _nodes(store)


def test_a_changed_annotation_replaces_the_old_type_edge(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root, {"models.py": _MODELS, "api.py": _API})
    store = _StatefulIngestor()
    _updater(root, store, "proj").run(force=True)

    (root / "api.py").write_text(
        "from models import Other\n\nMAX: Other = Other()\n", encoding="utf-8"
    )
    _updater(root, store, "proj").run()

    got = {e for e in _edges(store, _OF_TYPE) if e[0] == "proj.api.MAX"}
    assert got == {("proj.api.MAX", "proj.models.Other")}
    assert _nodes(store)["proj.api.MAX"][cs.KEY_TYPE_NAME] == "Other"


def test_an_incremental_run_requeues_its_own_projects_constants_only(
    tmp_path: Path,
) -> None:
    """`svc.` selects `svc.v2`'s rows too (issue #1970). Requeued, the sibling's
    fact resolved against THIS project's registry and wrote an OF_TYPE from
    `svc.v2.api.MAX` to `svc.models.Limit` -- across projects, and never
    deleted by a later run of `svc.v2`."""
    store = _StatefulIngestor()
    for project in ("svc", "svc.v2"):
        root = tmp_path / project
        _write(root, {"models.py": _MODELS, "api.py": _API})
        _updater(root, store, project).run(force=True)
    before = _edges(store, _OF_TYPE)
    assert ("svc.v2.api.MAX", "svc.v2.models.Limit") in before

    root = tmp_path / "svc"
    (root / "caller.py").write_text("def caller():\n    return 1\n")
    updater = _updater(root, store, "svc")
    updater.run()

    after = _edges(store, _OF_TYPE)
    assert ("svc.v2.api.MAX", "svc.models.Limit") not in after
    assert ("svc.api.MAX", "svc.models.Limit") in after
    assert {e for e in after if e[0].startswith("svc.v2.")} == {
        e for e in before if e[0].startswith("svc.v2.")
    }


def test_the_requeue_reads_nothing_on_a_full_build(tmp_path: Path) -> None:
    store = _index(tmp_path, _BOTH)
    updater = _updater(tmp_path / "proj", store, "proj")
    updater._is_full_build = True
    updater._requeue_constant_types({cs.KEY_PROJECT_PREFIX: "proj."})
    assert updater.factory.definition_processor.pending_constant_types == []


def test_the_requeue_skips_a_malformed_row(tmp_path: Path) -> None:
    store = _index(tmp_path, _BOTH)
    updater = _updater(tmp_path / "proj", store, "proj")
    rows = [
        {cs.KEY_QUALIFIED_NAME: "proj.api.MAX", cs.KEY_TYPE_NAME: None},
        {
            cs.KEY_QUALIFIED_NAME: "proj.api.MAX",
            cs.KEY_TYPE_NAME: "Limit",
            cs.KEY_PATH: "api.py",
        },
    ]
    store.fetch_all = lambda _q, _p=None: rows  # type: ignore[method-assign]
    updater._requeue_constant_types({cs.KEY_PROJECT_PREFIX: "proj."})
    assert updater.factory.definition_processor.pending_constant_types == [
        PendingConstantType("proj.api.MAX", "proj.api", "Limit", "api.py")
    ]


# --- The emitters directly -----------------------------------------------------


class _Recorder:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.nodes: list[tuple[object, dict]] = []
        self.rels: list[tuple[object, object, object]] = []

    def rel_enabled(self, _rel: object) -> bool:
        return self.enabled

    def ensure_node_batch(self, label: object, props: dict) -> None:
        self.nodes.append((label, props))

    def ensure_relationship_batch(self, src: object, rel: object, tgt: object) -> None:
        self.rels.append((src, rel, tgt))


@pytest.fixture(scope="module")
def python_parser():  # noqa: ANN201
    parsers, _ = load_parsers()
    return parsers[cs.SupportedLanguage.PYTHON]


def _emit(parser, source: str, ingestor, sink, props=None) -> int:  # noqa: ANN001
    root = parser.parse(source.encode()).root_node
    return emit_declared_constants(
        ingestor,
        sink,
        "proj.m",
        root,
        cs.SupportedLanguage.PYTHON,
        {cs.KEY_PATH: "m.py", cs.KEY_ABSOLUTE_PATH: "/r/m.py"}
        if props is None
        else props,
    )


def test_a_repeated_declaration_is_one_node_with_the_last_row(python_parser) -> None:  # noqa: ANN001
    ingestor, sink = _Recorder(), []
    count = _emit(python_parser, "THING: A = A()\nTHING = 1\n", ingestor, sink)
    assert count == 1
    ((_label, props),) = ingestor.nodes
    # The last binding wins the WHOLE row: no `type_name` left over from the
    # first declaration beside the second one's value.
    assert props[cs.KEY_VALUE] == "1"
    assert cs.KEY_TYPE_NAME not in props
    assert len(ingestor.rels) == 1
    assert sink == []


def test_a_typed_constant_is_queued_and_its_edge_follows_the_node(
    python_parser,  # noqa: ANN001
) -> None:
    ingestor, sink = _Recorder(), []
    assert _emit(python_parser, "MAX: Limit = 1\n", ingestor, sink) == 1
    assert sink == [PendingConstantType("proj.m.MAX", "proj.m", "Limit", "m.py")]
    assert ingestor.rels == [
        (
            (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, "proj.m"),
            cs.RelationshipType.DEFINES_CONSTANT,
            (_CONSTANT, cs.KEY_QUALIFIED_NAME, "proj.m.MAX"),
        )
    ]


def test_a_disabled_gate_or_no_constant_emits_nothing(python_parser) -> None:  # noqa: ANN001
    off, sink = _Recorder(enabled=False), []
    assert _emit(python_parser, "MAX: Limit = 1\n", off, sink) == 0
    assert (off.nodes, off.rels, sink) == ([], [], [])
    on = _Recorder()
    assert _emit(python_parser, "lower = 1\n", on, sink) == 0
    assert on.nodes == []


def test_a_module_without_a_path_queues_nothing(python_parser) -> None:  # noqa: ANN001
    ingestor, sink = _Recorder(), []
    assert _emit(python_parser, "MAX: Limit = 1\n", ingestor, sink, props={}) == 1
    ((_label, props),) = ingestor.nodes
    assert cs.KEY_PATH not in props
    assert cs.KEY_ABSOLUTE_PATH not in props
    assert sink == []


class _Resolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._registry = {"proj.t.Limit": cs.NodeLabel.CLASS.value}

    def resolve_annotation(self, type_name: str, module_qn: str) -> list[str]:
        self.calls.append((type_name, module_qn))
        return ["proj.t.Limit"] if type_name == "Limit" else []


def test_type_edges_resolve_once_per_type_and_module_and_empty_the_queue() -> None:
    pending = [
        PendingConstantType("proj.m.A", "proj.m", "Limit", "m.py"),
        PendingConstantType("proj.m.B", "proj.m", "Limit", "m.py"),
        PendingConstantType("proj.m.C", "proj.m", "Unknown", "m.py"),
    ]
    resolver, ingestor = _Resolver(), _Recorder()
    emitted = emit_constant_type_edges(pending, resolver, ingestor)  # type: ignore[arg-type]
    assert emitted == 2
    assert resolver.calls == [("Limit", "proj.m"), ("Unknown", "proj.m")]
    assert {(src[2], tgt[2]) for src, _rel, tgt in ingestor.rels} == {
        ("proj.m.A", "proj.t.Limit"),
        ("proj.m.B", "proj.t.Limit"),
    }
    assert pending == []


def _emitted(parser, source: str) -> dict[str, tuple[object, object]]:  # noqa: ANN001
    ingestor = _Recorder()
    _emit(parser, source, ingestor, [])
    return {
        props[cs.KEY_NAME]: (props.get(cs.KEY_TYPE_NAME), props.get(cs.KEY_VALUE))
        for _label, props in ingestor.nodes
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("size: Final[int] = 5\n", {"size": ("int", "5")}),
        ("n: typing.Final[Widget] = w\n", {"n": ("Widget", "w")}),
        # A bare `Final` names no type: no OF_TYPE hunt for a class `Final`.
        ("limit: t.Final = 5\n", {"limit": (None, "5")}),
        # A prefix that is not a dotted run of identifiers is no `Final`.
        ("x: f(1).Final = 1\n", {}),
        ("MAX: f(1).Final = 1\n", {"MAX": ("f(1).Final", "1")}),
        ("MAX = MIN = 0\n", {"MAX": (None, "0"), "MIN": (None, "0")}),
        ("A, B = 1, 2\nobj.C = 3\nD += 1\n", {}),
        ("MAX: int\n", {"MAX": ("int", None)}),
        (f"BIG = '{'x' * (cs.CONSTANT_VALUE_MAX_CHARS + 1)}'\n", {"BIG": (None, None)}),
        ("if True:\n    NESTED = 1\n", {}),
    ],
)
def test_what_a_module_level_assignment_emits(
    python_parser,  # noqa: ANN001
    source: str,
    expected: dict[str, tuple[object, object]],
) -> None:
    assert _emitted(python_parser, source) == expected


def test_a_language_not_covered_yet_emits_nothing(python_parser) -> None:  # noqa: ANN001
    ingestor = _Recorder()
    root = python_parser.parse(b"MAX = 1\n").root_node
    count = emit_declared_constants(
        ingestor, [], "proj.m", root, cs.SupportedLanguage.GO, {cs.KEY_PATH: "m.go"}
    )
    assert (count, ingestor.nodes) == (0, [])
