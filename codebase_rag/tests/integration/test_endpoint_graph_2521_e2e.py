"""The issue #2521 repo end to end: every client call links to its handler.

A Flask API, a `requests` client over a module-level `BASE`, an Express API
with inline handlers and a `fetch` client, indexed as one project. Before
the fix the Express routes were exposed by the module and three of the four
client URLs linked nowhere, and a project synced without the `io` group
answered the endpoint tools with `[]`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor
    from codebase_rag.types_defs import ResultValue

pytestmark = [pytest.mark.integration]

_FILES = {
    "api/app.py": (
        "from flask import Flask, jsonify\n"
        "app = Flask(__name__)\n\n\n"
        '@app.route("/users/<int:user_id>", methods=["GET"])\n'
        "def get_user(user_id):\n"
        '    return jsonify({"id": user_id})\n\n\n'
        '@app.post("/users")\n'
        "def create_user():\n"
        "    return jsonify({}), 201\n"
    ),
    "client/client.py": (
        "import requests\n\n"
        'BASE = "http://localhost:5000"\n\n\n'
        "def fetch_user(uid):\n"
        '    return requests.get(f"{BASE}/users/{uid}").json()\n\n\n'
        "def make_user():\n"
        '    return requests.post(BASE + "/users", json={})\n'
    ),
    "node/server.js": (
        "const express = require('express');\n"
        "const app = express();\n"
        "app.get('/orders/:id', function getOrder(req, res) { res.json({ id: req.params.id }); });\n"
        "app.delete('/orders/:id', (req, res) => res.sendStatus(204));\n"
        "app.listen(3000);\n"
    ),
    "node/web.js": (
        "export async function loadOrder(id) {\n"
        "  const r = await fetch(`/orders/${id}`);\n"
        "  return r.json();\n"
        "}\n"
        "export function removeOrder(id) {\n"
        '  return fetch("/orders/" + id, { method: "DELETE" });\n'
        "}\n"
    ),
}

_LINKS = (
    "MATCH (caller)-[:READS_FROM|WRITES_TO]->(url:Resource {kind: 'NETWORK'})"
    "-[:RESOLVES_TO]->(ep:Resource {kind: 'ENDPOINT'})<-[:EXPOSES]-(handler) "
    "RETURN caller.name AS caller, url.name AS url, ep.name AS endpoint, "
    "handler.qualified_name AS handler, labels(handler)[0] AS label"
)
# `/orders/{id}` is ONE resource that loadOrder reads and removeOrder writes,
# so it resolves to both the GET and the DELETE route and the join above
# pairs each caller with both. The rows a caller's own verb selects are the
# ones this issue is about.
_VERB_OF_CALLER = {
    "fetch_user": "GET",
    "make_user": "POST",
    "loadOrder": "GET",
    "removeOrder": "DELETE",
}


def test_every_issue_call_site_links_to_its_route_function(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "svc"
    for rel, content in _FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    memgraph_ingestor.flush_all()

    arrow_col = _FILES["node/server.js"].splitlines()[3].index("(req, res) =>")
    all_rows = memgraph_ingestor.fetch_all(_LINKS)
    assert all(r["label"] != cs.NodeLabel.MODULE.value for r in all_rows), all_rows
    rows = {
        (r["caller"], r["url"], r["endpoint"], r["handler"], r["label"])
        for r in all_rows
        if str(r["endpoint"]).startswith(f"{_VERB_OF_CALLER.get(str(r['caller']))} ")
    }
    function = cs.NodeLabel.FUNCTION.value
    assert rows == {
        (
            "fetch_user",
            "http://localhost:5000/users/{uid}",
            "GET /users/<int:user_id>",
            "svc.api.app.get_user",
            function,
        ),
        (
            "make_user",
            "http://localhost:5000/users",
            "POST /users",
            "svc.api.app.create_user",
            function,
        ),
        (
            "loadOrder",
            "/orders/{id}",
            "GET /orders/:id",
            "svc.node.server.getOrder",
            function,
        ),
        (
            "removeOrder",
            "/orders/{id}",
            "DELETE /orders/:id",
            f"svc.node.server.anonymous_3_{arrow_col}",
            function,
        ),
    }


_NESTED_SERVER = (
    "const express = require('express');\n"
    "const app = express();\n"
    "app.get('/carts/:id', (req, res) => res.json({}));\n"
    "module.exports = (router) => {\n"
    "  router.delete('/carts/:id', (req, res) => res.sendStatus(204));\n"
    "};\n"
)

_EXPOSERS = (
    "MATCH (h)-[:EXPOSES]->(ep:Resource {kind: 'ENDPOINT'}) "
    "RETURN ep.name AS endpoint, labels(h)[0] AS label, "
    "h.start_line AS line, h.start_col AS col"
)


def _int(value: ResultValue) -> int | None:
    # A Module handler has no start position.
    return value if isinstance(value, int) else None


def test_an_unchanged_module_keeps_its_inline_handlers_on_a_later_sync(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A later sync does not re-parse an unchanged server.js, yet it sweeps
    # and re-emits that module's routes: the arrow nested in the exported
    # function must be found again, not fall back to the module.
    repo = tmp_path / "shop"
    (repo / "node").mkdir(parents=True)
    (repo / "node" / "server.js").write_text(_NESTED_SERVER, encoding="utf-8")
    (repo / "node" / "web.js").write_text(_FILES["node/web.js"], encoding="utf-8")
    parsers, queries = load_parsers()

    def sync() -> set[tuple[str, str, int | None, int | None]]:
        GraphUpdater(
            ingestor=memgraph_ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            capture=resolve_capture([cs.CaptureGroup.IO.value]),
        ).run()
        memgraph_ingestor.flush_all()
        return {
            (str(r["endpoint"]), str(r["label"]), _int(r["line"]), _int(r["col"]))
            for r in memgraph_ingestor.fetch_all(_EXPOSERS)
        }

    lines = _NESTED_SERVER.splitlines()
    function = cs.NodeLabel.FUNCTION.value
    # The handler is the arrow's own node: 1-based line, 0-based column.
    expected = {
        ("GET /carts/:id", function, 3, lines[2].index("(req, res) =>")),
        ("DELETE /carts/:id", function, 5, lines[4].index("(req, res) =>")),
    }
    assert sync() == expected

    (repo / "node" / "web.js").write_text(
        _FILES["node/web.js"] + "export const unused = 1;\n", encoding="utf-8"
    )
    assert sync() == expected


def _sync(ingestor: MemgraphIngestor, repo: Path, tokens: list[str]) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(tokens),
    ).run()
    ingestor.flush_all()


def _registry(ingestor: MemgraphIngestor, repo: Path) -> MCPToolsRegistry:
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(repo), ingestor=ingestor, cypher_gen=MagicMock()
        )


@pytest.mark.anyio
async def test_a_default_sync_is_named_instead_of_answering_empty(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "users"
    (repo / "api").mkdir(parents=True)
    (repo / "api" / "app.py").write_text(_FILES["api/app.py"], encoding="utf-8")
    registry = _registry(memgraph_ingestor, repo)

    _sync(memgraph_ingestor, repo, [])
    refused = await registry.endpoints(project="users")
    assert isinstance(refused, dict), refused
    assert "--capture io" in refused[cs.DICT_KEY_ERROR]
    assert str(repo.resolve()) in refused[cs.DICT_KEY_ERROR]

    # The capture is part of the parser fingerprint: the io sync re-parses.
    _sync(memgraph_ingestor, repo, [cs.CaptureGroup.IO.value])
    answer = await registry.endpoints(project="users")
    assert {row["endpoint"] for row in answer} == {
        "GET /users/<int:user_id>",
        "POST /users",
    }


@pytest.mark.anyio
async def test_an_io_project_with_nothing_to_expose_still_answers_empty(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    repo = tmp_path / "lib"
    repo.mkdir()
    (repo / "util.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    _sync(memgraph_ingestor, repo, [cs.CaptureGroup.IO.value])
    registry = _registry(memgraph_ingestor, repo)
    assert await registry.endpoints(project="lib") == []
    assert await registry.remote_dependencies(project="lib") == []
