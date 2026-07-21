"""A rebuild without a hash cache must not accumulate stale graph state.

The cache lives in the repo working tree, so a fresh clone (or a deleted
cache) has none while the shared database still holds the project. Every
file then counts as "new", the per-file delete-before-reingest is skipped,
and symbols renamed or removed since the previous index linger alongside
the fresh parse, stale CALLS/REFERENCES edges included.
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


def _index(ingestor: MemgraphIngestor, repo_path: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo_path, parsers=parsers, queries=queries
    ).run()


def _function_names(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all(
        "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'renamerepo.' "
        "RETURN f.qualified_name AS qn"
    )
    return {str(row["qn"]) for row in rows}


class TestCachelessRebuild:
    def test_rebuild_without_cache_drops_renamed_symbols(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        repo = tmp_path / "renamerepo"
        repo.mkdir()
        source = repo / "main.py"
        source.write_text("def old_name():\n    return 1\n", encoding="utf-8")
        _index(memgraph_ingestor, repo)
        assert any(
            qn.endswith(".old_name") for qn in _function_names(memgraph_ingestor)
        )

        # A fresh clone: same repo content evolves, but the cache files are
        # gone while the database still holds the previous parse.
        source.write_text("def new_name():\n    return 1\n", encoding="utf-8")
        (repo / cs.HASH_CACHE_FILENAME).unlink()
        (repo / cs.DIR_MTIMES_FILENAME).unlink()

        _index(memgraph_ingestor, repo)
        names = _function_names(memgraph_ingestor)
        assert any(qn.endswith(".new_name") for qn in names), names
        assert not any(qn.endswith(".old_name") for qn in names), names
