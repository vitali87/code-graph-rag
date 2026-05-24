from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cgr_state
from codebase_rag.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    from codebase_rag.config import settings

    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    yield home


class TestRecordSync:
    def test_record_sync_creates_file(self, _temp_home: Path) -> None:
        cgr_state.record_sync("alpha")
        assert cgr_state.state_path().exists()
        ts = cgr_state.read_sync_timestamps()
        assert "alpha" in ts

    def test_record_sync_updates_existing(self, _temp_home: Path) -> None:
        cgr_state.record_sync("alpha")
        first = cgr_state.read_sync_timestamps()["alpha"]
        cgr_state.record_sync("alpha")
        second = cgr_state.read_sync_timestamps()["alpha"]
        assert second >= first

    def test_record_sync_multiple_projects(self, _temp_home: Path) -> None:
        cgr_state.record_sync("a")
        cgr_state.record_sync("b")
        ts = cgr_state.read_sync_timestamps()
        assert set(ts.keys()) == {"a", "b"}

    def test_read_when_no_state_returns_empty(self, _temp_home: Path) -> None:
        assert cgr_state.read_sync_timestamps() == {}


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
        assert "no projects synced" in result.output

    def test_status_lists_recorded_projects(self, _temp_home: Path) -> None:
        from codebase_rag.stack.constants import StackState
        from codebase_rag.stack.manager import StackStatus

        cgr_state.record_sync("alpha")
        cgr_state.record_sync("beta")
        fake = StackStatus(
            state=StackState.RUNNING,
            memgraph_reachable=True,
            qdrant_reachable=True,
            compose_file=Path("/tmp/cgr/docker-compose.yaml"),
            memgraph_endpoint="localhost:7687",
            qdrant_endpoint="localhost:6333",
        )
        with patch("codebase_rag.cli.StackManager") as mock_mgr:
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
