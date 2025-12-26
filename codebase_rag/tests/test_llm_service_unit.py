from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.services.llm import (
    CypherGenerator,
    _clean_cypher_response,
    create_rag_orchestrator,
)


class TestCleanCypherResponse:
    def test_removes_leading_whitespace(self) -> None:
        result = _clean_cypher_response("   MATCH (n) RETURN n")
        assert result.startswith("MATCH")

    def test_removes_trailing_whitespace(self) -> None:
        result = _clean_cypher_response("MATCH (n) RETURN n   ")
        assert result.endswith(";")

    def test_removes_backticks(self) -> None:
        result = _clean_cypher_response("```MATCH (n) RETURN n```")
        assert "```" not in result

    def test_removes_cypher_prefix(self) -> None:
        result = _clean_cypher_response("cypher MATCH (n) RETURN n")
        assert not result.lower().startswith("cypher ")

    def test_adds_semicolon_if_missing(self) -> None:
        result = _clean_cypher_response("MATCH (n) RETURN n")
        assert result.endswith(";")

    def test_keeps_existing_semicolon(self) -> None:
        result = _clean_cypher_response("MATCH (n) RETURN n;")
        assert result == "MATCH (n) RETURN n;"
        assert not result.endswith(";;")

    def test_handles_complex_query(self) -> None:
        query = """```cypher
MATCH (n:Function)-[:CALLS]->(m:Function)
WHERE n.name = 'main'
RETURN m.name
```"""
        result = _clean_cypher_response(query)
        assert result.startswith("MATCH")
        assert result.endswith(";")
        assert "```" not in result

    def test_handles_multiline_query(self) -> None:
        query = """MATCH (n)
WHERE n.type = 'class'
RETURN n.name"""
        result = _clean_cypher_response(query)
        assert result.endswith(";")
        assert "MATCH" in result


class TestCypherGenerator:
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    def test_init_creates_agent(
        self,
        mock_agent: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.provider = cs.Provider.GOOGLE
        mock_settings.active_cypher_config = mock_config
        mock_settings.AGENT_RETRIES = 3

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        generator = CypherGenerator()

        mock_agent.assert_called_once()
        assert generator.agent is not None

    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    def test_init_raises_on_error(
        self, mock_get_provider: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.active_cypher_config = MagicMock()
        mock_get_provider.side_effect = Exception("Provider error")

        with pytest.raises(ex.LLMGenerationError):
            CypherGenerator()

    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    def test_uses_local_prompt_for_ollama(
        self,
        mock_agent: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.provider = cs.Provider.OLLAMA
        mock_settings.active_cypher_config = mock_config
        mock_settings.AGENT_RETRIES = 3

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        CypherGenerator()

        call_kwargs = mock_agent.call_args.kwargs
        assert "system_prompt" in call_kwargs


class TestCypherGeneratorGenerate:
    @pytest.mark.asyncio
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    async def test_generate_returns_cleaned_query(
        self,
        mock_agent_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.provider = cs.Provider.GOOGLE
        mock_settings.active_cypher_config = mock_config
        mock_settings.AGENT_RETRIES = 3

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        mock_result = MagicMock()
        mock_result.output = "MATCH (n) RETURN n"
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_agent_cls.return_value = mock_agent

        generator = CypherGenerator()
        result = await generator.generate("Find all nodes")

        assert result == "MATCH (n) RETURN n;"

    @pytest.mark.asyncio
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    async def test_generate_raises_on_invalid_output(
        self,
        mock_agent_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.provider = cs.Provider.GOOGLE
        mock_settings.active_cypher_config = mock_config
        mock_settings.AGENT_RETRIES = 3

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        mock_result = MagicMock()
        mock_result.output = "Invalid response with no query keyword"
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=mock_result)
        mock_agent_cls.return_value = mock_agent

        generator = CypherGenerator()
        with pytest.raises(ex.LLMGenerationError):
            await generator.generate("Find all nodes")

    @pytest.mark.asyncio
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    async def test_generate_raises_on_agent_error(
        self,
        mock_agent_cls: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_config.provider = cs.Provider.GOOGLE
        mock_settings.active_cypher_config = mock_config
        mock_settings.AGENT_RETRIES = 3

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=Exception("API error"))
        mock_agent_cls.return_value = mock_agent

        generator = CypherGenerator()
        with pytest.raises(ex.LLMGenerationError):
            await generator.generate("Find all nodes")


class TestCreateRagOrchestrator:
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    @patch("codebase_rag.services.llm.build_rag_orchestrator_prompt")
    def test_creates_agent_with_tools(
        self,
        mock_build_prompt: MagicMock,
        mock_agent: MagicMock,
        mock_get_provider: MagicMock,
        mock_settings: MagicMock,
    ) -> None:
        mock_config = MagicMock()
        mock_settings.active_orchestrator_config = mock_config
        mock_settings.AGENT_RETRIES = 3
        mock_settings.ORCHESTRATOR_OUTPUT_RETRIES = 2

        mock_provider = MagicMock()
        mock_provider.create_model.return_value = MagicMock()
        mock_get_provider.return_value = mock_provider

        mock_build_prompt.return_value = "System prompt"
        mock_agent.return_value = MagicMock()

        tools = [MagicMock(), MagicMock()]
        result = create_rag_orchestrator(tools)

        mock_agent.assert_called_once()
        call_kwargs = mock_agent.call_args.kwargs
        assert call_kwargs["tools"] == tools
        assert result is not None

    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    def test_raises_on_error(
        self, mock_get_provider: MagicMock, mock_settings: MagicMock
    ) -> None:
        mock_settings.active_orchestrator_config = MagicMock()
        mock_get_provider.side_effect = Exception("Config error")

        with pytest.raises(ex.LLMGenerationError):
            create_rag_orchestrator([])
