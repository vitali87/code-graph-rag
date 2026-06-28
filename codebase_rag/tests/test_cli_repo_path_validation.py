from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    # (H) ANSI-stripped output with Rich soft-wrap newlines rejoined
    return _ANSI.sub("", output).replace("\n", "")


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli.connect_memgraph") as mock_connect,
        patch("codebase_rag.cli._maybe_start_stack"),
    ):
        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


class TestStartRepoPathValidation:
    def test_nonexistent_path_exits_with_error(
        self, mock_memgraph_connect: MagicMock, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does_not_exist"
        result = runner.invoke(app, ["start", "--clean", "--repo-path", str(missing)])

        assert result.exit_code == 1, result.output
        plain = _plain(result.output)
        assert str(missing) in plain
        assert "does not exist" in plain

    def test_file_path_exits_with_error(
        self, mock_memgraph_connect: MagicMock, tmp_path: Path
    ) -> None:
        file_path = tmp_path / "a_file.txt"
        file_path.write_text("not a directory")
        result = runner.invoke(app, ["start", "--clean", "--repo-path", str(file_path)])

        assert result.exit_code == 1, result.output
        plain = _plain(result.output)
        assert str(file_path) in plain
        assert "not a directory" in plain

    def test_valid_non_git_dir_warns_but_proceeds(
        self, mock_memgraph_connect: MagicMock, tmp_path: Path
    ) -> None:
        result = runner.invoke(app, ["start", "--clean", "--repo-path", str(tmp_path)])

        assert result.exit_code == 0, result.output
        plain = _plain(result.output)
        assert "not a Git repository" in plain
        assert str(tmp_path) in plain

    def test_git_dir_does_not_warn(
        self, mock_memgraph_connect: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / cs.GIT_DIR_NAME).mkdir()
        result = runner.invoke(app, ["start", "--clean", "--repo-path", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "not a Git repository" not in result.output

    def test_git_file_worktree_does_not_warn(
        self, mock_memgraph_connect: MagicMock, tmp_path: Path
    ) -> None:
        # (H) worktrees and submodules use a .git file, not a directory
        (tmp_path / cs.GIT_DIR_NAME).write_text("gitdir: /repo/.git/worktrees/wt\n")
        result = runner.invoke(app, ["start", "--clean", "--repo-path", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "not a Git repository" not in result.output


class TestIndexRepoPathValidation:
    def test_index_nonexistent_path_exits_with_error(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"
        result = runner.invoke(
            app,
            [
                "index",
                "--repo-path",
                str(missing),
                "-o",
                str(tmp_path / "out"),
            ],
        )

        assert result.exit_code == 1, result.output
        assert "does not exist" in _plain(result.output)
