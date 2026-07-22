"""Cross-project endpoint linking end to end (issue #425 phase 3).

A client service calling ``requests.get("http://user-service:8000/users/42")``
and a server service exposing ``@app.get("/users/{id}")`` are indexed as two
separate projects into one graph; the client's NETWORK resource must resolve
to the server's ENDPOINT resource, tracing caller to handler across projects.
"""

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

_CLIENT_SOURCE = """import requests


def create_order(user_id):
    user = requests.get("http://user-service:8000/users/42")
    return {"user": user}
"""

_SERVER_SOURCE = """app = object()


@app.get("/users/{id}")
def get_user(id):
    return {"id": id}
"""


def _index(ingestor: MemgraphIngestor, repo: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    ingestor.flush_all()


class TestEndpointLinkingE2E:
    def test_client_url_resolves_to_server_endpoint(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        client_repo = tmp_path / "order-service"
        client_repo.mkdir()
        (client_repo / "orders.py").write_text(_CLIENT_SOURCE, encoding="utf-8")
        server_repo = tmp_path / "user-service"
        server_repo.mkdir()
        (server_repo / "handlers.py").write_text(_SERVER_SOURCE, encoding="utf-8")

        _index(memgraph_ingestor, server_repo)
        _index(memgraph_ingestor, client_repo)

        rows = memgraph_ingestor.fetch_all(
            "MATCH (caller)-[:READS_FROM]->(url:Resource {kind: 'NETWORK'})"
            "-[:RESOLVES_TO]->(ep:Resource {kind: 'ENDPOINT'})"
            "<-[:EXPOSES]-(handler) "
            "RETURN caller.qualified_name AS caller, url.name AS url, "
            "ep.name AS endpoint, handler.qualified_name AS handler"
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["caller"] == "order-service.orders.create_order"
        assert row["url"] == "http://user-service:8000/users/42"
        assert row["endpoint"] == "GET /users/{id}"
        assert row["handler"] == "user-service.handlers.get_user"


_BROWSER_CLIENT_SOURCE = """export async function loadUser() {
  const res = await fetch("/users/42")
  return res.json()
}
"""


class TestRootfulRelativeLinkingE2E:
    def test_rootful_fetch_resolves_to_same_project_endpoint(
        self, memgraph_ingestor: MemgraphIngestor, tmp_path: Path
    ) -> None:
        # Issue #908: a browser frontend fetches a rootful relative path;
        # the decorated handler lives in the same project.
        repo = tmp_path / "web-app"
        repo.mkdir()
        (repo / "client.js").write_text(_BROWSER_CLIENT_SOURCE, encoding="utf-8")
        (repo / "handlers.py").write_text(_SERVER_SOURCE, encoding="utf-8")

        _index(memgraph_ingestor, repo)

        rows = memgraph_ingestor.fetch_all(
            "MATCH (caller)-[:READS_FROM]->(url:Resource {kind: 'NETWORK'})"
            "-[:RESOLVES_TO]->(ep:Resource {kind: 'ENDPOINT'})"
            "<-[:EXPOSES]-(handler) "
            "RETURN url.name AS url, ep.name AS endpoint, "
            "handler.qualified_name AS handler"
        )

        assert len(rows) == 1
        row = rows[0]
        assert row["url"] == "/users/42"
        assert row["endpoint"] == "GET /users/{id}"
        assert row["handler"] == "web-app.handlers.get_user"
