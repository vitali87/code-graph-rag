"""Issue #3676: `cgr status` reads the syncs from the graph the backend selects.

It read them only when the stack's Memgraph answered on its port, so with the
embedded graph, the default, the syncs were never listed and the line blamed a
Memgraph the install does not use. Memgraph's reachability decides the
`stack:` line, and the syncs only when Memgraph holds the graph.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.cli_runtime import app_context
from codebase_rag.config import settings
from codebase_rag.embedded_graph.store import open_connection
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_SQLITE
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.stack.constants import StackState
from codebase_rag.stack.manager import StackStatus

runner = CliRunner()

STACK_MEMGRAPH = "localhost:7687"
EMBEDDED_GRAPH = "the embedded graph"


@pytest.fixture
def no_stack(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """No Docker stack: its Memgraph does not answer."""
    # A console of our own, wide enough to keep each listed project on one line.
    monkeypatch.setattr(
        app_context, "console", Console(width=400, force_terminal=False, no_color=True)
    )
    stopped = StackStatus(
        state=StackState.STOPPED,
        memgraph_reachable=False,
        qdrant_reachable=False,
        compose_file=Path("/tmp/cgr/docker-compose.yaml"),
        memgraph_endpoint=STACK_MEMGRAPH,
        qdrant_endpoint="127.0.0.1:6333",
    )
    with (
        patch("codebase_rag.cli.StackManager") as manager,
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        manager.return_value.status.return_value = stopped
        yield manager


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch, no_stack: MagicMock) -> None:
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_SQLITE)


def _cgr(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def _sync(tmp_path: Path, name: str) -> None:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    _cgr(
        "start",
        "--repo-path",
        str(repo),
        "--update-graph",
        "--no-start-stack",
        "--no-embeddings",
        "--project-name",
        name,
    )


def _stamp(name: str) -> str:
    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as graph:
        rows = graph.fetch_all(
            "MATCH (p:Project) WHERE p.name = $name RETURN p.last_synced_at AS t",
            {"name": name},
        )
    (row,) = rows
    stamp = row["t"]
    assert isinstance(stamp, str), rows
    return stamp


def _listed(status_output: str) -> dict[str, str]:
    """`syncs:` rows, project name -> the rest of its line."""
    rows: dict[str, str] = {}
    for line in status_output.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and ":" in stripped:
            name, _, rest = stripped[2:].partition(":")
            rows[name] = rest.strip()
    return rows


def test_the_embedded_graphs_syncs_are_listed_without_memgraph(
    embedded: None, tmp_path: Path
) -> None:
    _sync(tmp_path, "billing")
    stamp = _stamp("billing")

    output = _cgr("status")

    assert f"memgraph={STACK_MEMGRAPH} reachable=False" in output
    listed = _listed(output)
    assert list(listed) == ["billing"], output
    assert listed["billing"].startswith(f"last sync {stamp} "), listed
    assert "could not be read" not in output


def test_an_empty_embedded_graph_names_its_file(embedded: None) -> None:
    output = _cgr("status")

    assert _listed(output) == {}
    assert cs.CLI_STATUS_SYNCS_NONE.format(endpoint=settings.graph_db_file) in output, (
        output
    )


def test_an_unreadable_embedded_graph_names_it_not_memgraph(
    embedded: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory where the graph file should be: SQLite cannot open it.
    unreadable = tmp_path / "graph.sqlite"
    unreadable.mkdir()
    monkeypatch.setattr(settings, "SQLITE_GRAPH_PATH", unreadable)

    output = _cgr("status")

    assert (
        cs.CLI_STATUS_SYNCS_NEED_GRAPH.format(
            engine=EMBEDDED_GRAPH, endpoint=unreadable
        )
        in output
    ), output
    assert _listed(output) == {}


def test_an_unreachable_stack_memgraph_is_not_opened(
    no_stack: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With Memgraph as the graph, its port answering nothing settles it:
    # the syncs are reported unknown without a connection attempt.
    monkeypatch.setattr(settings, "GRAPH_BACKEND", DIALECT_MEMGRAPH)
    with patch("codebase_rag.cli.connect_memgraph") as connect:
        output = _cgr("status")

    connect.assert_not_called()
    assert (
        cs.CLI_STATUS_SYNCS_NEED_GRAPH.format(
            engine="Memgraph", endpoint=STACK_MEMGRAPH
        )
        in output
    ), output


def test_status_creates_no_embedded_graph_on_a_fresh_install(embedded: None) -> None:
    # Status only reads: the engine's usual open would create the file and
    # write its schema (Codex on PR #3681). No file holds no project.
    assert not settings.graph_db_file.exists()

    output = _cgr("status")

    assert not settings.graph_db_file.exists(), output
    assert cs.CLI_STATUS_SYNCS_NONE.format(endpoint=settings.graph_db_file) in output


def test_status_opens_an_existing_embedded_graph_read_only(
    embedded: None, tmp_path: Path
) -> None:
    _sync(tmp_path, "billing")

    with patch(
        "codebase_rag.embedded_graph.connection.open_connection",
        wraps=open_connection,
    ) as opened:
        output = _cgr("status")

    assert list(_listed(output)) == ["billing"], output
    assert opened.call_args_list, output
    assert all(
        call.args[1:2] == (True,) or call.kwargs.get("read_only") is True
        for call in opened.call_args_list
    ), opened.call_args_list
