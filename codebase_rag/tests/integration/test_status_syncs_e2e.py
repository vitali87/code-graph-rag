# Issue #2444, against Memgraph: a completed sync stamps its Project, and
# `cgr status` lists the projects of the graph it is connected to, so a
# project deleted from it leaves the list with its node.
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.cli_runtime import app_context
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.stack.constants import StackState
from codebase_rag.stack.manager import StackStatus

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

runner = CliRunner()


def _sync(ingestor: MemgraphIngestor, root: Path, name: str) -> None:
    repo = root / name
    repo.mkdir()
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=name,
        skip_embeddings=True,
    ).run()
    ingestor.flush_all()


def _cgr(*args: str) -> str:
    endpoint = f"{settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}"
    running = StackStatus(
        state=StackState.RUNNING,
        memgraph_reachable=True,
        qdrant_reachable=True,
        compose_file=Path("/tmp/cgr/docker-compose.yaml"),
        memgraph_endpoint=endpoint,
        qdrant_endpoint="127.0.0.1:6333",
    )
    with (
        patch("codebase_rag.cli.StackManager") as manager,
        patch("codebase_rag.cli.delete_project_embeddings"),
    ):
        manager.return_value.status.return_value = running
        result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_status_lists_the_projects_this_memgraph_holds(
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MEMGRAPH_HOST", str(memgraph_container["host"]))
    monkeypatch.setattr(settings, "MEMGRAPH_PORT", int(memgraph_container["port"]))
    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    monkeypatch.setattr(
        app_context, "console", Console(width=200, force_terminal=False, no_color=True)
    )
    _sync(memgraph_ingestor, tmp_path, "billing")
    _sync(memgraph_ingestor, tmp_path, "ledger")

    rows = memgraph_ingestor.fetch_all(cq.CYPHER_PROJECT_SYNC_TIMES)
    stamps = {str(row[cs.KEY_NAME]): row[cs.KEY_LAST_SYNCED_AT] for row in rows}
    assert sorted(stamps) == ["billing", "ledger"]
    for stamp in stamps.values():
        assert isinstance(stamp, str)
        assert datetime.fromisoformat(stamp).tzinfo is not None

    listed = _cgr("status")
    assert f"- billing: last sync {stamps['billing']}" in listed, listed
    assert f"- ledger: last sync {stamps['ledger']}" in listed, listed

    _cgr("delete-project", "-n", "billing")

    listed = _cgr("status")
    assert "billing" not in listed, listed
    assert f"- ledger: last sync {stamps['ledger']}" in listed, listed
