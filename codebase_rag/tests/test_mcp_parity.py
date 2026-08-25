"""MCP exposure of `find_duplicate_code` and `get_function_source` (issue #1342).

Both tools already existed as CLI agentic tools. `find_duplicate_code` was
absent from the MCP server entirely; `get_function_source` was reachable only
inside the `ask_agent` orchestrator's toolset, which means an MCP client could
not call it directly, only ask an LLM to decide to call it.

The three CLI tools still absent from MCP are absent on purpose, and the last
test here pins that so closing this gap does not quietly widen into the others.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def main(): pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def mcp_registry(temp_project_root: Path) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(temp_project_root),
        ingestor=MagicMock(),
        cypher_gen=MagicMock(),
    )


def _schema(registry: MCPToolsRegistry, name: str) -> object | None:
    return next((s for s in registry.get_tool_schemas() if s.name == name), None)


class TestFindDuplicateCodeIsExposed:
    def test_tool_is_advertised(self, mcp_registry: MCPToolsRegistry) -> None:
        """An MCP client discovers the tool through get_tool_schemas."""
        assert _schema(mcp_registry, cs.MCPToolName.FIND_DUPLICATE_CODE) is not None

    def test_advertised_description_is_the_shared_one(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """The description must actually describe duplicate detection, not be
        an arbitrary non-empty string."""
        schema = _schema(mcp_registry, cs.MCPToolName.FIND_DUPLICATE_CODE)
        assert schema is not None
        assert schema.description == td.MCP_TOOLS[cs.MCPToolName.FIND_DUPLICATE_CODE]
        assert "duplicat" in schema.description.lower()

    def test_no_argument_is_required(self, mcp_registry: MCPToolsRegistry) -> None:
        """Every parameter has a default, so a bare call is valid."""
        schema = _schema(mcp_registry, cs.MCPToolName.FIND_DUPLICATE_CODE)
        assert schema is not None
        assert schema.inputSchema["required"] == []

    def test_tuning_parameters_are_advertised(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """threshold and min_size are the documented knobs; a client cannot
        use what the schema does not mention."""
        schema = _schema(mcp_registry, cs.MCPToolName.FIND_DUPLICATE_CODE)
        assert schema is not None
        properties = schema.inputSchema["properties"]
        assert cs.MCPParamName.THRESHOLD in properties
        assert cs.MCPParamName.MIN_SIZE in properties
        assert cs.MCPParamName.LIMIT in properties
        assert cs.MCPParamName.PROJECT in properties

    def test_handler_is_dispatchable(self, mcp_registry: MCPToolsRegistry) -> None:
        """Advertising a tool the dispatcher cannot route is worse than not
        advertising it."""
        entry = mcp_registry.get_tool_handler(cs.MCPToolName.FIND_DUPLICATE_CODE)
        assert entry is not None
        handler, returns_json = entry
        assert callable(handler)
        assert returns_json is False

    async def test_handler_returns_the_report(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """The MCP handler returns the underlying tool's report text."""
        mcp_registry._find_duplicates_tool = MagicMock()
        mcp_registry._find_duplicates_tool.function = AsyncMock(
            return_value="2 duplicate groups"
        )

        result = await mcp_registry.find_duplicate_code()

        assert result == "2 duplicate groups"

    async def test_handler_forwards_every_argument(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """A knob that the handler accepts but drops is invisible to the
        caller: the report would silently use the defaults."""
        mcp_registry._find_duplicates_tool = MagicMock()
        mcp_registry._find_duplicates_tool.function = AsyncMock(return_value="ok")

        await mcp_registry.find_duplicate_code(
            project="proj", threshold=0.5, min_size=7, limit=3
        )

        mcp_registry._find_duplicates_tool.function.assert_awaited_once_with(
            project="proj", threshold=0.5, min_size=7, limit=3
        )

    async def test_handler_defaults_match_the_cli(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Parity means the same call gives the same answer on both surfaces,
        so the defaults must be the CLI's, not new numbers."""
        mcp_registry._find_duplicates_tool = MagicMock()
        mcp_registry._find_duplicates_tool.function = AsyncMock(return_value="ok")

        await mcp_registry.find_duplicate_code()

        mcp_registry._find_duplicates_tool.function.assert_awaited_once_with(
            project=None,
            threshold=cs.DUPLICATES_DEFAULT_THRESHOLD,
            min_size=cs.DUPLICATES_DEFAULT_MIN_NODES,
            limit=cs.DUPLICATES_DEFAULT_GROUP_LIMIT,
        )


class TestGetFunctionSourceIsExposed:
    def test_tool_is_advertised(self, mcp_registry: MCPToolsRegistry) -> None:
        """Previously reachable only via ask_agent's toolset, never directly."""
        assert _schema(mcp_registry, cs.MCPToolName.GET_FUNCTION_SOURCE) is not None

    def test_advertised_description_is_the_shared_one(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        schema = _schema(mcp_registry, cs.MCPToolName.GET_FUNCTION_SOURCE)
        assert schema is not None
        assert schema.description == td.MCP_TOOLS[cs.MCPToolName.GET_FUNCTION_SOURCE]
        assert "source" in schema.description.lower()

    def test_node_id_is_required(self, mcp_registry: MCPToolsRegistry) -> None:
        """There is no sensible default node id, so it must be required."""
        schema = _schema(mcp_registry, cs.MCPToolName.GET_FUNCTION_SOURCE)
        assert schema is not None
        assert schema.inputSchema["required"] == [cs.MCPParamName.NODE_ID]

    def test_node_id_is_typed_as_an_integer(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Node ids come back from semantic_search as integers; typing this as
        a string would make every client stringify a number the graph then
        fails to match."""
        schema = _schema(mcp_registry, cs.MCPToolName.GET_FUNCTION_SOURCE)
        assert schema is not None
        node_id = schema.inputSchema["properties"][cs.MCPParamName.NODE_ID]
        assert node_id["type"] == cs.MCPSchemaType.INTEGER

    def test_handler_is_dispatchable(self, mcp_registry: MCPToolsRegistry) -> None:
        entry = mcp_registry.get_tool_handler(cs.MCPToolName.GET_FUNCTION_SOURCE)
        assert entry is not None
        handler, returns_json = entry
        assert callable(handler)
        assert returns_json is False

    async def test_handler_forwards_the_node_id(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Passing the wrong id would return someone else's source code."""
        mcp_registry._function_source_tool = MagicMock()
        mcp_registry._function_source_tool.function = AsyncMock(
            return_value="def f(): ..."
        )

        result = await mcp_registry.get_function_source(node_id=42)

        assert result == "def f(): ..."
        mcp_registry._function_source_tool.function.assert_awaited_once_with(node_id=42)


class TestDeliberateAbsencesAreUnchanged:
    """The other three CLI tools are absent by design, not by oversight."""

    @pytest.mark.parametrize("name", ["web_search", "research"])
    def test_web_reaching_tools_stay_off_mcp(
        self, mcp_registry: MCPToolsRegistry, name: str
    ) -> None:
        """Issue #1128 keeps external web content out of any context that also
        holds repository reads. MCP has no ReadContentRecord egress gate, so
        exposing either here would reopen that boundary with nothing watching
        it."""
        assert _schema(mcp_registry, name) is None

    def test_shell_stays_off_mcp(self, mcp_registry: MCPToolsRegistry) -> None:
        """execute_shell over MCP is a security decision for the maintainer,
        not a gap to be closed silently while fixing an unrelated one."""
        assert _schema(mcp_registry, "execute_shell") is None

    def test_ask_agent_still_offers_function_source(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        """Direct exposure adds a route; it must not remove the existing one
        from the orchestrator's toolset."""
        with patch("codebase_rag.mcp.tools.create_rag_orchestrator") as mock_create:
            mock_create.return_value = (MagicMock(), MagicMock())
            _ = mcp_registry.rag_agent

        tools = mock_create.call_args.kwargs["tools"]
        names = {getattr(tool, "name", None) for tool in tools}
        assert td.AgenticToolName.GET_FUNCTION_SOURCE in names
