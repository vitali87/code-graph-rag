"""Prefix-scoped deletes when the project registry is unreadable.

Without the registry, `svc.` also selects `svc.v2`'s rows (issue #1985;
CodeRabbit on PR #2125). The module delete must still run -- a full rebuild
over an unreadable graph deletes before it re-parses -- so its query reads
the nested projects from the graph itself; the orphan prune, which decides
ownership in Python, leaves the rows it cannot place for the next run.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor


def _updater(repo: Path, ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="svc",
    )


def _registry_unreadable(ingestor: MagicMock) -> None:
    def fetch_all(query: str, params: object = None) -> list[dict[str, str]]:
        if query == cq.CYPHER_LIST_PROJECTS:
            raise ConnectionError("registry read failed")
        return [{"path": "api.py", "qualified_name": "svc.v2.api"}]

    ingestor.fetch_all.side_effect = fetch_all


def _written(ingestor: MagicMock) -> list[str]:
    return [call.args[0] for call in ingestor.execute_write.call_args_list]


def test_the_module_delete_still_runs_when_the_registry_is_unreadable(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _registry_unreadable(mock_ingestor)
    updater = _updater(temp_repo, mock_ingestor)

    updater._delete_module_entities("api.py")

    (params,) = [
        call.args[1]
        for call in mock_ingestor.execute_write.call_args_list
        if call.args[0] == cs.CYPHER_DELETE_MODULE
    ]
    assert params[cs.KEY_NESTED_PROJECTS] == []


def test_the_module_delete_spares_nested_projects_the_parameter_misses() -> None:
    # The eval double mirrors the query's own Project-node exclusion, so an
    # empty `$nested_projects` (registry unread) still leaves `svc.v2` alone.
    store = _StatefulIngestor()
    for name in ("svc", "svc.v2"):
        store.nodes[(cs.NodeLabel.PROJECT.value, name)] = {cs.KEY_NAME: name}
    for qn in ("svc.api", "svc.v2.api"):
        store.nodes[(cs.NodeLabel.MODULE.value, qn)] = {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_PATH: "api.py",
        }

    store.execute_write(
        cs.CYPHER_DELETE_MODULE,
        {
            cs.KEY_PATH: "api.py",
            cs.KEY_PROJECT_NAME: "svc",
            cs.KEY_PROJECT_PREFIX: "svc.",
            cs.KEY_NESTED_PROJECTS: [],
        },
    )

    modules = {uid for label, uid in store.nodes if label == cs.NodeLabel.MODULE.value}
    assert modules == {"svc.v2.api"}


def test_the_module_delete_passes_nested_projects_when_the_registry_reads(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    mock_ingestor.fetch_all.side_effect = lambda query, params=None: (
        [{cs.KEY_NAME: "svc"}, {cs.KEY_NAME: "svc.v2"}, {cs.KEY_NAME: "other"}]
        if query == cq.CYPHER_LIST_PROJECTS
        else []
    )
    updater = _updater(temp_repo, mock_ingestor)

    updater._delete_module_entities("api.py")

    (params,) = [
        call.args[1]
        for call in mock_ingestor.execute_write.call_args_list
        if call.args[0] == cs.CYPHER_DELETE_MODULE
    ]
    assert params[cs.KEY_NESTED_PROJECTS] == ["svc.v2"]


def test_the_prune_leaves_qualified_rows_when_the_registry_is_unreadable(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `svc.v2.api` at `api.py` is absent from `svc`'s tree; with ownership
    # degraded to the prefix rule the prune would read it as `svc`'s orphan.
    _registry_unreadable(mock_ingestor)
    updater = _updater(temp_repo, mock_ingestor)

    updater._prune_orphan_nodes()

    assert cs.CYPHER_DELETE_MODULE not in _written(mock_ingestor)


def test_a_prune_that_leaves_rows_owes_the_next_run_a_prune(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Skipped rows are only swept by a run that prunes, and an unchanged
    # tree takes the in-sync fast path, which never does; the marker makes
    # that fast path refuse (CodeRabbit, PR #2125).
    _registry_unreadable(mock_ingestor)
    updater = _updater(temp_repo, mock_ingestor)

    updater._prune_orphan_nodes()

    assert state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()


def test_a_failed_path_read_owes_the_next_run_a_prune(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    def fetch_all(query: str, params: object = None) -> list[dict[str, str]]:
        if query == cs.CYPHER_REPO_FILE_PATHS:
            raise ConnectionError("path read failed")
        return []

    mock_ingestor.fetch_all.side_effect = fetch_all
    updater = _updater(temp_repo, mock_ingestor)

    updater._prune_orphan_nodes()

    assert state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()


def test_a_complete_prune_settles_what_an_earlier_run_owed(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).touch()
    mock_ingestor.fetch_all.side_effect = lambda query, params=None: []
    updater = _updater(temp_repo, mock_ingestor)

    updater._prune_orphan_nodes()
    # Not yet: the prune's deletes are durable only after run()'s flush, and
    # clearing before it would lose the retry if that flush failed.
    assert state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()

    updater._settle_prune_marker()

    assert not state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()


def test_a_failed_flush_after_a_complete_prune_keeps_what_was_owed(
    temp_repo: Path,
) -> None:
    (temp_repo / "api.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def updater() -> GraphUpdater:
        return GraphUpdater(
            ingestor=store,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
            project_name="svc",
        )

    updater().run()
    state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).touch()
    owed = updater()
    real_prune = owed._prune_orphan_nodes

    def prune_then_fail_the_flush() -> None:
        real_prune()
        owed.ingestor.flush_all = MagicMock(side_effect=ConnectionError("down"))

    owed._prune_orphan_nodes = prune_then_fail_the_flush
    with pytest.raises(ConnectionError):
        owed.run()

    assert state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()


def test_an_owed_prune_refuses_the_in_sync_fast_path(temp_repo: Path) -> None:
    (temp_repo / "api.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()

    def run() -> GraphUpdater:
        updater = GraphUpdater(
            ingestor=store,
            repo_path=temp_repo,
            parsers=parsers,
            queries=queries,
            project_name="svc",
        )
        updater.run()
        return updater

    run()
    assert run().skipped_because_in_sync is True
    state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).touch()

    owed = run()

    assert owed.skipped_because_in_sync is False
    assert not state_file(temp_repo, cs.PRUNE_PENDING_FILENAME).exists()
    # Removing the marker touched the root directory after the owed run
    # recorded its mtime, so one more run re-walks (as after the EXPOSES
    # marker); the run after that is back on the fast path.
    run()
    assert run().skipped_because_in_sync is True
