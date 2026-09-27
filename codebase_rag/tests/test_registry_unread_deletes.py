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

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
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
