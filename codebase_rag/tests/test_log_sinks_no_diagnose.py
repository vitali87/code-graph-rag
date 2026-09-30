"""User-facing log sinks never print local variable values (issue #2362).

loguru's `diagnose=True` default annotates every traceback frame with the
values of the variables on the failing line, so a logged exception could put
an API key or file contents in front of the user, or in an MCP client's log.
"""

import ast
import io
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger
from rich.console import Console

from codebase_rag import cli
from codebase_rag import main as cgr_main

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRET = "sk-test-not-a-real-key-2362"


def _production_sources() -> list[Path]:
    sources = [
        path
        for package in ("codebase_rag", "cgr", "codec")
        for path in (_REPO_ROOT / package).rglob("*.py")
        if "tests" not in path.relative_to(_REPO_ROOT).parts
    ]
    sources.append(_REPO_ROOT / "realtime_updater.py")
    return sources


def _logger_add_calls(source: Path) -> list[ast.Call]:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    ]


def _disables_diagnose(call: ast.Call) -> bool:
    return any(
        keyword.arg == "diagnose"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def test_the_scan_finds_the_known_sinks() -> None:
    found = {
        source.name for source in _production_sources() if _logger_add_calls(source)
    }
    assert {"cli.py", "main.py", "server.py", "realtime_updater.py"} <= found


def test_every_production_sink_disables_diagnose() -> None:
    offenders = [
        f"{source.relative_to(_REPO_ROOT)}:{call.lineno}"
        for source in _production_sources()
        for call in _logger_add_calls(source)
        if not _disables_diagnose(call)
    ]
    assert offenders == []


def _log_a_failure_holding_the_secret() -> None:
    secret_value = _SECRET
    try:
        raise ValueError(len(secret_value))
    except ValueError:
        logger.exception("lookup failed")


@pytest.fixture
def _clean_logger() -> Generator[None, None, None]:
    logger.remove()
    yield
    logger.remove()


@pytest.mark.usefixtures("_clean_logger")
def test_loguru_defaults_would_leak_the_secret() -> None:
    lines: list[str] = []
    logger.add(lambda message: lines.append(str(message)))
    _log_a_failure_holding_the_secret()
    assert _SECRET in "".join(lines)


def _assert_logged_without_the_secret(output: str) -> None:
    assert "lookup failed" in output
    assert "ValueError" in output
    assert _SECRET not in output


@pytest.mark.usefixtures("_clean_logger")
def test_the_chat_sink_keeps_the_secret_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lines: list[str] = []
    monkeypatch.setattr(cgr_main, "_rich_log_sink", lambda m: lines.append(str(m)))
    session = cgr_main.app_context.session
    monkeypatch.setattr(session, "target_repo", session.target_repo)
    cgr_main._setup_common_initialization(str(tmp_path))
    _log_a_failure_holding_the_secret()
    _assert_logged_without_the_secret("".join(lines))


@pytest.mark.usefixtures("_clean_logger")
def test_the_quiet_sink_keeps_the_secret_out(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(cli.app_context, "console", Console(file=buffer, width=200))
    monkeypatch.setattr(cli.settings, "QUIET", cli.settings.QUIET)
    cli._global_options(
        SimpleNamespace(invoked_subcommand=None), version=None, quiet=True
    )
    _log_a_failure_holding_the_secret()
    _assert_logged_without_the_secret(buffer.getvalue())
