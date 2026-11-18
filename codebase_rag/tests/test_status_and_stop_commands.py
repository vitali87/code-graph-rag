from __future__ import annotations

import re
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
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock({"dead__22222222": str(tmp_path / "gone")}),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        output = _words(result.output)
        assert "dead__22222222" in output
        assert cs.CLI_STATUS_ROOT_MISSING in output
        assert str(tmp_path / "gone") in output

    def test_live_project_is_not_marked_missing(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        live = tmp_path / "live"
        live.mkdir()
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock({"live__11111111": str(live)}),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        output = _words(result.output)
        assert "live__11111111" in output
        assert cs.CLI_STATUS_ROOT_MISSING not in output
        assert str(live) in output

    def test_marks_only_the_project_whose_root_is_gone(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        live = tmp_path / "live"
        live.mkdir()
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock(
                {"alive": str(live), "dead": str(tmp_path / "gone"), "legacy": None}
            ),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        output = _words(result.output)
        rows = {row.split(":", 1)[0]: row for row in output.split("- ")[1:]}
        assert cs.CLI_STATUS_ROOT_MISSING in rows["dead"], output
        assert cs.CLI_STATUS_ROOT_MISSING not in rows["alive"], output
        assert cs.CLI_STATUS_ROOT_MISSING not in rows["legacy"], output

    def test_a_root_is_printed_as_written(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A route directory such as `[slug]` would read as console markup.
        root = tmp_path / "[slug]"
        root.mkdir()
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            lambda *a, **k: _graph_mock({"web": str(root)}),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        assert str(root) in _words(result.output)

    def test_unreadable_graph_still_prints_status(
        self,
        _temp_home: Path,
        _stack_running: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Status is best effort: an unreadable graph leaves the project list
        # and its missing-root marks out rather than failing the command.
        monkeypatch.setattr(
            "codebase_rag.cli.connect_memgraph",
            MagicMock(side_effect=ConnectionError("graph down")),
        )
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 0, result.output
        assert cs.CLI_STATUS_ROOT_MISSING not in result.output


def _words(output: str) -> str:
    # Rich colours and wraps the lines; compare the words alone.
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def _graph_mock(roots: dict[str, str | None]) -> MagicMock:
    rows = [
        {cs.KEY_NAME: name, cs.KEY_LAST_SYNCED_AT: None, cs.KEY_ROOT_PATH: root}
        for name, root in sorted(roots.items())
    ]
    ingestor = MagicMock()
    ingestor.fetch_all.side_effect = lambda query, params=None: (
        rows if query == cq.CYPHER_PROJECT_SYNC_TIMES else []
    )
    context = MagicMock()
    context.__enter__ = MagicMock(return_value=ingestor)
    context.__exit__ = MagicMock(return_value=False)
    return context
