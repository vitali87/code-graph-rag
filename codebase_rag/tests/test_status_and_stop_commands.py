from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    return home


class TestStatusCommand:
    def test_status_runs_clean(self, _temp_home: Path) -> None:
        from codebase_rag.stack.constants import StackState
        from codebase_rag.stack.manager import StackStatus

        fake = StackStatus(
            state=StackState.STOPPED,
            memgraph_reachable=False,
            qdrant_reachable=False,
            compose_file=Path("/tmp/cgr/docker-compose.yaml"),
            memgraph_endpoint="localhost:7687",
            qdrant_endpoint="localhost:6333",
        )
        with patch("codebase_rag.cli.StackManager") as mock_mgr:
            mock_mgr.return_value.status.return_value = fake
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "stopped" in result.output
        assert "sync times are kept in the graph" in " ".join(result.output.split())

    def test_status_lists_the_graphs_projects(self, _temp_home: Path) -> None:
        from codebase_rag.stack.constants import StackState
        from codebase_rag.stack.manager import StackStatus

        store = MagicMock()
        store.fetch_all.side_effect = lambda query, params=None: (
            [
                {cs.KEY_NAME: "alpha", cs.KEY_LAST_SYNCED_AT: "2026-09-29T10:00:00"},
                {cs.KEY_NAME: "beta", cs.KEY_LAST_SYNCED_AT: None},
            ]
            if query == cq.CYPHER_PROJECT_SYNC_TIMES
            else []
        )
        connection = MagicMock()
        connection.__enter__.return_value = store
        connection.__exit__.return_value = False
        fake = StackStatus(
            state=StackState.RUNNING,
            memgraph_reachable=True,
            qdrant_reachable=True,
            compose_file=Path("/tmp/cgr/docker-compose.yaml"),
            memgraph_endpoint="localhost:7687",
            qdrant_endpoint="localhost:6333",
        )
        with (
            patch("codebase_rag.cli.StackManager") as mock_mgr,
            patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        ):
            mock_mgr.return_value.status.return_value = fake
            result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "running" in result.output


class TestStopCommand:
    def test_stop_invokes_daemon_down(self, _temp_home: Path) -> None:
        with patch("codebase_rag.cli.StackManager") as mock_mgr:
            instance = mock_mgr.return_value
            result = runner.invoke(app, ["stop"])
        assert result.exit_code == 0, result.output
        instance.down.assert_called_once()
