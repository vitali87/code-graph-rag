import os
import subprocess
import sys

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import (
    API_KEY_INFO,
    ModelConfig,
    format_missing_api_key_errors,
)


def test_import_does_not_walk_parent_directories_for_dotenv(tmp_path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    (parent / ".env").write_text("GOOGLE_API_KEY=parent-secret\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("GOOGLE_API_KEY", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; import codebase_rag.config; "
            "print(os.environ.get('GOOGLE_API_KEY', 'missing'))",
        ],
        cwd=child,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing"


class TestValidateApiKey:
    @pytest.fixture(autouse=True)
    def _no_provider_keys_in_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clear every provider key the gate reads.

        These cells assert that an absent or placeholder key is REFUSED, which
        is only true when the environment does not supply one. Since #1913 the
        gate honours the provider variable (`OPENAI_API_KEY`, `GOOGLE_API_KEY`,
        ...), so a developer who exports one in their shell would otherwise see
        six of them flip -- the cells would be measuring the shell, not the
        gate. A cell that wants a variable set sets it itself.
        """
        for info in API_KEY_INFO.values():
            monkeypatch.delenv(info["env_var"], raising=False)

    def test_local_providers_skip_validation(self) -> None:
        cfg = ModelConfig(provider=cs.Provider.OLLAMA, model_id="llama3")
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

    @pytest.mark.parametrize(
        "provider",
        [
            cs.Provider.OPENAI,
            cs.Provider.GOOGLE,
            cs.Provider.ANTHROPIC,
            cs.Provider.AZURE,
            cs.Provider.MINIMAX,
        ],
    )
    def test_the_gate_accepts_the_variable_its_message_names(
        self, provider: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#1913: the gate read a hand-kept subset that left OpenAI and Google
        out, so it refused a configuration naming the very variable it told the
        user to set -- and one the provider's own `_resolve_api_key` accepts.

        Every provider, not just the two that were broken: the point is that
        the gate and the message read one table, so a sixth provider cannot
        reintroduce the split.
        """
        env_var = API_KEY_INFO[provider]["env_var"]
        monkeypatch.setenv(env_var, "provider-variable-key")
        cfg = ModelConfig(provider=provider, model_id="m")

        cfg.validate_api_key(role="orchestrator")

    @pytest.mark.parametrize(
        ("provider", "env_var"),
        [
            (cs.Provider.OPENAI, cs.ENV_OPENAI_API_KEY),
            (cs.Provider.GOOGLE, cs.ENV_GOOGLE_API_KEY),
        ],
    )
    def test_the_message_names_the_variable_the_gate_reads(
        self, provider: str, env_var: str
    ) -> None:
        """The other half of #1913: when it does refuse, the variable it names
        has to be the one that would have satisfied it."""
        cfg = ModelConfig(provider=provider, model_id="m")

        with pytest.raises(ValueError) as excinfo:
            cfg.validate_api_key(role="orchestrator")

        assert env_var in str(excinfo.value)

    def test_an_unknown_provider_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The accept control. Deriving the gate from the message table must
        not become "any variable named after the provider will do": a provider
        with no entry has no `_resolve_api_key` fallback either, so accepting
        one would let through a configuration that cannot run.
        """
        monkeypatch.setenv("MADEUP_API_KEY", "k")
        cfg = ModelConfig(provider="madeup", model_id="m")

        with pytest.raises(ValueError, match="API Key Missing"):
            cfg.validate_api_key()

    def test_an_explicit_key_still_wins_over_an_absent_variable(self) -> None:
        """The second accept control: the configured key path is untouched."""
        cfg = ModelConfig(
            provider=cs.Provider.OPENAI, model_id="m", api_key="sk-configured"
        )

        cfg.validate_api_key()

    def test_minimax_provider_env_key_passes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(cs.ENV_MINIMAX_API_KEY, "minimax-key")
        cfg = ModelConfig(provider=cs.Provider.MINIMAX, model_id="MiniMax-M3")
        cfg.validate_api_key()


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

    def test_known_provider_minimax(self) -> None:
        msg = format_missing_api_key_errors(cs.Provider.MINIMAX)
        assert "MINIMAX_API_KEY" in msg
        assert "https://platform.minimax.io/" in msg
        assert "MiniMax" in msg

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
