import os
from unittest.mock import patch

from codebase_rag.config import AppConfig


class TestGitHubIssuesIntegration:
    """Integration tests for specific GitHub issue scenarios."""

    def test_env_file_ollama_configuration_respected(self) -> None:
        """
        Test the scenario where a user sets up .env file with Ollama configuration
        and expects it to be used instead of defaulting to other providers.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "ollama",
            "ORCHESTRATOR_MODEL": "llama3.2",
            "ORCHESTRATOR_ENDPOINT": "http://localhost:11434/v1",
            "CYPHER_PROVIDER": "ollama",
            "CYPHER_MODEL": "codellama",
            "CYPHER_ENDPOINT": "http://localhost:11434/v1",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "ollama", (
                "Should use Ollama from .env, not default to Gemini"
            )
            assert orchestrator.model_id == "llama3.2"
            assert orchestrator.endpoint == "http://localhost:11434/v1"

            cypher = config.active_cypher_config
            assert cypher.provider == "ollama", (
                "Should use Ollama from .env, not default to Gemini"
            )
            assert cypher.model_id == "codellama"
            assert cypher.endpoint == "http://localhost:11434/v1"

    def test_custom_model_names_with_colons_parsing(self) -> None:
        """
        Test the scenario where a user wants to use custom model names
        that contain colons, which would previously be parsed incorrectly.
        """
        config = AppConfig()

        provider, model = config.parse_model_string("openai/gpt-oss:20b")
        assert provider == "openai", "Should correctly identify provider"
        assert model == "gpt-oss:20b", "Should correctly preserve model name with colon"

        test_cases = [
            ("ollama/mistral:7b-instruct", "ollama", "mistral:7b-instruct"),
            ("google/custom:model:v1.2", "google", "custom:model:v1.2"),
            (
                "openai/ft:gpt-3.5-turbo:my-org:custom:model:id",
                "openai",
                "ft:gpt-3.5-turbo:my-org:custom:model:id",
            ),
        ]

        for input_string, expected_provider, expected_model in test_cases:
            provider, model = config.parse_model_string(input_string)
            assert provider == expected_provider, f"Failed for {input_string}"
            assert model == expected_model, f"Failed for {input_string}"

    def test_mixed_provider_real_world_scenario(self) -> None:
        """
        Test a realistic mixed provider scenario that users might want.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "google",
            "ORCHESTRATOR_MODEL": "gemini-2.5-pro",
            "ORCHESTRATOR_API_KEY": "test-google-key",
            "CYPHER_PROVIDER": "ollama",
            "CYPHER_MODEL": "llama3.2:8b",
            "CYPHER_ENDPOINT": "http://localhost:11434/v1",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "google"
            assert orchestrator.model_id == "gemini-2.5-pro"
            assert orchestrator.api_key == "test-google-key"

            cypher = config.active_cypher_config
            assert cypher.provider == "ollama"
            assert cypher.model_id == "llama3.2:8b"
            assert cypher.endpoint == "http://localhost:11434/v1"

    def test_cli_override_real_scenario(self) -> None:
        """
        Test CLI overrides work in realistic scenarios where users
        want to temporarily use different models.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "ollama",
            "ORCHESTRATOR_MODEL": "llama3.2",
            "CYPHER_PROVIDER": "ollama",
            "CYPHER_MODEL": "codellama",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            assert config.active_orchestrator_config.provider == "ollama"
            assert config.active_cypher_config.provider == "ollama"

            config.set_orchestrator("google", "gemini-2.5-pro", api_key="temp-key")
            config.set_cypher("openai", "gpt-4o-mini", api_key="temp-openai-key")

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "google"
            assert orchestrator.model_id == "gemini-2.5-pro"
            assert orchestrator.api_key == "temp-key"

            cypher = config.active_cypher_config
            assert cypher.provider == "openai"
            assert cypher.model_id == "gpt-4o-mini"
            assert cypher.api_key == "temp-openai-key"

    def test_openai_compatible_endpoints(self) -> None:
        """
        Test that users can use OpenAI-compatible endpoints like Together AI, etc.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "openai",
            "ORCHESTRATOR_MODEL": "meta-llama/Llama-2-70b-chat-hf",
            "ORCHESTRATOR_API_KEY": "together-api-key",
            "ORCHESTRATOR_ENDPOINT": "https://api.together.xyz/v1",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "openai"
            assert orchestrator.model_id == "meta-llama/Llama-2-70b-chat-hf"
            assert orchestrator.api_key == "together-api-key"
            assert orchestrator.endpoint == "https://api.together.xyz/v1"

    def test_vertex_ai_enterprise_scenario(self) -> None:
        """
        Test enterprise Vertex AI configuration scenario.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "google",
            "ORCHESTRATOR_MODEL": "gemini-2.5-pro",
            "ORCHESTRATOR_PROJECT_ID": "my-enterprise-project",
            "ORCHESTRATOR_REGION": "us-central1",
            "ORCHESTRATOR_PROVIDER_TYPE": "vertex",
            "ORCHESTRATOR_SERVICE_ACCOUNT_FILE": "/path/to/service-account.json",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "google"
            assert orchestrator.model_id == "gemini-2.5-pro"
            assert orchestrator.project_id == "my-enterprise-project"
            assert orchestrator.region == "us-central1"
            assert orchestrator.provider_type == "vertex"
            assert orchestrator.service_account_file == "/path/to/service-account.json"

    def test_reasoning_model_thinking_budget(self) -> None:
        """
        Test configuration for reasoning models with thinking budget.
        """
        env_content = {
            "ORCHESTRATOR_PROVIDER": "google",
            "ORCHESTRATOR_MODEL": "gemini-2.0-flash-thinking-exp",
            "ORCHESTRATOR_API_KEY": "test-key",
            "ORCHESTRATOR_THINKING_BUDGET": "15000",
        }

        with patch.dict(os.environ, env_content):
            config = AppConfig()

            orchestrator = config.active_orchestrator_config
            assert orchestrator.provider == "google"
            assert orchestrator.model_id == "gemini-2.0-flash-thinking-exp"
            assert orchestrator.thinking_budget == 15000
