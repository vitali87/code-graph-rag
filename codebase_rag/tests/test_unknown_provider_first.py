"""A misspelled provider is reported as unknown before any key is asked for.

`--orchestrator antropic:claude-sonnet-4-5` printed "API Key Missing" for an
invented `ANTROPIC_API_KEY`, and exporting that variable changed nothing: no
setting could satisfy the gate, and the existing "Unknown provider" error,
raised only when the agent was built, was never reached (issue #2897).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from codebase_rag import cli as cli_module
from codebase_rag import constants as cs
from codebase_rag.config import ModelConfig, settings
from codebase_rag.providers.base import get_provider
from codebase_rag.tools.health_checker import HealthChecker


@pytest.fixture
def misspelled_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", "antropic")
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "claude-sonnet-4-5")
    monkeypatch.setattr(settings, "CYPHER_PROVIDER", "")
    monkeypatch.setattr(settings, "CYPHER_MODEL", "")
    monkeypatch.setattr(settings, "_active_orchestrator", None)
    monkeypatch.setattr(settings, "_active_cypher", None)
    # Even the variable the old message asked for changes nothing.
    monkeypatch.setenv("ANTROPIC_API_KEY", "sk-test")


@pytest.mark.usefixtures("misspelled_orchestrator")
def test_the_startup_gate_names_the_unknown_provider() -> None:
    with patch.object(cli_module.app_context, "console") as console:
        with pytest.raises(typer.Exit) as exc_info:
            cli_module.validate_models_early()
    assert exc_info.value.exit_code == 1
    printed = str(console.print.call_args)
    assert "Unknown provider 'antropic'" in printed, printed
    assert "Did you mean 'anthropic'?" in printed, printed
    assert "API_KEY" not in printed, printed


@pytest.mark.parametrize(
    ("flag", "value", "suggestion"),
    [
        ("--orchestrator", "antropic:claude-sonnet-4-5", "anthropic"),
        ("--cypher", "opnai:gpt-4o", "openai"),
    ],
)
def test_the_start_flag_names_the_unknown_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    value: str,
    suggestion: str,
) -> None:
    # The issue's reproduction, through the real `start` command.
    monkeypatch.setattr(settings, "_active_orchestrator", None)
    monkeypatch.setattr(settings, "_active_cypher", None)
    args = ["start", "--repo-path", str(tmp_path), "--no-sync", flag, value]
    result = CliRunner().invoke(cli_module.app, [*args, "-a", "hi"])
    assert result.exit_code == 1, result.output
    provider = value.partition(":")[0]
    assert f"Unknown provider '{provider}'" in result.output, result.output
    assert f"Did you mean '{suggestion}'?" in result.output, result.output
    assert "API Key Missing" not in result.output, result.output


@pytest.mark.usefixtures("misspelled_orchestrator")
def test_doctor_names_the_unknown_provider() -> None:
    result = HealthChecker().check_model_role(cs.ModelRole.ORCHESTRATOR)
    assert not result.passed
    assert result.error is not None
    assert "Unknown provider 'antropic'" in result.error, result.error


def test_a_known_provider_without_a_key_still_asks_for_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: the key gate is unchanged for a provider that exists.
    monkeypatch.delenv(cs.ENV_ANTHROPIC_API_KEY, raising=False)
    config = ModelConfig(provider="anthropic", model_id="claude-sonnet-4-5")
    with pytest.raises(ValueError) as exc_info:
        config.validate_api_key(cs.ModelRole.ORCHESTRATOR)
    assert "ANTHROPIC_API_KEY" in str(exc_info.value)
    assert "Unknown provider" not in str(exc_info.value)


def test_a_keyless_provider_passes() -> None:
    # Negative: a local provider still needs no key.
    ModelConfig(provider="ollama", model_id="llama3.2").validate_api_key()


def test_the_check_matches_what_the_runtime_accepts() -> None:
    # The runtime looks providers up as written, so `OLLAMA` passed the key
    # gate and then failed when the agent was built.
    with pytest.raises(ValueError) as exc_info:
        ModelConfig(provider="OLLAMA", model_id="llama3.2").validate_api_key()
    assert "Did you mean 'ollama'?" in str(exc_info.value)


def test_a_name_far_from_any_provider_gets_no_suggestion() -> None:
    config = ModelConfig(provider="zzz", model_id="m")
    with pytest.raises(ValueError) as exc_info:
        config.validate_api_key()
    message = str(exc_info.value)
    assert "Unknown provider 'zzz'" in message and "Did you mean" not in message


def test_get_provider_suggests_the_same_way() -> None:
    with pytest.raises(ValueError) as exc_info:
        get_provider("opnai")
    assert "Did you mean 'openai'?" in str(exc_info.value)
