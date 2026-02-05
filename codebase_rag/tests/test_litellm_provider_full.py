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
    """Test creating models with various model_id formats (without provider prefix)."""
    provider = LiteLLMProvider(provider=Provider.OPENAI, api_key="test")

    with (
        patch(
            "codebase_rag.providers.litellm.PydanticLiteLLMProvider"
        ) as mock_pydantic_provider,
        patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model,
    ):
        provider.create_model("gpt-4o")
        args, kwargs = mock_model.call_args
        assert args[0] == "openai/gpt-4o"
        assert kwargs["provider"] == mock_pydantic_provider.return_value
        assert isinstance(kwargs["settings"], dict)

        provider.create_model("ft:gpt-3.5-turbo:my-org:custom:model:id")
        args, kwargs = mock_model.call_args
        assert args[0] == "openai/ft:gpt-3.5-turbo:my-org:custom:model:id"

        provider_ollama = LiteLLMProvider(provider="ollama", api_key="test")
        provider_ollama.create_model("llama3:7b-instruct")
        args, kwargs = mock_model.call_args
        assert args[0] == "ollama/llama3:7b-instruct"


def test_provider_specific_env_vars():
    """Verify project_id/region are set in os.environ when creating the model."""
    provider = LiteLLMProvider(
        provider="vertex_ai",
        project_id="test-project",
        region="us-central1",
        api_key="test",
    )

    env_during_creation = {}

    def capture_env(*args, **kwargs):
        env_during_creation["project"] = os.environ.get("VERTEXAI_PROJECT")
        env_during_creation["location"] = os.environ.get("VERTEXAI_LOCATION")
        from unittest.mock import MagicMock

        return MagicMock()

    with patch.dict(os.environ, {}, clear=True):
        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch(
                "codebase_rag.providers.litellm.OpenAIChatModel",
                side_effect=capture_env,
            ),
        ):
            provider.create_model("gemini-1.5-pro")

            assert env_during_creation["project"] == "test-project"
            assert env_during_creation["location"] == "us-central1"


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


def test_service_account_file_path_resolution():
    """Verify service account file path is resolved to absolute path."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        sa_file = f.name
        f.write('{"type": "service_account"}')

    env_during_creation = {}

    def capture_env(*args, **kwargs):
        env_during_creation["creds"] = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        from unittest.mock import MagicMock

        return MagicMock()

    try:
        provider = LiteLLMProvider(
            provider="vertex_ai",
            project_id="test-project",
            region="us-central1",
            api_key="test",
            service_account_file=sa_file,
        )

        with (
            patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
            patch(
                "codebase_rag.providers.litellm.OpenAIChatModel",
                side_effect=capture_env,
            ),
        ):
            provider.create_model("gemini-1.5-pro")

            creds = env_during_creation["creds"]
            assert creds is not None
            assert Path(creds).is_absolute()
    finally:
        os.unlink(sa_file)


def test_timeout_configuration():
    """Verify timeout settings are passed to ModelSettings."""
    provider = LiteLLMProvider(provider="openai", api_key="test")

    with patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model:
        with patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"):
            provider.create_model("gpt-4")

        _, kwargs = mock_model.call_args
        settings = kwargs.get("settings")
        assert settings is not None
        assert settings["timeout"] == 300


def test_timeout_configuration_custom():
    """Verify custom timeout settings are respected."""
    provider = LiteLLMProvider(provider="openai", api_key="test")

    with patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model:
        with patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"):
            provider.create_model("gpt-4", timeout=600, stream_timeout=60)

        _, kwargs = mock_model.call_args
        settings = kwargs.get("settings")
        assert settings is not None
        assert settings["timeout"] == 600


def test_vertex_env_cleanup_after_creation():
    """Verify Vertex AI env vars are cleaned up after model creation."""
    original_project = os.environ.get("VERTEXAI_PROJECT")
    original_location = os.environ.get("VERTEXAI_LOCATION")

    provider = LiteLLMProvider(
        provider="vertex_ai",
        project_id="test-project",
        region="us-west1",
        api_key="test",
    )

    with (
        patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"),
        patch("codebase_rag.providers.litellm.OpenAIChatModel"),
    ):
        provider.create_model("gemini-1.5-pro")

    assert os.environ.get("VERTEXAI_PROJECT") == original_project
    assert os.environ.get("VERTEXAI_LOCATION") == original_location


def test_thinking_budget_passed_to_extra_body():
    """Verify thinking_budget is passed in extra_body."""
    provider = LiteLLMProvider(
        provider="openai",
        api_key="test",
        thinking_budget=10000,
    )

    with patch("codebase_rag.providers.litellm.OpenAIChatModel") as mock_model:
        with patch("codebase_rag.providers.litellm.PydanticLiteLLMProvider"):
            provider.create_model("gpt-4")

        _, kwargs = mock_model.call_args
        settings = kwargs.get("settings")
        assert settings is not None
        assert settings["extra_body"]["thinking_budget"] == 10000
