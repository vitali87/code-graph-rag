"""Issue #2411: a project name already indexing another repository is not
silently taken over, and the repository that lost it is not "in sync".

`cgr start --update-graph --project-name api2` on `orgB/api` replaced the
graph `orgA/api` had written under `api2` with no warning. Syncing `orgA/api`
again then reported "already in sync": its hash cache still matched its files
and the cache check only asked whether the project had SOME modules, so the
graph kept claiming `root_path: orgA/api` while holding orgB's code.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import (
    _exit_if_project_owned_elsewhere,
    _pre_chat_sync,
    _run_graph_sync,
    app,
)
from codebase_rag.cli_help import HELP_PROJECT_NAME
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceRepo
from evals.cgr_graph import _StatefulIngestor

runner = CliRunner()


def _repos(tmp_path: Path) -> tuple[Path, Path]:
    org_a = tmp_path / "orgA" / "api"
    org_b = tmp_path / "orgB" / "api"
    org_a.mkdir(parents=True)
    org_b.mkdir(parents=True)
    (org_a / "billing.py").write_text("def charge_card():\n    return 1\n")
    (org_b / "users.py").write_text("def list_users():\n    return []\n")
    return org_a, org_b


def _sync(
    repo: Path,
    store: _StatefulIngestor,
    *,
    force: bool = False,
    state_dir: Path | None = None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="api2",
        project_named=True,
        state_dir=state_dir,
    )
    updater.run(force=force)
    return updater


def _functions(store: _StatefulIngestor) -> set[str]:
    return {str(uid) for label, uid in store.nodes if label == cs.NodeLabel.FUNCTION}


def test_a_repo_whose_project_another_repo_replaced_is_rebuilt(
    tmp_path: Path,
) -> None:
    org_a, org_b = _repos(tmp_path)
    store = _StatefulIngestor()
    _sync(org_a, store)
    _sync(org_b, store)

    again = _sync(org_a, store)

    assert again.skipped_because_in_sync is False
    assert _functions(store) == {"api2.billing.charge_card"}


def test_a_replaced_project_is_rebuilt_when_the_cache_lives_outside_the_repo(
    tmp_path: Path,
) -> None:
    org_a, org_b = _repos(tmp_path)
    state_a, state_b = tmp_path / "state_a", tmp_path / "state_b"
    state_a.mkdir()
    state_b.mkdir()
    store = _StatefulIngestor()
    _sync(org_a, store, state_dir=state_a)
    _sync(org_b, store, state_dir=state_b)

    again = _sync(org_a, store, state_dir=state_a)

    assert again.skipped_because_in_sync is False
    assert _functions(store) == {"api2.billing.charge_card"}


def test_an_unchanged_repo_with_its_cache_outside_the_repo_is_still_in_sync(
    tmp_path: Path,
) -> None:
    org_a, _ = _repos(tmp_path)
    state_a = tmp_path / "state_a"
    state_a.mkdir()
    store = _StatefulIngestor()
    _sync(org_a, store, state_dir=state_a)

    again = _sync(org_a, store, state_dir=state_a)

    assert again.skipped_because_in_sync is True


def test_an_unchanged_repo_is_still_in_sync(tmp_path: Path) -> None:
    # Negative: the root check must not defeat the fast path it guards.
    org_a, _ = _repos(tmp_path)
    store = _StatefulIngestor()
    _sync(org_a, store)

    again = _sync(org_a, store)

    assert again.skipped_because_in_sync is True


def test_a_project_with_no_recorded_root_keeps_its_cache(tmp_path: Path) -> None:
    # Negative: graphs written before `root_path` existed prove nothing about
    # which repository they hold.
    org_a, _ = _repos(tmp_path)
    store = _StatefulIngestor()
    _sync(org_a, store)
    store.nodes[(cs.NodeLabel.PROJECT, "api2")].pop(cs.KEY_ROOT_PATH)

    again = _sync(org_a, store)

    assert again.skipped_because_in_sync is True


class _RootUnreadable(_StatefulIngestor):
    """A graph that answers everything but the project-root read."""

    def fetch_all(self, query: str, params: object = None) -> list[dict[str, object]]:
        if query == cq.CYPHER_PROJECT_ROOT_PATH:
            raise ConnectionError("lost connection")
        return super().fetch_all(query, params)


def test_an_unreadable_root_stops_the_sync_before_its_project_write(
    tmp_path: Path,
) -> None:
    # Review of PR 2499: a failed read looked like "no recorded root", so a
    # repository displaced from its name trusted its unchanged cache and
    # reported the other repository's code as in sync, and the Project write
    # then replaced the root that would have shown the mismatch later.
    org_a, org_b = _repos(tmp_path)
    store = _RootUnreadable()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=org_a,
        parsers=parsers,
        queries=queries,
        project_name="api2",
        project_named=True,
    ).run()
    store.nodes[(cs.NodeLabel.PROJECT, "api2")] = {
        cs.KEY_NAME: "api2",
        cs.KEY_ROOT_PATH: str(org_b.resolve()),
    }

    with pytest.raises(ConnectionError):
        _sync(org_a, store)

    project = store.nodes[(cs.NodeLabel.PROJECT, "api2")]
    assert project[cs.KEY_ROOT_PATH] == str(org_b.resolve())


def test_a_write_only_sink_still_syncs_with_a_cache(tmp_path: Path) -> None:
    # Negative: the protobuf exporter has no graph that could own a project,
    # so there is no root to read and nothing to refuse.
    org_a, _ = _repos(tmp_path)
    parsers, queries = load_parsers()
    for run in ("first", "second"):
        GraphUpdater(
            ingestor=ProtobufFileIngestor(str(tmp_path / run)),
            repo_path=org_a,
            parsers=parsers,
            queries=queries,
            project_name="api2",
            project_named=True,
        ).run()

    assert (org_a / cs.HASH_CACHE_FILENAME).is_file()


@pytest.fixture
def graph() -> Generator[MagicMock, None, None]:
    ingestor = MagicMock()
    ingestor.fetch_all.return_value = []
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli.main_single_query"),
        patch("codebase_rag.graph_updater.GraphUpdater") as updater,
    ):
        connect.return_value.__enter__ = MagicMock(return_value=ingestor)
        connect.return_value.__exit__ = MagicMock(return_value=False)
        ingestor.updater = updater
        yield ingestor


def _owned_by(ingestor: MagicMock, root: Path | None) -> None:
    # Matches on the exact name, as the Cypher lookup does. The claim answers
    # a name nobody holds with the claimant's own root, as its MERGE does.
    def fetch_all(
        query: str, params: dict[str, str] | None = None
    ) -> list[dict[str, str]]:
        params = params or {}
        owned = root is not None and params.get(cs.KEY_PROJECT_NAME) == "api2"
        if query == cq.CYPHER_PROJECT_ROOT_PATH and owned:
            return [{cs.KEY_ROOT_PATH: str(root)}]
        if query == cq.CYPHER_CLAIM_PROJECT_ROOT:
            claimed = str(root) if owned else params[cs.KEY_ROOT_PATH]
            return [{cs.KEY_ROOT_PATH: claimed}]
        return []

    ingestor.fetch_all.side_effect = fetch_all


def _start(repo: Path, *extra: str) -> list[str]:
    return [
        "start",
        "--repo-path",
        str(repo),
        "--update-graph",
        "--project-name",
        "api2",
        "--no-start-stack",
        *extra,
    ]


def test_start_refuses_a_name_that_indexes_another_repo(
    graph: MagicMock, tmp_path: Path
) -> None:
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)

    result = runner.invoke(app, _start(org_b))

    assert result.exit_code == 1, result.output
    output = "".join(click.unstyle(result.output).split())
    assert str(org_a) in output
    assert "--yes" in output
    graph.updater.assert_not_called()


def test_yes_replaces_the_other_repos_project(graph: MagicMock, tmp_path: Path) -> None:
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)

    result = runner.invoke(app, _start(org_b, "--yes"))

    assert result.exit_code == 0, result.output
    graph.updater.return_value.run.assert_called_once()


@pytest.mark.parametrize("owner", ["same", "none"])
def test_a_name_this_repo_owns_or_nobody_owns_syncs(
    graph: MagicMock, tmp_path: Path, owner: str
) -> None:
    # Negative.
    org_a, _ = _repos(tmp_path)
    _owned_by(graph, org_a if owner == "same" else None)

    result = runner.invoke(app, _start(org_a))

    assert result.exit_code == 0, result.output
    graph.updater.return_value.run.assert_called_once()


def _root_read_fails(ingestor: MagicMock) -> None:
    def fetch_all(query: str, params: object = None) -> list[dict[str, str]]:
        if query in (cq.CYPHER_PROJECT_ROOT_PATH, cq.CYPHER_CLAIM_PROJECT_ROOT):
            raise ConnectionError("lost connection")
        return []

    ingestor.fetch_all.side_effect = fetch_all


def test_start_refuses_when_the_owner_cannot_be_read(
    graph: MagicMock, tmp_path: Path
) -> None:
    # Review of PR 2499: an unreadable owner is not an absent one.
    _, org_b = _repos(tmp_path)
    _root_read_fails(graph)

    result = runner.invoke(app, _start(org_b))

    assert result.exit_code == 1, result.output
    output = " ".join(click.unstyle(result.output).split())
    assert "lost connection" in output
    assert "--yes" in output
    graph.updater.assert_not_called()


def test_yes_syncs_when_the_owner_cannot_be_read(
    graph: MagicMock, tmp_path: Path
) -> None:
    _, org_b = _repos(tmp_path)
    _root_read_fails(graph)

    result = runner.invoke(app, _start(org_b, "--yes"))

    assert result.exit_code == 0, result.output
    graph.updater.return_value.run.assert_called_once()


@pytest.mark.parametrize("padded", [" api2", "api2 ", "  api2  "])
def test_a_padded_name_is_checked_as_the_name_it_writes(
    graph: MagicMock, tmp_path: Path, padded: str
) -> None:
    # Review of PR 2499: the updater strips the name, so the check must ask
    # about the same one or a padded name slips past it.
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)
    args = _start(org_b)
    args[args.index("api2")] = padded

    result = runner.invoke(app, args)

    assert result.exit_code == 1, result.output
    graph.updater.assert_not_called()


def test_a_padded_name_this_repo_owns_syncs_under_the_stripped_name(
    graph: MagicMock, tmp_path: Path
) -> None:
    # Negative.
    org_a, _ = _repos(tmp_path)
    _owned_by(graph, org_a)
    args = _start(org_a)
    args[args.index("api2")] = " api2 "

    result = runner.invoke(app, args)

    assert result.exit_code == 0, result.output
    assert graph.updater.call_args.kwargs["project_name"] == "api2"


def _workspace_sync(repo: Path, project_name: str) -> None:
    # The pre-chat sync of an active workspace, whose repositories carry the
    # project name their workspace file records.
    workspace = WorkspaceConfig(
        name="ws", repos=[WorkspaceRepo(path=str(repo), project_name=project_name)]
    )
    sync, _ = _pre_chat_sync(workspace, lambda: None, 10, None, None, False)
    sync()


@pytest.mark.parametrize("padded", [" api2", "api2 ", "  api2  "])
def test_a_padded_workspace_name_is_checked_as_the_name_it_writes(
    graph: MagicMock, tmp_path: Path, padded: str
) -> None:
    # Greptile review of PR 2499: a workspace file can hold a padded name,
    # which the check looked up as given while the updater wrote it stripped.
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)

    with pytest.raises(typer.Exit) as refused:
        _workspace_sync(org_b, padded)

    assert refused.value.exit_code == 1
    graph.updater.assert_not_called()


def test_a_padded_workspace_name_this_repo_owns_syncs_under_the_stripped_name(
    graph: MagicMock, tmp_path: Path
) -> None:
    # Negative: the name checked is the name handed to the updater.
    org_a, _ = _repos(tmp_path)
    _owned_by(graph, org_a)

    _workspace_sync(org_a, " api2 ")

    assert graph.updater.call_args.kwargs["project_name"] == "api2"


class _SyncedGraph(_StatefulIngestor):
    """The in-memory graph behind a CLI sync's connections."""

    def ensure_constraints(self) -> None:
        return None


def _owner_check(repo: Path, store: _SyncedGraph, *, assume_yes: bool = False) -> bool:
    # One sync's ownership check on its own; True when the sync may go on.
    with patch("codebase_rag.cli.connect_memgraph", return_value=nullcontext(store)):
        try:
            _exit_if_project_owned_elsewhere(
                10, "api2", repo, clean=False, assume_yes=assume_yes
            )
        except typer.Exit:
            return False
    return True


def _write_phase(repo: Path, store: _SyncedGraph) -> None:
    # The rest of a sync whose ownership check has already run.
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=nullcontext(store)),
        patch("codebase_rag.cli._exit_if_project_owned_elsewhere"),
    ):
        _run_graph_sync(
            repo=repo,
            project_name="api2",
            project_named=True,
            batch_size=10,
            exclude=None,
            interactive_setup=False,
        )


def _root(store: _SyncedGraph) -> object:
    return store.nodes[(cs.NodeLabel.PROJECT, "api2")][cs.KEY_ROOT_PATH]


def test_two_syncs_racing_for_an_unowned_name_refuse_the_loser(
    tmp_path: Path,
) -> None:
    # Greptile review of PR 2499: two syncs of a name nobody held both read
    # "no owner" before either wrote, so both went ahead and the later one
    # replaced the first one's graph without --yes. Both checks run first
    # here, as they did in that run.
    org_a, org_b = _repos(tmp_path)
    store = _SyncedGraph()

    allowed = {repo: _owner_check(repo, store) for repo in (org_a, org_b)}
    for repo in (org_a, org_b):
        if allowed[repo]:
            _write_phase(repo, store)

    assert allowed == {org_a: True, org_b: False}
    assert _root(store) == str(org_a.resolve())
    assert _functions(store) == {"api2.billing.charge_card"}


def test_the_repo_that_claimed_a_name_syncs_it_again(tmp_path: Path) -> None:
    # Negative: a claim is this repository's own on every later sync.
    org_a, _ = _repos(tmp_path)
    store = _SyncedGraph()
    assert _owner_check(org_a, store)
    _write_phase(org_a, store)

    assert _owner_check(org_a, store)


def test_yes_still_takes_a_claimed_name(tmp_path: Path) -> None:
    # Negative: --yes replaces the project whoever claimed it.
    org_a, org_b = _repos(tmp_path)
    store = _SyncedGraph()
    assert _owner_check(org_a, store)
    _write_phase(org_a, store)

    assert _owner_check(org_b, store, assume_yes=True)
    _write_phase(org_b, store)

    assert _root(store) == str(org_b.resolve())
    assert _functions(store) == {"api2.users.list_users"}


def _chat(repo: Path, *extra: str) -> list[str]:
    return [
        "start",
        "--repo-path",
        str(repo),
        "--project-name",
        "api2",
        "--no-start-stack",
        *extra,
        "--ask-agent",
        "hi",
    ]


def test_the_pre_chat_sync_refuses_too(graph: MagicMock, tmp_path: Path) -> None:
    # The chat's own sync runs before the question; it must not take over
    # another repository's project either.
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)

    result = runner.invoke(app, _chat(org_b))

    assert result.exit_code == 1, result.output
    graph.updater.assert_not_called()


def test_the_pre_chat_sync_honours_yes(graph: MagicMock, tmp_path: Path) -> None:
    org_a, org_b = _repos(tmp_path)
    _owned_by(graph, org_a)

    result = runner.invoke(app, _chat(org_b, "--yes"))

    assert result.exit_code == 0, result.output
    graph.updater.return_value.run.assert_called_once()


def test_the_help_states_the_real_default() -> None:
    assert "__" in HELP_PROJECT_NAME
    assert "directory name." not in HELP_PROJECT_NAME
