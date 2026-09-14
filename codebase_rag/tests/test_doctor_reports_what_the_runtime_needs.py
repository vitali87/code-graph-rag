"""`cgr doctor` reports what the runtime actually needs (issue #1910).

One run on a Windows terminal produced three results that were not about
the user's setup:

* the pass/fail marks are `✓`/`✗`, which a CP950 stdout cannot encode, so
  doctor crashed before a single check was shown;
* "Orchestrator API key" and "Cypher API key" failed on an install whose
  default model (Ollama) needs no key, and the runtime's own start-up
  gate would have accepted it;
* "cmake is not installed" failed although cmake only builds pymgclient,
  which doctor has already imported by the time it runs.

The model checks are held to one rule: doctor's verdict for a role must
equal the verdict of the gate `cgr start` applies (`validate_api_key`).
"""

from __future__ import annotations

import io

import pytest
from click.testing import Result
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import cli as cli_module
from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.schemas import HealthCheckResult
from codebase_rag.tools.health_checker import HealthChecker

runner = CliRunner()

_PASS = HealthCheckResult(name="Docker daemon is running", passed=True, message="ok")
_FAIL = HealthCheckResult(
    name="Memgraph connection failed", passed=False, message="no", error="down"
)

_KEY_VARIABLES = (
    "ORCHESTRATOR_API_KEY",
    "CYPHER_API_KEY",
    cs.ENV_OPENAI_API_KEY,
    cs.ENV_GOOGLE_API_KEY,
    cs.ENV_ANTHROPIC_API_KEY,
    cs.ENV_AZURE_API_KEY,
    cs.ENV_MINIMAX_API_KEY,
)


class _StubChecker:
    """Two fixed results, so the test is about rendering, not probing."""

    def run_all_checks(self) -> list[HealthCheckResult]:
        return [_PASS, _FAIL]

    def get_summary(self) -> tuple[int, int]:
        return 1, 2


def _run_doctor(monkeypatch: pytest.MonkeyPatch, encoding: str) -> tuple[Result, str]:
    """Run `cgr doctor` against a console whose stdout uses `encoding`."""
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, write_through=True)
    console = Console(file=stream, force_terminal=False, width=80)
    monkeypatch.setattr(cli_module.app_context, "console", console)
    monkeypatch.setattr(cli_module, "HealthChecker", _StubChecker)
    result = runner.invoke(cli_module.app, ["doctor"])
    stream.flush()
    return result, raw.getvalue().decode(encoding, errors="replace")


class TestMarksMatchTheConsole:
    def test_a_stdout_that_cannot_encode_the_glyphs_gets_ascii_marks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, text = _run_doctor(monkeypatch, "cp950")
        # Only the failed check's exit may remain; any other exception is a
        # crash, or a command that never ran.
        assert isinstance(result.exception, SystemExit), result.exception
        assert result.exit_code == 1, result.output
        assert f"{cs.HEALTH_MARK_PASS_ASCII} {_PASS.name}" in text, text
        assert f"{cs.HEALTH_MARK_FAIL_ASCII} {_FAIL.name}" in text, text

    def test_a_utf8_stdout_keeps_the_glyphs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the ASCII fallback is for streams that need it only."""
        result, text = _run_doctor(monkeypatch, cs.ENCODING_UTF8)
        # Only the failed check's exit may remain; any other exception is a
        # crash, or a command that never ran.
        assert isinstance(result.exception, SystemExit), result.exception
        assert result.exit_code == 1, result.output
        assert f"{cs.HEALTH_MARK_PASS} {_PASS.name}" in text, text
        assert f"{cs.HEALTH_MARK_FAIL} {_FAIL.name}" in text, text


@pytest.fixture
def bare_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No role configured, no key variable set: the shipped default.

    No teardown of its own: every change here is a monkeypatch one, which
    pytest undoes when the fixture's own monkeypatch goes out of scope.
    """
    for role in ("ORCHESTRATOR", "CYPHER"):
        monkeypatch.setattr(settings, f"{role}_PROVIDER", "")
        monkeypatch.setattr(settings, f"{role}_MODEL", "")
        monkeypatch.setattr(settings, f"{role}_API_KEY", None)
        monkeypatch.setattr(settings, f"{role}_PROVIDER_TYPE", None)
    monkeypatch.setattr(settings, "_active_orchestrator", None)
    monkeypatch.setattr(settings, "_active_cypher", None)
    for name in _KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _runtime_accepts(role: cs.ModelRole) -> bool:
    """The verdict `cgr start` reaches for this role, by the same call."""
    config = (
        settings.active_orchestrator_config
        if role == cs.ModelRole.ORCHESTRATOR
        else settings.active_cypher_config
    )
    try:
        config.validate_api_key(role)
    except ValueError:
        return False
    return True


def _role_results() -> dict[cs.ModelRole, HealthCheckResult]:
    results = HealthChecker().check_model_roles()
    assert len(results) == len(cs.ModelRole), [r.name for r in results]
    return dict(zip(cs.ModelRole, results, strict=True))


@pytest.mark.usefixtures("bare_model_env")
class TestModelChecksAgreeWithTheStartupGate:
    def test_the_default_local_model_needs_no_key(self) -> None:
        results = _role_results()
        for role, result in results.items():
            assert result.passed, (role, result)
            assert cs.Provider.OLLAMA in result.name, result.name
            assert _runtime_accepts(role)

    def test_the_provider_variable_satisfies_the_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "ORCHESTRATOR_PROVIDER", cs.Provider.ANTHROPIC)
        monkeypatch.setattr(settings, "ORCHESTRATOR_MODEL", "claude-test")
        monkeypatch.setenv(cs.ENV_ANTHROPIC_API_KEY, "placeholder-for-test")
        result = _role_results()[cs.ModelRole.ORCHESTRATOR]
        assert result.passed, result
        assert _runtime_accepts(cs.ModelRole.ORCHESTRATOR)

    def test_a_missing_key_fails_and_names_the_role_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CYPHER_PROVIDER", cs.Provider.OPENAI)
        monkeypatch.setattr(settings, "CYPHER_MODEL", "gpt-test")
        result = _role_results()[cs.ModelRole.CYPHER]
        assert not result.passed, result
        assert "CYPHER_API_KEY" in (result.error or ""), result
        assert f"{cs.Provider.OPENAI}:gpt-test" in result.name, result.name
        assert not _runtime_accepts(cs.ModelRole.CYPHER)

    def test_the_role_variable_itself_satisfies_the_role(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "CYPHER_PROVIDER", cs.Provider.OPENAI)
        monkeypatch.setattr(settings, "CYPHER_MODEL", "gpt-test")
        monkeypatch.setattr(settings, "CYPHER_API_KEY", "placeholder-for-test")
        result = _role_results()[cs.ModelRole.CYPHER]
        assert result.passed, result
        assert _runtime_accepts(cs.ModelRole.CYPHER)


class TestRunAllChecksMeasuresOnlyWhatMatters:
    def test_no_check_on_cmake_or_on_a_variable_nothing_reads(
        self, monkeypatch: pytest.MonkeyPatch, bare_model_env: None
    ) -> None:
        # HealthChecker declares __slots__, so the probes are stubbed on the
        # class: the environment-touching checks are not what is measured.
        monkeypatch.setattr(HealthChecker, "check_docker", lambda self: _PASS)
        monkeypatch.setattr(
            HealthChecker, "check_memgraph_connection", lambda self: _FAIL
        )
        monkeypatch.setattr(HealthChecker, "check_graph_integrity", lambda self: [])
        probed: list[str] = []

        def fake_tool(
            self: HealthChecker, tool_name: str, command: str | None = None
        ) -> HealthCheckResult:
            probed.append(command or tool_name)
            return HealthCheckResult(
                name=f"{tool_name} is installed", passed=True, message="ok"
            )

        monkeypatch.setattr(HealthChecker, "check_external_tool", fake_tool)
        monkeypatch.setenv("GEMINI_API_KEY", "placeholder-for-test")

        checker = HealthChecker()
        names = [r.name for r in checker.run_all_checks()]

        assert "cmake" not in probed, probed
        assert "rg" in probed, probed
        assert not any("Gemini" in n or "OpenAI" in n for n in names), names
        assert sum("model" in n for n in names) == len(cs.ModelRole), names
        assert checker.get_summary() == (len(names) - 1, len(names))
