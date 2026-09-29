"""A blank or sentinel credential is no credential, whatever its source (#2119).

`validate_api_key` already stripped the role's own `api_key` and rejected the
`ollama` placeholder, but the environment was read raw in three places: the
start-up gate, `_resolve_api_key`, and the OpenAI embedding client. So `"  "`
or `"ollama"` in a provider variable passed the gate and failed later in the
provider call path, and the embedder sent `Authorization: Bearer   `.

Every site keeps two controls: a valid key is still accepted, and an unset
variable is still refused, so a green result cannot come from a site that
accepts or refuses everything.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import (
    API_KEY_INFO,
    ModelConfig,
    normalised_credential,
    settings,
)
from codebase_rag.embedder import _openai_client
from codebase_rag.providers.base import _resolve_api_key

NOT_A_KEY = ["", "   ", "\t\n", cs.DEFAULT_API_KEY, f"  {cs.DEFAULT_API_KEY} "]
REAL_KEY = "sk-real-key"

_ANTHROPIC_VAR = API_KEY_INFO[cs.Provider.ANTHROPIC]["env_var"]
_OPENAI_VAR = API_KEY_INFO[cs.Provider.OPENAI]["env_var"]


class TestTheHelper:
    @pytest.mark.parametrize("value", [None, *NOT_A_KEY])
    def test_a_blank_or_sentinel_value_is_none(self, value: str | None) -> None:
        assert normalised_credential(value) is None

    def test_a_key_comes_back_stripped(self) -> None:
        """Whitespace around a pasted key is accidental, so it is removed
        rather than sent to the provider."""
        assert normalised_credential(f"  {REAL_KEY}\n") == REAL_KEY


class TestTheStartUpGate:
    def _gate(self) -> None:
        ModelConfig(provider=cs.Provider.ANTHROPIC, model_id="m").validate_api_key()

    @pytest.mark.parametrize("value", NOT_A_KEY)
    def test_a_blank_or_sentinel_provider_variable_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(_ANTHROPIC_VAR, value)
        with pytest.raises(ValueError, match="API Key Missing"):
            self._gate()

    def test_a_real_provider_variable_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_ANTHROPIC_VAR, REAL_KEY)
        self._gate()

    def test_an_unset_provider_variable_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(_ANTHROPIC_VAR, raising=False)
        with pytest.raises(ValueError, match="API Key Missing"):
            self._gate()


class TestTheProviderResolver:
    @pytest.mark.parametrize("value", NOT_A_KEY)
    def test_the_environment_is_held_to_the_argument_rule(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(_OPENAI_VAR, value)
        assert _resolve_api_key(None, _OPENAI_VAR) is None
        assert _resolve_api_key(cs.DEFAULT_API_KEY, _OPENAI_VAR) is None

    def test_a_blank_argument_falls_through_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OPENAI_VAR, f" {REAL_KEY} ")
        assert _resolve_api_key("   ", _OPENAI_VAR) == REAL_KEY

    def test_an_explicit_key_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_OPENAI_VAR, "from-the-environment")
        assert _resolve_api_key(REAL_KEY, _OPENAI_VAR) == REAL_KEY

    def test_an_unset_variable_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(_OPENAI_VAR, raising=False)
        assert _resolve_api_key(None, _OPENAI_VAR) is None


class TestTheEmbeddingClientHeader:
    """Asserted on the header the client carries, not on a key read back: the
    defect was the header's shape (`Bearer   `)."""

    def _authorization(self) -> str | None:
        with _openai_client() as client:
            return client.headers.get("Authorization")

    @pytest.mark.parametrize("value", NOT_A_KEY)
    def test_a_blank_or_sentinel_key_sends_no_header(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", value)
        monkeypatch.setenv(_OPENAI_VAR, value)
        assert self._authorization() is None

    def test_a_blank_setting_falls_through_to_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", "   ")
        monkeypatch.setenv(_OPENAI_VAR, REAL_KEY)
        assert self._authorization() == f"Bearer {REAL_KEY}"

    def test_a_real_key_is_sent_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", f" {REAL_KEY} ")
        assert self._authorization() == f"Bearer {REAL_KEY}"

    def test_no_key_anywhere_sends_no_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "OPENAI_EMBEDDING_API_KEY", None)
        monkeypatch.delenv(_OPENAI_VAR, raising=False)
        assert self._authorization() is None
