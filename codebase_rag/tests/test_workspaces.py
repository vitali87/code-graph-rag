from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.workspaces import (
    WorkspaceError,
    add_repo,
    create_workspace,
    delete_workspace,
    list_workspaces,
    load_workspace,
    remove_repo,
)
from codebase_rag.workspaces.models import WorkspaceConfig

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    yield tmp_path / "cgr-home"


class TestStorage:
    def test_create_then_load(self, _temp_home: Path) -> None:
        config, _ = create_workspace("alpha", description="testing")
        assert config.name == "alpha"
        loaded = load_workspace("alpha")
        assert loaded.name == "alpha"
        assert loaded.description == "testing"
        assert loaded.repos == []

    def test_create_duplicate_raises(self, _temp_home: Path) -> None:
        create_workspace("dup")
        with pytest.raises(WorkspaceError):
            create_workspace("dup")

    def test_create_with_force_overwrites(self, _temp_home: Path) -> None:
        create_workspace("over", description="first")
        config, _ = create_workspace("over", description="second", overwrite=True)
        assert config.description == "second"

    def test_load_missing_raises(self, _temp_home: Path) -> None:
        with pytest.raises(WorkspaceError):
            load_workspace("nope")

    def test_list_empty(self, _temp_home: Path) -> None:
        assert list_workspaces() == []

    def test_list_sorted(self, _temp_home: Path) -> None:
        create_workspace("b")
        create_workspace("a")
        create_workspace("c")
        assert list_workspaces() == ["a", "b", "c"]

    def test_delete(self, _temp_home: Path) -> None:
        create_workspace("kill")
        delete_workspace("kill")
        with pytest.raises(WorkspaceError):
            load_workspace("kill")

    def test_delete_missing_raises(self, _temp_home: Path) -> None:
        with pytest.raises(WorkspaceError):
            delete_workspace("nope")

    def test_add_repo_derives_project_name(
        self, tmp_path: Path, _temp_home: Path
    ) -> None:
        repo_dir = tmp_path / "some_repo"
        repo_dir.mkdir()
        create_workspace("mono")
        config, repo = add_repo("mono", str(repo_dir))
        assert repo.path == str(repo_dir.resolve())
        assert repo.project_name.startswith("some_repo__")
        assert config.repos[0].project_name == repo.project_name

    def test_add_repo_with_explicit_project_name(
        self, tmp_path: Path, _temp_home: Path
    ) -> None:
        repo_dir = tmp_path / "second_repo"
        repo_dir.mkdir()
        create_workspace("mono")
        _, repo = add_repo("mono", str(repo_dir), project_name="custom_name")
        assert repo.project_name == "custom_name"

    def test_add_repo_missing_path(self, tmp_path: Path, _temp_home: Path) -> None:
        create_workspace("mono")
        with pytest.raises(WorkspaceError):
            add_repo("mono", str(tmp_path / "does_not_exist"))

    def test_add_repo_duplicate(self, tmp_path: Path, _temp_home: Path) -> None:
        repo_dir = tmp_path / "dup_repo"
        repo_dir.mkdir()
        create_workspace("mono")
        add_repo("mono", str(repo_dir))
        with pytest.raises(WorkspaceError):
            add_repo("mono", str(repo_dir))

    def test_remove_repo(self, tmp_path: Path, _temp_home: Path) -> None:
        repo_dir = tmp_path / "rem_repo"
        repo_dir.mkdir()
        create_workspace("mono")
        add_repo("mono", str(repo_dir))
        config, _ = remove_repo("mono", str(repo_dir))
        assert config.repos == []

    def test_remove_repo_not_in_workspace(
        self, tmp_path: Path, _temp_home: Path
    ) -> None:
        repo_dir = tmp_path / "missing_repo"
        repo_dir.mkdir()
        create_workspace("mono")
        with pytest.raises(WorkspaceError):
            remove_repo("mono", str(repo_dir))


class TestCli:
    def test_workspace_list_empty(self, _temp_home: Path) -> None:
        result = runner.invoke(app, ["workspace", "list"])
        assert result.exit_code == 0, result.output
        assert "no workspaces" in result.output.lower()

    def test_workspace_create_list_show_delete(
        self, tmp_path: Path, _temp_home: Path
    ) -> None:
        result = runner.invoke(app, ["workspace", "create", "mono"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workspace", "list"])
        assert "mono" in result.output

        result = runner.invoke(app, ["workspace", "show", "mono"])
        assert "mono" in result.output

        result = runner.invoke(app, ["workspace", "delete", "mono"])
        assert result.exit_code == 0, result.output

        result = runner.invoke(app, ["workspace", "list"])
        assert "no workspaces" in result.output.lower()

    def test_workspace_add_remove_repo_via_cli(
        self, tmp_path: Path, _temp_home: Path
    ) -> None:
        repo_dir = tmp_path / "the_repo"
        repo_dir.mkdir()

        runner.invoke(app, ["workspace", "create", "mono"])
        result = runner.invoke(app, ["workspace", "add-repo", "mono", str(repo_dir)])
        assert result.exit_code == 0, result.output
        assert str(repo_dir.resolve()) in result.output

        result = runner.invoke(app, ["workspace", "show", "mono"])
        assert str(repo_dir.resolve()) in result.output

        result = runner.invoke(app, ["workspace", "remove-repo", "mono", str(repo_dir)])
        assert result.exit_code == 0, result.output


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


@pytest.fixture
def mock_validate_models() -> Generator[None, None, None]:
    with patch("codebase_rag.cli._update_and_validate_models"):
        yield


def test_start_with_workspace_passes_all_projects(
    mock_memgraph_connect: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()

    create_workspace("mono")
    add_repo("mono", str(repo_a), project_name="proj_a")
    add_repo("mono", str(repo_b), project_name="proj_b")

    with (
        patch("codebase_rag.cli._run_graph_sync") as mock_sync,
        patch("codebase_rag.cli.main_single_query") as mock_single,
    ):
        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(repo_a),
                "--workspace",
                "mono",
                "--ask-agent",
                "hi",
            ],
        )
    assert result.exit_code == 0, result.output
    assert mock_sync.call_count == 2
    project_names_synced = [c.kwargs["project_name"] for c in mock_sync.call_args_list]
    assert set(project_names_synced) == {"proj_a", "proj_b"}
    mock_single.assert_called_once()
    assert mock_single.call_args.kwargs["active_projects"] == ["proj_a", "proj_b"]


def test_start_with_unknown_workspace_errors(
    mock_memgraph_connect: MagicMock,
    mock_validate_models: None,
    tmp_path: Path,
    _temp_home: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "start",
            "--repo-path",
            str(tmp_path),
            "--workspace",
            "doesnotexist",
            "--ask-agent",
            "hi",
            "--no-sync",
        ],
    )
    assert result.exit_code != 0


def test_workspace_model_project_names() -> None:
    config = WorkspaceConfig(
        name="x",
        repos=[],
    )
    assert config.project_names() == []
