# A mistyped model id must fail at startup, not at the first query (issue #1492).
#
# Reported as:
#
#     pydantic_ai.exceptions.ModelHTTPError: status_code: 404,
#     model_name: opus-5, body: {'type': 'error', 'error':
#     {'type': 'not_found_error', 'message': 'model: opus-5'}}
#
# `opus-5` is not a model id -- the real one is `claude-opus-5`. Startup
# already runs `_validate_provider_config` for both roles, but that checks
# CREDENTIALS only: `validate_config()` takes no model id, so it structurally
# cannot catch this. The user gets a raw provider traceback after ingestion
# has already run.
#
# The hard constraint is the other direction. Ollama is this project's
# DEFAULT provider and pydantic-ai enumerates zero Ollama models, because
# the model space is whatever the user has pulled locally. The same is true
# of LiteLLM and any custom endpoint. A strict allow-list would reject every
# legitimate local setup -- turning a rare typo into a universal outage.
#
# So the check is provider-scoped: enforced where the catalogue is
# authoritative, silent where the model space is open by design.
from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig


def _config(provider: str, model_id: str) -> ModelConfig:
    return ModelConfig(
        provider=provider,
        model_id=model_id,
        api_key="k",
        project_id="p",
    )


class TestTheReportedTypo:
    """The exact string from the issue."""

    def test_the_reported_model_id_is_rejected(self) -> None:
        """`opus-5` must not reach the provider.

        This is the whole issue: it is a plausible-looking id that no
        provider serves, and today it costs a full startup plus a 404.
        """
        from codebase_rag.providers.base import validate_model_id

        with pytest.raises(ValueError) as excinfo:
            validate_model_id(_config(cs.Provider.ANTHROPIC, "opus-5"))

        assert "opus-5" in str(excinfo.value)

    def test_the_error_names_the_real_model_id(self) -> None:
        """A rejection that does not say what to type instead is half a fix.

        `opus-5` -> `claude-opus-5` is a near-miss, so the message must
        carry the correction.

        Asserting the SUGGESTION form, not just that `claude-opus-5` occurs
        somewhere: the fallback branch lists the whole catalogue, which
        contains that id too. A mutation dropping suggestions entirely once
        passed this test for exactly that reason -- the assertion was true
        of the working and the degraded message alike.
        """
        from codebase_rag.providers.base import validate_model_id

        with pytest.raises(ValueError) as excinfo:
            validate_model_id(_config(cs.Provider.ANTHROPIC, "opus-5"))

        message = str(excinfo.value)

        assert "Did you mean" in message
        assert "claude-opus-5" in message
        # The near-miss branch offers at most three; the fallback dumps all
        # 17. Counting them is what distinguishes the two messages.
        assert message.count("claude-") <= 3


class TestValidIdsSurvive:
    """The control: rejecting real models would be a worse bug."""

    @pytest.mark.parametrize(
        "provider,model_id",
        [
            (cs.Provider.ANTHROPIC, "claude-opus-5"),
            (cs.Provider.ANTHROPIC, "claude-haiku-4-5"),
            (cs.Provider.OPENAI, "gpt-4o"),
        ],
    )
    def test_a_real_model_id_passes(self, provider: str, model_id: str) -> None:
        """Each must pass on its own, so one bad entry cannot hide behind another."""
        from codebase_rag.providers.base import validate_model_id

        validate_model_id(_config(provider, model_id))


class TestOpenModelSpaces:
    """Providers whose catalogue cannot be enumerated must not be gated.

    This is the constraint that shapes the design, and it is measured
    rather than assumed: pydantic-ai ships zero Ollama entries.
    """

    def test_pydantic_ai_really_enumerates_no_ollama_models(self) -> None:
        """The premise, asserted directly.

        If a future pydantic-ai DID enumerate Ollama models, the exemption
        below would silently become over-permissive and this test is what
        would say so. Without it the exemption looks like an arbitrary
        choice rather than a forced one.
        """
        from pydantic_ai.models import known_model_names

        ollama = [n for n in known_model_names() if n.startswith("ollama")]

        assert ollama == []

    @pytest.mark.parametrize(
        "provider,model_id",
        [
            (cs.Provider.OLLAMA, "llama3"),
            (cs.Provider.OLLAMA, "qwen2.5-coder:7b"),
            (cs.Provider.OLLAMA, "some-locally-pulled-model"),
        ],
    )
    def test_a_local_model_is_not_rejected(self, provider: str, model_id: str) -> None:
        """Ollama is the DEFAULT provider.

        Gating it would break the out-of-the-box configuration for every
        local user -- the expensive error direction by a wide margin.
        """
        from codebase_rag.providers.base import validate_model_id

        validate_model_id(_config(provider, model_id))


class TestCustomEndpoints:
    """A non-default endpoint reopens the model space.

    The catalogue describes what the PROVIDER'S OWN endpoint serves. Point
    `provider=openai` at vLLM or an OpenAI-compatible proxy -- which the
    docs actively recommend -- and the served model names are whatever that
    server hosts. Validating those against OpenAI's catalogue rejects a
    working configuration, which is the expensive direction again.
    """

    def test_a_custom_openai_endpoint_is_not_gated(self) -> None:
        """The concrete case: a self-hosted model behind the OpenAI protocol."""
        from codebase_rag.providers.base import validate_model_id

        config = ModelConfig(
            provider=cs.Provider.OPENAI,
            model_id="Qwen2.5-Coder-32B-Instruct",
            api_key="k",
            endpoint="http://localhost:8000/v1",
        )

        validate_model_id(config)

    def test_the_default_endpoint_is_still_gated(self) -> None:
        """The control, and the one that keeps the fix meaningful.

        Without it, `endpoint`-suppression could be written to disable the
        check for everyone -- the typo from the issue would sail through
        again and every other test here would still pass.
        """
        from codebase_rag.providers.base import validate_model_id

        config = ModelConfig(
            provider=cs.Provider.OPENAI,
            model_id="Qwen2.5-Coder-32B-Instruct",
            api_key="k",
            endpoint=cs.OPENAI_DEFAULT_ENDPOINT,
        )

        with pytest.raises(ValueError):
            validate_model_id(config)


class TestStartupWiring:
    """The check must actually run at startup, not merely exist."""

    def test_startup_validation_rejects_the_bad_id(self) -> None:
        """A validator nothing calls fixes nothing.

        `_validate_provider_config` is the startup seam that already runs
        for both roles. This asserts the model id is checked THERE, which
        is the difference between a helpful message and the reported 404.
        """
        from codebase_rag.main import _validate_provider_config

        with pytest.raises(ValueError) as excinfo:
            _validate_provider_config(
                cs.ModelRole.ORCHESTRATOR,
                _config(cs.Provider.ANTHROPIC, "opus-5"),
            )

        assert "opus-5" in str(excinfo.value)

    def test_startup_validation_accepts_a_good_id(self) -> None:
        """The control for the wiring test.

        Without it, a `_validate_provider_config` that raised on EVERY
        config would pass the test above while breaking all startups.
        """
        from codebase_rag.main import _validate_provider_config

        _validate_provider_config(
            cs.ModelRole.ORCHESTRATOR,
            _config(cs.Provider.ANTHROPIC, "claude-opus-5"),
        )
