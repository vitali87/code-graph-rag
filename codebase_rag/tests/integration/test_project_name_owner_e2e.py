# Issue #2411, against Memgraph: a repository whose project name another
# repository took over is rebuilt on its next sync, not reported in sync
# while the graph holds the other repository's code under its root.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_FUNCTIONS = (
    "MATCH (p:Project {name: 'api2'}) "
    "OPTIONAL MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'api2.' "
    "RETURN p.root_path AS root, collect(f.qualified_name) AS functions"
)


def _sync(ingestor: MemgraphIngestor, repo: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="api2",
        project_named=True,
    )
    updater.run(force=False)
    ingestor.flush_all()
    return updater


def test_the_repo_that_lost_its_project_is_rebuilt(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    org_a = tmp_path / "orgA" / "api"
    org_b = tmp_path / "orgB" / "api"
    org_a.mkdir(parents=True)
    org_b.mkdir(parents=True)
    (org_a / "billing.py").write_text("def charge_card():\n    return 1\n")
    (org_b / "users.py").write_text("def list_users():\n    return []\n")
    _sync(memgraph_ingestor, org_a)
    _sync(memgraph_ingestor, org_b)

    again = _sync(memgraph_ingestor, org_a)

    assert again.skipped_because_in_sync is False
    [row] = memgraph_ingestor.fetch_all(_FUNCTIONS)
    assert row["root"] == str(org_a.resolve())
    assert row["functions"] == ["api2.billing.charge_card"]
