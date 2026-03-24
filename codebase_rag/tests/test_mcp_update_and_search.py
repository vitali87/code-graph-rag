from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.mcp.client import query_mcp_server
from codebase_rag.mcp.tools import MCPToolsRegistry

pytestmark = [pytest.mark.anyio]


@pytest.fixture(params=["asyncio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    sample_file = tmp_path / "app.py"
    sample_file.write_text("def main(): pass\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def mcp_registry(temp_project_root: Path) -> MCPToolsRegistry:
    mock_ingestor = MagicMock()
    mock_cypher_gen = MagicMock()

    registry = MCPToolsRegistry(
        project_root=str(temp_project_root),
        ingestor=mock_ingestor,
        cypher_gen=mock_cypher_gen,
    )
    return registry


class TestUpdateRepository:
    async def test_update_repository_success(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater_cls.return_value = mock_updater

            result = await mcp_registry.update_repository()

            mock_updater_cls.assert_called_once()
            mock_updater.run.assert_called_once()
            assert mcp_registry.project_root in result

    async def test_update_repository_error(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.side_effect = RuntimeError("parse error")

            result = await mcp_registry.update_repository()

            assert "Error" in result

    async def test_update_repository_registered(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        assert cs.MCPToolName.UPDATE_REPOSITORY in mcp_registry._tools

    async def test_update_repository_no_wipe(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater_cls.return_value = mock_updater

            await mcp_registry.update_repository()

            mcp_registry.ingestor.delete_project.assert_not_called()
            mcp_registry.ingestor.clean_database.assert_not_called()


class TestSemanticSearchRegistration:
    def test_semantic_search_not_registered_without_deps(
        self, temp_project_root: Path
    ) -> None:
        mock_ingestor = MagicMock()
        mock_cypher_gen = MagicMock()

        with patch(
            "codebase_rag.mcp.tools.has_semantic_dependencies",
            return_value=False,
        ):
            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

        assert cs.MCPToolName.SEMANTIC_SEARCH not in registry._tools
        assert registry._semantic_search_available is False

    def test_semantic_search_registered_with_deps(
        self, temp_project_root: Path
    ) -> None:
        mock_ingestor = MagicMock()
        mock_cypher_gen = MagicMock()

        with (
            patch(
                "codebase_rag.mcp.tools.has_semantic_dependencies",
                return_value=True,
            ),
            patch(
                "codebase_rag.tools.semantic_search.create_semantic_search_tool"
            ) as mock_create,
        ):
            mock_tool = MagicMock()
            mock_create.return_value = mock_tool

            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

            assert cs.MCPToolName.SEMANTIC_SEARCH in registry._tools
            assert registry._semantic_search_available is True

    async def test_semantic_search_calls_tool(self, temp_project_root: Path) -> None:
        mock_ingestor = MagicMock()
        mock_cypher_gen = MagicMock()

        with (
            patch(
                "codebase_rag.mcp.tools.has_semantic_dependencies",
                return_value=True,
            ),
            patch(
                "codebase_rag.tools.semantic_search.create_semantic_search_tool"
            ) as mock_create,
        ):
            mock_tool = MagicMock()
            mock_tool.function = AsyncMock(return_value="result1, result2")
            mock_create.return_value = mock_tool

            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

            result = await registry.semantic_search("find auth functions", top_k=3)

            mock_tool.function.assert_called_once_with(
                query="find auth functions", top_k=3
            )
            assert "result1" in result


class TestAskAgent:
    async def test_ask_agent_registered(self, mcp_registry: MCPToolsRegistry) -> None:
        assert cs.MCPToolName.ASK_AGENT in mcp_registry._tools

    async def test_ask_agent_success(self, mcp_registry: MCPToolsRegistry) -> None:
        mock_agent = MagicMock()
        mock_response = MagicMock()
        mock_response.output = "The auth module uses JWT tokens."
        mock_agent.run = AsyncMock(return_value=mock_response)
        mcp_registry.rag_agent = mock_agent

        result = await mcp_registry.ask_agent("How is auth implemented?")

        assert result["output"] == "The auth module uses JWT tokens."
        mock_agent.run.assert_called_once_with(
            "How is auth implemented?", message_history=[]
        )

    async def test_ask_agent_error(self, mcp_registry: MCPToolsRegistry) -> None:
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        mcp_registry.rag_agent = mock_agent

        result = await mcp_registry.ask_agent("What does main do?")

        assert "error" in result


class TestToolDescriptions:
    def test_update_repository_in_tool_map(self) -> None:
        from codebase_rag.tools.tool_descriptions import MCP_TOOLS

        assert cs.MCPToolName.UPDATE_REPOSITORY in MCP_TOOLS

    def test_semantic_search_in_tool_map(self) -> None:
        from codebase_rag.tools.tool_descriptions import MCP_TOOLS

        assert cs.MCPToolName.SEMANTIC_SEARCH in MCP_TOOLS

    def test_ask_agent_in_tool_map(self) -> None:
        from codebase_rag.tools.tool_descriptions import MCP_TOOLS

        assert cs.MCPToolName.ASK_AGENT in MCP_TOOLS

    def test_index_repository_warns_about_project_clear(self) -> None:
        from codebase_rag.tools.tool_descriptions import MCP_INDEX_REPOSITORY

        assert "current project" in MCP_INDEX_REPOSITORY
        assert "entire database" not in MCP_INDEX_REPOSITORY


class TestRagAgentProperty:
    def test_rag_agent_setter_allows_mock(self, mcp_registry: MCPToolsRegistry) -> None:
        mock_agent = MagicMock()
        mcp_registry.rag_agent = mock_agent
        assert mcp_registry.rag_agent is mock_agent

    def test_rag_agent_lazy_init(self, temp_project_root: Path) -> None:
        mock_ingestor = MagicMock()
        mock_cypher_gen = MagicMock()

        with patch(
            "codebase_rag.mcp.tools.has_semantic_dependencies",
            return_value=False,
        ):
            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

        assert registry._rag_agent is None

        with patch("codebase_rag.mcp.tools.create_rag_orchestrator") as mock_create:
            mock_agent = MagicMock()
            mock_create.return_value = mock_agent

            agent = registry.rag_agent

            mock_create.assert_called_once()
            assert agent is mock_agent

    def test_rag_agent_includes_function_source_tool(
        self, temp_project_root: Path
    ) -> None:
        mock_ingestor = MagicMock()
        mock_cypher_gen = MagicMock()

        with patch(
            "codebase_rag.mcp.tools.has_semantic_dependencies",
            return_value=False,
        ):
            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

        with (
            patch("codebase_rag.mcp.tools.create_rag_orchestrator") as mock_create,
            patch(
                "codebase_rag.tools.semantic_search.create_get_function_source_tool"
            ) as mock_fst,
        ):
            mock_tool = MagicMock()
            mock_fst.return_value = mock_tool
            mock_create.return_value = MagicMock()

            registry.rag_agent

            tools_arg = mock_create.call_args[1]["tools"]
            assert mock_tool in tools_arg


class TestMCPClientImport:
    def test_query_mcp_server_is_async(self) -> None:
        import asyncio

        assert asyncio.iscoroutinefunction(query_mcp_server)

    def test_client_uses_constants(self) -> None:
        import inspect

        from codebase_rag.mcp import client

        source = inspect.getsource(client)
        assert "MCPToolName.ASK_AGENT" in source
        assert "MCPParamName.QUESTION" in source
