from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.prompts import (
    build_cypher_system_prompt,
    build_local_cypher_system_prompt,
)
from codebase_rag.services.llm import CypherGenerator


class TestCypherSystemPromptProjectScope:
    def test_single_project_instructs_starts_with_scoping(self) -> None:
        prompt = build_cypher_system_prompt(active_projects=["myproj"])
        assert "myproj" in prompt
        assert "STARTS WITH 'myproj.'" in prompt

    def test_no_projects_notes_multiple_projects_possible(self) -> None:
        prompt = build_cypher_system_prompt(active_projects=None)
        assert "STARTS WITH" in prompt

    def test_multiple_projects_lists_each(self) -> None:
        prompt = build_cypher_system_prompt(active_projects=["a", "b"])
        assert "STARTS WITH 'a.'" in prompt
        assert "STARTS WITH 'b.'" in prompt

    def test_local_prompt_single_project_scopes(self) -> None:
        prompt = build_local_cypher_system_prompt(active_projects=["only_one"])
        assert "only_one" in prompt
        assert "STARTS WITH 'only_one.'" in prompt

    def test_project_scoped_example_present(self) -> None:
        prompt = build_cypher_system_prompt(active_projects=None)
        assert "qualified_name STARTS WITH 'myproject.'" in prompt


class TestCypherGeneratorProjectScope:
    @patch("codebase_rag.services.llm.settings")
    @patch("codebase_rag.services.llm.get_provider_from_config")
    @patch("codebase_rag.services.llm.Agent")
    def test_generator_threads_project_into_system_prompt(
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

        CypherGenerator(active_projects=["scoped_proj"])

        system_prompt = mock_agent.call_args.kwargs["system_prompt"]
        assert "STARTS WITH 'scoped_proj.'" in system_prompt
