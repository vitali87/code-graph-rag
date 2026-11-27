from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.mcp.server import get_workspace
from codebase_rag.workspaces import (
    WorkspaceConfig,
    WorkspaceError,
    add_repo,
    create_workspace,
    delete_workspace,
    list_workspaces,
    load_workspace,
    workspaces_dir,
)
from codebase_rag.workspaces.constants import MAX_WORKSPACE_NAME_LENGTH

runner = CliRunner()

TARGET_BODY = '[project]\nname = "proj"\n'
INVALID_NAME = "Invalid workspace name"

BAD_NAMES = [
    "",
    ".",
    "..",
    "../x",
    "a/b",
    "/abs/path",
    "a\\b",
    ".hidden",
    "name\n",
    "with space",
]
GOOD_NAMES = ["alpha", "my-ws", "my_ws", "ws.v2", "A1", "2024"]


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    return tmp_path / "cgr-home"


@pytest.fixture
def outside_file(tmp_path: Path) -> Path:
    # The workspaces directory must exist for the kernel to resolve `..`
    # through it, exactly as it does once any workspace has been created.
    workspaces_dir().mkdir(parents=True)
    target = tmp_path / "proj" / "pyproject.toml"
    target.parent.mkdir()
    target.write_text(TARGET_BODY)
    return target


# From <tmp>/cgr-home/workspaces, two levels up is <tmp>.
TRAVERSING_NAME = "../../proj/pyproject"


class TestTraversalIsRefused:
    def test_delete_does_not_remove_a_file_outside(self, outside_file: Path) -> None:
        with pytest.raises(WorkspaceError, match=INVALID_NAME):
            delete_workspace(TRAVERSING_NAME)
        assert outside_file.read_text() == TARGET_BODY

    def test_create_force_does_not_overwrite_a_file_outside(
        self, outside_file: Path
    ) -> None:
        with pytest.raises(WorkspaceError, match=INVALID_NAME):
            create_workspace(TRAVERSING_NAME, overwrite=True)
        assert outside_file.read_text() == TARGET_BODY

    def test_load_names_the_bad_name_not_a_missing_file(
        self, outside_file: Path
    ) -> None:
        with pytest.raises(WorkspaceError, match=INVALID_NAME):
            load_workspace(TRAVERSING_NAME)

    def test_cli_delete_exits_1_and_keeps_the_file(self, outside_file: Path) -> None:
        result = runner.invoke(app, ["workspace", "delete", TRAVERSING_NAME])
        assert result.exit_code == 1, result.output
        assert INVALID_NAME in result.output
        assert "Deleted workspace" not in result.output
        assert outside_file.read_text() == TARGET_BODY

    def test_cli_create_force_exits_1_and_keeps_the_file(
        self, outside_file: Path
    ) -> None:
        result = runner.invoke(app, ["workspace", "create", "--force", TRAVERSING_NAME])
        assert result.exit_code == 1, result.output
        assert INVALID_NAME in result.output
        assert outside_file.read_text() == TARGET_BODY

    def test_mcp_server_refuses_a_traversing_workspace(
        self, outside_file: Path
    ) -> None:
        with pytest.raises(ValueError, match=INVALID_NAME):
            get_workspace(TRAVERSING_NAME)


class TestBadNamesCreateNothing:
    @pytest.mark.parametrize("name", BAD_NAMES)
    def test_create_refuses_and_writes_no_file(
        self, name: str, _temp_home: Path
    ) -> None:
        with pytest.raises(WorkspaceError, match=INVALID_NAME):
            create_workspace(name)
        written = list(_temp_home.rglob("*")) if _temp_home.exists() else []
        assert [p for p in written if p.is_file()] == []

    @pytest.mark.parametrize("name", BAD_NAMES)
    def test_delete_refuses(self, name: str) -> None:
        with pytest.raises(WorkspaceError, match=INVALID_NAME):
            delete_workspace(name)

    def test_nested_and_empty_names_never_reach_list(self) -> None:
        for name in ("a/b", ""):
            result = runner.invoke(app, ["workspace", "create", name])
            assert result.exit_code == 1, result.output
        assert list_workspaces() == []


class TestHandEditedName:
    def test_a_traversing_name_inside_the_file_is_refused_on_load(
        self, tmp_path: Path, outside_file: Path
    ) -> None:
        # add-repo loads by the file's stem but saves under config.name, so a
        # bad name in the TOML would redirect that save outside the directory.
        (workspaces_dir() / "good.toml").write_text(
            f'[workspace]\nname = "{TRAVERSING_NAME}"\nrepos = []\n'
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        with pytest.raises(WorkspaceError):
            load_workspace("good")
        with pytest.raises(WorkspaceError):
            add_repo("good", str(repo))
        assert outside_file.read_text() == TARGET_BODY


class TestOrdinaryNamesStillWork:
    @pytest.mark.parametrize("name", GOOD_NAMES)
    def test_round_trip(self, name: str) -> None:
        _, path = create_workspace(name, description="d")
        assert path == workspaces_dir() / f"{name}.toml"
        assert load_workspace(name).name == name
        assert list_workspaces() == [name]
        delete_workspace(name)
        assert list_workspaces() == []

    def test_missing_valid_name_still_reports_not_found(self) -> None:
        with pytest.raises(WorkspaceError, match="not found"):
            delete_workspace("absent")

    def test_cli_create_and_delete_a_dotted_name(self) -> None:
        result = runner.invoke(app, ["workspace", "create", "team.backend"])
        assert result.exit_code == 0, result.output
        result = runner.invoke(app, ["workspace", "delete", "team.backend"])
        assert result.exit_code == 0, result.output
        assert "Deleted workspace 'team.backend'" in result.output


class TestWorkspaceNameLengthLimit:
    def test_max_length_name_succeeds(self) -> None:
        max_name = "A" * MAX_WORKSPACE_NAME_LENGTH
        result = runner.invoke(app, ["workspace", "create", max_name])
        assert result.exit_code == 0, result.output
        assert load_workspace(max_name).name == max_name
        result = runner.invoke(app, ["workspace", "delete", max_name])
        assert result.exit_code == 0, result.output

    @pytest.mark.parametrize(
        "over_long",
        ["A" * (MAX_WORKSPACE_NAME_LENGTH + 1), "A" * 300],
    )
    def test_over_long_name_is_refused_without_os_error(
        self, tmp_path: Path, over_long: str
    ) -> None:
        # Ensure the workspaces directory exists so stat() probes would reach
        # ENAMETOOLONG if validation missed the length bound (issue #3686).
        create_workspace("demo")
        repo = tmp_path / "repo"
        repo.mkdir()

        for args in (
            ["workspace", "create", over_long],
            ["workspace", "show", over_long],
            ["workspace", "add-repo", over_long, str(repo)],
            ["workspace", "remove-repo", over_long, str(repo)],
            ["workspace", "delete", over_long],
            [
                "start",
                "--repo-path",
                str(repo),
                "--workspace",
                over_long,
                "--ask-agent",
                "hi",
                "--no-sync",
            ],
        ):
            result = runner.invoke(app, args)
            assert result.exit_code == 1, (args, result.output)
            assert INVALID_NAME in result.output, (args, result.output)
            assert "File name too long" not in result.output
            assert "Traceback" not in result.output

        with pytest.raises(ValueError, match=INVALID_NAME):
            get_workspace(over_long)

    def test_model_and_hand_edited_toml_refuse_over_long_name(
        self, tmp_path: Path
    ) -> None:
        over_long = "A" * (MAX_WORKSPACE_NAME_LENGTH + 1)
        with pytest.raises(ValidationError):
            WorkspaceConfig(name=over_long)

        workspaces_dir().mkdir(parents=True)
        (workspaces_dir() / "good.toml").write_text(
            f'[workspace]\nname = "{over_long}"\nrepos = []\n'
        )
        repo = tmp_path / "repo"
        repo.mkdir()

        with pytest.raises(WorkspaceError):
            load_workspace("good")
        with pytest.raises(WorkspaceError):
            add_repo("good", str(repo))
