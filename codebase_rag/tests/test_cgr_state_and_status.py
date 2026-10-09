from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import cgr_state
from codebase_rag.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    return home


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


class TestStatusMissingRoots:
    """The missing-root marks `cgr status` prints (issue #2479)."""

    @pytest.fixture
    def _stack_running(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        from codebase_rag.stack.constants import StackState
        from codebase_rag.stack.manager import StackStatus

        fake = StackStatus(
            state=StackState.RUNNING,
            memgraph_reachable=True,
            qdrant_reachable=True,
            compose_file=Path("/tmp/cgr/docker-compose.yaml"),
            memgraph_endpoint="localhost:7687",
            qdrant_endpoint="localhost:6333",
        )
        manager = MagicMock()
        manager.status.return_value = fake
        monkeypatch.setattr("codebase_rag.cli.StackManager", lambda: manager)
        return manager

    def test_marks_project_whose_root_is_missing(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cgr_state.record_sync("dead__22222222")
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock({"dead__22222222": str(tmp_path / "gone")}),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        assert "dead__22222222" in result.output
        assert "(missing)" in result.output
        assert str(tmp_path / "gone") in result.output

    def test_live_project_is_not_marked_missing(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        live = tmp_path / "live"
        live.mkdir()
        cgr_state.record_sync("live__11111111")
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock({"live__11111111": str(live)}),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        assert "live__11111111" in result.output
        assert "(missing)" not in result.output
        assert str(live) in result.output


def _graph_mock(roots: dict[str, str | None]) -> MagicMock:
    ingestor = MagicMock()
    ingestor.list_project_roots.return_value = roots
    ingestor.fetch_all.return_value = []
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=ingestor)
    context.__exit__ = MagicMock(return_value=False)
    return context
