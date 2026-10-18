# `cgr graph implementors|overrides --depth N` / MCP `implementors` and
# `overrides` with `depth` (issue #2624). Both followed ONE INHERITS /
# IMPLEMENTS / OVERRIDES edge, so `implementors Graph` stopped at DiGraph and
# MultiGraph and never reached `MultiDiGraph(MultiGraph, DiGraph)`, and
# `implementors IValidator` answered only `AbstractValidator`, the one class
# every validator implements the interface through. The fake graph below is
# those two hierarchies, networkx- and FluentValidation-shaped, plus the
# cases the walk must refuse: a cycle, and another project's type sitting
# between this project's types.
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"
# A registered project whose name extends this one: its rows start with
# `proj.` and pass every prefix filter, so only ownership can drop them.
EXTRA = "proj.extra"
# A project the Cypher prefix filter itself keeps out.
OTHER = "other"

GRAPH = f"{P}.classes.graph.Graph"
DIGRAPH = f"{P}.classes.digraph.DiGraph"
MULTIGRAPH = f"{P}.classes.multigraph.MultiGraph"
MULTIDIGRAPH = f"{P}.classes.multidigraph.MultiDiGraph"
MYGRAPH = f"{P}.classes.tests.test_special.MyGraph"
DEEP = f"{P}.algorithms.deep.DeepGraph"
IVALIDATOR = f"{P}.src.FluentValidation.IValidator.IValidator"
ABSTRACT = f"{P}.src.FluentValidation.AbstractValidator.AbstractValidator"
INLINE = f"{P}.src.FluentValidation.InlineValidator.InlineValidator"
PERSON = f"{P}.src.Validators.PersonValidator"
TESTV = f"{P}.src.Tests.TestValidator"
CYC_A = f"{P}.cyc.A"
CYC_B = f"{P}.cyc.B"
SUB = f"{EXTRA}.app.Sub"
LEAF = f"{P}.via_foreign.Leaf"
FAR = f"{OTHER}.app.Far"

LABELS = {
    IVALIDATOR: "Interface",
}

# (sub qn, base qn, relationship)
INHERITS: list[tuple[str, str, str]] = [
    (DIGRAPH, GRAPH, "INHERITS"),
    (MULTIGRAPH, GRAPH, "INHERITS"),
    (MYGRAPH, GRAPH, "INHERITS"),
    # The diamond: `class MultiDiGraph(MultiGraph, DiGraph)`.
    (MULTIDIGRAPH, MULTIGRAPH, "INHERITS"),
    (MULTIDIGRAPH, DIGRAPH, "INHERITS"),
    (DEEP, MULTIDIGRAPH, "INHERITS"),
    (ABSTRACT, IVALIDATOR, "IMPLEMENTS"),
    (INLINE, ABSTRACT, "INHERITS"),
    (PERSON, ABSTRACT, "INHERITS"),
    (TESTV, INLINE, "INHERITS"),
    # A cycle the graph can hold when two same-named bases resolve to each
    # other; the walk must end and never list the start as its own subtype.
    (CYC_A, CYC_B, "INHERITS"),
    (CYC_B, CYC_A, "INHERITS"),
    # The extending project subclasses this project's Graph, and this
    # project subclasses THAT: Leaf is reachable only through a foreign hop.
    (SUB, GRAPH, "INHERITS"),
    (LEAF, SUB, "INHERITS"),
    (FAR, GRAPH, "INHERITS"),
]

# (overrider qn, overridden qn)
OVERRIDES: list[tuple[str, str]] = [
    (f"{DIGRAPH}.add_edge", f"{GRAPH}.add_edge"),
    (f"{MULTIGRAPH}.add_edge", f"{GRAPH}.add_edge"),
    # MRO-first parent only, as the indexer records it.
    (f"{MULTIDIGRAPH}.add_edge", f"{MULTIGRAPH}.add_edge"),
    (f"{DEEP}.add_edge", f"{MULTIDIGRAPH}.add_edge"),
    (f"{CYC_A}.run", f"{CYC_B}.run"),
    (f"{CYC_B}.run", f"{CYC_A}.run"),
    (f"{SUB}.add_edge", f"{GRAPH}.add_edge"),
    (f"{LEAF}.add_edge", f"{SUB}.add_edge"),
]


def _path(qn: str) -> str:
    return qn.removeprefix(f"{P}.").replace(".", "/") + ".cs"


def _label(qn: str) -> str:
    if qn in LABELS:
        return LABELS[qn]
    return "Method" if qn.rsplit(".", 1)[-1][0].islower() else "Class"


def _node_row(qn: str, rel: str) -> ResultRow:
    return {
        cs.KEY_LABEL: _label(qn),
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_PATH: _path(qn),
        cs.KEY_REL_TYPE: rel,
    }


def _fetch_for(
    inherits: list[tuple[str, str, str]] = INHERITS,
    overrides: list[tuple[str, str]] = OVERRIDES,
    budget: int = 100,
    log: list[tuple[str, PropertyDict]] | None = None,
) -> Callable[[str, PropertyDict | None], list[ResultRow]]:
    """The graph answering exactly the hierarchy reads, each with the
    project-prefix filter its Cypher applies; anything else is refused, and
    a walk that never ends fails past `budget` calls instead of hanging."""
    calls = 0
    prefix = f"{P}."

    def fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        nonlocal calls
        calls += 1
        assert calls <= budget, "the walk did not terminate"
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in (P, EXTRA, OTHER)]
        p = params or {}
        assert p.get(cs.KEY_PROJECT_PREFIX) == prefix, "every read is scoped"
        if log is not None:
            log.append((query, p))
        out: list[ResultRow] = []
        if query == cq.CYPHER_GRAPH_IMPLEMENTORS:
            out = [
                _node_row(sub, rel)
                for sub, base, rel in inherits
                if base == p[cs.KEY_QN] and sub.startswith(prefix)
            ]
        elif query == cq.CYPHER_GRAPH_OVERRIDES:
            for a, b in overrides:
                for other, this in ((a, b), (b, a)):
                    if this == p[cs.KEY_QN] and other.startswith(prefix):
                        out.append(_node_row(other, "OVERRIDES"))
        else:
            qns = p[cs.KEY_QNS]
            assert isinstance(qns, list), "a hop reads its whole frontier at once"
            frontier = {str(qn) for qn in qns}
            if query == cq.CYPHER_GRAPH_IMPLEMENTORS_OF:
                edges = [(sub, base, rel) for sub, base, rel in inherits]
            elif query == cq.CYPHER_GRAPH_OVERRIDERS_OF:
                edges = [(a, b, "OVERRIDES") for a, b in overrides]
            else:
                assert query == cq.CYPHER_GRAPH_OVERRIDDEN_BY, query[:60]
                edges = [(b, a, "OVERRIDES") for a, b in overrides]
            out = [
                {**_node_row(other, rel), cs.KEY_THROUGH: this}
                for other, this, rel in edges
                if this in frontier and other.startswith(prefix)
            ]
        # Deliberately unsorted: the tools must order their own output.
        return list(reversed(out))

    return fetch


def _walk(rows: Sequence[Mapping[str, object]]) -> list[tuple[object, ...]]:
    return [(r["depth"], r["qualified_name"], r["through"]) for r in rows]


# --- the subtypes and overriders the issue says are missing -----------------------


def test_implementors_depth_reaches_the_subclass_of_a_subclass() -> None:
    rows = graph_query.implementors(_fetch_for(), P, GRAPH, depth=3)
    assert _walk(rows) == [
        (1, DIGRAPH, GRAPH),
        (1, MULTIGRAPH, GRAPH),
        (1, MYGRAPH, GRAPH),
        # Reached through BOTH parents at the depth it is first reached.
        (2, MULTIDIGRAPH, DIGRAPH),
        (2, MULTIDIGRAPH, MULTIGRAPH),
        (3, DEEP, MULTIDIGRAPH),
    ]


def test_implementors_depth_reaches_every_validator_through_the_abstract_base() -> None:
    rows = graph_query.implementors(_fetch_for(), P, IVALIDATOR, depth=3)
    assert _walk(rows) == [
        (1, ABSTRACT, IVALIDATOR),
        (2, INLINE, ABSTRACT),
        (2, PERSON, ABSTRACT),
        (3, TESTV, INLINE),
    ]
    assert [r["relationship"] for r in rows] == [
        "IMPLEMENTS",
        "INHERITS",
        "INHERITS",
        "INHERITS",
    ]


def test_transitive_rows_keep_the_direct_fields() -> None:
    rows = graph_query.implementors(_fetch_for(), P, IVALIDATOR, depth=2)
    assert rows[0] == {
        "label": "Class",
        "qualified_name": ABSTRACT,
        "path": _path(ABSTRACT),
        "relationship": "IMPLEMENTS",
        "depth": 1,
        "through": IVALIDATOR,
    }


def test_overrides_depth_reaches_the_transitive_override() -> None:
    rows = graph_query.overrides(_fetch_for(), P, f"{GRAPH}.add_edge", depth=2)
    assert _walk(rows) == [
        (1, f"{DIGRAPH}.add_edge", f"{GRAPH}.add_edge"),
        (1, f"{MULTIGRAPH}.add_edge", f"{GRAPH}.add_edge"),
        (2, f"{MULTIDIGRAPH}.add_edge", f"{MULTIGRAPH}.add_edge"),
    ]


def test_overrides_depth_follows_each_direction_on_its_own() -> None:
    """Up from the method it overrides, down from its overriders: a walk
    that turned round would list the sibling `DiGraph.add_edge`, which
    neither overrides `MultiGraph.add_edge` nor is overridden by it."""
    rows = graph_query.overrides(_fetch_for(), P, f"{MULTIGRAPH}.add_edge", depth=5)
    assert _walk(rows) == [
        (1, f"{GRAPH}.add_edge", f"{MULTIGRAPH}.add_edge"),
        (1, f"{MULTIDIGRAPH}.add_edge", f"{MULTIGRAPH}.add_edge"),
        (2, f"{DEEP}.add_edge", f"{MULTIDIGRAPH}.add_edge"),
    ]


def test_overrides_depth_walks_up_the_chain_it_overrides() -> None:
    rows = graph_query.overrides(_fetch_for(), P, f"{DEEP}.add_edge", depth=3)
    assert _walk(rows) == [
        (1, f"{MULTIDIGRAPH}.add_edge", f"{DEEP}.add_edge"),
        (2, f"{MULTIGRAPH}.add_edge", f"{MULTIDIGRAPH}.add_edge"),
        (3, f"{GRAPH}.add_edge", f"{MULTIGRAPH}.add_edge"),
    ]


def test_depth_stops_at_the_requested_hop() -> None:
    rows = graph_query.implementors(_fetch_for(), P, GRAPH, depth=2)
    assert DEEP not in {r["qualified_name"] for r in rows}
    assert max(depth for depth, _, _ in _walk(rows)) == 2


def test_each_hop_is_one_batched_query() -> None:
    log: list[tuple[str, PropertyDict]] = []
    graph_query.implementors(_fetch_for(log=log), P, GRAPH, depth=5)
    hops = [p for q, p in log if q == cq.CYPHER_GRAPH_IMPLEMENTORS_OF]
    assert [p[cs.KEY_QNS] for p in hops] == [
        [GRAPH],
        [DIGRAPH, MULTIGRAPH, MYGRAPH],
        [MULTIDIGRAPH],
        [DEEP],
    ]


# --- what must not change ---------------------------------------------------------


def test_the_default_is_the_direct_rows_unchanged() -> None:
    fetch = _fetch_for()
    assert graph_query.implementors(fetch, P, GRAPH) == [
        {
            "label": "Class",
            "qualified_name": DIGRAPH,
            "path": _path(DIGRAPH),
            "relationship": "INHERITS",
        },
        {
            "label": "Class",
            "qualified_name": MULTIGRAPH,
            "path": _path(MULTIGRAPH),
            "relationship": "INHERITS",
        },
        {
            "label": "Class",
            "qualified_name": MYGRAPH,
            "path": _path(MYGRAPH),
            "relationship": "INHERITS",
        },
    ]
    assert graph_query.implementors(fetch, P, GRAPH, depth=1) == (
        graph_query.implementors(fetch, P, GRAPH)
    )
    assert [
        r["qualified_name"]
        for r in graph_query.overrides(fetch, P, f"{MULTIGRAPH}.add_edge")
    ] == [f"{GRAPH}.add_edge", f"{MULTIDIGRAPH}.add_edge"]
    assert graph_query.overrides(fetch, P, f"{GRAPH}.add_edge", depth=1) == (
        graph_query.overrides(fetch, P, f"{GRAPH}.add_edge")
    )


def test_the_first_hop_lists_what_the_direct_query_does() -> None:
    fetch = _fetch_for()
    for query, target in (
        (graph_query.implementors, GRAPH),
        (graph_query.implementors, IVALIDATOR),
        (graph_query.overrides, f"{GRAPH}.add_edge"),
        (graph_query.overrides, f"{MULTIDIGRAPH}.add_edge"),
    ):
        direct = query(fetch, P, target)
        rows: Sequence[Mapping[str, object]] = query(fetch, P, target, depth=3)
        first = [
            {k: v for k, v in r.items() if k not in ("depth", "through")}
            for r in rows
            if r["depth"] == 1
        ]
        assert first == direct


def test_a_cycle_terminates_and_never_lists_the_start() -> None:
    fetch = _fetch_for(budget=20)
    assert _walk(graph_query.implementors(fetch, P, CYC_A, depth=5)) == [
        (1, CYC_B, CYC_A),
    ]
    assert _walk(graph_query.overrides(fetch, P, f"{CYC_A}.run", depth=5)) == [
        (1, f"{CYC_B}.run", f"{CYC_A}.run"),
        (1, f"{CYC_B}.run", f"{CYC_A}.run"),
    ]


def test_a_type_reached_again_deeper_is_not_listed_again() -> None:
    # MultiDiGraph is also a Graph through a longer path; nothing repeats it.
    inherits = [*INHERITS, (DEEP, GRAPH, "INHERITS")]
    rows = graph_query.implementors(_fetch_for(inherits=inherits), P, GRAPH, depth=5)
    assert [r["qualified_name"] for r in rows].count(DEEP) == 1
    assert (1, DEEP, GRAPH) in _walk(rows)


def test_other_projects_types_are_neither_listed_nor_hops() -> None:
    fetch = _fetch_for()
    for rows in (
        graph_query.implementors(fetch, P, GRAPH, depth=5),
        graph_query.overrides(fetch, P, f"{GRAPH}.add_edge", depth=5),
    ):
        names = {r["qualified_name"] for r in rows}
        assert not {qn for qn in names if qn.startswith((f"{EXTRA}.", f"{OTHER}."))}
        # Leaf and its method are reachable ONLY through the foreign Sub.
        assert not {qn for qn in names if qn.startswith(LEAF)}


def test_an_unknown_target_answers_empty_as_today() -> None:
    fetch = _fetch_for()
    for depth in (1, 3):
        assert graph_query.implementors(fetch, P, f"{P}.nope", depth=depth) == []
        assert graph_query.overrides(fetch, P, f"{P}.nope.go", depth=depth) == []


def test_the_output_does_not_depend_on_the_fetch_order() -> None:
    reordered = list(reversed(INHERITS))
    assert json.dumps(
        graph_query.implementors(_fetch_for(), P, GRAPH, depth=5), sort_keys=True
    ) == json.dumps(
        graph_query.implementors(_fetch_for(inherits=reordered), P, GRAPH, depth=5),
        sort_keys=True,
    )


# --- cgr graph implementors / overrides ------------------------------------------


def _mock_connect() -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch_for(budget=1000))
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    return ingestor


def _cli(tmp_path: Path, command: str, target: str, *extra: str) -> str:
    args = [command, target, "--project", P, "--repo-path", str(tmp_path), *extra]
    with patch(
        "codebase_rag.cli_runtime.connect_memgraph", return_value=_mock_connect()
    ):
        result = CliRunner().invoke(graph_cli, args)
    assert result.exit_code == 0, result.output
    return result.output


def _json(rows: object) -> str:
    return json.dumps(rows, indent=cs.MCP_JSON_INDENT, sort_keys=True) + "\n"


def test_cli_implementors_depth_lists_the_transitive_subtypes(
    tmp_path: Path,
) -> None:
    rows = json.loads(_cli(tmp_path, "implementors", GRAPH, "--depth", "2"))
    assert (2, MULTIDIGRAPH, MULTIGRAPH) in _walk(rows)


def test_cli_overrides_depth_lists_the_transitive_overriders(tmp_path: Path) -> None:
    rows = json.loads(_cli(tmp_path, "overrides", f"{GRAPH}.add_edge", "--depth", "2"))
    assert (2, f"{MULTIDIGRAPH}.add_edge", f"{MULTIGRAPH}.add_edge") in _walk(rows)


def test_cli_without_depth_prints_exactly_the_direct_rows(tmp_path: Path) -> None:
    fetch = _fetch_for()
    assert _cli(tmp_path, "implementors", GRAPH) == _json(
        graph_query.implementors(fetch, P, GRAPH)
    )
    assert _cli(tmp_path, "overrides", f"{GRAPH}.add_edge") == _json(
        graph_query.overrides(fetch, P, f"{GRAPH}.add_edge")
    )


@pytest.mark.parametrize("command", ["implementors", "overrides"])
def test_cli_depth_is_bounded_as_for_callers(command: str, tmp_path: Path) -> None:
    for bad in ("0", str(cs.GRAPH_QUERY_MAX_DEPTH + 1)):
        result = CliRunner().invoke(
            graph_cli, [command, "x", "--depth", bad, "--repo-path", str(tmp_path)]
        )
        assert result.exit_code == 2, result.output
        assert "is not in the range" in result.output


@pytest.mark.parametrize(
    ("command", "note"),
    [
        ("implementors", "Direct subtypes only, unless --depth is above 1."),
        ("overrides", "Direct overrides only, unless --depth is above 1."),
    ],
)
def test_cli_help_says_the_default_is_direct_only(command: str, note: str) -> None:
    result = CliRunner().invoke(graph_cli, [command, "--help"])
    assert result.exit_code == 0
    # Click wraps the help to the terminal width.
    text = " ".join(result.output.split())
    assert "--depth" in text
    assert note in text


# --- MCP implementors / overrides ---------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch_for(budget=1000))
    ingestor.list_projects.return_value = [P, EXTRA, OTHER]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


@pytest.mark.parametrize(
    "tool", [cs.MCPToolName.IMPLEMENTORS, cs.MCPToolName.OVERRIDES]
)
def test_mcp_tool_declares_depth_as_an_optional_integer(
    registry: MCPToolsRegistry, tool: cs.MCPToolName
) -> None:
    schema = registry._tools[tool].input_schema
    prop = schema["properties"][cs.MCPParamName.DEPTH]
    assert prop["type"] == cs.MCPSchemaType.INTEGER
    assert cs.MCPParamName.DEPTH not in schema["required"]
    # Same schema as callers / callees take.
    callers = registry._tools[cs.MCPToolName.CALLERS].input_schema
    assert prop == callers["properties"][cs.MCPParamName.DEPTH]


@pytest.mark.parametrize(
    "tool", [cs.MCPToolName.IMPLEMENTORS, cs.MCPToolName.OVERRIDES]
)
def test_mcp_description_says_direct_by_default(tool: cs.MCPToolName) -> None:
    text = td.MCP_TOOLS[tool]
    assert "only by default" in text
    assert "`depth`" in text
    assert "`through`" in text


async def test_mcp_depth_answers_what_the_cli_does(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    rows = await registry.implementors(GRAPH, depth=3, project=P)
    assert _json(rows) == _cli(tmp_path, "implementors", GRAPH, "--depth", "3")
    rows = await registry.overrides(f"{GRAPH}.add_edge", depth=3, project=P)
    assert _json(rows) == _cli(
        tmp_path, "overrides", f"{GRAPH}.add_edge", "--depth", "3"
    )


async def test_mcp_default_is_the_direct_rows(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    rows = await registry.implementors(GRAPH, project=P)
    assert _json(rows) == _cli(tmp_path, "implementors", GRAPH)
    assert isinstance(rows, list)
    assert all("depth" not in r and "through" not in r for r in rows)
    rows = await registry.overrides(f"{GRAPH}.add_edge", project=P)
    assert _json(rows) == _cli(tmp_path, "overrides", f"{GRAPH}.add_edge")


async def test_mcp_positional_project_still_scopes_the_query(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    rows = await registry.implementors(GRAPH, P)
    assert _json(rows) == _cli(tmp_path, "implementors", GRAPH)
    rows = await registry.overrides(f"{GRAPH}.add_edge", P)
    assert _json(rows) == _cli(tmp_path, "overrides", f"{GRAPH}.add_edge")


async def test_mcp_depth_is_clamped(registry: MCPToolsRegistry) -> None:
    rows = await registry.implementors(GRAPH, depth=99, project=P)
    assert isinstance(rows, list)
    assert max(r["depth"] for r in rows) <= cs.GRAPH_QUERY_MAX_DEPTH


async def test_mcp_unknown_project_is_still_refused_before_any_query(
    registry: MCPToolsRegistry,
) -> None:
    result = await registry.implementors(GRAPH, depth=3, project="typo")
    assert isinstance(result, dict)
    assert "typo" in result["error"]
    registry.ingestor.fetch_all.assert_not_called()
