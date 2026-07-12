from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

# (H) issue #711: two projects sharing one database. `collide` defines the bare
# (H) names `len` / `getenv`; `caller` makes unqualified calls on a tainted value.
# (H) Indexing `caller` after `collide` (no --clean) rehydrates the resolver trie
# (H) from the whole DB, so the bare-name fallback must NOT bind into `collide`.
COLLIDE_CODE = """\
def len(x):
    return 0


def getenv(k):
    return None
"""

CALLER_CODE = """\
import os


def leak():
    t = os.getenv("SECRET")
    n = len(t)
    return n
"""


def _index(ingestor: MemgraphIngestor, project: Path, *, io: bool) -> None:
    parsers, queries = load_parsers()
    capture = resolve_capture([cs.CaptureGroup.IO.value]) if io else None
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()


def _cross_project_edges(ingestor: MemgraphIngestor) -> list[dict[str, str]]:
    rows = ingestor.fetch_all(
        "MATCH (a)-[r]->(b) "
        "WHERE a.qualified_name STARTS WITH 'caller.' "
        "AND b.qualified_name STARTS WITH 'collide.' "
        "RETURN type(r) AS rel, a.qualified_name AS frm, b.qualified_name AS to"
    )
    return [{k: str(v) for k, v in row.items()} for row in rows]


def test_bare_name_calls_do_not_leak_into_another_project(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    collide = tmp_path / "collide"
    collide.mkdir()
    (collide / "mod.py").write_text(COLLIDE_CODE, encoding="utf-8")

    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "app.py").write_text(CALLER_CODE, encoding="utf-8")

    # (H) Index the collider first so its symbols are already in the DB when the
    # (H) caller's run rehydrates the registry from the graph.
    _index(memgraph_ingestor, collide, io=True)
    _index(memgraph_ingestor, caller, io=True)

    leaks = _cross_project_edges(memgraph_ingestor)
    assert leaks == [], f"cross-project edges leaked from caller into collide: {leaks}"
