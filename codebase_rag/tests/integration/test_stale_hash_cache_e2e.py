"""A hash cache must not survive the graph it describes.

The cache lives inside the repo, but the database is shared: cleaning the
database while indexing another repo (or MCP wipe_database, or pointing cgr
at a fresh instance) voids every other repo's cache without touching it.
An incremental sync that trusts the orphaned cache skips every file and
leaves the project silently empty.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    project = tmp_path / "cachedrepo"
    project.mkdir()
    (project / "main.py").write_text(
        """def hello():
    return "hi"
""",
        encoding="utf-8",
    )
    return project


def _index(ingestor: MemgraphIngestor, repo_path: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo_path, parsers=parsers, queries=queries
    ).run()


def _module_count(ingestor: MemgraphIngestor) -> int:
    rows = ingestor.fetch_all(
        "MATCH (m:Module) WHERE m.qualified_name STARTS WITH 'cachedrepo.' "
        "RETURN count(m) AS count"
    )
    return int(rows[0]["count"])


class TestStaleHashCache:
    def test_reindex_after_external_wipe_rebuilds_fully(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        _index(memgraph_ingestor, repo)
        assert _module_count(memgraph_ingestor) > 0
        assert (repo / cs.HASH_CACHE_FILENAME).is_file()

        # Anything outside this repo's own clean path can wipe the shared
        # database: --clean while indexing another repo, MCP wipe_database,
        # a fresh Memgraph container.
        memgraph_ingestor._execute_query("MATCH (n) DETACH DELETE n")

        _index(memgraph_ingestor, repo)
        assert _module_count(memgraph_ingestor) > 0

    def test_intact_graph_keeps_incremental_sync(
        self, memgraph_ingestor: MemgraphIngestor, repo: Path
    ) -> None:
        _index(memgraph_ingestor, repo)

        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=memgraph_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
        )
        updater.run()
        assert updater.skipped_because_in_sync
