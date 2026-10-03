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

from codebase_rag import constants as cs
from codebase_rag.cli import _launch_session, _run_graph_sync, app
from codebase_rag.workspaces import add_repo, create_workspace, load_workspace

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


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        (["-a", "where is main?"], {"ask_agent": "where is main?"}),
        (
            ["-a", "where is main?", "--output-format", "json"],
            {"ask_agent": "where is main?", "output_format": cs.QueryFormat.JSON},
        ),
        (["--projects", "alpha,beta"], {"active_projects": ["alpha", "beta"]}),
    ],
    ids=["ask-agent", "ask-agent-json", "projects"],
)
def test_the_workspace_assistant_reads_its_options_after_update_graph(
    sync: MagicMock,
    session: MagicMock,
    shop: tuple[Path, Path, Path],
    extra: list[str],
    expected: dict[str, object],
) -> None:
    # #2478 refuses these with a repository's `--update-graph`, which exits
    # before anything reads them. A workspace's opens the assistant next, so
    # they reach it instead of being refused.
    _, _, elsewhere = shop

    result = runner.invoke(
        app, _start(elsewhere, "--workspace", "shop", "--update-graph", *extra)
    )

    assert result.exit_code == 0, result.output
    assert sync.call_count == 2
    launched = _launched_with(session)
    assert {name: launched[name] for name in expected} == expected


@pytest.mark.parametrize(
    ("extra", "option"),
    [
        (["-a", "where is main?"], "--ask-agent"),
        (["--output-format", "json"], "--output-format json"),
        (["--projects", "alpha,beta"], "--projects"),
        (["--no-sync"], "--no-sync"),
    ],
    ids=["ask-agent", "output-format-json", "projects", "no-sync"],
)
def test_a_repository_update_graph_still_refuses_what_it_would_drop(
    sync: MagicMock,
    session: MagicMock,
    tmp_path: Path,
    extra: list[str],
    option: str,
) -> None:
    # Negative: without `--workspace`, #2478's refusals stand.
    result = runner.invoke(app, _start(tmp_path, "--update-graph", *extra))

    out = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, out
    assert f"{option} cannot be combined with --update-graph" in out
    sync.assert_not_called()
    session.assert_not_called()


def test_a_workspace_update_graph_still_refuses_no_sync(
    sync: MagicMock, session: MagicMock, shop: tuple[Path, Path, Path]
) -> None:
    # Negative: `--update-graph --no-sync` contradicts itself in either mode.
    _, _, elsewhere = shop

    result = runner.invoke(
        app,
        _start(elsewhere, "--workspace", "shop", "--update-graph", "--no-sync"),
    )

    out = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, out
    assert "--no-sync cannot be combined with --update-graph" in out
    sync.assert_not_called()
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


@pytest.fixture
def empty(tmp_path: Path) -> Path:
    # A workspace with no repositories, and a directory to start from.
    create_workspace("empty")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    return elsewhere


@pytest.mark.parametrize(
    "extra",
    [
        ["--update-graph"],
        ["--update-graph", "-a", "where is main?"],
        ["--update-graph", "--projects", "alpha"],
        [],
        ["--no-sync"],
        ["-a", "where is main?"],
    ],
    ids=["update", "update-ask", "update-projects", "chat", "no-sync", "ask"],
)
def test_an_empty_workspace_opens_no_assistant(
    sync: MagicMock, session: MagicMock, empty: Path, extra: list[str]
) -> None:
    # Greptile review of PR 2507: a workspace with no repositories left the
    # project scope empty, and an empty scope is what "no scope" looks like
    # to the assistant, so it opened on every project in the shared graph.
    # Its `--update-graph` synced nothing and reported a completed update.
    result = runner.invoke(app, _start(empty, "--workspace", "empty", *extra))

    out = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, out
    assert "'empty' has no repositories" in out
    assert "Graph update completed" not in out
    session.assert_not_called()
    sync.assert_not_called()


def test_an_empty_workspace_with_projects_still_opens_a_chat_on_them(
    sync: MagicMock, session: MagicMock, empty: Path
) -> None:
    # Negative: `--projects` names the scope of a chat that syncs nothing,
    # as it did before (fe30ec957).
    result = runner.invoke(
        app, _start(empty, "--workspace", "empty", "--projects", "alpha,beta")
    )

    assert result.exit_code == 0, result.output
    assert _launched_with(session)["active_projects"] == ["alpha", "beta"]


def test_a_workspace_without_update_graph_syncs_it_before_the_chat(
    sync: MagicMock, session: MagicMock, shop: tuple[Path, Path, Path]
) -> None:
    # Negative: without `--update-graph` nothing is synced up front; the chat
    # opens on the workspace's projects and syncs its repositories first.
    backend, frontend, elsewhere = shop

    result = runner.invoke(app, _start(elsewhere, "--workspace", "shop"))

    assert result.exit_code == 0, result.output
    sync.assert_not_called()
    launched = _launched_with(session)
    assert launched["active_projects"] == load_workspace("shop").project_names()
    sync_task = launched["sync_task"]
    assert callable(sync_task)
    sync_task()
    synced = [call.kwargs["repo"] for call in sync.call_args_list]
    assert synced == [backend.resolve(), frontend.resolve()]


def test_start_without_a_workspace_opens_the_chat_on_the_repository(
    sync: MagicMock, session: MagicMock, tmp_path: Path
) -> None:
    # Negative: the non-workspace chat is scoped to `--repo-path`'s project.
    result = runner.invoke(app, _start(tmp_path, "--project-name", "solo"))

    assert result.exit_code == 0, result.output
    sync.assert_not_called()
    launched = _launched_with(session)
    assert launched["active_projects"] == ["solo"]
    sync_task = launched["sync_task"]
    assert callable(sync_task)
    sync_task()
    assert sync.call_args.kwargs["repo"] == tmp_path.resolve()


def _assistant_scope_check(active_projects: list[str] | None) -> None:
    # Stop `_initialize_services_and_agent` at its first step after the scope
    # check, so no model or tool is built.
    from codebase_rag import main as main_mod

    with patch.object(
        main_mod, "_validate_provider_config", side_effect=RuntimeError("past")
    ):
        main_mod._initialize_services_and_agent(
            "/repo", MagicMock(), active_projects=active_projects
        )


def test_the_assistant_refuses_an_empty_project_scope() -> None:
    # Fail closed: an empty scope must never be read as every project.
    with pytest.raises(ValueError, match="no project"):
        _assistant_scope_check([])


@pytest.mark.parametrize(
    "active_projects", [None, ["alpha"], ["alpha", "beta"]], ids=str
)
def test_the_assistant_accepts_a_named_or_absent_scope(
    active_projects: list[str] | None,
) -> None:
    # Negative: a named scope, or none at all (`cgr optimize`), gets past it.
    with pytest.raises(RuntimeError, match="past"):
        _assistant_scope_check(active_projects)
