"""A model role with only its provider or only its model is an error.

`_get_default_config` used to honour `<ROLE>_PROVIDER` only when
`<ROLE>_MODEL` was also set and otherwise returned the Ollama default
silently. `ORCHESTRATOR_PROVIDER=anthropic` with no model therefore passed
the API-key gate (Ollama needs no key) and then failed as "Ollama not
running", or quietly ran `llama3.2` instead of the model the user wanted.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import typer

from codebase_rag import cli as cli_module
from codebase_rag import constants as cs
from codebase_rag.config import AppConfig, settings
from codebase_rag.tools.health_checker import HealthChecker

_ROLE_VARIABLES = (
    "ORCHESTRATOR_PROVIDER",
    "ORCHESTRATOR_MODEL",
    "CYPHER_PROVIDER",
    "CYPHER_MODEL",
)


def _config_from(env: dict[str, str]) -> AppConfig:
    """An AppConfig built from exactly these role variables.

    The other role variables are set to empty rather than removed, because
    process environment outranks .env: a developer's own .env must not
    fill in the half the test deliberately leaves unset.
    """
    cleared = {name: "" for name in _ROLE_VARIABLES}
    with patch.dict(os.environ, {**cleared, **env}):
        return AppConfig()


class TestHalfConfiguredRole:
    @pytest.mark.parametrize(
        ("role", "prop"),
        [
            ("ORCHESTRATOR", "active_orchestrator_config"),
            ("CYPHER", "active_cypher_config"),
        ],
    )
    def test_provider_without_model_names_the_missing_variable(
        self, role: str, prop: str
    ) -> None:
        config = _config_from({f"{role}_PROVIDER": "anthropic"})

        with pytest.raises(ValueError) as exc_info:
            getattr(config, prop)

        message = str(exc_info.value)
        assert f"{role}_PROVIDER=anthropic is set" in message
        assert f"{role}_MODEL is not" in message

    @pytest.mark.parametrize(
        ("role", "prop"),
        [
            ("ORCHESTRATOR", "active_orchestrator_config"),
            ("CYPHER", "active_cypher_config"),
        ],
    )
    def test_model_without_provider_names_the_missing_variable(
        self, role: str, prop: str
    ) -> None:
        config = _config_from({f"{role}_MODEL": "gpt-4o"})

        with pytest.raises(ValueError) as exc_info:
            getattr(config, prop)

        message = str(exc_info.value)
        assert f"{role}_MODEL=gpt-4o is set" in message
        assert f"{role}_PROVIDER is not" in message

    def test_neither_set_still_uses_the_ollama_default(self) -> None:
        config = _config_from({})

        orch = config.active_orchestrator_config
        assert orch.provider == cs.Provider.OLLAMA
        assert orch.model_id == cs.DEFAULT_MODEL

    def test_both_set_is_honoured(self) -> None:
        config = _config_from(
            {"ORCHESTRATOR_PROVIDER": "Anthropic", "ORCHESTRATOR_MODEL": "claude-x"}
        )

        orch = config.active_orchestrator_config
        assert orch.provider == "anthropic"
        assert orch.model_id == "claude-x"

    def test_an_explicit_override_does_not_consult_the_env(self) -> None:
        config = _config_from({"ORCHESTRATOR_PROVIDER": "anthropic"})
        config.set_orchestrator("openai", "gpt-4o")

        assert config.active_orchestrator_config.model_id == "gpt-4o"


@pytest.fixture
def half_configured_orchestrator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", "anthropic")
    monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "")
    monkeypatch.setattr(settings, "CYPHER_PROVIDER", "")
    monkeypatch.setattr(settings, "CYPHER_MODEL", "")
    monkeypatch.setattr(settings, "_active_orchestrator", None)
    monkeypatch.setattr(settings, "_active_cypher", None)


@pytest.mark.usefixtures("half_configured_orchestrator")
class TestSurfaces:
    def test_startup_gate_prints_the_error_and_exits(self) -> None:
        """`cgr start` / `cgr optimize` run this gate before anything else."""
        with patch.object(cli_module.app_context, "console") as console:
            with pytest.raises(typer.Exit) as exc_info:
                cli_module.validate_models_early()

        assert exc_info.value.exit_code == 1
        printed = str(console.print.call_args)
        assert "ORCHESTRATOR_MODEL is not" in printed

    def test_doctor_reports_a_failed_check_instead_of_crashing(self) -> None:
        result = HealthChecker().check_model_role(cs.ModelRole.ORCHESTRATOR)

        assert not result.passed
        assert result.error is not None
        assert "ORCHESTRATOR_MODEL is not" in result.error

    def test_doctor_still_checks_the_other_role(self) -> None:
        result = HealthChecker().check_model_role(cs.ModelRole.CYPHER)

        assert result.passed
