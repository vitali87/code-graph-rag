from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from codebase_rag.constants import GoogleProviderType, Provider
from codebase_rag.providers.base import (
    AnthropicProvider,
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

    def test_get_invalid_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider 'invalid_provider'"):
            get_provider("invalid_provider")

    def test_list_providers(self) -> None:
        providers = list_providers()
        assert Provider.GOOGLE in providers
        assert Provider.OPENAI in providers
        assert Provider.OLLAMA in providers
        assert Provider.ANTHROPIC in providers
        assert len(providers) >= 4

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


class TestAnthropicProvider:
    def test_anthropic_with_api_key(self) -> None:
        provider = AnthropicProvider(api_key="sk-ant-test-key")
        assert provider.provider_name == Provider.ANTHROPIC
        assert provider.api_key == "sk-ant-test-key"

        provider.validate_config()

    def test_anthropic_with_proxy_headers(self) -> None:
        headers = {
            "x-portkey-api-key": "pk-test",
            "x-portkey-config": "pc-test",
        }
        provider = AnthropicProvider(
            endpoint="https://portkey.example.com",
            custom_headers=headers,
        )
        assert provider.provider_name == Provider.ANTHROPIC
        assert provider.custom_headers == headers
        assert provider.endpoint == "https://portkey.example.com"

        provider.validate_config()

    def test_anthropic_validation_error_no_auth(self) -> None:
        with patch(
            "codebase_rag.providers.base.get_anthropic_config_from_claude_settings"
        ) as mock:
            mock.return_value = (None, {})
            provider = AnthropicProvider()

            with pytest.raises(
                ValueError,
                match="Anthropic provider requires either api_key or custom_headers",
            ):
                provider.validate_config()

    def test_anthropic_custom_endpoint(self) -> None:
        custom_endpoint = "https://custom-anthropic.example.com/v1"
        provider = AnthropicProvider(
            api_key="sk-ant-test-key",
            endpoint=custom_endpoint,
        )
        assert provider.endpoint == custom_endpoint

    def test_anthropic_loads_claude_settings(self) -> None:
        with patch(
            "codebase_rag.providers.base.get_anthropic_config_from_claude_settings"
        ) as mock:
            mock.return_value = (
                "https://portkey.example.com",
                {"x-portkey-api-key": "pk-from-settings"},
            )
            provider = AnthropicProvider()
            assert provider.endpoint == "https://portkey.example.com"
            assert provider.custom_headers == {"x-portkey-api-key": "pk-from-settings"}

            provider.validate_config()

    @patch("codebase_rag.providers.base.PydanticAnthropicProvider")
    @patch("codebase_rag.providers.base.AnthropicModel")
    def test_anthropic_model_creation_with_api_key(
        self, mock_anthropic_model: Any, mock_anthropic_provider: Any
    ) -> None:
        provider = AnthropicProvider(api_key="sk-ant-test-key")

        mock_model = MagicMock()
        mock_anthropic_model.return_value = mock_model

        provider.create_model("claude-sonnet-4.5-20250929")

        mock_anthropic_provider.assert_called_once_with(
            api_key="sk-ant-test-key",
            base_url="https://api.anthropic.com/v1",
            http_client=None,
        )
        mock_anthropic_model.assert_called_once()
        call_kwargs = mock_anthropic_model.call_args[1]
        assert call_kwargs["model_name"] == "claude-sonnet-4.5-20250929"
        assert call_kwargs["provider"] == mock_anthropic_provider.return_value

    @patch("codebase_rag.providers.base.httpx.AsyncClient")
    @patch("codebase_rag.providers.base.PydanticAnthropicProvider")
    @patch("codebase_rag.providers.base.AnthropicModel")
    def test_anthropic_model_creation_with_custom_headers(
        self,
        mock_anthropic_model: Any,
        mock_anthropic_provider: Any,
        mock_http_client: Any,
    ) -> None:
        headers = {"x-portkey-api-key": "pk-test"}
        provider = AnthropicProvider(
            endpoint="https://portkey.example.com",
            custom_headers=headers,
        )

        mock_model = MagicMock()
        mock_anthropic_model.return_value = mock_model
        mock_client = MagicMock()
        mock_http_client.return_value = mock_client

        provider.create_model("claude-haiku-4-20250514")

        mock_http_client.assert_called_once_with(headers=headers, timeout=30.0)
        mock_anthropic_provider.assert_called_once_with(
            api_key="proxy-auth-via-headers",
            base_url="https://portkey.example.com",
            http_client=mock_client,
        )
        mock_anthropic_model.assert_called_once()
        call_kwargs = mock_anthropic_model.call_args[1]
        assert call_kwargs["model_name"] == "claude-haiku-4-20250514"
        assert call_kwargs["provider"] == mock_anthropic_provider.return_value
