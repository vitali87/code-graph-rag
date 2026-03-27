from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from codebase_rag.constants import GoogleProviderType, Provider
from codebase_rag.providers.base import (
    AnthropicProvider,
    AzureOpenAIProvider,
    GoogleProvider,
    ModelProvider,
    OllamaProvider,
    OpenAIProvider,
    get_provider,
    list_providers,
    register_provider,
)


class TestProviderRegistry:
    def test_get_valid_providers(self) -> None:
        google_provider = get_provider(
            Provider.GOOGLE, api_key="test-key", provider_type=GoogleProviderType.GLA
        )
        assert isinstance(google_provider, GoogleProvider)
        assert google_provider.provider_name == Provider.GOOGLE

        openai_provider = get_provider(Provider.OPENAI, api_key="test-key")
        assert isinstance(openai_provider, OpenAIProvider)
        assert openai_provider.provider_name == Provider.OPENAI

        ollama_provider = get_provider(
            Provider.OLLAMA, endpoint="http://localhost:11434/v1"
        )
        assert isinstance(ollama_provider, OllamaProvider)
        assert ollama_provider.provider_name == Provider.OLLAMA

        anthropic_provider = get_provider(Provider.ANTHROPIC, api_key="test-key")
        assert isinstance(anthropic_provider, AnthropicProvider)
        assert anthropic_provider.provider_name == Provider.ANTHROPIC

        azure_provider = get_provider(
            Provider.AZURE,
            api_key="test-key",
            endpoint="https://myresource.openai.azure.com",
        )
        assert isinstance(azure_provider, AzureOpenAIProvider)
        assert azure_provider.provider_name == Provider.AZURE

    def test_get_invalid_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider 'invalid_provider'"):
            get_provider("invalid_provider")

    def test_get_litellm_provider(self) -> None:
        litellm_provider = get_provider(
            Provider.LITELLM_PROXY,
            api_key="sk-test",
            endpoint="http://localhost:4000/v1",
        )
        from codebase_rag.providers.litellm import LiteLLMProvider

        assert isinstance(litellm_provider, LiteLLMProvider)
        assert litellm_provider.provider_name == Provider.LITELLM_PROXY

    def test_list_providers(self) -> None:
        providers = list_providers()
        assert Provider.GOOGLE in providers
        assert Provider.OPENAI in providers
        assert Provider.OLLAMA in providers
        assert Provider.ANTHROPIC in providers
        assert Provider.AZURE in providers
        assert Provider.LITELLM_PROXY in providers
        assert len(providers) >= 6

    def test_register_custom_provider(self) -> None:
        class CustomProvider(ModelProvider):
            @property
            def provider_name(self) -> Provider:
                return Provider.GOOGLE

            def validate_config(self) -> None:
                pass

            def create_model(
                self, model_id: str, **kwargs: str | int | None
            ) -> GoogleModel | OpenAIResponsesModel | OpenAIChatModel:
                return MagicMock(spec=GoogleModel)

        register_provider("custom", CustomProvider)

        providers = list_providers()
        assert "custom" in providers

        custom_provider = get_provider("custom")
        assert isinstance(custom_provider, CustomProvider)


class TestGoogleProvider:
    def test_google_gla_configuration(self) -> None:
        provider = GoogleProvider(
            api_key="test-key", provider_type=GoogleProviderType.GLA
        )
        assert provider.provider_name == Provider.GOOGLE
        assert provider.api_key == "test-key"
        assert provider.provider_type == GoogleProviderType.GLA

        provider.validate_config()

    def test_google_vertex_configuration(self) -> None:
        provider = GoogleProvider(
            provider_type=GoogleProviderType.VERTEX,
            project_id="test-project",
            region="us-central1",
            service_account_file="/path/to/service-account.json",
        )
        assert provider.provider_name == Provider.GOOGLE
        assert provider.provider_type == GoogleProviderType.VERTEX
        assert provider.project_id == "test-project"

        provider.validate_config()

    def test_google_gla_validation_error(self) -> None:
        provider = GoogleProvider(provider_type=GoogleProviderType.GLA)

        with pytest.raises(ValueError, match="Gemini GLA provider requires api_key"):
            provider.validate_config()

    def test_google_vertex_validation_error(self) -> None:
        provider = GoogleProvider(provider_type=GoogleProviderType.VERTEX)

        with pytest.raises(
            ValueError, match="Gemini Vertex provider requires project_id"
        ):
            provider.validate_config()

    def test_google_thinking_budget(self) -> None:
        provider = GoogleProvider(
            api_key="test-key",
            provider_type=GoogleProviderType.GLA,
            thinking_budget=5000,
        )
        assert provider.thinking_budget == 5000


class TestOpenAIProvider:
    def test_openai_configuration(self) -> None:
        provider = OpenAIProvider(
            api_key="sk-test-key", endpoint="https://api.openai.com/v1"
        )
        assert provider.provider_name == Provider.OPENAI
        assert provider.api_key == "sk-test-key"
        assert provider.endpoint == "https://api.openai.com/v1"

        provider.validate_config()

    def test_openai_validation_error(self) -> None:
        provider = OpenAIProvider()

        with pytest.raises(ValueError, match="OpenAI provider requires api_key"):
            provider.validate_config()

    def test_openai_custom_endpoint(self) -> None:
        provider = OpenAIProvider(
            api_key="sk-test-key", endpoint="https://api.custom-openai.com/v1"
        )
        assert provider.endpoint == "https://api.custom-openai.com/v1"


class TestOllamaProvider:
    def test_ollama_configuration(self) -> None:
        provider = OllamaProvider(
            endpoint="http://localhost:11434/v1", api_key="ollama"
        )
        assert provider.provider_name == Provider.OLLAMA
        assert provider.endpoint == "http://localhost:11434/v1"
        assert provider.api_key == "ollama"

    def test_ollama_custom_endpoint(self) -> None:
        provider = OllamaProvider(
            endpoint="http://remote-ollama:11434/v1", api_key="custom-key"
        )
        assert provider.endpoint == "http://remote-ollama:11434/v1"
        assert provider.api_key == "custom-key"

    @patch("httpx.Client")
    def test_ollama_validation_success(self, mock_client: Any) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        provider = OllamaProvider()
        provider.validate_config()

    @patch("httpx.Client")
    def test_ollama_validation_server_not_running(self, mock_client: Any) -> None:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        provider = OllamaProvider()
        with pytest.raises(ValueError, match="Ollama server not responding"):
            provider.validate_config()

    @patch("httpx.Client")
    def test_ollama_validation_connection_error(self, mock_client: Any) -> None:
        import httpx

        mock_client.return_value.__enter__.return_value.get.side_effect = (
            httpx.ConnectError("Connection failed")
        )

        provider = OllamaProvider()
        with pytest.raises(ValueError, match="Ollama server not responding"):
            provider.validate_config()


class TestAnthropicProvider:
    def test_anthropic_configuration(self) -> None:
        provider = AnthropicProvider(api_key="sk-ant-test-key")
        assert provider.provider_name == Provider.ANTHROPIC
        assert provider.api_key == "sk-ant-test-key"
        provider.validate_config()

    def test_anthropic_validation_error(self) -> None:
        provider = AnthropicProvider()
        with pytest.raises(ValueError, match="Anthropic provider requires api_key"):
            provider.validate_config()

    @patch("codebase_rag.providers.base.PydanticAnthropicProvider")
    @patch("codebase_rag.providers.base.AnthropicModel")
    def test_anthropic_model_creation(
        self, mock_anthropic_model: Any, mock_anthropic_provider: Any
    ) -> None:
        provider = AnthropicProvider(api_key="sk-ant-test-key")
        mock_model = MagicMock()
        mock_anthropic_model.return_value = mock_model
        result = provider.create_model("claude-opus-4-6")
        mock_anthropic_model.assert_called_once()
        assert result == mock_model

    def test_anthropic_api_key_from_env(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}):
            provider = AnthropicProvider()
            assert provider.api_key == "env-key"


class TestAzureOpenAIProvider:
    def test_azure_configuration(self) -> None:
        provider = AzureOpenAIProvider(
            api_key="azure-key",
            endpoint="https://myresource.openai.azure.com",
            api_version="2024-06-01",
        )
        assert provider.provider_name == Provider.AZURE
        assert provider.api_key == "azure-key"
        assert provider.endpoint == "https://myresource.openai.azure.com"
        assert provider.api_version == "2024-06-01"
        provider.validate_config()

    def test_azure_validation_error_no_key(self) -> None:
        provider = AzureOpenAIProvider(endpoint="https://myresource.openai.azure.com")
        with pytest.raises(ValueError, match="Azure OpenAI provider requires api_key"):
            provider.validate_config()

    def test_azure_validation_error_no_endpoint(self) -> None:
        provider = AzureOpenAIProvider(api_key="azure-key")
        with pytest.raises(ValueError, match="Azure OpenAI provider requires endpoint"):
            provider.validate_config()

    @patch("codebase_rag.providers.base.PydanticAzureProvider")
    @patch("codebase_rag.providers.base.OpenAIChatModel")
    def test_azure_model_creation(
        self, mock_chat_model: Any, mock_azure_provider: Any
    ) -> None:
        provider = AzureOpenAIProvider(
            api_key="azure-key",
            endpoint="https://myresource.openai.azure.com",
        )
        mock_model = MagicMock()
        mock_chat_model.return_value = mock_model
        result = provider.create_model("gpt-4o")
        mock_azure_provider.assert_called_once_with(
            api_key="azure-key",
            azure_endpoint="https://myresource.openai.azure.com",
            api_version=None,
        )
        mock_chat_model.assert_called_once_with(
            "gpt-4o", provider=mock_azure_provider.return_value
        )
        assert result == mock_model

    def test_azure_api_key_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AZURE_API_KEY": "env-key",
                "AZURE_OPENAI_ENDPOINT": "https://env.openai.azure.com",
            },
        ):
            provider = AzureOpenAIProvider()
            assert provider.api_key == "env-key"
            assert provider.endpoint == "https://env.openai.azure.com"


class TestModelCreation:
    @patch("codebase_rag.providers.base.PydanticGoogleProvider")
    @patch("codebase_rag.providers.base.GoogleModel")
    def test_google_model_creation_without_thinking_budget(
        self, mock_google_model: Any, mock_google_provider: Any
    ) -> None:
        provider = GoogleProvider(
            api_key="test-key", provider_type=GoogleProviderType.GLA
        )

        mock_model = MagicMock()
        mock_google_model.return_value = mock_model

        provider.create_model("gemini-2.5-pro")

        mock_google_model.assert_called_once()
        call_kwargs = mock_google_model.call_args[1]
        assert "settings" not in call_kwargs

    @patch("codebase_rag.providers.base.PydanticGoogleProvider")
    @patch("codebase_rag.providers.base.GoogleModel")
    @patch("codebase_rag.providers.base.GoogleModelSettings")
    def test_google_model_creation_with_thinking_budget(
        self,
        mock_model_settings: Any,
        mock_google_model: Any,
        mock_google_provider: Any,
    ) -> None:
        provider = GoogleProvider(
            api_key="test-key",
            provider_type=GoogleProviderType.GLA,
            thinking_budget=5000,
        )

        mock_model = MagicMock()
        mock_google_model.return_value = mock_model
        mock_settings = MagicMock()
        mock_model_settings.return_value = mock_settings

        provider.create_model("gemini-2.0-flash-thinking-exp")

        mock_model_settings.assert_called_once_with(
            google_thinking_config={"thinking_budget": 5000}
        )

        mock_google_model.assert_called_once()
        call_kwargs = mock_google_model.call_args[1]
        assert "settings" in call_kwargs
        assert call_kwargs["settings"] == mock_settings

    @patch("codebase_rag.providers.base.PydanticOpenAIProvider")
    @patch("codebase_rag.providers.base.OpenAIResponsesModel")
    def test_openai_model_creation(
        self, mock_openai_model: Any, mock_openai_provider: Any
    ) -> None:
        provider = OpenAIProvider(api_key="sk-test-key")

        mock_model = MagicMock()
        mock_openai_model.return_value = mock_model

        provider.create_model("gpt-4o")

        mock_openai_provider.assert_called_once_with(
            api_key="sk-test-key", base_url="https://api.openai.com/v1"
        )
        mock_openai_model.assert_called_once_with(
            "gpt-4o", provider=mock_openai_provider.return_value
        )

    @patch("codebase_rag.providers.base.PydanticOpenAIProvider")
    @patch("codebase_rag.providers.base.OpenAIChatModel")
    def test_ollama_model_creation(
        self, mock_openai_chat_model: Any, mock_openai_provider: Any
    ) -> None:
        with patch.object(OllamaProvider, "validate_config"):
            provider = OllamaProvider()

            mock_model = MagicMock()
            mock_openai_chat_model.return_value = mock_model

            provider.create_model("llama3.2")

            mock_openai_provider.assert_called_once_with(
                api_key="ollama", base_url="http://localhost:11434/v1"
            )


class TestLiteLLMProvider:
    def test_litellm_configuration(self) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        provider = LiteLLMProvider(
            api_key="sk-litellm-key", endpoint="http://litellm:4000/v1"
        )
        assert provider.provider_name == Provider.LITELLM_PROXY
        assert provider.api_key == "sk-litellm-key"
        assert provider.endpoint == "http://litellm:4000/v1"

    def test_litellm_default_endpoint(self) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        provider = LiteLLMProvider()
        assert provider.endpoint == "http://localhost:4000/v1"

    def test_litellm_no_endpoint_validation_error(self) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        provider = LiteLLMProvider(endpoint="")
        with pytest.raises(ValueError, match="LiteLLM provider requires endpoint"):
            provider.validate_config()

    @patch("httpx.Client")
    def test_litellm_validation_success(self, mock_client: Any) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        provider = LiteLLMProvider(api_key="sk-test", endpoint="http://litellm:4000/v1")
        provider.validate_config()

    @patch("httpx.Client")
    def test_litellm_validation_server_not_running(self, mock_client: Any) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        provider = LiteLLMProvider(endpoint="http://litellm:4000/v1")
        with pytest.raises(ValueError, match="LiteLLM proxy server not responding"):
            provider.validate_config()

    @patch("httpx.Client")
    def test_litellm_validation_fallback_to_models_endpoint(
        self, mock_client: Any
    ) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        health_response = MagicMock()
        health_response.status_code = 401
        models_response = MagicMock()
        models_response.status_code = 200
        mock_client.return_value.__enter__.return_value.get.side_effect = [
            health_response,
            models_response,
        ]

        provider = LiteLLMProvider(api_key="sk-test", endpoint="http://litellm:4000/v1")
        provider.validate_config()

    @patch("httpx.Client")
    def test_litellm_validation_connection_error(self, mock_client: Any) -> None:
        import httpx

        from codebase_rag.providers.litellm import LiteLLMProvider

        mock_client.return_value.__enter__.return_value.get.side_effect = (
            httpx.ConnectError("Connection failed")
        )

        provider = LiteLLMProvider(endpoint="http://litellm:4000/v1")
        with pytest.raises(ValueError, match="LiteLLM proxy server not responding"):
            provider.validate_config()

    @patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider")
    @patch("codebase_rag.providers.litellm.OpenAIChatModel")
    @patch("httpx.Client")
    def test_litellm_model_creation(
        self, mock_client: Any, mock_chat_model: Any, mock_litellm_provider: Any
    ) -> None:
        from codebase_rag.providers.litellm import LiteLLMProvider

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client.return_value.__enter__.return_value.get.return_value = mock_response

        provider = LiteLLMProvider(api_key="sk-test", endpoint="http://litellm:4000/v1")
        mock_model = MagicMock()
        mock_chat_model.return_value = mock_model

        result = provider.create_model("openai/gpt-4o")

        mock_litellm_provider.assert_called_once_with(
            api_key="sk-test", api_base="http://litellm:4000/v1"
        )
        mock_chat_model.assert_called_once_with(
            "openai/gpt-4o", provider=mock_litellm_provider.return_value
        )
        assert result == mock_model
