# Issue #2547: an enum constant with arguments (`CLASS("c")`) runs the enum's
# constructor, and outside the enum's own constructors nothing else can: `new`
# on an enum type is a compile error (JLS 8.9). The indexer recorded no edge for
# the constant, so every parameterised enum constructor was reported by
# `cgr dead-code`, had no callers, and dropped whatever it calls from
# reachability. A constant now CALLS the constructor overload its argument count
# selects, from the scope whose class initialisation creates it.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict, ResultRow

PROJECT = "jenum"
MODULE_QN = f"{PROJECT}.com.acme.Joiner"
OUTER_QN = f"{MODULE_QN}.Joiner"

_DEAD_CODE_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.MODULE.value,
    }
)


def _source(temp_repo: Path) -> Path:
    return temp_repo / PROJECT / "com" / "acme" / "Joiner.java"


def _index(temp_repo: Path, mock_ingestor: MagicMock, body: str) -> GraphUpdater:
    source = _source(temp_repo)
    source.parent.mkdir(parents=True)
    source.write_text(
        f"package com.acme;\n\npublic final class Joiner {{\n{body}}}\n",
        encoding="utf-8",
    )
    return create_and_run_updater(
        temp_repo / PROJECT, mock_ingestor, skip_if_missing="java"
    )


def _calls(mock_ingestor: MagicMock) -> dict[tuple[str, str, str], PropertyDict]:
    edges: dict[tuple[str, str, str], PropertyDict] = {}
    for c in mock_ingestor.ensure_relationship_batch.call_args_list:
        if c.args[1] != cs.RelationshipType.CALLS:
            continue
        props = c.kwargs.get("properties") or {}
        edges[(str(c.args[0][0]), str(c.args[0][2]), str(c.args[2][2]))] = props
    return edges


def _callers_of(mock_ingestor: MagicMock, callee_qn: str) -> set[tuple[str, str]]:
    return {
        (label, caller)
        for label, caller, callee in _calls(mock_ingestor)
        if callee == callee_qn
    }


def _dead(mock_ingestor: MagicMock) -> set[str]:
    # The dead-code engine over the graph the indexer recorded, fetched the
    # way the two Cypher reads return it, so the verdict is the one
    # `cgr dead-code` would print for this source.
    nodes: list[ResultRow] = []
    for c in mock_ingestor.ensure_node_batch.call_args_list:
        label, props = str(c.args[0]), c.args[1]
        if label not in _DEAD_CODE_LABELS:
            continue
        nodes.append(
            {
                cs.KEY_LABEL: label,
                cs.KEY_QUALIFIED_NAME: props.get(cs.KEY_QUALIFIED_NAME),
                cs.KEY_NAME: props.get(cs.KEY_NAME),
                cs.KEY_PATH: props.get(cs.KEY_PATH),
                cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                cs.KEY_DECORATORS: props.get(cs.KEY_DECORATORS) or [],
            }
        )
    rels: list[ResultRow] = [
        {
            cs.KEY_FROM_LABEL: str(c.args[0][0]),
            cs.KEY_FROM_QN: str(c.args[0][2]),
            cs.KEY_REL_TYPE: str(c.args[1]),
            cs.KEY_TO_LABEL: str(c.args[2][0]),
            cs.KEY_TO_QN: str(c.args[2][2]),
            cs.KEY_RESOLUTION: (c.kwargs.get("properties") or {}).get(
                cs.KEY_RESOLUTION
            ),
        }
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if str(c.args[0][0]) in _DEAD_CODE_LABELS
        and c.args[0][1] == cs.KEY_QUALIFIED_NAME
    ]
    graph = MagicMock()
    graph.fetch_all = MagicMock(
        side_effect=lambda query, params=None: (
            nodes
            if query == cq.CYPHER_DEAD_CODE_NODES
            else rels
            if query == cq.CYPHER_DEAD_CODE_RELS
            else []
        )
    )
    config = default_dead_code_config(include_tests=True, include_classes=False)
    return {
        str(row[cs.KEY_QUALIFIED_NAME])
        for row in collect_dead_code(graph, PROJECT, config)
    }


_KIND = (
    "  enum Kind {\n"
    '    CLASS("c"), IFACE("i");\n'
    "    private final String tag;\n"
    "    Kind(String tag) { this.tag = tag; }\n"
    "  }\n"
    "\n"
    "  public static String tagOf(Kind k) { return k.tag; }\n"
)
_KIND_CTOR = f"{OUTER_QN}.Kind.Kind(String)"


def test_enum_constant_arguments_call_the_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's repro: the file has no call expression at all, only the
    # constants, and each of them runs Kind(String).
    _index(temp_repo, mock_ingestor, _KIND)

    calls = _calls(mock_ingestor)
    edge = calls.get((cs.NodeLabel.MODULE.value, MODULE_QN, _KIND_CTOR))
    assert edge is not None, sorted(calls)
    # One declared constructor takes one argument: the binding is certain.
    assert edge[cs.KEY_RESOLUTION] == cs.EdgeResolution.EXACT
    assert edge[cs.KEY_LINE] == 5
    assert edge[cs.KEY_ARG_COUNT] == 1


def test_constant_edges_do_not_depend_on_recorded_definition_locations(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A call pass over a file whose definitions the run did not re-record
    # (incremental) derives the constructor's qn the way the class pass does.
    updater = _index(temp_repo, mock_ingestor, _KIND)
    call_processor = updater.factory.call_processor
    for key in [k for k in call_processor.function_locations if k[0] == MODULE_QN]:
        del call_processor.function_locations[key]
    mock_ingestor.reset_mock()
    root = updater._ast_for(_source(temp_repo))
    assert root is not None

    call_processor.process_calls_in_file(
        _source(temp_repo), root, cs.SupportedLanguage.JAVA, updater.queries
    )

    assert _callers_of(mock_ingestor, _KIND_CTOR) == {
        (cs.NodeLabel.MODULE.value, MODULE_QN)
    }


def test_enum_constructor_is_not_reported_dead(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, _KIND)

    assert _KIND_CTOR not in _dead(mock_ingestor)


def test_enum_constant_picks_the_constructor_overload_by_argument_count(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # NONE runs Op(), ONE runs Op(String): the comment is not an argument. No
    # constant passes two arguments, so Op(String,int) never runs and stays dead.
    _index(
        temp_repo,
        mock_ingestor,
        "  enum Op {\n"
        '    NONE, ONE(/* sym */ "a");\n'
        "    Op() { }\n"
        "    Op(String sym) { }\n"
        "    Op(String sym, int prec) { }\n"
        "  }\n",
    )

    calls = _calls(mock_ingestor)
    module = cs.NodeLabel.MODULE.value
    none_edge = calls.get((module, MODULE_QN, f"{OUTER_QN}.Op.Op()"))
    one_edge = calls.get((module, MODULE_QN, f"{OUTER_QN}.Op.Op(String)"))
    assert none_edge is not None, sorted(calls)
    assert one_edge is not None, sorted(calls)
    assert none_edge[cs.KEY_LINE] == one_edge[cs.KEY_LINE] == 5
    assert none_edge[cs.KEY_COL] < one_edge[cs.KEY_COL]
    assert (none_edge[cs.KEY_ARG_COUNT], one_edge[cs.KEY_ARG_COUNT]) == (0, 1)
    assert not _callers_of(mock_ingestor, f"{OUTER_QN}.Op.Op(String,int)")

    dead = _dead(mock_ingestor)
    assert f"{OUTER_QN}.Op.Op()" not in dead
    assert f"{OUTER_QN}.Op.Op(String)" not in dead
    assert f"{OUTER_QN}.Op.Op(String,int)" in dead


def test_same_arity_constructor_overloads_all_take_an_overload_edge(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Argument count alone cannot tell Op(String) from Op(int); like `new X(..)`
    # the constant reaches both, labelled `overload` so --min-resolution exact
    # can drop the guess.
    _index(
        temp_repo,
        mock_ingestor,
        '  enum Op {\n    ONE("a");\n    Op(String sym) { }\n    Op(int n) { }\n  }\n',
    )

    calls = _calls(mock_ingestor)
    module = cs.NodeLabel.MODULE.value
    for ctor in ("Op(String)", "Op(int)"):
        edge = calls.get((module, MODULE_QN, f"{OUTER_QN}.Op.{ctor}"))
        assert edge is not None, sorted(calls)
        assert edge[cs.KEY_RESOLUTION] == cs.EdgeResolution.OVERLOAD


def test_varargs_enum_constructor_accepts_any_argument_count(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A variable-arity constructor is applicable to any count from its fixed
    # parameters up (JLS 15.12.2.1): A passes none, B passes two.
    _index(
        temp_repo,
        mock_ingestor,
        "  enum Mode {\n"
        '    A, B("x", "y");\n'
        "    Mode(String... tags) { }\n"
        "    Mode(int a, int b, int c) { }\n"
        "  }\n",
    )

    varargs_ctor = f"{OUTER_QN}.Mode.Mode(String...)"
    assert _callers_of(mock_ingestor, varargs_ctor) == {
        (cs.NodeLabel.MODULE.value, MODULE_QN)
    }
    assert not _callers_of(mock_ingestor, f"{OUTER_QN}.Mode.Mode(int,int,int)")
    assert varargs_ctor not in _dead(mock_ingestor)


def test_enum_constant_with_a_class_body_still_calls_the_constructor(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `PLUS("+") { ... }` is an anonymous subclass whose implicit constructor
    # passes the arguments on to Op(String); `MINUS { ... }` passes none.
    _index(
        temp_repo,
        mock_ingestor,
        "  enum Op {\n"
        '    PLUS("+") { int apply(int a, int b) { return add(a, b); } },\n'
        "    MINUS { int apply(int a, int b) { return a - b; } };\n"
        "    Op() { }\n"
        "    Op(String sym) { }\n"
        "    abstract int apply(int a, int b);\n"
        "  }\n"
        "\n"
        "  static int add(int a, int b) { return a + b; }\n",
    )

    module = (cs.NodeLabel.MODULE.value, MODULE_QN)
    assert module in _callers_of(mock_ingestor, f"{OUTER_QN}.Op.Op(String)")
    assert module in _callers_of(mock_ingestor, f"{OUTER_QN}.Op.Op()")
    # The constant's body keeps its own methods and their own calls.
    add_callers = _callers_of(mock_ingestor, f"{OUTER_QN}.add(int,int)")
    assert any(
        label == cs.NodeLabel.METHOD.value and caller.startswith(f"{OUTER_QN}.Op.apply")
        for label, caller in add_callers
    ), add_callers


def test_local_enum_constants_are_attributed_to_the_enclosing_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A local enum initialises when its method first uses it, so the method,
    # not the module, runs the constructor: if run() were dead, so would be
    # Color(int).
    _index(
        temp_repo,
        mock_ingestor,
        "  static int run() {\n"
        "    enum Color {\n"
        "      RED(1), GREEN(2);\n"
        "      final int v;\n"
        "      Color(int v) { this.v = v; }\n"
        "    }\n"
        "    return Color.RED.v;\n"
        "  }\n",
    )

    assert _callers_of(mock_ingestor, f"{OUTER_QN}.run.Color.Color(int)") == {
        (cs.NodeLabel.METHOD.value, f"{OUTER_QN}.run()")
    }


def test_only_the_enums_own_constructors_are_targeted(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A nested class's constructor with the same arity is not run by the
    # enum's constants, and neither is a plain class's constructor nobody
    # calls: both stay dead.
    _index(
        temp_repo,
        mock_ingestor,
        "  enum Kind {\n"
        '    CLASS("c");\n'
        "    Kind(String tag) { }\n"
        "    static final class Helper { Helper(String s) { } }\n"
        "  }\n"
        "\n"
        "  static final class Widget { Widget(String s) { } }\n",
    )

    helper_ctor = f"{OUTER_QN}.Kind.Helper.Helper(String)"
    widget_ctor = f"{OUTER_QN}.Widget.Widget(String)"
    assert not _callers_of(mock_ingestor, helper_ctor)
    assert not _callers_of(mock_ingestor, widget_ctor)
    dead = _dead(mock_ingestor)
    assert helper_ctor in dead
    assert widget_ctor in dead
    assert f"{OUTER_QN}.Kind.Kind(String)" not in dead


def test_enum_without_a_declared_constructor_emits_no_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The implicit constructor has no node to call; nothing is invented.
    _index(temp_repo, mock_ingestor, "  enum Flag { ON, OFF }\n")

    assert not [
        callee
        for _, _, callee in _calls(mock_ingestor)
        if callee.startswith(f"{OUTER_QN}.Flag")
    ]
