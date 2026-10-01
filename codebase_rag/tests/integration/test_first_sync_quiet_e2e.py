# Issue #2404, against Memgraph: a new user's first sync, on a brand-new graph,
# logs no WARNING. The prune of the seeded module map read an empty graph as
# "unreadable" and warned on exactly the run every new user makes first.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def test_a_first_sync_on_an_empty_graph_logs_no_warning(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "myrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "core.py").write_text("def a():\n    return 1\n")
    parsers, queries = load_parsers()
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        GraphUpdater(
            ingestor=memgraph_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            project_name="myrepo",
        ).run(force=False)
    finally:
        logger.remove(sink)

    assert warnings == []
