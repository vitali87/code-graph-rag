"""Regression tests for `cgr prune` (issue #2479).

Prune destroys whole projects from the shared graph, so a wrong candidate is
a lost graph, not a stale node: absence must be proven, the current checkout
must never be a candidate, a root that reappears is skipped, and a purge that
cannot be verified as complete is reported as a failure.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings

runner = CliRunner()


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, str]:
    live = tmp_path / "live"
    live.mkdir()
    return {
        "live__11111111": str(live),
        "dead__22222222": str(tmp_path / "gone"),
    }


@pytest.fixture(autouse=True)
def _isolated_cgr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    return home


@pytest.fixture
def mock_memgraph_connect(
    roots: dict[str, str],
) -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_ingestor.list_project_roots.return_value = dict(roots)
        # The verification read after a purge: the pruned project is gone.
        mock_ingestor.list_projects.side_effect = [["live__11111111"]]
        mock_ingestor.fetch_all.side_effect = _fake_fetch_all
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _fake_fetch_all(
    query: str, params: dict[str, object] | None = None
) -> list[dict[str, object]]:
    if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
        return [{cs.KEY_NODE_ID: 1}]
    if query == cq.CYPHER_COUNT_PROJECT_NODES:
        assert params == {cs.KEY_PROJECT_NAME: "dead__22222222"}
        return [{cs.KEY_RESIDUAL_NODES: 0}]
    return []


def _empty_residual_fetch_all(
    query: str, params: dict[str, object] | None = None
) -> list[dict[str, object]]:
    if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
        return [{cs.KEY_NODE_ID: 1}]
    if query == cq.CYPHER_COUNT_PROJECT_NODES:
        return []
    return []


def test_residual_row_key_agrees_with_query_alias() -> None:
    assert f"AS {cs.KEY_RESIDUAL_NODES}" in cq.CYPHER_COUNT_PROJECT_NODES


def _ingestor(mock_connect: MagicMock) -> MagicMock:
    return mock_connect.return_value.__enter__.return_value


class TestCandidateSelection:
    def test_no_candidates_prints_nothing_to_prune(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock
    ) -> None:
        live = tmp_path / "live"
        _ingestor(mock_memgraph_connect).list_project_roots.return_value = {
            "live__11111111": str(live)
        }
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_dry_run_lists_candidates_without_deleting(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        result = runner.invoke(app, ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "dead__22222222" in result.output
        assert "would be pruned" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_unreadable_root_is_not_a_candidate(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock
    ) -> None:
        with patch(
            "codebase_rag.utils.path_utils.os.stat",
            side_effect=PermissionError("stat blocked"),
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_current_checkout_is_never_a_candidate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
            mock_ingestor = MagicMock()
            mock_ingestor.list_project_roots.return_value = {
                "here__33333333": str(tmp_path)
            }
            mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
            mock_connect.return_value.__exit__ = MagicMock(return_value=False)
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        mock_ingestor.delete_project.assert_not_called()

    def test_legacy_project_without_root_is_not_a_candidate(
        self, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        _ingestor(mock_memgraph_connect).list_project_roots.return_value = {
            "legacy__44444444": None,
            **roots,
        }
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.delete_project.assert_called_once_with("dead__22222222")


class TestPruneDeletion:
    def test_removes_only_the_missing_root_project(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.delete_project.assert_called_once_with("dead__22222222")
        assert "Pruned project 'dead__22222222'" in result.output

    def test_cleans_embeddings_before_deleting(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with patch("codebase_rag.cli.delete_project_embeddings") as mock_embeddings:
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        mock_embeddings.assert_called_once_with("dead__22222222", [1])

    def test_recreated_root_is_skipped(self, mock_memgraph_connect: MagicMock) -> None:
        # The listing saw the root missing, then the checkout came back: the
        # re-check inside the delete path must refuse the purge (#2479).
        with patch(
            "codebase_rag.cli.root_proven_missing",
            side_effect=[False, True, False],
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "no longer a prune candidate" in result.output
        assert "Pruned" not in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()


class TestPruneSyncRecord:
    def test_forgets_the_sync_record_of_a_verified_purge(
        self, mock_memgraph_connect: MagicMock, _isolated_cgr_home: Path
    ) -> None:
        from codebase_rag import cgr_state

        cgr_state.record_sync("dead__22222222", home=_isolated_cgr_home)
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert cgr_state.read_sync_timestamps(home=_isolated_cgr_home) == {}

    def test_keeps_the_sync_record_when_verification_fails(
        self, mock_memgraph_connect: MagicMock, _isolated_cgr_home: Path
    ) -> None:
        from codebase_rag import cgr_state

        cgr_state.record_sync("dead__22222222", home=_isolated_cgr_home)
        _ingestor(mock_memgraph_connect).fetch_all.side_effect = _residual_fetch_all
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert set(cgr_state.read_sync_timestamps(home=_isolated_cgr_home)) == {
            "dead__22222222"
        }


class TestPruneVerification:
    def test_project_still_listed_fails_verification(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_projects.side_effect = [
            ["live__11111111", "dead__22222222"],
            ["live__11111111", "dead__22222222"],
        ]
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert "could not be verified" in result.output
        assert "Pruned project" not in result.output

    def test_residual_nodes_fail_verification(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.fetch_all.side_effect = _residual_fetch_all
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert "could not be verified" in result.output
        assert "Pruned project" not in result.output

    def test_empty_verification_read_fails(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        # A purge that cannot be proven complete is a failure, never a
        # success: an unreadable residual count must not read as zero.
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.fetch_all.side_effect = _empty_residual_fetch_all
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert "could not be verified" in result.output
        assert "Pruned project" not in result.output


def _residual_fetch_all(
    query: str, params: dict[str, object] | None = None
) -> list[dict[str, object]]:
    if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
        return [{cs.KEY_NODE_ID: 1}]
    if query == cq.CYPHER_COUNT_PROJECT_NODES:
        assert params == {cs.KEY_PROJECT_NAME: "dead__22222222"}
        return [{cs.KEY_RESIDUAL_NODES: 7}]
    return []


class TestPruneConfirmation:
    def test_non_interactive_run_refuses_without_yes(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        result = runner.invoke(app, ["prune"])

        assert result.exit_code == 1, result.output
        assert "--yes" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_declined_confirmation_leaves_the_graph_untouched(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
            result = runner.invoke(app, ["prune"], input="n\n")

        assert result.exit_code == 1, result.output
        assert "left untouched" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_accepted_confirmation_prunes(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
            result = runner.invoke(app, ["prune"], input="y\n")

        assert result.exit_code == 0, result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_called_once_with(
            "dead__22222222"
        )
        assert "Pruned 1 project(s)" in result.output
