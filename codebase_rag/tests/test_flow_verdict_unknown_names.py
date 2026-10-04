"""Issue #2939: `flow_verdict` refuses a source or sink that names nothing.

The verdict walked the project's FLOWS_TO edges from the source and, with no
path and no coverage gap, answered NO_FLOW: the verified absence a taint
question reads as "proven safe". A name with no node has no edges, so a
typo, the other spelling of the project prefix (an MCP server's
`name__hash` beside a CLI index's `name`) or `no.such.thing` all got it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.flow_verdict import (
    CYPHER_FLOW_COVERAGE_GAPS,
    CYPHER_FLOW_EDGES,
    CYPHER_FLOW_REMOTE_EDGES,
)
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import PropertyParams, ResultRow

P = "flowenv__efe0eaf7"
CLI = "flowenv"
SOURCE = f"{P}.app.inputs.read_user_input"
VIEW = f"{P}.app.views.direct"
SINK = f"{P}.app.db.run_query"
OTHER_SOURCE = f"{CLI}.app.inputs.read_user_input"
ENV = "ENV::SEARCH_TERM"
# A project registered under a dotted name inside this one's prefix.
NESTED = f"{P}.v2"
NESTED_SOURCE = f"{NESTED}.api.read"
NODES = {
    SOURCE,
    VIEW,
    SINK,
    OTHER_SOURCE,
    f"{CLI}.app.db.run_query",
    ENV,
    NESTED_SOURCE,
}
EDGES = [(SOURCE, VIEW), (VIEW, SINK), (ENV, SOURCE)]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
    p = params or {}
    prefix = str(p.get(cs.KEY_PROJECT_PREFIX, ""))
    if query == cq.CYPHER_GRAPH_NODE_EXISTS:
        qn = str(p[cs.KEY_QN])
        return [{cs.KEY_QUALIFIED_NAME: qn}] if qn in NODES else []
    if query == cq.CYPHER_LIST_PROJECTS:
        return [{cs.KEY_NAME: name, cs.KEY_ROOT_PATH: None} for name in (CLI, P)]
    if query == cq.CYPHER_GRAPH_DEFINITIONS_UNDER:
        return [{cs.KEY_QUALIFIED_NAME: qn} for qn in NODES if qn.startswith(prefix)]
    if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
        name = str(p[cs.KEY_NAME])
        return [
            {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_NAME: qn.rsplit(".", 1)[-1]}
            for qn in sorted(NODES)
            if qn.startswith(prefix) and qn.rsplit(".", 1)[-1] == name
        ]
    if query == CYPHER_FLOW_EDGES:
        return [{"source": s, "target": t} for s, t in EDGES]
    if query in (CYPHER_FLOW_REMOTE_EDGES, CYPHER_FLOW_COVERAGE_GAPS):
        return []
    raise AssertionError(query[:60])


def _registry(tmp_path: Path, projects: list[str]) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=_fetch_all)
    ingestor.list_projects.return_value = projects
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )
    registry._fixed_root_project = MagicMock(return_value=(P, None))
    registry._incomplete_refusal = MagicMock(return_value=None)
    return registry


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    return _registry(tmp_path, [CLI, P])


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("source", "sink", "named"),
    [
        (f"{P}.app.inputs.read_user_inpt", SINK, "source"),
        ("no.such.thing", SINK, "source"),
        (SOURCE, f"{P}.app.db.run_qury", "sink"),
    ],
    ids=["typo", "nothing", "sink-typo"],
)
async def test_a_name_with_no_node_is_refused_not_no_flow(
    registry: MCPToolsRegistry, source: str, sink: str, named: str
) -> None:
    result = await registry.flow_verdict(source, sink)

    assert "verdict" not in result
    error = result[cs.DICT_KEY_ERROR]
    assert f"The {named}" in error
    assert "is not in the graph" in error


@pytest.mark.anyio
async def test_a_typo_names_the_close_match(registry: MCPToolsRegistry) -> None:
    result = await registry.flow_verdict(f"{P}.app.inputs.read_user_inpt", SINK)

    assert f"Did you mean: {SOURCE}" in result[cs.DICT_KEY_ERROR]


@pytest.mark.anyio
async def test_a_source_of_another_project_names_that_project(
    registry: MCPToolsRegistry,
) -> None:
    result = await registry.flow_verdict(OTHER_SOURCE, SINK)

    assert "verdict" not in result
    assert f"belongs to project '{CLI}'" in result[cs.DICT_KEY_ERROR]


@pytest.mark.anyio
async def test_a_source_of_a_nested_project_names_that_project(
    tmp_path: Path,
) -> None:
    # `NESTED` sits under this project's prefix but is a project of its own,
    # and the flow walk's prefix match would read its edges as this one's.
    registry = _registry(tmp_path, [CLI, P, NESTED])

    result = await registry.flow_verdict(NESTED_SOURCE, SINK)

    assert "verdict" not in result
    assert f"belongs to project '{NESTED}'" in result[cs.DICT_KEY_ERROR]


# Negative: what must not change.


@pytest.mark.anyio
@pytest.mark.parametrize(
    "source", [SOURCE, ENV], ids=["function", "resource-outside-any-project"]
)
async def test_a_real_path_is_still_found(
    registry: MCPToolsRegistry, source: str
) -> None:
    result = await registry.flow_verdict(source, SINK)

    assert result["verdict"] == "FOUND"
    assert result["path"][-1] == SINK


@pytest.mark.anyio
async def test_real_names_with_no_path_are_still_no_flow(
    registry: MCPToolsRegistry,
) -> None:
    # Both names exist; the sink just is not downstream of the source.
    result = await registry.flow_verdict(SINK, SOURCE)

    assert result["verdict"] == "NO_FLOW"
