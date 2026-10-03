# Real-Memgraph check of the `callers` / `callees` walk (issue #2597). The
# unit tests drive a fake that answers the walk's reads in Python, so only a
# real index proves two things: the reads run on the Memgraph the stack ships
# and start from the label + qualified_name indexes (the label-less lookup
# was a ScanAll over every node of the shared graph, once per frontier node),
# and the CALLS edges the parsers write -- module-level, function and method
# callers -- carry what the batched walk reads.
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

PROJECT = "cw"

FILES = {
    "app/__init__.py": "",
    "app/base.py": "def reverse(name):\n    return name\n",
    # A module-level call: its caller is the Module node.
    "app/urls.py": 'from app.base import reverse\n\nROOT = reverse("root")\n',
    "app/views.py": "from app.base import reverse\n\n\n"
    'def index():\n    return reverse("index")\n\n\n'
    'class Detail:\n    def get(self):\n        return reverse("detail") + index()\n',
    "tests/__init__.py": "",
    "tests/test_views.py": "from app.views import index\n\n\n"
    "def test_index():\n    assert index()\n",
}

REVERSE = f"{PROJECT}.app.base.reverse"
URLS = f"{PROJECT}.app.urls"
INDEX = f"{PROJECT}.app.views.index"
DETAIL_GET = f"{PROJECT}.app.views.Detail.get"
TEST_INDEX = f"{PROJECT}.tests.test_views.test_index"

# A plain `ScanAll (n)` row: every node of the shared graph is visited.
_FULL_SCAN = re.compile(r"\*\s+ScanAll\s+\(")


@pytest.fixture
def indexed(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    repo = tmp_path / PROJECT
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    # The indexes `cgr start` creates before it parses anything.
    memgraph_ingestor.ensure_constraints()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run()
    return memgraph_ingestor


def _hops(rows: list[graph_query.CallSiteRow]) -> list[tuple[int, str, str, str]]:
    return [(r["depth"], r["through"], r["qualified_name"], r["label"]) for r in rows]


def test_callers_depth_two_walks_every_kind_of_caller(
    indexed: MemgraphIngestor,
) -> None:
    rows = graph_query.callers(indexed.fetch_all, PROJECT, REVERSE, depth=2)
    assert _hops(rows) == [
        (1, REVERSE, URLS, "Module"),
        (1, REVERSE, DETAIL_GET, "Method"),
        (1, REVERSE, INDEX, "Function"),
        (2, INDEX, DETAIL_GET, "Method"),
        (2, INDEX, TEST_INDEX, "Function"),
    ]
    # Each row is still the exact site: `path:line` is the call.
    by_qn = {(r["through"], r["qualified_name"]): r for r in rows}
    assert (by_qn[(REVERSE, URLS)]["path"], by_qn[(REVERSE, URLS)]["line"]) == (
        "app/urls.py",
        3,
    )
    assert by_qn[(INDEX, TEST_INDEX)]["line"] == 5


def test_callees_depth_two_walks_from_a_method(indexed: MemgraphIngestor) -> None:
    rows = graph_query.callees(indexed.fetch_all, PROJECT, DETAIL_GET, depth=2)
    assert _hops(rows) == [
        (1, DETAIL_GET, REVERSE, "Function"),
        (1, DETAIL_GET, INDEX, "Function"),
        (2, INDEX, REVERSE, "Function"),
    ]


@pytest.mark.parametrize(
    "query",
    [cq.CYPHER_GRAPH_CALLERS, cq.CYPHER_GRAPH_CALLEES],
    ids=["callers", "callees"],
)
def test_the_walk_starts_from_the_label_indexes(
    indexed: MemgraphIngestor, query: str
) -> None:
    plan = [
        str(next(iter(row.values()), ""))
        for row in indexed.fetch_all(
            cs.CYPHER_EXPLAIN_PREFIX + query,
            {
                cs.KEY_PROJECT_PREFIX: f"{PROJECT}.",
                cs.KEY_QNS: [INDEX, URLS],
                cs.KEY_QN: INDEX,
            },
        )
    ]
    assert any("ScanAllByLabelProperties" in row for row in plan), plan
    assert not [row for row in plan if _FULL_SCAN.search(row)], plan
