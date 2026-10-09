"""Issue #3190: a same-origin URL links only its own project's handler.

Two projects in one graph serve `GET /carts/:id` and request `/carts/7` from
their own client. The URL's NETWORK node was shared by both, so each client
was linked to both handlers; `endpoint_callers` named the other app as a
caller. A host-qualified URL still reaches the one service it names.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

SERVER_JS = """const express = require("express");
const app = express();
function getCart(req, res) { res.json({ id: req.params.id }); }
app.get("/carts/:id", getCart);
async function load() { return fetch("/carts/7"); }
module.exports = { app, load };
"""
CLIENT_JS = """async function order() { return fetch("http://alpha/carts/9"); }
module.exports = { order };
"""


def _index(ingestor: MemgraphIngestor, root: Path, name: str, source: str) -> None:
    root.mkdir()
    (root / "server.js").write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    ingestor.flush_all()


def _links(ingestor: MemgraphIngestor) -> set[tuple[str, str]]:
    rows = ingestor.fetch_all(
        "MATCH (c)-[:READS_FROM|WRITES_TO]->(:Resource {kind: 'NETWORK'})"
        "-[:RESOLVES_TO]->(:Resource)<-[:EXPOSES]-(h) "
        "RETURN DISTINCT c.qualified_name AS caller, h.qualified_name AS handler"
    )
    return {(str(r["caller"]), str(r["handler"])) for r in rows}


def test_each_client_reaches_only_its_own_handler(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, tmp_path / "alpha", "alpha", SERVER_JS)
    _index(memgraph_ingestor, tmp_path / "beta", "beta", SERVER_JS)

    assert _links(memgraph_ingestor) == {
        ("alpha.server.load", "alpha.server.getCart"),
        ("beta.server.load", "beta.server.getCart"),
    }
    rows = graph_query.endpoint_callers(
        memgraph_ingestor.fetch_all, "alpha", "alpha.server.getCart"
    )
    assert [r["qualified_name"] for r in rows] == ["alpha.server.load"]


def test_a_host_qualified_url_still_reaches_another_project(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Negative: `http://alpha/carts/9` names alpha's host, so a third
    # project's client links to alpha's route, and only to it.
    _index(memgraph_ingestor, tmp_path / "alpha", "alpha", SERVER_JS)
    _index(memgraph_ingestor, tmp_path / "beta", "beta", SERVER_JS)
    _index(memgraph_ingestor, tmp_path / "shop", "shop", CLIENT_JS)

    links = _links(memgraph_ingestor)
    assert ("shop.server.order", "alpha.server.getCart") in links
    assert ("shop.server.order", "beta.server.getCart") not in links
