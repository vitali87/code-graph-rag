"""Issue #2443: a graph that cannot be connected to is one actionable line
and exit 1, not a traceback.

With the stack down (or a wrong port, or missing credentials), every command
that reads the graph ended in mgclient's `TransientError` /
`OperationalError` traceback: 13 lines for `stats`, 148 for
`start --update-graph`. `cgr doctor` and `cgr status` already knew how to say
it; the ingestor now raises one typed error at connect time and the CLI
renders it once for every command.
"""

from __future__ import annotations

import socket
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import click
import mgclient
import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.services.graph_service import GraphUnavailableError, MemgraphIngestor

runner = CliRunner()


def _unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def nothing_listening(monkeypatch: pytest.MonkeyPatch) -> Generator[int, None, None]:
    port = _unused_port()
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", port)
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", None)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", None)
    yield port


def test_an_unreachable_graph_raises_one_typed_error(nothing_listening: int) -> None:
    with pytest.raises(GraphUnavailableError) as raised:
        with MemgraphIngestor(host="127.0.0.1", port=nothing_listening):
            pass

    message = str(raised.value)
    assert f"127.0.0.1:{nothing_listening}" in message
    assert "cgr daemon up" in message and "MEMGRAPH_PORT" in message
    assert "cgr doctor" in message


def test_rejected_credentials_name_the_credential_variables() -> None:
    refused = mgclient.OperationalError("Authentication failure")
    with patch.object(mgclient, "connect", side_effect=refused):
        with pytest.raises(GraphUnavailableError) as raised:
            with MemgraphIngestor(
                host="127.0.0.1", port=7687, username="me", password="wrong"
            ):
                pass

    message = str(raised.value)
    assert "Authentication failure" in message
    assert "MEMGRAPH_USERNAME" in message and "MEMGRAPH_PASSWORD" in message


@pytest.mark.parametrize(
    "args",
    [
        ["stats"],
        ["export", "-o", "graph.json"],
        ["dead-code"],
        ["duplicates"],
        ["delete-project", "-n", "x"],
        ["graph", "resolve", "foo"],
    ],
    ids=[
        "stats",
        "export",
        "dead-code",
        "duplicates",
        "delete-project",
        "graph-resolve",
    ],
)
def test_a_command_prints_one_line_and_exits_1(
    nothing_listening: int, args: list[str]
) -> None:
    errors: list[str] = []
    sink = logger.add(errors.append, level="ERROR", format="{message}")
    try:
        result = runner.invoke(app, args)
    finally:
        logger.remove(sink)

    output = "".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert output.count(f"127.0.0.1:{nothing_listening}") == 1, output
    assert "Traceback" not in output and "TransientError" not in output
    assert not isinstance(result.exception, mgclient.Error)
    # The command's own "Failed to ..." handler must not log it again with
    # its traceback.
    assert errors == [], errors


def test_a_sync_prints_one_line_and_exits_1(
    nothing_listening: int, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "start",
            "--repo-path",
            str(tmp_path),
            "--update-graph",
            "--no-start-stack",
            "--no-embeddings",
        ],
    )

    output = click.unstyle(result.output)
    assert result.exit_code == 1, output
    assert "cgr daemon up" in " ".join(output.split())
    assert not isinstance(result.exception, mgclient.Error)


def test_a_failure_after_connecting_is_not_reported_as_unreachable(
    nothing_listening: int,
) -> None:
    # Negative: only the connect is translated; a query failure keeps its own
    # type and handling.
    class _Conn:
        autocommit = False

        def close(self) -> None:
            pass

    with patch.object(MemgraphIngestor, "_create_connection", return_value=_Conn()):
        with pytest.raises(RuntimeError, match="query broke"):
            with MemgraphIngestor(host="127.0.0.1", port=nothing_listening):
                raise RuntimeError("query broke")
