"""Inline-mod items stay in the containment tree whatever the batch size.

With a batch size of 1, every DEFINES edge from `mod tests` was written
before the `tests` Module node existed, so Memgraph dropped them: the items
were orphans, survived edits as ghosts and outlived `delete-project`
(issue #3000).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_rust_inline_mod_defines_order import write_crate

if TYPE_CHECKING:
    from pathlib import Path

    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_ORPHANS = (
    "MATCH (n) WHERE n.qualified_name STARTS WITH $prefix "
    "AND NOT n:Project AND NOT n:Module "
    "OPTIONAL MATCH (p)-[:DEFINES|DEFINES_METHOD]->(n) "
    "WITH n, count(p) AS parents WHERE parents = 0 "
    "RETURN n.qualified_name AS qn ORDER BY qn"
)
_LEFT = "MATCH (n) WHERE n.qualified_name STARTS WITH $prefix RETURN count(n) AS c"


def test_a_one_row_batch_keeps_every_inline_mod_item_contained(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    parsers, queries = load_parsers()
    if "rust" not in parsers:
        pytest.skip("rust parser not available")
    root = tmp_path / "rsghost"
    write_crate(root)
    ing = memgraph_ingestor
    ing.ensure_constraints()
    ing.batch_size = 1
    GraphUpdater(
        ingestor=ing,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="rsghost",
    ).run(force=True)
    ing.flush_all()

    assert ing.fetch_all(_ORPHANS, {"prefix": "rsghost."}) == []

    ing.delete_project("rsghost")
    assert ing.fetch_all(_LEFT, {"prefix": "rsghost."})[0]["c"] == 0
