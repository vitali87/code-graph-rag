# `definition` of a decorated definition against a real Memgraph (issue #2428):
# the indexer records the decorated span beside the node's own start, and the
# definition query reads it back, falling back to the old span on a node that
# has no such property (a graph indexed before it existed).
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

SOURCE = """\
import functools


def retry(times):
    def deco(fn):
        return fn

    return deco


@retry(times=3)
@functools.lru_cache(maxsize=None)
def fetch(url):
    return url


def plain():
    return 2
"""
DECORATORS = ["@retry(times=3)", "@functools.lru_cache(maxsize=None)"]


def _index(ingestor: MemgraphIngestor, repo: Path) -> str:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor, repo_path=repo, parsers=parsers, queries=queries
    )
    updater.run()
    return updater.project_name


def test_definition_reads_the_decorated_span_back_from_memgraph(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "decor"
    repo.mkdir()
    # LF pinned: `source` is the file's text as written (see the unit tests).
    (repo / "svc.py").write_text(SOURCE, encoding="utf-8", newline="\n")
    project = _index(memgraph_ingestor, repo)
    fetch_qn = f"{project}.svc.fetch"

    row = graph_query.definition(memgraph_ingestor.fetch_all, project, fetch_qn, repo)
    assert (row["start_line"], row["name_line"], row["end_line"]) == (11, 13, 14)
    assert row["source"] == "\n".join(
        [*DECORATORS, "def fetch(url):", "    return url"]
    )
    assert row["decorators"] == DECORATORS

    stored = memgraph_ingestor.fetch_all(
        "MATCH (n:Function) WHERE n.qualified_name IN $qns "
        "RETURN n.name AS name, n.start_line AS start, "
        "n.decorated_start_line AS decorated",
        {"qns": [fetch_qn, f"{project}.svc.plain"]},
    )
    assert sorted((r["name"], r["start"], r["decorated"]) for r in stored) == [
        ("fetch", 13, 11),
        ("plain", 17, None),
    ]

    # A graph indexed before the property existed.
    memgraph_ingestor.execute_write(
        f"MATCH (n:Function {{qualified_name: $qn}}) "
        f"REMOVE n.{cs.KEY_DECORATED_START_LINE}",
        {"qn": fetch_qn},
    )
    old = graph_query.definition(memgraph_ingestor.fetch_all, project, fetch_qn, repo)
    assert (old["start_line"], old["name_line"], old["end_line"]) == (13, 13, 14)
    assert old["source"] == "def fetch(url):\n    return url"
    assert old["decorators"] == DECORATORS
