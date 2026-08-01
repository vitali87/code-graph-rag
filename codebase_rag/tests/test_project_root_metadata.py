"""Project nodes must record the indexed repository root (issue #425).

Cross-project retrieval reads files via stored absolute paths; knowing each
project's indexed root lets tools bound those reads to known repositories
and lets users see which checkout a graph came from.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.cypher_queries import CYPHER_LIST_PROJECTS
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor


def test_project_node_records_root_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
    )

    updater.run()

    project_calls = [
        props
        for label, props in (
            c.args for c in mock_ingestor.ensure_node_batch.call_args_list
        )
        if label == cs.NODE_PROJECT
    ]
    assert project_calls, "no Project node was ingested"
    assert project_calls[0][cs.KEY_ROOT_PATH] == str(temp_repo.resolve())


def test_list_projects_query_returns_root_path() -> None:
    assert "root_path" in CYPHER_LIST_PROJECTS


def test_list_project_roots_maps_names_to_roots() -> None:
    ingestor = MemgraphIngestor(host="localhost", port=7687)
    with patch.object(
        MemgraphIngestor,
        "fetch_all",
        return_value=[
            {"name": "backend__12345678", "root_path": "/repos/backend"},
            {"name": "legacy__87654321", "root_path": None},
        ],
    ):
        roots = ingestor.list_project_roots()

    assert roots == {
        "backend__12345678": "/repos/backend",
        "legacy__87654321": None,
    }


def test_list_projects_still_returns_names_only() -> None:
    ingestor = MemgraphIngestor(host="localhost", port=7687)
    with patch.object(
        MemgraphIngestor,
        "fetch_all",
        return_value=[
            {"name": "backend__12345678", "root_path": "/repos/backend"},
        ],
    ):
        assert ingestor.list_projects() == ["backend__12345678"]
