"""The live tool-calling integration tests must skip on local Ollama models.

Small local models (e.g. llama3.2) frequently emit tool calls as JSON text
instead of native tool-call messages, so asserting that tools executed tests
the model, not our pydantic-ai wiring. The suite must skip those assertions
when the active orchestrator is the OLLAMA provider and keep them for
capable API providers.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig, settings
from codebase_rag.tests.integration.test_tool_calling import (
    _orchestrator_reliably_tool_calls,
)


def _set_orchestrator(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    monkeypatch.setattr(
        settings,
        "_active_orchestrator",
        ModelConfig(provider=provider, model_id="some-model"),
    )


def test_ollama_orchestrator_is_not_trusted_for_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_orchestrator(monkeypatch, cs.Provider.OLLAMA)

    assert _orchestrator_reliably_tool_calls() is False


@pytest.mark.parametrize(
    "provider",
    [cs.Provider.OPENAI, cs.Provider.ANTHROPIC, cs.Provider.GOOGLE],
)
def test_api_orchestrators_are_trusted_for_tool_calls(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    _set_orchestrator(monkeypatch, provider)

    assert _orchestrator_reliably_tool_calls() is True
