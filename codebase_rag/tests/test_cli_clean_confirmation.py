"""Regression tests for the --clean confirmation guard (issue #1098).

`--clean` runs `MATCH (n) DETACH DELETE n` against the shared graph, so a user
copy-pasting it from the quick start silently destroys every other project they
have indexed. It must ask first whenever other projects would be lost.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.config import CgrignorePatterns

runner = CliRunner()

OTHER_PROJECTS = ["alpha", "beta"]
PROJECT_NAME = "this-repo"


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_ingestor.list_projects.return_value = []
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _ingestor(mock_connect: MagicMock) -> MagicMock:
    return mock_connect.return_value.__enter__.return_value


def _with_other_projects(mock_connect: MagicMock) -> MagicMock:
    ingestor = _ingestor(mock_connect)
    ingestor.list_projects.return_value = [*OTHER_PROJECTS, PROJECT_NAME]
    return ingestor


def _start_args(repo: Path, *extra: str) -> list[str]:
    return [
        "start",
        "--clean",
        "--repo-path",
        str(repo),
        "--project-name",
        PROJECT_NAME,
        *extra,
    ]


@pytest.fixture
def interactive_stdin() -> Generator[None, None, None]:
    with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
        yield


@pytest.fixture
def stub_sync() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli.GraphUpdater") as mock_updater,
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli.load_ignore_patterns") as mock_cgrignore,
    ):
        mock_cgrignore.return_value = CgrignorePatterns(
            exclude=frozenset(), unignore=frozenset()
        )
        yield mock_updater


class TestCleanConfirmation:
    def test_declining_the_prompt_leaves_the_graph_untouched(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(app, _start_args(tmp_path), input="n\n")

        assert result.exit_code == 1, result.output
        ingestor.clean_database.assert_not_called()

    def test_accepting_the_prompt_wipes_the_graph(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(app, _start_args(tmp_path), input="y\n")

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    def test_prompt_names_the_projects_that_would_be_destroyed(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        tmp_path: Path,
    ) -> None:
        _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(app, _start_args(tmp_path), input="n\n")

        for project in OTHER_PROJECTS:
            assert project in result.output

    def test_no_prompt_when_the_graph_holds_only_this_project(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        tmp_path: Path,
    ) -> None:
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_projects.return_value = [PROJECT_NAME]

        result = runner.invoke(app, _start_args(tmp_path))

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    def test_yes_flag_skips_the_prompt(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(app, _start_args(tmp_path, "--yes"))

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    def test_non_interactive_run_refuses_instead_of_deleting(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(app, _start_args(tmp_path))

        assert result.exit_code == 1, result.output
        ingestor.clean_database.assert_not_called()
        assert "--yes" in result.output

    def test_unreadable_project_list_refuses_to_clean(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        # Fail closed: if the projects cannot be listed there is no way to show
        # what the wipe destroys, so proceeding would delete every project
        # unconfirmed precisely when we know least.
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_projects.side_effect = RuntimeError("memgraph down")

        result = runner.invoke(app, _start_args(tmp_path))

        assert result.exit_code == 1, result.output
        ingestor.clean_database.assert_not_called()

    def test_unreadable_project_list_still_yields_to_an_explicit_yes(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        ingestor = _ingestor(mock_memgraph_connect)
        ingestor.list_projects.side_effect = RuntimeError("memgraph down")

        result = runner.invoke(app, _start_args(tmp_path, "--yes"))

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()


class TestCleanConfirmationWithUpdateGraph:
    def test_declining_skips_the_wipe_and_the_rebuild(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        stub_sync: MagicMock,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(
            app,
            _start_args(tmp_path, "--update-graph"),
            input="n\n",
        )

        assert result.exit_code == 1, result.output
        ingestor.clean_database.assert_not_called()
        # A regression that ran the rebuild before the confirmation exited
        # would still pass the assertion above.
        stub_sync.return_value.run.assert_not_called()

    def test_yes_flag_allows_the_wipe(
        self,
        mock_memgraph_connect: MagicMock,
        interactive_stdin: None,
        stub_sync: MagicMock,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(
            app,
            _start_args(tmp_path, "--update-graph", "--yes"),
        )

        assert result.exit_code == 0, result.output
        ingestor.clean_database.assert_called_once()

    def test_non_interactive_run_refuses_instead_of_deleting(
        self,
        mock_memgraph_connect: MagicMock,
        stub_sync: MagicMock,
        tmp_path: Path,
    ) -> None:
        ingestor = _with_other_projects(mock_memgraph_connect)

        result = runner.invoke(
            app,
            _start_args(tmp_path, "--update-graph"),
        )

        assert result.exit_code == 1, result.output
        ingestor.clean_database.assert_not_called()


def test_clean_help_marks_the_flag_destructive() -> None:
    result = runner.invoke(app, ["start", "--help"])

    assert result.exit_code == 0, result.output
    assert "DESTRUCTIVE" in result.output


def test_clean_done_message_unchanged_for_the_happy_path(
    mock_memgraph_connect: MagicMock,
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, _start_args(tmp_path))

    assert result.exit_code == 0, result.output
    assert cs.CLI_MSG_CLEAN_DONE in result.output


def test_prompt_counts_every_project_the_wipe_deletes(
    mock_memgraph_connect: MagicMock,
    interactive_stdin: None,
    tmp_path: Path,
) -> None:
    # This project has never been synced, so it is not in the graph. Deriving
    # the count from the OTHER projects would report one more than exists.
    ingestor = _ingestor(mock_memgraph_connect)
    ingestor.list_projects.return_value = list(OTHER_PROJECTS)

    result = runner.invoke(app, _start_args(tmp_path), input="n\n")

    assert f"Delete all {len(OTHER_PROJECTS)} project(s)" in result.output, (
        result.output
    )


def test_prompt_counts_this_project_when_it_is_already_in_the_graph(
    mock_memgraph_connect: MagicMock,
    interactive_stdin: None,
    tmp_path: Path,
) -> None:
    ingestor = _ingestor(mock_memgraph_connect)
    ingestor.list_projects.return_value = [*OTHER_PROJECTS, PROJECT_NAME]

    result = runner.invoke(app, _start_args(tmp_path), input="n\n")

    expected = len(OTHER_PROJECTS) + 1
    assert f"Delete all {expected} project(s)" in result.output, result.output
