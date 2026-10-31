"""Regression tests for `cgr prune` (issue #2479).

Prune destroys whole projects from the shared graph, so a wrong candidate is
a lost graph, not a stale node: absence must be proven, the current checkout
must never be a candidate, a root that reappears is skipped, and a purge that
cannot be verified as complete is reported as a failure.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.config import settings
from codebase_rag.types_defs import PropertyParams, ResultRow

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
        mock_ingestor.list_project_roots.side_effect = [dict(roots)]
        # delete_project is conditional now: the fixture's default is a
        # purge that fired (the repoint test overrides it).
        mock_ingestor.delete_project.return_value = True
        # The verification read after a purge: the pruned project is gone.
        mock_ingestor.list_projects.side_effect = [["live__11111111"]]
        mock_ingestor.fetch_all.side_effect = _fake_fetch_all(0)
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _fake_fetch_all(
    residual: int | None,
) -> Callable[[str, PropertyParams | None], list[ResultRow]]:
    """One fake for every fetch: node ids read normally, the residual
    count returning whatever the caller wants the verification to see —
    a row with the given count, or an empty read for None."""

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_PROJECT_NODE_IDS:
            return [{cs.KEY_NODE_ID: 1}]
        if query == cq.CYPHER_COUNT_PROJECT_NODES:
            assert params is not None
            assert set(params) == {cs.KEY_PROJECT_NAME, cs.KEY_PROJECT_PREFIX}
            name = params[cs.KEY_PROJECT_NAME]
            assert isinstance(name, str)
            assert params[cs.KEY_PROJECT_PREFIX] == f"{name}{cs.SEPARATOR_DOT}"
            return [] if residual is None else [{cs.KEY_RESIDUAL_NODES: residual}]
        return []

    return fetch_all


def test_residual_row_key_agrees_with_query_alias() -> None:
    assert f"AS {cs.KEY_RESIDUAL_NODES}" in cq.CYPHER_COUNT_PROJECT_NODES


def _ingestor(mock_connect: MagicMock) -> MagicMock:
    return mock_connect.return_value.__enter__.return_value


class TestCandidateSelection:
    def test_no_candidates_prints_nothing_to_prune(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock
    ) -> None:
        live = tmp_path / "live"
        only_live = {"live__11111111": str(live)}
        live.mkdir(exist_ok=True)
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_project_roots.side_effect = lambda *a, **k: dict(only_live)
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        ingestor.delete_project.assert_not_called()

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

    def test_undeterminable_cwd_prunes_nothing(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        # A shell can outlive its own directory: with no cwd to exempt or
        # compare against, no project may be a candidate (#2479).
        with patch(
            "codebase_rag.cli.Path.cwd", side_effect=FileNotFoundError("cwd gone")
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "nothing to prune" in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    @pytest.mark.skipif(
        sys.platform == "win32", reason="symlinks need privileges on Windows"
    )
    def test_a_looping_root_is_not_a_candidate(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        # Resolving a symlink loop raises on Python 3.12; the listing must
        # read it as undeterminable, hence live, rather than crash.
        loop = tmp_path / "loop"
        loop.symlink_to(loop)
        _ingestor(mock_memgraph_connect).list_project_roots.side_effect = [
            {**roots, "loop__66666666": str(loop)}
        ]
        result = runner.invoke(app, ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "dead__22222222" in result.output
        assert "loop__66666666" not in result.output

    def test_a_root_is_listed_as_written(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock
    ) -> None:
        # A route directory such as `[slug]` would read as console markup.
        gone = str(tmp_path / "[slug]")
        _ingestor(mock_memgraph_connect).list_project_roots.side_effect = [
            {"web__55555555": gone}
        ]
        result = runner.invoke(app, ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert gone in " ".join(result.output.split())

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
        ingestor.delete_project.assert_called_once_with(
            "dead__22222222", expected_root=roots["dead__22222222"]
        )


class TestPruneDeletion:
    def test_removes_only_the_missing_root_project(
        self, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.delete_project.assert_called_once_with(
            "dead__22222222", expected_root=roots["dead__22222222"]
        )
        assert "Pruned project 'dead__22222222'" in result.output

    def test_cleans_embeddings_when_the_purge_fires(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with patch("codebase_rag.cli.delete_project_embeddings") as mock_embeddings:
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        mock_embeddings.assert_called_once_with("dead__22222222", [1], [])

    def test_a_nested_project_keeps_its_vectors(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        # `dead__22222222.v2` sits under the pruned name, so the prefix-scoped
        # node-id read names its nodes too; the store is told to keep them.
        nested = tmp_path / "v2"
        nested.mkdir()
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_project_roots.side_effect = [
            {**roots, "dead__22222222.v2": str(nested)}
        ]
        with patch("codebase_rag.cli.delete_project_embeddings") as mock_embeddings:
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        mock_embeddings.assert_called_once_with(
            "dead__22222222", [1], ["dead__22222222.v2"]
        )

    def test_keeps_vectors_when_their_deletion_fails(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        # The graph purge is proven; unrecoverable vector leaks warn and
        # still count, a silent success would not.
        with patch(
            "codebase_rag.cli.delete_project_embeddings", return_value=False
        ) as mock_embeddings:
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        mock_embeddings.assert_called_once()
        assert "remain in the vector store" in result.output
        assert "Pruned project 'dead__22222222'" in result.output

    def test_recreated_root_is_skipped(self, mock_memgraph_connect: MagicMock) -> None:
        # The listing saw the root missing, then the checkout came back: the
        # loop-top re-check must refuse the purge (#2479).
        with patch(
            "codebase_rag.cli.root_proven_missing",
            side_effect=[False, True, False],
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "no longer a prune candidate" in result.output
        assert "Pruned" not in result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_not_called()

    def test_repointed_root_is_skipped(
        self, tmp_path: Path, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        # While the prompt was open a sync moved the project's root_path to
        # its new live location: the conditional delete must purge the
        # project only when the graph still names the checked root
        # (Greptile P1 on PR #3221).
        moved = tmp_path / "moved"
        moved.mkdir()
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.delete_project.return_value = False
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "has changed" in result.output
        assert "Pruned" not in result.output
        ingestor.delete_project.assert_called_once_with(
            "dead__22222222", expected_root=roots["dead__22222222"]
        )


class TestPruneAftercare:
    """What a proven purge takes after the graph, as `delete-project` does."""

    def test_a_verified_purge_sweeps_notes_and_markers(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with (
            patch(
                "codebase_rag.cli.prune_orphaned_glosses", return_value=True
            ) as sweep,
            patch("codebase_rag.cli._clear_deleted_project_markers") as clear,
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        ingestor = _ingestor(mock_memgraph_connect)
        sweep.assert_called_once_with(ingestor, "dead__22222222")
        # The marker would keep the project in `cgr status` as interrupted.
        clear.assert_called_once_with(ingestor, "dead__22222222")

    def test_a_failed_note_sweep_warns_and_the_prune_still_counts(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        with (
            patch("codebase_rag.cli.prune_orphaned_glosses", return_value=False),
            patch("codebase_rag.cli._clear_deleted_project_markers"),
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 0, result.output
        assert "could not be removed" in " ".join(result.output.split())
        assert "Pruned project 'dead__22222222'" in result.output

    def test_an_unverified_purge_keeps_notes_and_markers(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        _ingestor(mock_memgraph_connect).fetch_all.side_effect = _fake_fetch_all(7)
        with (
            patch("codebase_rag.cli.prune_orphaned_glosses") as sweep,
            patch("codebase_rag.cli._clear_deleted_project_markers") as clear,
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        sweep.assert_not_called()
        clear.assert_not_called()

    def test_a_delete_that_raises_is_a_failure_and_sweeps_if_gone(
        self, mock_memgraph_connect: MagicMock
    ) -> None:
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.delete_project.side_effect = RuntimeError("cleanup failed")
        with (
            patch("codebase_rag.cli._sweep_glosses_after_failed_delete") as sweep,
            patch("codebase_rag.cli.delete_project_embeddings") as embeddings,
        ):
            result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert "Failed to prune project 'dead__22222222'" in result.output
        assert "Pruned project" not in result.output
        sweep.assert_called_once_with(ingestor, "dead__22222222")
        embeddings.assert_not_called()


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
        ingestor.fetch_all.side_effect = _fake_fetch_all(7)
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
        ingestor.fetch_all.side_effect = _fake_fetch_all(None)
        result = runner.invoke(app, ["prune", "--yes"])

        assert result.exit_code == 1, result.output
        assert "could not be verified" in result.output
        assert "Pruned project" not in result.output


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
        self, mock_memgraph_connect: MagicMock, roots: dict[str, str]
    ) -> None:
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
            result = runner.invoke(app, ["prune"], input="y\n")

        assert result.exit_code == 0, result.output
        _ingestor(mock_memgraph_connect).delete_project.assert_called_once_with(
            "dead__22222222", expected_root=roots["dead__22222222"]
        )
        assert "Pruned 1 project(s)" in result.output
