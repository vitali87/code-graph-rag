from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

from pydantic_ai.models.openai import OpenAIChatModel

from codebase_rag.constants import Provider
from codebase_rag.providers.base import (
    ModelProvider,
    get_provider,
    list_providers,
    register_provider,
)
from codebase_rag.providers.litellm import LiteLLMProvider


class TestProviderRegistry:
    def test_get_valid_providers(self) -> None:
        google_provider = get_provider(
            Provider.GOOGLE, api_key="test-key", provider_type="api"
        )
        assert isinstance(google_provider, LiteLLMProvider)
        assert google_provider.provider_name == Provider.GOOGLE

        openai_provider = get_provider(Provider.OPENAI, api_key="test-key")
        assert isinstance(openai_provider, LiteLLMProvider)
        assert openai_provider.provider_name == Provider.OPENAI

        ollama_provider = get_provider(
            Provider.OLLAMA, endpoint="http://localhost:11434/v1"
        )
        assert isinstance(ollama_provider, LiteLLMProvider)
        assert ollama_provider.provider_name == Provider.OLLAMA

    def test_get_unknown_provider(self) -> None:
        provider = get_provider("unknown_provider", api_key="xyz")
        assert isinstance(provider, LiteLLMProvider)
        assert provider.provider_name == "unknown_provider"

    def test_list_providers(self) -> None:
        providers = list_providers()
        assert Provider.GOOGLE in providers
        assert Provider.OPENAI in providers
        assert Provider.OLLAMA in providers
        assert len(providers) > 20
        assert (
            "bedrock" in providers
            or "sagemaker" in providers
            or "replicate" in providers
        )

    def test_register_custom_provider(self) -> None:
        class CustomProvider(ModelProvider):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._provider_name = "custom"

            @property
            def provider_name(self) -> str:
                return self._provider_name

            def validate_config(self) -> None:
                pass

            def create_model(
                self, model_id: str, **kwargs: str | int | None
            ) -> OpenAIChatModel:
                return MagicMock(spec=OpenAIChatModel)

        register_provider("custom", CustomProvider)

        providers = list_providers()
        assert "custom" in providers

        custom_provider = get_provider("custom")
        assert isinstance(custom_provider, CustomProvider)


class TestLiteLLMProvider:
    def test_initialization(self) -> None:
        provider = LiteLLMProvider(
            provider=Provider.OPENAI,
            api_key="sk-test",
            endpoint="https://api.openai.com/v1",
            project_id="my-project",
            region="us-east-1",
            thinking_budget=1000,
        )
        assert provider.provider_name == Provider.OPENAI
        assert provider.api_key == "sk-test"
        assert provider.endpoint == "https://api.openai.com/v1"
        assert provider.project_id == "my-project"
        assert provider.region == "us-east-1"
        assert provider.thinking_budget == 1000

    @patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider")
    @patch("codebase_rag.providers.litellm.OpenAIChatModel")
    def test_create_model(
        self, mock_openai_model: Any, mock_pydantic_provider: Any
    ) -> None:
        provider = LiteLLMProvider(provider=Provider.GOOGLE, api_key="test-key")

        mock_model = MagicMock()
        mock_openai_model.return_value = mock_model

        provider.create_model("gemini-1.5-pro")

        mock_pydantic_provider.assert_called_once_with(
            api_key="test-key", api_base=None
        )

        mock_openai_model.assert_called_once()
        args = mock_openai_model.call_args[0]
        assert args[0] == "google/gemini-1.5-pro"
        assert "provider" in mock_openai_model.call_args[1]

    def test_vertex_env_vars(self) -> None:
        provider = LiteLLMProvider(
            provider=Provider.GOOGLE, project_id="vertex-proj", region="us-central1"
        )

        with patch.dict(os.environ, {}, clear=True):
            with (
                patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
                patch("codebase_rag.providers.litellm.OpenAIChatModel"),
            ):
                provider.create_model("gemini-pro")

                assert os.environ.get("VERTEXAI_PROJECT") == "vertex-proj"
                assert os.environ.get("VERTEXAI_LOCATION") == "us-central1"
