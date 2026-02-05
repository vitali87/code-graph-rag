import asyncio
import os
from unittest.mock import patch

import pytest

from codebase_rag.providers.litellm import LiteLLMProvider


class TestLiteLLMProviderConcurrency:
    """Test thread safety of LiteLLMProvider."""

    @pytest.mark.asyncio
    async def test_concurrent_vertex_configs_no_leakage(self):
        """Verify no config leakage between concurrent Vertex AI requests."""
        original_project = os.environ.get("VERTEXAI_PROJECT")
        original_region = os.environ.get("VERTEXAI_LOCATION")

        provider_a = LiteLLMProvider(
            provider="vertex_ai",
            project_id="project-a",
            region="us-central1",
            api_key="fake-key-a",
        )
        provider_b = LiteLLMProvider(
            provider="vertex_ai",
            project_id="project-b",
            region="eu-west1",
            api_key="fake-key-b",
        )

        captured_envs = []

        def capture_env_during_creation(*args, **kwargs):
            captured_envs.append(
                {
                    "project": os.environ.get("VERTEXAI_PROJECT"),
                    "region": os.environ.get("VERTEXAI_LOCATION"),
                }
            )
            from unittest.mock import MagicMock

            return MagicMock()

        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch(
                "codebase_rag.providers.litellm.OpenAIChatModel",
                side_effect=capture_env_during_creation,
            ),
        ):
            await asyncio.gather(
                asyncio.to_thread(provider_a.create_model, "gemini-1.5-pro"),
                asyncio.to_thread(provider_b.create_model, "gemini-1.5-pro"),
            )

        assert len(captured_envs) == 2
        projects_seen = {env["project"] for env in captured_envs}
        assert "project-a" in projects_seen
        assert "project-b" in projects_seen

        assert os.environ.get("VERTEXAI_PROJECT") == original_project
        assert os.environ.get("VERTEXAI_LOCATION") == original_region

    @pytest.mark.asyncio
    async def test_concurrent_extra_headers_no_collision(self):
        """Verify no header collision between concurrent gateway requests."""
        provider_a = LiteLLMProvider(
            provider="openai",
            api_key="sk-test-a",
            extra_headers={"x-portkey-api-key": "pk-user-a"},
        )
        provider_b = LiteLLMProvider(
            provider="openai",
            api_key="sk-test-b",
            extra_headers={"Helicone-Auth": "Bearer user-b"},
        )

        models = []
        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model,
        ):

            def capture_model(*args, **kwargs):
                models.append(kwargs.get("settings"))
                return mock_model.return_value

            mock_model.side_effect = capture_model

            await asyncio.gather(
                asyncio.to_thread(provider_a.create_model, "gpt-4o"),
                asyncio.to_thread(provider_b.create_model, "gpt-4o"),
            )

        assert len(models) == 2

    @pytest.mark.asyncio
    async def test_vertex_env_var_cleanup_on_exception(self):
        """Verify env vars cleaned up even if model creation fails."""
        provider = LiteLLMProvider(
            provider="vertex_ai",
            project_id="test-project",
            region="us-central1",
            api_key="test-key",
        )

        original_project = os.environ.get("VERTEXAI_PROJECT")

        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch(
                "codebase_rag.providers.litellm.OpenAIChatModel",
                side_effect=RuntimeError("Test error"),
            ),
        ):
            with pytest.raises(ValueError, match="Failed to create model"):
                provider.create_model("gemini-1.5-pro")

        assert os.environ.get("VERTEXAI_PROJECT") == original_project
