from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import ReingestAborted
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

            # `project=None` is part of the contract now (issue #1494): the
            # handler forwards the scope, and NONE is what an unscoped call
            # must send. Asserting the value rather than dropping the kwarg
            # from the check -- a handler forwarding some other project
            # would still satisfy a looser assertion while silently
            # narrowing every unscoped caller's results.
            mock_tool.function.assert_called_once_with(
                query="find auth functions", top_k=3, project=None
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
            mock_create.return_value = (mock_agent, "system prompt")

            agent = registry.rag_agent

            mock_create.assert_called_once()
            assert agent is mock_agent

    def test_rag_agent_includes_function_source_tool(
        self, temp_project_root: Path
    ) -> None:
        """The orchestrator gets the same instance the direct MCP tool uses.

        Since issue #1342 the tool is built once in the constructor rather than
        lazily inside this property, so the assertion is identity against
        `registry._function_source_tool`; patching the factory would no longer
        intercept a call that has already happened.
        """
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

        with patch("codebase_rag.mcp.tools.create_rag_orchestrator") as mock_create:
            mock_create.return_value = (MagicMock(), "system prompt")

            registry.rag_agent

            tools_arg = mock_create.call_args[1]["tools"]
            assert registry._function_source_tool in tools_arg

    def test_rag_agent_includes_semantic_search_when_available(
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
            ) as mock_ss,
        ):
            mock_ss_tool = MagicMock()
            mock_ss.return_value = mock_ss_tool

            registry = MCPToolsRegistry(
                project_root=str(temp_project_root),
                ingestor=mock_ingestor,
                cypher_gen=mock_cypher_gen,
            )

        with (
            patch("codebase_rag.mcp.tools.create_rag_orchestrator") as mock_create,
            patch("codebase_rag.tools.semantic_search.create_get_function_source_tool"),
        ):
            mock_create.return_value = (MagicMock(), "system prompt")
            registry.rag_agent

            tools_arg = mock_create.call_args[1]["tools"]
            assert mock_ss_tool in tools_arg

    def test_rag_agent_caches_after_first_access(self, temp_project_root: Path) -> None:
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
            patch("codebase_rag.tools.semantic_search.create_get_function_source_tool"),
        ):
            mock_create.return_value = (MagicMock(), "system prompt")

            agent1 = registry.rag_agent
            agent2 = registry.rag_agent

            mock_create.assert_called_once()
            assert agent1 is agent2


class TestMainSingleQuery:
    def test_main_single_query_prints_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from codebase_rag.main import main_single_query

        mock_response = MagicMock()
        mock_response.output = "The answer is 42."

        with (
            patch("codebase_rag.main.connect_memgraph") as mock_conn,
            patch("codebase_rag.main._initialize_services_and_agent") as mock_init,
            patch("codebase_rag.main.asyncio") as mock_asyncio,
            patch("codebase_rag.main._setup_common_initialization"),
        ):
            mock_agent = MagicMock()
            mock_init.return_value = (mock_agent, [], "system prompt")
            mock_asyncio.run.return_value = mock_response
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            main_single_query(str(tmp_path), 1000, "What is the answer?")

            captured = capsys.readouterr()
            assert "The answer is 42." in captured.out

    def test_main_single_query_routes_logs_to_stderr(self, tmp_path: Path) -> None:
        from codebase_rag.main import main_single_query

        mock_response = MagicMock()
        mock_response.output = "result"

        with (
            patch("codebase_rag.main.connect_memgraph") as mock_conn,
            patch("codebase_rag.main._initialize_services_and_agent") as mock_init,
            patch("codebase_rag.main.asyncio") as mock_asyncio,
            patch("codebase_rag.main._setup_common_initialization"),
            patch("codebase_rag.main.logger") as mock_logger,
        ):
            mock_agent = MagicMock()
            mock_init.return_value = (mock_agent, [], "system prompt")
            mock_asyncio.run.return_value = mock_response
            mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            main_single_query(str(tmp_path), 1000, "test")

            mock_logger.remove.assert_called_once()
            mock_logger.add.assert_called_once()
            add_args = mock_logger.add.call_args
            import sys

            assert add_args[0][0] is sys.stderr


class TestMCPClient:
    def test_query_mcp_server_is_callable(self) -> None:
        assert callable(query_mcp_server)

    def test_client_uses_constants(self) -> None:
        import inspect

        from codebase_rag.mcp import client

        source = inspect.getsource(client)
        assert "MCPToolName.ASK_AGENT" in source
        assert "MCPParamName.QUESTION" in source

    def test_query_with_errlog_is_async(self) -> None:
        import asyncio

        from codebase_rag.mcp.client import _query_with_errlog

        assert asyncio.iscoroutinefunction(_query_with_errlog)

    async def test_query_with_errlog_json_response(self) -> None:
        import io

        from codebase_rag.mcp.client import _query_with_errlog

        mock_content = MagicMock()
        mock_content.text = '{"output": "test answer"}'
        mock_result = MagicMock()
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("codebase_rag.mcp.client.stdio_client", return_value=mock_transport),
            patch("codebase_rag.mcp.client.ClientSession", return_value=mock_session),
        ):
            result = await _query_with_errlog("test question", io.StringIO())

        assert result == {"output": "test answer"}

    async def test_query_with_errlog_non_json_response(self) -> None:
        import io

        from codebase_rag.mcp.client import _query_with_errlog

        mock_content = MagicMock()
        mock_content.text = "plain text response"
        mock_result = MagicMock()
        mock_result.content = [mock_content]

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("codebase_rag.mcp.client.stdio_client", return_value=mock_transport),
            patch("codebase_rag.mcp.client.ClientSession", return_value=mock_session),
        ):
            result = await _query_with_errlog("test", io.StringIO())

        assert result == {"output": "plain text response"}

    async def test_query_with_errlog_empty_response(self) -> None:
        import io

        from codebase_rag.mcp.client import _query_with_errlog

        mock_result = MagicMock()
        mock_result.content = []

        mock_session = AsyncMock()
        mock_session.initialize = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        mock_transport = AsyncMock()
        mock_transport.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
        mock_transport.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("codebase_rag.mcp.client.stdio_client", return_value=mock_transport),
            patch("codebase_rag.mcp.client.ClientSession", return_value=mock_session),
        ):
            result = await _query_with_errlog("test", io.StringIO())

        assert result == {"output": "No response from server"}

    def test_query_mcp_server_opens_devnull(self) -> None:
        with (
            patch("codebase_rag.mcp.client.asyncio") as mock_asyncio,
            patch("builtins.open", MagicMock()) as mock_open,
        ):
            mock_asyncio.run.return_value = {"output": "result"}
            query_mcp_server("test")
            mock_open.assert_called_once()


def _mark_indexed(registry: MCPToolsRegistry) -> str:
    from codebase_rag.utils.path_utils import derive_project_name

    project = derive_project_name(Path(registry.project_root))
    registry.ingestor.list_projects.return_value = [project]
    return project


class TestReingest:
    async def test_reingest_registered(self, mcp_registry: MCPToolsRegistry) -> None:
        meta = mcp_registry._tools[cs.MCPToolName.REINGEST]
        assert meta.returns_json is True
        schema = meta.input_schema
        assert schema["required"] == [cs.MCPParamName.PATHS]
        assert schema["properties"][cs.MCPParamName.PATHS]["type"] == "array"
        assert schema["properties"][cs.MCPParamName.PATHS]["items"] == {
            "type": "string"
        }

    async def test_reingest_builds_one_updater_and_reuses_it(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # The registry must stay warm across calls: a fresh updater per call
        # would read every definition back from the graph each time.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.reingest.return_value = MagicMock(
                reparsed=("a.py",),
                affected=("b.py",),
                removed=(),
                skipped=(),
                elapsed_ms=12.34,
            )
            mock_updater_cls.return_value = mock_updater

            first = await mcp_registry.reingest(["a.py"])
            second = await mcp_registry.reingest(["a.py"], deleted=["c.py"])

            mock_updater_cls.assert_called_once()
            assert mock_updater.reingest.call_args_list == [
                call(["a.py"], deleted=[]),
                call(["a.py"], deleted=["c.py"]),
            ]
            assert first == {
                "reparsed": ["a.py"],
                "affected": ["b.py"],
                "removed": [],
                "skipped": [],
                "elapsed_ms": 12.3,
            }
            assert second == first

    async def test_reingest_builds_its_updater_with_the_ignore_sets(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # index_repository and update_repository hand the resolved ignore
        # files to the updater; reingest's fresh updater must get the same
        # set, or an agent-named path under an excluded directory is indexed
        # here and kept by every later update.
        _mark_indexed(mcp_registry)
        exclude, unignore = frozenset({"vendor"}), frozenset({"vendor/keep"})
        with (
            patch.object(
                mcp_registry, "_ignore_sets", return_value=(exclude, unignore)
            ),
            patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls,
        ):
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.5
            )
            await mcp_registry.reingest(["a.py"])
            kwargs = mock_updater_cls.call_args.kwargs
            assert kwargs["exclude_paths"] == exclude
            assert kwargs["unignore_paths"] == unignore

    async def test_reingest_reuses_the_updater_update_repository_built(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater = MagicMock()
            mock_updater.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.5
            )
            mock_updater_cls.return_value = mock_updater

            await mcp_registry.update_repository()
            await mcp_registry.reingest(["a.py"])

            mock_updater_cls.assert_called_once()
            mock_updater.reingest.assert_called_once_with(["a.py"], deleted=[])

    async def test_reingest_error_is_reported_not_raised(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.side_effect = ValueError(
                "Path is outside the repository: ../x"
            )
            result = await mcp_registry.reingest(["../x"])
        assert "outside the repository" in result["error"]
        assert result["reparsed"] == []
        # The error result carries every field the success result does, so
        # a caller reading it never hits a KeyError on the failure path.
        assert result["skipped"] == []
        assert result["removed"] == []

    async def test_delete_project_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # The retained updater holds the deleted graph's definitions; a
        # reingest after the delete must refuse until the project is indexed
        # again (a scoped re-ingest cannot stand in for the first index).
        project = _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            mcp_registry.ingestor.list_projects.return_value = [project, "other"]
            await mcp_registry.delete_project(project)
            mcp_registry.ingestor.list_projects.return_value = ["other"]
            result = await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 1
            assert "not indexed" in result["error"]
            # Re-indexed: the next reingest builds a fresh updater.
            mcp_registry.ingestor.list_projects.return_value = [project]
            await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 2

    async def test_a_failed_reindex_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # index_repository deletes the project first; if the rebuild then
        # fails, the updater retained from the deleted graph must not serve
        # a later reingest, which would resolve against dead definitions.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 1

            mcp_registry.ingestor.ensure_constraints.side_effect = RuntimeError(
                "database gone"
            )
            outcome = await mcp_registry.index_repository()
            assert "database gone" in outcome
            mcp_registry.ingestor.ensure_constraints.side_effect = None

            # The rebuild deleted the project and then died: the graph is
            # incomplete, and the refusal says so rather than "not indexed",
            # since a partial rebuild may already have recreated the project.
            mcp_registry.ingestor.list_projects.return_value = []
            result = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in result["error"]
            assert mock_updater_cls.call_count == 1

    async def test_wipe_database_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            await mcp_registry.wipe_database(confirm=True)
            mcp_registry.ingestor.list_projects.return_value = []
            result = await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 1
            assert "not indexed" in result["error"]

    async def test_a_failed_update_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # update_repository mutates the graph before it can fail; the
        # updater retained from before describes the graph as it was, so a
        # later reingest must hydrate from the store rather than reuse it.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            first = MagicMock()
            first.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            failing = MagicMock()
            failing.run.side_effect = RuntimeError("died mid-run")
            recovered = MagicMock()
            recovered.reingest.return_value = first.reingest.return_value
            mock_updater_cls.side_effect = [first, failing, recovered]
            await mcp_registry.reingest(["a.py"])
            assert mcp_registry._live_updater is first
            result = await mcp_registry.update_repository()
            assert "Error" in result
            assert mcp_registry._live_updater is None
            # The graph the failed update left is partial: a scoped reingest
            # must not hydrate from it and treat it as authoritative.
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            assert mock_updater_cls.call_count == 2
            # A completed update lifts the refusal.
            assert "Error" not in await mcp_registry.update_repository()
            assert "error" not in await mcp_registry.reingest(["a.py"])
            recovered.reingest.assert_called_once()

    async def test_a_failed_initial_flush_marks_the_graph_incomplete(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # update_repository's first flush commits batches left by earlier
        # calls; a failure there can already have written part of them.
        _mark_indexed(mcp_registry)
        mcp_registry.ingestor.flush_all.side_effect = [RuntimeError("flush died")]
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            result = await mcp_registry.update_repository()
            assert "flush died" in result
            mcp_registry.ingestor.flush_all.side_effect = None
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            mock_updater_cls.assert_not_called()

    async def test_a_failed_project_delete_marks_the_graph_incomplete(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # index_repository's project delete is the first write of the
        # rebuild; a failure there may have removed part of the project.
        _mark_indexed(mcp_registry)
        mcp_registry.ingestor.delete_project.side_effect = RuntimeError("delete died")
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            result = await mcp_registry.index_repository()
            assert "delete died" in result
            mcp_registry.ingestor.delete_project.side_effect = None
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            mock_updater_cls.assert_not_called()

    async def test_a_failed_reingest_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # A reingest that dies after its refusal checks may have deleted the
        # affected subtrees without rebuilding them: the retained updater
        # must not serve the next scoped call over that partial graph.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            updater = mock_updater_cls.return_value
            updater.reingest.side_effect = RuntimeError("died after the delete")
            result = await mcp_registry.reingest(["a.py"])
            assert "died after the delete" in result["error"]
            assert mcp_registry._live_updater is None
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            assert mock_updater_cls.call_count == 1
            # A completed update lifts the refusal.
            updater.reingest.side_effect = None
            updater.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            assert "Error" not in await mcp_registry.update_repository()
            assert "error" not in await mcp_registry.reingest(["a.py"])

    async def test_an_aborted_reingest_keeps_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # An abort while the call was still reading the graph (a failed
        # module-path or inbound-edge query) wrote nothing: the updater and
        # the graph are intact, so the next call must not be refused as
        # if the graph were partial.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            updater = mock_updater_cls.return_value
            updater.reingest.side_effect = ReingestAborted("graph read failed")
            result = await mcp_registry.reingest(["a.py"])
            assert "graph read failed" in result["error"]
            assert mcp_registry._live_updater is updater
            updater.reingest.side_effect = None
            updater.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            assert "error" not in await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 1

    async def test_a_refused_reingest_keeps_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # A refusal is raised while the paths are split, before any write:
        # the updater stays valid and the next call reuses it.
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            updater = mock_updater_cls.return_value
            updater.reingest.side_effect = ValueError("Path is outside: ../x")
            result = await mcp_registry.reingest(["../x"])
            assert "outside" in result["error"]
            assert mcp_registry._live_updater is updater
            updater.reingest.side_effect = None
            updater.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            assert "error" not in await mcp_registry.reingest(["a.py"])
            assert mock_updater_cls.call_count == 1

    async def test_a_failed_project_delete_call_marks_the_graph_incomplete(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        # The delete_project tool can fail after removing part of the
        # graph while the project is still listed; the next reingest must
        # refuse rather than reuse the retained updater over what is left.
        project = _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            mcp_registry.ingestor.delete_project.side_effect = RuntimeError(
                "delete died"
            )
            result = await mcp_registry.delete_project(project)
            assert result["success"] is False
            assert "delete died" in result["error"]
            mcp_registry.ingestor.delete_project.side_effect = None
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            assert mock_updater_cls.call_count == 1

    async def test_a_failed_wipe_marks_the_graph_incomplete(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        _mark_indexed(mcp_registry)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls:
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), skipped=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            mcp_registry.ingestor.clean_database.side_effect = RuntimeError("wipe died")
            result = await mcp_registry.wipe_database(confirm=True)
            assert "wipe died" in result
            mcp_registry.ingestor.clean_database.side_effect = None
            refused = await mcp_registry.reingest(["a.py"])
            assert "failed part way" in refused["error"]
            assert mock_updater_cls.call_count == 1

    async def test_failed_embedding_wipe_still_drops_the_retained_updater(
        self, mcp_registry: MCPToolsRegistry
    ) -> None:
        _mark_indexed(mcp_registry)
        with (
            patch("codebase_rag.mcp.tools.GraphUpdater") as mock_updater_cls,
            patch(
                "codebase_rag.mcp.tools.clear_all_embeddings",
                side_effect=RuntimeError("x"),
            ),
        ):
            mock_updater_cls.return_value.reingest.return_value = MagicMock(
                reparsed=(), affected=(), removed=(), elapsed_ms=0.1
            )
            await mcp_registry.reingest(["a.py"])
            result = await mcp_registry.wipe_database(confirm=True)
            assert "rror" in result
            assert mcp_registry._live_updater is None


class TestIncompleteMarkerSurvivesTheProcess:
    """The refusal must outlive the process that earned it (issue #1679)."""

    @staticmethod
    def _store() -> MagicMock:
        """An ingestor whose marker property persists across registries.

        Backed by a dict rather than a canned return value: the point of the
        fix is that the SECOND registry reads what the FIRST one wrote, and a
        static return value would satisfy the assertion without any write
        having happened.
        """
        store: dict[str, bool] = {}
        ingestor = MagicMock()

        # A plain dict, never an attribute on the MagicMock: `getattr` on a
        # MagicMock returns a truthy child mock for ANY name, so a
        # `getattr(ingestor, "_fail_clear", False)` switch is permanently on.
        failing: set[str] = set()

        def _write(query: str, params: dict | None = None) -> None:
            name = str((params or {}).get(cs.KEY_PROJECT_NAME))
            if "clear" in failing and "DELETE m" in query:
                raise RuntimeError("clear write refused")
            if "mark" in failing and "SET m.run_incomplete" in query:
                raise RuntimeError("mark write refused")
            # MERGE creates the node when absent; MATCH does not, and a
            # MATCH-based mark therefore writes NOTHING on a first run. The
            # fake has to honour that difference or it answers the question
            # the code is supposed to answer: with a branch keyed only on
            # "SET m.run_incomplete", reverting MERGE to MATCH left every
            # test green (#1705 review).
            if "SET m.run_incomplete" in query:
                if query.lstrip().startswith("MERGE") or name in store:
                    store[name] = True
            elif "DELETE m" in query:
                store.pop(name, None)

        def _read(query: str, params: dict | None = None) -> list[dict]:
            if "run_incomplete" not in query:
                return []
            if "read" in failing:
                raise RuntimeError("marker read refused")
            name = str((params or {}).get(cs.KEY_PROJECT_NAME))
            # A real MATCH returns NO ROWS when the marker node is absent, not
            # a row saying false. Modelling the empty result matters: code that
            # reads rows[0] would pass against a canned false and fail here.
            return [{"run_incomplete": True}] if store.get(name) else []

        def _delete_project(name: str) -> None:
            # Models CYPHER_DELETE_PROJECT: it detaches the Project and
            # everything it CONTAINS. A marker kept ON the Project would go
            # with it; one on its own unconnected node must survive.
            store.pop(f"__project_anchored__{name}", None)

        ingestor.execute_write.side_effect = _write
        ingestor.fetch_all.side_effect = _read
        ingestor.delete_project.side_effect = _delete_project
        ingestor._marker_store = store
        ingestor._failing = failing
        return ingestor

    def _registry(self, root: Path, ingestor: MagicMock) -> MCPToolsRegistry:
        """A registry sharing `ingestor` but nothing else.

        Each call is a separate process as far as `_graph_incomplete` is
        concerned, which is precisely the case the persisted marker exists
        for; `cypher_gen` is per-registry because nothing here queries it.
        """
        return MCPToolsRegistry(
            project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
        )

    async def test_a_fresh_registry_refuses_reingest_after_a_crashed_update(
        self, temp_project_root: Path
    ) -> None:
        """A new process must still refuse, which is the whole gap.

        `_graph_incomplete` lives on the registry, so the ORIGINAL process
        refuses correctly. The defect was that a crash or an MCP restart
        produced a registry whose flag is False, which then hydrated a scoped
        updater from the partial graph and treated its missing definitions as
        authoritative.

        The second registry here shares only the store, never the flag, which
        is exactly what a restarted server has.
        """
        ingestor = self._store()
        first = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(first)

        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            updater_cls.return_value.run.side_effect = RuntimeError("update died")
            assert "Error" in await first.update_repository()

        assert ingestor._marker_store.get(project) is True, (
            "the failed update left no persisted marker, so a fresh process "
            "has nothing to read"
        )

        # The crash: a brand new registry over the same store.
        second = self._registry(temp_project_root, ingestor)
        _mark_indexed(second)
        assert second._graph_incomplete is False, (
            "fixture guard: the new registry must NOT carry the in-process "
            "flag, or this test cannot tell the persisted marker apart from it"
        )

        refused = await second.reingest(["a.py"])
        assert "failed part way" in refused.get("error", ""), (
            "a fresh registry hydrated a scoped updater from the partial "
            f"graph left by a crashed update; got {refused}"
        )

    async def test_a_completed_update_lifts_the_refusal_for_a_fresh_registry(
        self, temp_project_root: Path
    ) -> None:
        """The marker must be CLEARED, not merely set.

        Without this the fix would be indistinguishable from one that refuses
        every reingest forever, which also passes the test above.
        """
        ingestor = self._store()
        first = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(first)

        with patch("codebase_rag.mcp.tools.GraphUpdater"):
            assert "Error" not in await first.update_repository()

        assert project not in ingestor._marker_store, (
            "a completed update left the marker set, so every later reingest "
            "would refuse for a run that actually finished"
        )

        second = self._registry(temp_project_root, ingestor)
        _mark_indexed(second)
        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            updater_cls.return_value.reingest.return_value = SimpleNamespace(
                reparsed=[], affected=[], removed=[], skipped=[], elapsed_ms=1.0
            )
            result = await second.reingest(["a.py"])
        assert "error" not in result, f"a completed run must not refuse: {result}"

    async def test_a_failed_first_index_leaves_a_marker(
        self, temp_project_root: Path
    ) -> None:
        """A FIRST index has no Project node yet, and must still mark.

        The marker used to be a property set on the Project with `MATCH`,
        which updates zero rows when no Project exists. `GraphUpdater.run()`
        is what creates it, so a first index that failed left no marker at
        all -- precisely the run the guard exists to catch (#1705 review).
        """
        ingestor = self._store()
        first = self._registry(temp_project_root, ingestor)
        from codebase_rag.utils.path_utils import derive_project_name

        project = derive_project_name(Path(first.project_root))
        # No list_projects entry: nothing has ever been indexed.
        ingestor.list_projects.return_value = []

        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            updater_cls.return_value.run.side_effect = RuntimeError("first index died")
            assert "Error" in await first.index_repository()

        assert ingestor._marker_store.get(project) is True, (
            "a failed FIRST index left no marker, so a fresh process cannot "
            "tell the graph is partial"
        )

    async def test_the_marker_survives_the_project_delete_an_index_performs(
        self, temp_project_root: Path
    ) -> None:
        """`index_repository` deletes the Project before rebuilding it.

        A marker stored on the Project would be destroyed by the very
        operation whose failure it records, so it lives on its own
        unconnected node instead (#1705 review). The fake's delete_project
        drops Project-anchored keys, so a regression to that storage fails
        here rather than passing quietly.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(registry)

        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            updater_cls.return_value.run.side_effect = RuntimeError("rebuild died")
            assert "Error" in await registry.index_repository()

        ingestor.delete_project.assert_called_once()
        assert ingestor._marker_store.get(project) is True, (
            "the project delete removed the marker it was supposed to outlive"
        )

    async def test_a_failed_clear_does_not_report_the_run_complete(
        self, temp_project_root: Path
    ) -> None:
        """A clear that never reached the store must not read as complete.

        Otherwise this process believes the run finished while the graph
        still says incomplete, and a fresh registry refuses reingest forever
        with nothing to explain it (#1705 review).
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(registry)
        ingestor._failing.add("clear")

        with patch("codebase_rag.mcp.tools.GraphUpdater"):
            result = await registry.update_repository()

        assert ingestor._marker_store.get(project) is True, (
            "fixture guard: the clear must actually have failed, or this test "
            "proves nothing"
        )
        # Invariant (b): a run whose marker could not be cleared is a
        # RETRYABLE FAILURE, not a success. Reporting success would leave the
        # marker blocking every later reingest with nothing to show the user
        # why (#1705 review, round 4). This assertion was inverted before that
        # round: it required the run to report success, which is precisely the
        # state the invariant now forbids.
        assert "Error" in result, (
            f"a run that left its marker stuck must report a failure: {result}"
        )
        assert registry._graph_incomplete is True, (
            "the local flag reported complete while the graph still says "
            "incomplete; a fresh registry would refuse reingest forever"
        )

    async def test_an_unwritable_marker_aborts_before_destructive_work(
        self, temp_project_root: Path
    ) -> None:
        """A mark that cannot be stored must stop the run, not proceed.

        The marker is the only protection that survives a restart. Indexing
        on without it means a crash leaves a partial graph a fresh process
        cannot distinguish from a complete one, and a scoped reingest then
        treats it as authoritative. Nothing is lost by refusing: no graph
        state has changed yet (#1705 review).

        Asserts the DELETE never happened, not merely that an error was
        returned: an abort that still wiped the project would satisfy an
        error-message assertion while doing the exact damage this prevents.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        _mark_indexed(registry)
        ingestor._failing.add("mark")

        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            result = await registry.index_repository()

        assert "Error" in result, "an unstorable marker must fail the run"
        (
            ingestor.delete_project.assert_not_called(),
            (
                "the run destroyed the existing graph despite having no marker "
                "to record that it had started"
            ),
        )
        updater_cls.assert_not_called()

    async def test_an_unreadable_marker_refuses_reingest(
        self, temp_project_root: Path
    ) -> None:
        """ "Cannot tell" must not be read as "the last run finished".

        Returning False on a failed read hydrates a scoped reingest from a
        graph that may be partial, which is precisely the failure the marker
        exists to prevent. An update_repository recovers either way.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        _mark_indexed(registry)
        ingestor._failing.add("read")

        refused = await registry.reingest(["a.py"])

        assert "failed part way" in refused.get("error", ""), (
            f"an unreadable marker was treated as a completed run; got {refused}"
        )

    async def test_the_marker_precedes_constraint_migration(
        self, temp_project_root: Path
    ) -> None:
        """`ensure_constraints` is destructive, so the marker must precede it.

        It runs `_migrate_legacy_path_keys`, which drops constraints and can
        run purge queries -- autocommitted work. Marking after it left a
        window where a crash during migration produced a damaged graph with
        nothing recording that a run had started (#1705 review, round 2).

        Asserts the ORDER of calls rather than the end state: after a
        successful run both have happened either way, so only the sequence
        distinguishes the fixed code from the broken code.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        _mark_indexed(registry)

        order: list[str] = []
        ingestor.ensure_constraints.side_effect = lambda: order.append("constraints")
        real_write = ingestor.execute_write.side_effect

        def _tracking_write(query: str, params: dict | None = None) -> None:
            if "SET m.run_incomplete" in query:
                order.append("mark")
            return real_write(query, params)

        ingestor.execute_write.side_effect = _tracking_write

        with patch("codebase_rag.mcp.tools.GraphUpdater"):
            await registry.update_repository()

        assert "mark" in order and "constraints" in order, (
            f"fixture guard: both steps must run, saw {order}"
        )
        assert order.index("mark") < order.index("constraints"), (
            "the constraint migration ran before the marker was written, so a "
            f"crash during it would leave no record of the run: {order}"
        )

    async def test_deleting_a_project_clears_its_durable_marker(
        self, temp_project_root: Path
    ) -> None:
        """An intentional delete must not strand the marker.

        The marker lives on its own node so `delete_project` cannot reach it,
        which is what lets it survive an index's delete-then-rebuild. The
        consequence is that a deliberate deletion leaves it behind, and a
        fresh registry then refuses reingest forever for a project the user
        removed on purpose (#1705 review, round 2).
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(registry)

        result = await registry.delete_project(project)

        assert result.get("success"), f"the delete itself must succeed: {result}"
        assert project not in ingestor._marker_store, (
            "the deleted project kept its incomplete-run marker, so a fresh "
            "registry would refuse reingest for a project that is gone"
        )


class TestIncompleteMarkerInvariant:
    """The invariant, over every mutating path and both failure points.

    Stated once in `tools.py` and asserted once here, because three review
    rounds each found the same shape next to the previous fix -- the rule was
    being re-derived per call site instead of written down (#1705 round 4):

      (a) no destructive step starts unless the marker was persisted
      (b) success is reported only after the marker is durably cleared
      (c) the fresh scoped-reingest path marks before its constraint migration
    """

    @staticmethod
    def _store() -> MagicMock:
        return TestIncompleteMarkerSurvivesTheProcess._store()

    def _registry(self, root: Path, ingestor: MagicMock) -> MCPToolsRegistry:
        return MCPToolsRegistry(
            project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
        )

    @pytest.mark.parametrize("path", ["index", "update", "delete", "reingest"])
    @pytest.mark.parametrize("failure", ["mark", "clear"])
    async def test_a_marker_failure_never_reports_success(
        self, temp_project_root: Path, path: str, failure: str
    ) -> None:
        """Every path, both failure points: a failure is reported, never success.

        The `mark` case additionally pins invariant (a) by asserting the
        destructive call never happened -- an abort that still deleted the
        project would satisfy an error-message assertion while doing the exact
        damage the marker exists to prevent.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(registry)
        ingestor._failing.add(failure)

        with patch("codebase_rag.mcp.tools.GraphUpdater"):
            if path == "index":
                result = str(await registry.index_repository())
            elif path == "update":
                result = str(await registry.update_repository())
            elif path == "delete":
                result = str(await registry.delete_project(project))
            else:
                # The fresh-reingest path: no retained updater, so it
                # hydrates, marks before its constraint migration, and must
                # clear on success. Its clear was hand-written rather than
                # routed through the helper and kept reporting success on a
                # failed clear after every other path was fixed (#1705 r5).
                registry._live_updater = None
                result = str(await registry.reingest(["a.py"]))

        assert "Error" in result or "error" in result, (
            f"{path} with a failing {failure} reported success: {result}"
        )
        if failure == "mark":
            assert not ingestor.delete_project.called, (
                f"{path} began destructive work despite failing to persist "
                "the marker, so a crash would leave nothing recording it"
            )

    async def test_a_retained_updater_reingest_is_marked_too(
        self, temp_project_root: Path
    ) -> None:
        """The fifth mutating path: reingest through a RETAINED updater.

        Both branches of `_reingest_sync` reach `updater.reingest`, which
        mutates, but the marker was established only inside the hydrating
        branch. A reingest through a retained updater therefore ran entirely
        unmarked, and an interruption left a partially mutated graph a
        restarted process could not tell from a complete one (#1705 round 6).

        Drives the retained path by leaving `_live_updater` set, which is the
        state a previous successful call leaves behind.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        project = _mark_indexed(registry)

        retained = MagicMock()
        retained.reingest.return_value = SimpleNamespace(
            reparsed=[], affected=[], removed=[], skipped=[], elapsed_ms=1.0
        )
        registry._live_updater = retained

        marked_during: list[bool] = []
        retained.reingest.side_effect = lambda *a, **k: (
            marked_during.append(ingestor._marker_store.get(project) is True)
            or SimpleNamespace(
                reparsed=[], affected=[], removed=[], skipped=[], elapsed_ms=1.0
            )
        )

        await registry.reingest(["a.py"])

        assert marked_during == [True], (
            "the retained-updater reingest mutated the graph without the "
            "durable marker set, so an interruption would leave no record"
        )
        assert project not in ingestor._marker_store, (
            "the marker must be cleared once the reingest completes"
        )

    async def test_a_fresh_scoped_reingest_marks_before_migrating(
        self, temp_project_root: Path
    ) -> None:
        """Invariant (c): the third `ensure_constraints` caller, easy to miss.

        The index and update paths were fixed first; this one was found a
        round later, still running the migration that drops constraints and
        can purge with nothing recording the run. Asserts the ORDER, because
        after a success both have happened either way.
        """
        ingestor = self._store()
        registry = self._registry(temp_project_root, ingestor)
        _mark_indexed(registry)

        order: list[str] = []
        ingestor.ensure_constraints.side_effect = lambda: order.append("constraints")
        real_write = ingestor.execute_write.side_effect

        def _tracking(query: str, params: dict | None = None) -> None:
            if "SET m.run_incomplete" in query:
                order.append("mark")
            return real_write(query, params)

        ingestor.execute_write.side_effect = _tracking

        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            updater_cls.return_value.reingest.return_value = SimpleNamespace(
                reparsed=[], affected=[], removed=[], skipped=[], elapsed_ms=1.0
            )
            await registry.reingest(["a.py"])

        assert order[:2] == ["mark", "constraints"], (
            "the fresh scoped reingest ran its destructive constraint "
            f"migration before recording the run: {order}"
        )
