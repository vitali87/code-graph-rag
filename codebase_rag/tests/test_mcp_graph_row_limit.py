"""The MCP graph tools bound what they return.

Nothing bounded them: django's `tests_reaching` on `QuerySet.filter` was
20,244 rows (6.5 MB, ~1.5M tokens, depth up to 21), `callers` at depth 5 was
0.7 MB, and neither tool could be asked for less. An MCP host rejects such a
result, cuts it at an arbitrary point, or fills the model's context with it
(issue #2815).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tests.test_graph_query import P, fake_fetch_all

_HELPER = f"{P}.util.helper"
_MANY = 500


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=fake_fetch_all)
    ingestor.list_projects.return_value = [P]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


def _reach_rows(n: int) -> list[graph_query.TestReachRow]:
    return [
        graph_query.TestReachRow(
            label="Function",
            qualified_name=f"{P}.tests.test_m.test_{i:04d}",
            path="tests/test_m.py",
            depth=1 + i // 25,
            through=_HELPER,
        )
        for i in range(n)
    ]


def _site_rows(n: int) -> list[graph_query.CallSiteRow]:
    return [
        graph_query.CallSiteRow(
            label="Function",
            qualified_name=f"{P}.app.caller_{i:04d}",
            path="app.py",
            callee_path="util.py",
            line=i,
            col=0,
            end_line=i,
            end_col=8,
            arg_count=0,
            kwarg_names=None,
            resolution="exact",
            depth=1,
            through=_HELPER,
        )
        for i in range(n)
    ]


async def test_a_large_answer_is_capped_nearest_first(
    registry: MCPToolsRegistry,
) -> None:
    rows = _reach_rows(_MANY)
    with patch("codebase_rag.mcp.tools.graph_query.tests_reaching", return_value=rows):
        result = await registry.tests_reaching(_HELPER, project=P)

    assert isinstance(result, dict), type(result)
    assert result[cs.KEY_MCP_TOTAL] == _MANY
    assert result[cs.KEY_MCP_TRUNCATED] is True
    assert result[cs.KEY_MCP_ROWS] == rows[: cs.MCP_GRAPH_ROW_LIMIT]
    assert str(_MANY) in result[cs.KEY_MCP_HINT]


@pytest.mark.parametrize(
    ("tool", "patched", "rows"),
    [
        ("tests_reaching", "tests_reaching", _reach_rows(_MANY)),
        ("callers", "callers", _site_rows(_MANY)),
        ("callees", "callees", _site_rows(_MANY)),
        ("importers", "importers", _site_rows(_MANY)),
        ("implementors", "implementors", _site_rows(_MANY)),
    ],
)
async def test_limit_asks_for_fewer_or_more(
    registry: MCPToolsRegistry,
    tool: str,
    patched: str,
    rows: list[object],
) -> None:
    with patch(f"codebase_rag.mcp.tools.graph_query.{patched}", return_value=rows):
        few = await getattr(registry, tool)(_HELPER, limit=10, project=P)
        everything = await getattr(registry, tool)(_HELPER, limit=_MANY, project=P)

    assert isinstance(few, dict)
    assert few[cs.KEY_MCP_ROWS] == rows[:10]
    assert few[cs.KEY_MCP_TOTAL] == _MANY
    assert everything == rows


async def test_max_depth_stops_the_walk(
    registry: MCPToolsRegistry,
) -> None:
    # The fixture's tests reach `util.helper` at depths 1, 2 and 3.
    rows = await registry.tests_reaching(_HELPER, max_depth=2, project=P)
    assert isinstance(rows, list)
    assert [r["depth"] for r in rows] == [1, 2]


async def test_a_small_answer_is_still_a_plain_list(
    registry: MCPToolsRegistry,
) -> None:
    # Negatives: an answer under the cap keeps its shape, and an unbounded
    # walk still finds the farthest test.
    rows = await registry.tests_reaching(_HELPER, project=P)
    assert isinstance(rows, list)
    assert [r["depth"] for r in rows] == [1, 2, 3]
    callers = await registry.callers(_HELPER, project=P)
    assert isinstance(callers, list) and callers


async def test_the_bounds_are_declared_in_the_schemas(
    registry: MCPToolsRegistry,
) -> None:
    for tool in (
        cs.MCPToolName.CALLERS,
        cs.MCPToolName.CALLEES,
        cs.MCPToolName.IMPORTERS,
        cs.MCPToolName.IMPLEMENTORS,
        cs.MCPToolName.TESTS_REACHING,
    ):
        properties = registry._tools[tool].input_schema["properties"]
        assert properties[cs.MCPParamName.LIMIT]["type"] == "integer", tool
    reach = registry._tools[cs.MCPToolName.TESTS_REACHING].input_schema["properties"]
    assert reach[cs.MCPParamName.MAX_DEPTH]["type"] == "integer"
