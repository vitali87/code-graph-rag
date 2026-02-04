import os
from unittest.mock import patch

import pytest

from codebase_rag.constants import Provider, get_all_providers
from codebase_rag.providers.litellm import LiteLLMProvider


def test_get_all_providers_dynamic():
    """Verify get_all_providers returns a large list and caches it."""
    providers = get_all_providers()
    assert isinstance(providers, list)
    assert len(providers) > 0
    assert "openai" in providers
    assert "anthropic" in providers

    providers_again = get_all_providers()
    assert providers is providers_again


def test_create_model_formats():
    """Test creating models with various formats."""
    provider = LiteLLMProvider(provider=Provider.OPENAI, api_key="test")

    with (
        patch(
            "codebase_rag.providers.litellm.PydanticLiteLLMProvider"
        ) as mock_pydantic_provider,
        patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model,
    ):
        # (H) Match settings argument structure

        provider.create_model("gpt-4o")
        args, kwargs = mock_model.call_args
        assert args[0] == "openai/gpt-4o"
        assert kwargs["provider"] == mock_pydantic_provider.return_value
        assert isinstance(kwargs["settings"], dict)

        provider.create_model("anthropic/claude-3-5-sonnet")
        args, kwargs = mock_model.call_args
        assert args[0] == "anthropic/claude-3-5-sonnet"

        provider.create_model("ollama:llama3")
        args, kwargs = mock_model.call_args
        assert args[0] == "ollama/llama3"


def test_provider_specific_env_vars():
    """Verify project_id/region are set in os.environ when creating the model."""
    provider = LiteLLMProvider(
        provider="vertex_ai",
        project_id="test-project",
        region="us-central1",
        api_key="test",
    )

    with patch.dict(os.environ, {}, clear=True):
        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch("codebase_rag.providers.litellm.OpenAIChatModel"),
        ):
            provider.create_model("gemini-1.5-pro")

            assert os.environ.get("VERTEXAI_PROJECT") == "test-project"
            assert os.environ.get("VERTEXAI_LOCATION") == "us-central1"


def test_api_key_handling():
    """Ensure API key is passed correctly."""
    api_key = "sk-test-key"
    provider = LiteLLMProvider(provider="openai", api_key=api_key)

    with (
        patch(
            "codebase_rag.providers.litellm.PydanticLiteLLMProvider"
        ) as mock_pydantic_provider,
        patch("codebase_rag.providers.litellm.OpenAIChatModel"),
    ):
        provider.create_model("gpt-4")

        mock_pydantic_provider.assert_called_once()
        args, kwargs = mock_pydantic_provider.call_args
        assert kwargs["api_key"] == api_key


def test_error_handling():
    """Mock litellm raising an exception."""
    provider = LiteLLMProvider(provider="openai", api_key="test")

    with patch(
        "codebase_rag.providers.litellm.PydanticLiteLLMProvider",
        side_effect=ValueError("Invalid config"),
    ):
        with pytest.raises(ValueError, match="Invalid config"):
            provider.create_model("gpt-4")


def test_extra_headers_handling():
    """Test that extra_headers are passed to OpenAIChatModel settings."""
    headers = {"x-portkey-api-key": "pk-test", "x-portkey-provider": "anthropic"}
    provider = LiteLLMProvider(provider="openai", api_key="test", extra_headers=headers)

    with patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model:
        with patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"):
            provider.create_model("gpt-4")

        mock_model.assert_called_once()
        _, kwargs = mock_model.call_args
        settings = kwargs.get("settings")
        assert settings is not None
        assert settings["extra_headers"] == headers
