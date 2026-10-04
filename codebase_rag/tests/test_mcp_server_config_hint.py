"""Issue #2881: the `TARGET_REPO_PATH` hint follows only a repository-path error.

`cgr mcp-server` caught every `ValueError` as a configuration error and always
added "Hint: Make sure TARGET_REPO_PATH environment variable is set." So the
refusal to bind HTTP to 0.0.0.0 without a token, an unknown workspace or a
missing API key all sent the user to a variable that was set.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import typer

from codebase_rag import cli
from codebase_rag import constants as cs
from codebase_rag.config import AppConfig
from codebase_rag.mcp import server as srv

HINT = "TARGET_REPO_PATH environment variable"


@pytest.fixture(autouse=True)
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(root))
    monkeypatch.delenv(cs.MCPEnvVar.MCP_WORKSPACE, raising=False)
    with (
        patch.object(srv, "setup_logging"),
        patch.object(cli.settings, "QUIET", False),
    ):
        yield root


def _serve(
    transport: cs.MCPTransport = cs.MCPTransport.STDIO,
    host: str = "127.0.0.1",
    workspace: str | None = None,
) -> int:
    # Every case is refused before the server binds, so the port is unused.
    try:
        cli.mcp_server(transport=transport, host=host, port=18765, workspace=workspace)
    except typer.Exit as exit_:
        return exit_.exit_code
    return 0


def test_the_http_bind_refusal_has_no_repo_path_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(srv.settings, "MCP_HTTP_AUTH_TOKEN", None):
        code = _serve(cs.MCPTransport.HTTP, host="0.0.0.0")

    _out, err = capsys.readouterr()
    assert code == 1
    assert "Refusing to bind the HTTP MCP server to 0.0.0.0" in err
    assert HINT not in err


def test_an_unknown_workspace_has_no_repo_path_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = _serve(workspace="no-such-workspace-2881")

    _out, err = capsys.readouterr()
    assert code == 1
    assert "no-such-workspace-2881" in err
    assert HINT not in err


def test_a_missing_api_key_has_no_repo_path_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = MagicMock()
    config.validate_api_key.side_effect = ValueError("provider requires api_key")
    with patch.object(
        AppConfig,
        "active_orchestrator_config",
        new_callable=PropertyMock,
        return_value=config,
    ):
        code = _serve()

    _out, err = capsys.readouterr()
    assert code == 1
    assert "provider requires api_key" in err
    assert HINT not in err


# Negative: what must not change.


def test_a_missing_repo_path_still_gets_the_hint(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(repo / "missing"))

    code = _serve()

    _out, err = capsys.readouterr()
    assert code == 1
    assert "Target repository path does not exist" in err
    assert HINT in err


def test_a_repo_path_that_is_a_file_still_gets_the_hint(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    afile = repo / "afile.txt"
    afile.write_text("x", encoding="utf-8")
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(afile))

    code = _serve()

    _out, err = capsys.readouterr()
    assert code == 1
    assert "Target repository path is not a directory" in err
    assert HINT in err


def test_quiet_still_drops_the_hint(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(cs.MCPEnvVar.TARGET_REPO_PATH, str(repo / "missing"))

    with patch.object(cli.settings, "QUIET", True):
        code = _serve()

    _out, err = capsys.readouterr()
    assert code == 1
    assert "Target repository path does not exist" in err
    assert HINT not in err


def test_the_bind_refusal_is_still_a_configuration_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(srv.settings, "MCP_HTTP_AUTH_TOKEN", None):
        _serve(cs.MCPTransport.HTTP, host="0.0.0.0")

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("Configuration Error: Refusing to bind")
