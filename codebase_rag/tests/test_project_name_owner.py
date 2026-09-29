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
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.cli_help import HELP_PROJECT_NAME
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
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


def _sync(repo: Path, store: _StatefulIngestor, *, force: bool = False) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="api2",
        project_named=True,
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
    def fetch_all(query: str, params: object = None) -> list[dict[str, str]]:
        if query == cq.CYPHER_PROJECT_ROOT_PATH and root is not None:
            return [{cs.KEY_ROOT_PATH: str(root)}]
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
    assert str(org_a) in output and "--yes" in output
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
    assert "__" in HELP_PROJECT_NAME and "directory name." not in HELP_PROJECT_NAME
