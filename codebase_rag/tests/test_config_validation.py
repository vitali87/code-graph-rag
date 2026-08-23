import os
import subprocess
import sys

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import AppConfig, ModelConfig, format_missing_api_key_errors


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
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "missing"


class TestValidateApiKey:
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


class TestGraphBackendDefaulting:
    """GRAPH_BACKEND must default to memgraph rather than fail validation
    when a user blanks it out -- the obvious way to "turn it off" in .env
    (see docs/getting-started/choosing-a-graph-backend.md)."""

    def test_unset_defaults_to_memgraph(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GRAPH_BACKEND", raising=False)
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.MEMGRAPH

    def test_empty_string_defaults_to_memgraph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.MEMGRAPH

    def test_whitespace_only_defaults_to_memgraph(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "   ")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.MEMGRAPH

    def test_explicit_arcadedb_is_respected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "arcadedb")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.ARCADEDB

    def test_padded_value_is_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", " memgraph ")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.MEMGRAPH

    def test_mixed_case_value_is_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "Memgraph")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.MEMGRAPH

    def test_padded_mixed_case_arcadedb_is_normalized(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "  ArcadeDB  ")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.GRAPH_BACKEND == cs.GraphBackend.ARCADEDB

    def test_invalid_value_still_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRAPH_BACKEND", "not-a-backend")
        with pytest.raises(ValueError, match="GRAPH_BACKEND"):
            AppConfig(_env_file=None)  # ty: ignore[unknown-argument]


class TestArcadeHttpScheme:
    """ARCADEDB_HTTP_SCHEME threads into ArcadeHttpClient, which refuses
    plaintext Basic auth to a non-loopback host (see test_arcade_http.py's
    TestPlaintextCredentialsRefused for the enforcement itself)."""

    def test_defaults_to_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARCADEDB_HTTP_SCHEME", raising=False)
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.ARCADEDB_HTTP_SCHEME == cs.ArcadeHttpScheme.HTTP

    def test_accepts_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCADEDB_HTTP_SCHEME", "https")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.ARCADEDB_HTTP_SCHEME == cs.ArcadeHttpScheme.HTTPS

    def test_invalid_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCADEDB_HTTP_SCHEME", "ftp")
        with pytest.raises(ValueError, match="ARCADEDB_HTTP_SCHEME"):
            AppConfig(_env_file=None)  # ty: ignore[unknown-argument]


class TestArcadeBoltScheme:
    """ARCADEDB_BOLT_SCHEME threads into ArcadeDBIngestor, which refuses
    plaintext Bolt traffic (credentials plus all graph data) to a
    non-loopback host (see test_arcadedb_ingestor.py's
    TestBoltPlaintextCredentialsRefused for the enforcement itself)."""

    def test_defaults_to_bolt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARCADEDB_BOLT_SCHEME", raising=False)
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.ARCADEDB_BOLT_SCHEME == cs.ArcadeBoltScheme.BOLT

    def test_accepts_bolt_s(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCADEDB_BOLT_SCHEME", "bolt+s")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.ARCADEDB_BOLT_SCHEME == cs.ArcadeBoltScheme.BOLT_S

    def test_accepts_bolt_ssc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCADEDB_BOLT_SCHEME", "bolt+ssc")
        config = AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
        assert config.ARCADEDB_BOLT_SCHEME == cs.ArcadeBoltScheme.BOLT_SSC

    def test_invalid_value_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARCADEDB_BOLT_SCHEME", "ftp")
        with pytest.raises(ValueError, match="ARCADEDB_BOLT_SCHEME"):
            AppConfig(_env_file=None)  # ty: ignore[unknown-argument]
