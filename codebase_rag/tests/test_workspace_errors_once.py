"""Issue #2420: `cgr workspace` prints each error once, and `add-repo` only
accepts a directory that no other member of the workspace overlaps.

Every handler logged the error at ERROR and then echoed the same text, so
the user saw it twice. `add-repo` accepted a file (registered as a project
`cgr start --repo-path` then refuses to sync) and a path inside another
member, whose files a workspace sync would index under two projects.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from loguru import logger

from codebase_rag.workspaces import (
    WorkspaceError,
    add_repo,
    create_workspace,
    load_workspace,
)
from codebase_rag.workspaces.cli import cli as workspace_cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    return tmp_path / "cgr-home"


def _invoke(args: list[str]) -> tuple[Result, list[str]]:
    records: list[str] = []
    sink = logger.add(records.append, level="INFO", format="{message}")
    try:
        result = runner.invoke(workspace_cli, args)
    finally:
        logger.remove(sink)
    return result, records


def _setup_shop(tmp_path: Path) -> Path:
    backend = tmp_path / "src" / "backend"
    backend.mkdir(parents=True)
    create_workspace("shop")
    add_repo("shop", str(backend))
    return backend


@pytest.mark.parametrize(
    ("case", "args", "message"),
    [
        ("show-missing", lambda _: ["show", "nosuchws"], "not found"),
        ("create-existing", lambda _: ["create", "shop"], "already exists"),
        ("delete-missing", lambda _: ["delete", "nosuchws"], "not found"),
        (
            "add-duplicate",
            lambda backend: ["add-repo", "shop", str(backend)],
            "already in workspace",
        ),
        (
            "add-missing-path",
            lambda backend: ["add-repo", "shop", str(backend / "nope")],
            "does not exist",
        ),
        (
            "remove-not-member",
            lambda backend: ["remove-repo", "shop", str(backend.parent)],
            "No repo with path",
        ),
    ],
)
def test_each_workspace_error_is_printed_once(
    tmp_path: Path, case: str, args: Callable[[Path], list[str]], message: str
) -> None:
    backend = _setup_shop(tmp_path)

    result, records = _invoke(args(backend))

    assert result.exit_code == 1, result.output
    shown = result.output.count(message) + sum(r.count(message) for r in records)
    assert shown == 1, (result.output, records)


def test_add_repo_refuses_a_file(tmp_path: Path) -> None:
    backend = _setup_shop(tmp_path)
    api = backend / "api.py"
    api.write_text("def a():\n    pass\n")

    with pytest.raises(WorkspaceError, match="not a directory"):
        add_repo("shop", str(api))

    assert [r.path for r in load_workspace("shop").repos] == [str(backend)]


@pytest.mark.parametrize("where", ["inside", "containing"])
def test_add_repo_refuses_a_path_overlapping_a_member(
    tmp_path: Path, where: str
) -> None:
    backend = _setup_shop(tmp_path)
    overlapping = backend / "pkg" if where == "inside" else backend.parent
    overlapping.mkdir(exist_ok=True)

    # A Windows path is full of backslashes, which a raw pattern reads as escapes.
    with pytest.raises(WorkspaceError, match=re.escape(str(backend))):
        add_repo("shop", str(overlapping))

    assert len(load_workspace("shop").repos) == 1


@pytest.mark.parametrize("name", ["frontend", "backend2", "backend-old"])
def test_add_repo_accepts_a_sibling_directory(tmp_path: Path, name: str) -> None:
    # Negative: `backend2` merely shares a string prefix with `backend`.
    backend = _setup_shop(tmp_path)
    sibling = backend.parent / name
    sibling.mkdir()

    add_repo("shop", str(sibling))

    assert len(load_workspace("shop").repos) == 2


def test_a_successful_command_still_reports(tmp_path: Path) -> None:
    # Negative.
    _setup_shop(tmp_path)
    frontend = tmp_path / "src" / "frontend"
    frontend.mkdir()

    result, _ = _invoke(["add-repo", "shop", str(frontend)])

    assert result.exit_code == 0, result.output
    assert "Added repo" in result.output
