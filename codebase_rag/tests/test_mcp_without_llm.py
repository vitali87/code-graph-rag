"""`cgr mcp-server` must start without a reachable Cypher model (issue #2518).

Only `query_code_graph` turns natural language into Cypher; indexing and the
deterministic graph tools run fixed queries. Building the Cypher model at
start-up let an unreachable provider (the default Ollama, not running) stop
the whole server, and the CLI then wrote the error to stdout -- the stdio
transport's JSON-RPC stream -- and exited 0.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from pydantic_ai.exceptions import ModelAPIError

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.config import ModelConfig
from codebase_rag.mcp import server as srv
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherQueryGenerator
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.workspaces import WorkspaceConfig

OLLAMA_DOWN = "Ollama server not responding"
GENERATED_QUERY = "MATCH (f:Function) RETURN f.qualified_name AS qualified_name;"
ROW = {"qualified_name": "proj.calc.add"}

# Tools that need no LLM: indexing, the fixed graph queries, and the
# file tools. Each must be served by a server that has no Cypher model.
NO_LLM_TOOLS = (
    cs.MCPToolName.INDEX_REPOSITORY,
    cs.MCPToolName.RESOLVE,
    cs.MCPToolName.DEFINITION,
    cs.MCPToolName.CALLERS,
    cs.MCPToolName.CALLEES,
    cs.MCPToolName.TESTS_REACHING,
    cs.MCPToolName.LIST_PROJECTS,
    cs.MCPToolName.READ_FILE,
)


def _ollama() -> ModelConfig:
    return ModelConfig(provider="ollama", model_id="llama3.2", api_key=None)


@contextmanager
def _server_without_cypher_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[MCPToolsRegistry, MagicMock, MagicMock]]:
    """Build the real server against a default (Ollama) configuration whose
    endpoint does not answer, and hand back the registry it serves.

    Only the network edges are replaced: the Ollama health probe, the
    Memgraph connection and the pydantic-ai agent. Everything between them
    -- provider selection, CypherGenerator, the registry -- is the real code.
    """
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(tmp_path))
    built: list[MCPToolsRegistry] = []
    real_registry = srv.create_mcp_tools_registry

    def capture(
        project_root: str,
        ingestor: MemgraphIngestor,
        cypher_gen: CypherQueryGenerator,
        workspace: WorkspaceConfig | None = None,
    ) -> MCPToolsRegistry:
        registry = real_registry(
            project_root=project_root,
            ingestor=ingestor,
            cypher_gen=cypher_gen,
            workspace=workspace,
        )
        built.append(registry)
        return registry

    ingestor = MagicMock()
    ingestor.fetch_all.return_value = []
    ingestor.fetch_read_only.return_value = [ROW]
    with (
        patch.object(srv, "setup_logging"),
        patch.object(
            type(srv.settings),
            "active_orchestrator_config",
            property(lambda _: _ollama()),
        ),
        patch.object(
            type(srv.settings), "active_cypher_config", property(lambda _: _ollama())
        ),
        patch(
            "codebase_rag.providers.base.check_ollama_running", return_value=False
        ) as ollama_up,
        patch("codebase_rag.services.llm.Agent") as agent_cls,
        patch.object(srv, "MemgraphIngestor", return_value=ingestor),
        patch.object(srv, "create_mcp_tools_registry", side_effect=capture),
    ):
        agent_cls.return_value.run = AsyncMock(
            return_value=MagicMock(output=GENERATED_QUERY)
        )
        server, _ = srv.create_server()
        assert server is not None
        yield built[0], ingestor, ollama_up


class TestServerStartsWithoutCypherModel:
    def test_create_server_succeeds_when_the_cypher_model_is_unreachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (registry, _, _):
            assert registry is not None

    def test_indexing_and_deterministic_tools_are_served(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (registry, _, _):
            served = {schema.name for schema in registry.get_tool_schemas()}
            for tool in NO_LLM_TOOLS:
                assert tool in served, tool
                assert registry.get_tool_handler(tool) is not None, tool

    async def test_a_deterministic_tool_answers_without_a_cypher_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (registry, _, _):
            handler, _ = registry.get_tool_handler(cs.MCPToolName.READ_FILE) or (
                None,
                None,
            )
            assert handler is not None
            content = await handler(file_path="calc.py")
            assert "def add" in str(content)

    async def test_query_code_graph_fails_clearly_while_no_model_is_reachable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (
            registry,
            ingestor,
            _,
        ):
            result = await registry.query_code_graph("Which functions call add?")

        # The `error` key is how an MCP caller tells a request that could not
        # be served from a genuine empty answer.
        assert OLLAMA_DOWN in result.get("error", "")
        assert "CYPHER_PROVIDER" in result["error"]
        assert result["results"] == []
        assert result["query_used"] == cs.QUERY_NOT_AVAILABLE
        ingestor.fetch_read_only.assert_not_called()

    async def test_query_code_graph_works_once_the_provider_comes_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (
            registry,
            _,
            ollama_up,
        ):
            first = await registry.query_code_graph("Which functions call add?")
            ollama_up.return_value = True
            second = await registry.query_code_graph("Which functions call add?")

        assert OLLAMA_DOWN in first.get("error", "")
        # A failed build is not remembered: no restart is needed once the
        # provider answers.
        assert "error" not in second
        assert second["query_used"] == GENERATED_QUERY
        assert second["results"] == [ROW]

    async def test_ask_agent_still_fails_clearly_without_a_reachable_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _server_without_cypher_model(tmp_path, monkeypatch) as (registry, _, _):
            result = await registry.ask_agent("What does add do?")

        assert OLLAMA_DOWN in result.get("error", "")


class TestQueryToolFailureShapes:
    """Only an unavailable model is a refusal; a model that answers badly is
    still a translation failure reported in `summary`, as before."""

    async def test_a_bad_translation_stays_a_summary_without_an_error_key(
        self,
    ) -> None:
        cypher_gen = MagicMock()
        cypher_gen.generate = AsyncMock(
            side_effect=ex.LLMGenerationError("model returned prose")
        )
        ingestor = MagicMock()
        tool = create_query_tool(ingestor=ingestor, cypher_gen=cypher_gen)

        data = await tool.function(natural_language_query="Which functions call add?")

        assert data.error is None
        assert "model returned prose" in data.summary
        ingestor.fetch_read_only.assert_not_called()

    async def test_a_built_model_that_does_not_answer_is_a_refusal(self) -> None:
        from codebase_rag.services import llm

        # Built fine -- Ollama answered its health probe, or a hosted provider
        # needs none -- and then the request itself never reached the model.
        with (
            patch.object(llm, "_create_provider_model"),
            patch.object(llm, "Agent") as agent_cls,
        ):
            agent_cls.return_value.run = AsyncMock(
                side_effect=ModelAPIError("llama3.2", "Connection error.")
            )
            ingestor = MagicMock()
            tool = create_query_tool(
                ingestor=ingestor, cypher_gen=llm.CypherGenerator()
            )

            data = await tool.function(
                natural_language_query="Which functions call add?"
            )

        assert "Connection error." in (data.error or "")
        assert "CYPHER_PROVIDER" in (data.error or "")
        assert data.query_used == cs.QUERY_NOT_AVAILABLE
        ingestor.fetch_read_only.assert_not_called()


class TestLazyCypherGenerator:
    async def test_the_model_is_not_built_until_the_first_query(self) -> None:
        from codebase_rag.services import llm

        with patch.object(llm, "CypherGenerator") as generator_cls:
            generator_cls.return_value.generate = AsyncMock(return_value="MATCH (n)")
            lazy = llm.LazyCypherGenerator(active_projects=["proj"])
            generator_cls.assert_not_called()

            assert await lazy.generate("q1") == "MATCH (n)"
            assert await lazy.generate("q2") == "MATCH (n)"

        # Built once, scoped to the server's projects, then reused.
        generator_cls.assert_called_once_with(active_projects=["proj"])

    async def test_an_unavailable_model_raises_the_refusal_type(self) -> None:
        from codebase_rag.services import llm

        with patch.object(
            llm,
            "CypherGenerator",
            side_effect=ex.LLMGenerationError(ex.LLM_INIT_CYPHER.format(error="down")),
        ):
            lazy = llm.LazyCypherGenerator()
            with pytest.raises(ex.CypherModelUnavailableError, match="down"):
                await lazy.generate("q")

    async def test_repair_asks_the_lazily_built_model(self) -> None:
        from codebase_rag.services import llm

        with patch.object(llm, "CypherGenerator") as generator_cls:
            generator_cls.return_value.generate = AsyncMock(return_value="MATCH (m)")
            lazy = llm.LazyCypherGenerator()

            assert await lazy.repair("q", "MATCH (", "syntax error") == "MATCH (m)"

        generator_cls.assert_called_once()
        sent = generator_cls.return_value.generate.await_args.args[0]
        assert "MATCH (" in sent
        assert "syntax error" in sent

    async def test_repair_with_an_unavailable_model_raises_the_refusal_type(
        self,
    ) -> None:
        from codebase_rag.services import llm

        with patch.object(
            llm,
            "CypherGenerator",
            side_effect=ex.LLMGenerationError(ex.LLM_INIT_CYPHER.format(error="down")),
        ):
            lazy = llm.LazyCypherGenerator()
            with pytest.raises(ex.CypherModelUnavailableError, match="down"):
                await lazy.repair("q", "MATCH (", "syntax error")


def _run_cli(
    serve: AsyncMock, transport: cs.MCPTransport = cs.MCPTransport.STDIO
) -> int:
    from codebase_rag import cli

    target = (
        "codebase_rag.mcp.serve_http"
        if transport == cs.MCPTransport.HTTP
        else "codebase_rag.mcp.serve_stdio"
    )
    with patch(target, serve):
        try:
            cli.mcp_server(transport=transport, host=None, port=None, workspace=None)
        except typer.Exit as exit_:
            return exit_.exit_code
    return 0


class TestCliKeepsStdoutForTheProtocol:
    def test_a_startup_failure_goes_to_stderr_and_exits_non_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run_cli(AsyncMock(side_effect=RuntimeError("boom")))

        out, err = capsys.readouterr()
        assert code == 1
        assert out == ""
        assert "boom" in err

    def test_a_configuration_error_goes_to_stderr_and_exits_non_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        missing = tmp_path / "missing"
        monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(missing))
        with patch.object(srv, "setup_logging"):
            code = _run_cli(AsyncMock(wraps=srv.serve_stdio))

        out, err = capsys.readouterr()
        assert code == 1
        assert out == ""
        assert str(missing) in err

    def test_the_http_transport_reports_a_startup_failure_the_same_way(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run_cli(
            AsyncMock(side_effect=RuntimeError("port taken")), cs.MCPTransport.HTTP
        )

        out, err = capsys.readouterr()
        assert code == 1
        assert out == ""
        assert "port taken" in err

    def test_a_user_interrupt_is_not_a_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run_cli(AsyncMock(side_effect=KeyboardInterrupt))

        out, _ = capsys.readouterr()
        assert code == 0
        assert out == ""

    def test_a_clean_shutdown_exits_zero_and_writes_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = _run_cli(AsyncMock(return_value=None))

        out, err = capsys.readouterr()
        assert code == 0
        assert out == ""
        assert err == ""
