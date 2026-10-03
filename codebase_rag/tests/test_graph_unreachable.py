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

import contextlib
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
def nothing_listening(monkeypatch: pytest.MonkeyPatch) -> int:
    port = _unused_port()
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", port)
    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", None)
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", None)
    return port


def test_an_unreachable_graph_raises_one_typed_error(nothing_listening: int) -> None:
    with pytest.raises(GraphUnavailableError) as raised:
        with MemgraphIngestor(host="127.0.0.1", port=nothing_listening):
            pass

    message = str(raised.value)
    assert f"127.0.0.1:{nothing_listening}" in message
    assert "cgr daemon up" in message
    assert "MEMGRAPH_PORT" in message
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
    assert "MEMGRAPH_USERNAME" in message
    assert "MEMGRAPH_PASSWORD" in message


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
    assert "Traceback" not in output
    assert "TransientError" not in output
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

    broke = RuntimeError("query broke")
    with patch.object(MemgraphIngestor, "_create_connection", return_value=_Conn()):
        with pytest.raises(RuntimeError, match="query broke"):
            with MemgraphIngestor(host="127.0.0.1", port=nothing_listening):
                raise broke


def _mcp_server(ingestor: MemgraphIngestor) -> tuple[MagicMock, MagicMock]:
    server = MagicMock()
    server.run = AsyncMock()
    return server, patch(
        "codebase_rag.mcp.server.create_server", return_value=(server, ingestor)
    )


@pytest.fixture
def mcp_http_port(monkeypatch: pytest.MonkeyPatch) -> int:
    port = _unused_port()
    monkeypatch.setattr(settings, "MCP_HTTP_PORT", port)
    return port


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_an_mcp_server_that_cannot_reach_the_graph_exits_1(
    nothing_listening: int, mcp_http_port: int, transport: str
) -> None:
    # Greptile review of PR 2504: `mcp-server` caught the connection error
    # itself (stdio), or uvicorn swallowed it in the app's lifespan (http),
    # so a server that never started exited 0 and a launcher saw success.
    ingestor = MemgraphIngestor(host="127.0.0.1", port=nothing_listening)
    _, server = _mcp_server(ingestor)
    with server:
        result = runner.invoke(app, ["mcp-server", "--transport", transport])

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert output.count(f"127.0.0.1:{nothing_listening}") == 1, output
    assert "cgr daemon up" in output
    assert "Traceback" not in output


class _Connection:
    autocommit = False

    def close(self) -> None:
        pass


@pytest.fixture
def graph_answers() -> Iterator[None]:
    with patch.object(
        MemgraphIngestor, "_create_connection", return_value=_Connection()
    ):
        yield


def test_a_healthy_stdio_mcp_server_still_starts(
    graph_answers: None, nothing_listening: int
) -> None:
    # Negative.
    @contextlib.asynccontextmanager
    async def streams() -> AsyncIterator[tuple[MagicMock, MagicMock]]:
        yield (MagicMock(), MagicMock())

    server, created = _mcp_server(
        MemgraphIngestor(host="127.0.0.1", port=nothing_listening)
    )
    with created, patch("codebase_rag.mcp.server.stdio_server", streams):
        result = runner.invoke(app, ["mcp-server", "--transport", "stdio"])

    assert result.exit_code == 0, result.output
    server.run.assert_awaited_once()


def test_a_healthy_http_mcp_server_still_starts(
    graph_answers: None, nothing_listening: int, mcp_http_port: int
) -> None:
    # Negative.
    _, created = _mcp_server(MemgraphIngestor(host="127.0.0.1", port=nothing_listening))
    with created, patch("uvicorn.Server.serve", new_callable=AsyncMock) as serve:
        result = runner.invoke(app, ["mcp-server", "--transport", "http"])

    assert result.exit_code == 0, result.output
    serve.assert_awaited_once()
