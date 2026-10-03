"""Issue #2418: `cgr start --workspace NAME --update-graph` syncs the
workspace's repositories, and a sync never crawls a home directory or the
filesystem root by accident.

The update path ignored `--workspace` and synced `--repo-path`'s default,
the current directory: run from `$HOME`, that indexed the user's whole home
directory into the shared graph under a new project and wrote sync state
there, while the workspace's own repositories were not synced at all.
"""

from __future__ import annotations

import inspect
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from typer.testing import CliRunner

from codebase_rag.cli import _launch_session, _run_graph_sync, app
from codebase_rag.workspaces import add_repo, create_workspace

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    return tmp_path / "cgr-home"


@pytest.fixture
def sync() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli._run_graph_sync") as run_graph_sync,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli._maybe_start_stack"),
    ):
        yield run_graph_sync


@pytest.fixture
def shop(tmp_path: Path) -> tuple[Path, Path, Path]:
    backend, frontend, elsewhere = (
        tmp_path / "src" / "backend",
        tmp_path / "src" / "frontend",
        tmp_path / "home",
    )
    for path in (backend, frontend, elsewhere):
        path.mkdir(parents=True)
    create_workspace("shop")
    add_repo("shop", str(backend))
    add_repo("shop", str(frontend))
    return backend, frontend, elsewhere


def _start(cwd: Path, *extra: str) -> list[str]:
    return ["start", "--repo-path", str(cwd), "--no-start-stack", *extra]


@pytest.mark.usefixtures("session")
def test_update_graph_with_a_workspace_syncs_its_repositories(
    sync: MagicMock, shop: tuple[Path, Path, Path]
) -> None:
    backend, frontend, elsewhere = shop

    result = runner.invoke(
        app, _start(elsewhere, "--workspace", "shop", "--update-graph")
    )

    assert result.exit_code == 0, result.output
    synced = [call.kwargs["repo"] for call in sync.call_args_list]
    assert synced == [backend.resolve(), frontend.resolve()]


def test_an_unknown_workspace_syncs_nothing(
    sync: MagicMock, shop: tuple[Path, Path, Path]
) -> None:
    _, _, elsewhere = shop

    result = runner.invoke(
        app, _start(elsewhere, "--workspace", "nope", "--update-graph")
    )

    assert result.exit_code == 1, result.output
    sync.assert_not_called()


@pytest.mark.parametrize(
    ("option", "extra"),
    [
        ("--clean", ["--clean", "--yes"]),
        ("--output", ["-o", "graph.json"]),
        ("--interactive-setup", ["--interactive-setup"]),
    ],
)
def test_options_a_workspace_sync_cannot_honour_are_refused(
    sync: MagicMock, shop: tuple[Path, Path, Path], option: str, extra: list[str]
) -> None:
    _, _, elsewhere = shop

    result = runner.invoke(
        app, _start(elsewhere, "--workspace", "shop", "--update-graph", *extra)
    )

    assert result.exit_code == 1, result.output
    assert option in click.unstyle(result.output)
    sync.assert_not_called()


def test_update_graph_without_a_workspace_syncs_the_repo_path(
    sync: MagicMock, tmp_path: Path
) -> None:
    # Negative.
    result = runner.invoke(app, _start(tmp_path, "--update-graph"))

    assert result.exit_code == 0, result.output
    assert sync.call_args.kwargs["repo"] == tmp_path.resolve()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    user_home = tmp_path / "me"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    # Windows resolves Path.home() from USERPROFILE, not HOME.
    monkeypatch.setenv("USERPROFILE", str(user_home))
    return user_home


@pytest.fixture
def graph() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.graph_updater.GraphUpdater") as updater,
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        yield updater


def _sync_repo(repo: Path, *, assume_yes: bool = False) -> None:
    _run_graph_sync(
        repo=repo,
        project_name="p",
        project_named=False,
        batch_size=1,
        exclude=None,
        interactive_setup=False,
        assume_yes=assume_yes,
    )


@pytest.mark.parametrize("target", ["home", "root"])
def test_a_sync_of_the_home_directory_or_root_is_refused(
    graph: MagicMock, home: Path, target: str
) -> None:
    repo = home if target == "home" else Path(home.anchor)

    with pytest.raises(typer.Exit):
        _sync_repo(repo)

    graph.assert_not_called()


def test_yes_indexes_the_home_directory_anyway(graph: MagicMock, home: Path) -> None:
    _sync_repo(home, assume_yes=True)

    graph.return_value.run.assert_called_once()


def test_a_repository_inside_the_home_directory_syncs(
    graph: MagicMock, home: Path
) -> None:
    # Negative: only the home directory itself is refused.
    repo = home / "src" / "api"
    repo.mkdir(parents=True)

    _sync_repo(repo)

    graph.return_value.run.assert_called_once()


def _launched_with(session: MagicMock) -> dict[str, object]:
    # The arguments the chat (or `-a`) was opened with, by name.
    call = session.call_args
    return inspect.signature(_launch_session).bind(*call.args, **call.kwargs).arguments


@pytest.fixture
def session() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli._launch_session") as launch,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli._maybe_start_stack"),
    ):
        yield launch


def test_update_graph_with_a_workspace_then_opens_the_assistant_scoped_to_it(
    sync: MagicMock, session: MagicMock, shop: tuple[Path, Path, Path]
) -> None:
    # Issue #2418's first option: each repository is synced, then the
    # assistant opens on the workspace's projects, with nothing left to sync.
    _, _, elsewhere = shop

    result = runner.invoke(
        app, _start(elsewhere, "--workspace", "shop", "--update-graph")
    )

    assert result.exit_code == 0, result.output
    assert sync.call_count == 2
    launched = _launched_with(session)
    assert launched["active_projects"] == [
        call.kwargs["project_name"] for call in sync.call_args_list
    ]
    assert launched["sync_task"] is None


def test_update_graph_without_a_workspace_still_stops_after_the_sync(
    sync: MagicMock, session: MagicMock, tmp_path: Path
) -> None:
    # Negative: a repository's `--update-graph` syncs and stops, as before.
    result = runner.invoke(app, _start(tmp_path, "--update-graph"))

    assert result.exit_code == 0, result.output
    sync.assert_called_once()
    session.assert_not_called()


@pytest.fixture
def home_workspace(home: Path) -> Path:
    create_workspace("dotfiles")
    add_repo("dotfiles", str(home))
    return home


@pytest.mark.parametrize("assume_yes", [True, False])
def test_yes_reaches_each_repository_of_a_workspace_sync(
    graph: MagicMock,
    session: MagicMock,
    home_workspace: Path,
    tmp_path: Path,
    assume_yes: bool,
) -> None:
    # CodeRabbit review of PR 2507: `--yes` was not passed on to the
    # workspace's syncs, so a repository that is the home directory was
    # refused even with it. Without `--yes` it still is.
    extra = ["--yes"] if assume_yes else []

    result = runner.invoke(
        app, _start(tmp_path, "--workspace", "dotfiles", "--update-graph", *extra)
    )

    if assume_yes:
        assert result.exit_code == 0, result.output
        graph.return_value.run.assert_called_once()
    else:
        assert result.exit_code == 1, result.output
        assert "refusing to index" in click.unstyle(result.output)
        graph.assert_not_called()


@pytest.mark.parametrize("assume_yes", [True, False])
def test_yes_reaches_the_sync_before_the_chat(
    graph: MagicMock, session: MagicMock, home: Path, assume_yes: bool
) -> None:
    # The sync the chat runs first is guarded the same way, so `--yes` must
    # reach it too.
    extra = ["--yes"] if assume_yes else []

    result = runner.invoke(app, _start(home, *extra))
    assert result.exit_code == 0, result.output
    sync_task = _launched_with(session)["sync_task"]

    assert callable(sync_task)
    if assume_yes:
        sync_task()
        graph.return_value.run.assert_called_once()
    else:
        with pytest.raises(typer.Exit):
            sync_task()
        graph.assert_not_called()
