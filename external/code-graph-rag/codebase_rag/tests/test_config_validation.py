import pytest

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig, format_missing_api_key_errors


class TestValidateApiKey:
    @pytest.mark.parametrize(
        ("provider", "model_id"),
        [
            (cs.Provider.OLLAMA, "llama3"),
            (cs.Provider.LOCAL, "local-model"),
            (cs.Provider.VLLM, "vllm-model"),
        ],
    )
    def test_local_providers_skip_validation(
        self, provider: cs.Provider, model_id: str
    ) -> None:
        cfg = ModelConfig(provider=provider, model_id=model_id)
        cfg.validate_api_key()

    def test_google_vertex_skips_validation(self) -> None:
        cfg = ModelConfig(
            provider=cs.Provider.GOOGLE,
            model_id="gemini-pro",
            provider_type=cs.GoogleProviderType.VERTEX,
        )
        cfg.validate_api_key()

    def test_google_gla_requires_api_key(self) -> None:
        cfg = ModelConfig(
            provider=cs.Provider.GOOGLE,
            model_id="gemini-pro",
            provider_type=cs.GoogleProviderType.GLA,
        )
        with pytest.raises(ValueError, match="API Key Missing"):
            cfg.validate_api_key()

    @pytest.mark.parametrize(
        "api_key_kwargs",
        [
            {},
            {"api_key": ""},
            {"api_key": "   "},
            {"api_key": cs.DEFAULT_API_KEY},
        ],
    )
    def test_invalid_api_key_raises(self, api_key_kwargs: dict[str, str]) -> None:
        cfg = ModelConfig(
            provider=cs.Provider.OPENAI, model_id="gpt-4", **api_key_kwargs
        )
        with pytest.raises(ValueError, match="API Key Missing"):
            cfg.validate_api_key()

    def test_valid_api_key_passes(self) -> None:
        cfg = ModelConfig(
            provider=cs.Provider.OPENAI, model_id="gpt-4", api_key="sk-real-key-123"
        )
        cfg.validate_api_key()

    def test_role_forwarded_to_error_message(self) -> None:
        cfg = ModelConfig(provider=cs.Provider.OPENAI, model_id="gpt-4")
        with pytest.raises(ValueError, match="cypher"):
            cfg.validate_api_key(role="cypher")


class TestFormatMissingApiKeyErrors:
    def test_known_provider_openai(self) -> None:
        msg = format_missing_api_key_errors(cs.Provider.OPENAI)
        assert "OPENAI_API_KEY" in msg
        assert "https://platform.openai.com/api-keys" in msg
        assert "OpenAI" in msg

    def test_known_provider_anthropic(self) -> None:
        msg = format_missing_api_key_errors(cs.Provider.ANTHROPIC)
        assert "ANTHROPIC_API_KEY" in msg
        assert "Anthropic" in msg

    def test_unknown_provider_generic_message(self) -> None:
        msg = format_missing_api_key_errors("deepseek")
        assert "DEEPSEEK_API_KEY" in msg
        assert "Deepseek" in msg

    def test_role_appears_in_message(self) -> None:
        msg = format_missing_api_key_errors(cs.Provider.OPENAI, role="cypher")
        assert "for cypher" in msg

    def test_default_role_omits_role_from_message(self) -> None:
        msg = format_missing_api_key_errors(cs.Provider.OPENAI)
        assert "for model" not in msg

    def test_case_insensitive_lookup(self) -> None:
        msg = format_missing_api_key_errors("OpenAI")
        assert "OPENAI_API_KEY" in msg
        assert "OpenAI" in msg
