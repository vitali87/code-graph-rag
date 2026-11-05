"""Issue #2757: an omitted MCP `project` means what its description says.

The graph tools, `rename` and `find_duplicate_code` advertised "Omit to search
every project", but the graph tools and `rename` read the server's own
project (`_graph_query` derives it from the root), so a question about another
indexed project came back `[]` with nothing to say the search was narrowed.
`find_duplicate_code` neither searched everything nor used the server's
project: on a multi-project graph it refused with every project's name.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.utils.path_utils import derive_project_name

pytestmark = [pytest.mark.anyio]

SERVER_DEFAULT_TOOLS = (
    cs.MCPToolName.RESOLVE,
    cs.MCPToolName.CALLERS,
    cs.MCPToolName.ENDPOINTS,
    cs.MCPToolName.FIND_DUPLICATE_CODE,
    cs.MCPToolName.RENAME,
)
EVERY_PROJECT_TOOLS = (cs.MCPToolName.QUERY_CODE_GRAPH,)


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    root = tmp_path / "client"
    root.mkdir()
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(root), ingestor=MagicMock(), cypher_gen=MagicMock()
        )


def _project_description(registry: MCPToolsRegistry, tool: str) -> str:
    schemas = {s.name: s for s in registry.get_tool_schemas()}
    return schemas[tool].inputSchema["properties"][cs.MCPParamName.PROJECT][
        "description"
    ]


@pytest.mark.parametrize("tool", SERVER_DEFAULT_TOOLS)
def test_a_server_scoped_tool_does_not_promise_every_project(
    registry: MCPToolsRegistry, tool: str
) -> None:
    assert "Omit to search every project" not in _project_description(registry, tool)


@pytest.mark.parametrize("tool", SERVER_DEFAULT_TOOLS)
def test_a_server_scoped_tool_names_the_project_it_defaults_to(
    registry: MCPToolsRegistry, tool: str
) -> None:
    project = derive_project_name(Path(registry.project_root))

    assert project in _project_description(registry, tool)


async def test_find_duplicate_code_without_a_project_scans_the_servers(
    registry: MCPToolsRegistry,
) -> None:
    registry._find_duplicates_tool = MagicMock()
    registry._find_duplicates_tool.function = AsyncMock(return_value="ok")

    await registry.find_duplicate_code()

    registry._find_duplicates_tool.function.assert_awaited_once_with(
        project=derive_project_name(Path(registry.project_root)),
        threshold=cs.DUPLICATES_DEFAULT_THRESHOLD,
        min_size=cs.DUPLICATES_DEFAULT_MIN_NODES,
        limit=cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
    )


# Negative: what must not change.


@pytest.mark.parametrize("tool", EVERY_PROJECT_TOOLS)
def test_a_tool_that_reads_every_project_still_says_so(
    registry: MCPToolsRegistry, tool: str
) -> None:
    assert _project_description(registry, tool) == td.MCP_PARAM_PROJECT


async def test_a_named_project_is_still_passed_through(
    registry: MCPToolsRegistry,
) -> None:
    registry._find_duplicates_tool = MagicMock()
    registry._find_duplicates_tool.function = AsyncMock(return_value="ok")

    await registry.find_duplicate_code(project="other")

    registry._find_duplicates_tool.function.assert_awaited_once_with(
        project="other",
        threshold=cs.DUPLICATES_DEFAULT_THRESHOLD,
        min_size=cs.DUPLICATES_DEFAULT_MIN_NODES,
        limit=cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
    )
