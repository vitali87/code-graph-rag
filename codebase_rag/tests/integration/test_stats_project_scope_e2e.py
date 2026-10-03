"""Issue #2391: a project's scoped counts are what it holds on its own.

Indexes two projects into one real Memgraph and checks that `cgr stats -n`
counts for one of them equal the totals the same project has in a database
where it is alone, and that the other project's nodes do not leak in.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import ResultRow

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

ALPHA = """def hello():
    return "alpha"


class Service:
    def run(self):
        return hello()
"""
BETA = """def greet():
    return "beta"


def other():
    return greet()


class Handler:
    def handle(self, value):
        return other()
"""


def _repo(root: Path, name: str, source: str) -> Path:
    repo = root / name
    repo.mkdir()
    (repo / f"{name}.py").write_text(source, encoding="utf-8")
    return repo


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    ingestor.flush_all()


def _counts(rows: list[ResultRow], key: str) -> dict[str, int]:
    counted: dict[str, int] = {}
    for row in rows:
        raw = row[key]
        label = str(raw[0]) if isinstance(raw, list) else str(raw)
        counted[label] = counted.get(label, 0) + int(row["count"])
    return counted


def test_scoped_counts_match_the_project_indexed_alone(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    alpha = _repo(tmp_path, "alpha", ALPHA)
    beta = _repo(tmp_path, "beta", BETA)
    _index(memgraph_ingestor, alpha)
    alone_nodes = _counts(
        memgraph_ingestor.fetch_all(cq.CYPHER_STATS_NODE_COUNTS), "labels"
    )
    alone_rels = _counts(
        memgraph_ingestor.fetch_all(cq.CYPHER_STATS_RELATIONSHIP_COUNTS), "type"
    )
    _index(memgraph_ingestor, beta)
    params = {cs.KEY_PROJECT_NAMES: ["alpha"]}

    scoped_nodes = _counts(
        memgraph_ingestor.fetch_all(cq.CYPHER_STATS_PROJECT_NODE_COUNTS, params),
        "labels",
    )
    scoped_rels = _counts(
        memgraph_ingestor.fetch_all(
            cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS, params
        ),
        "type",
    )

    assert scoped_nodes == alone_nodes
    assert scoped_rels == alone_rels


def test_the_breakdown_attributes_every_project(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, _repo(tmp_path, "alpha", ALPHA))
    _index(memgraph_ingestor, _repo(tmp_path, "beta", BETA))
    total_nodes = sum(
        _counts(
            memgraph_ingestor.fetch_all(cq.CYPHER_STATS_NODE_COUNTS), "labels"
        ).values()
    )

    rows = memgraph_ingestor.fetch_all(cq.CYPHER_STATS_PER_PROJECT)

    by_project = {str(r["project"]): int(r["nodes"]) for r in rows}
    assert set(by_project) == {"alpha", "beta"}
    assert by_project["beta"] > by_project["alpha"] > 0
    assert sum(by_project.values()) == total_nodes
