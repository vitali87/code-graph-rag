"""Issue #3189: a scoped export keeps the links between the resources it holds.

A project export took only the relationships that start at a node the
project owns, and a Resource is never owned. So `RESOLVES_TO` (a client URL
to the endpoint it reaches) and resource-to-resource `FLOWS_TO` (an env var
into a URL) were dropped even with both ends in the file: exporting a client
with its server showed two services that never call each other.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import GraphData

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

# The issue's `envflow` project: a same-origin fetch of the project's own
# route, and an env var flowing into a request URL.
SERVER_JS = """const express = require("express");
const app = express();
function getUser(req, res) { res.json({ id: req.params.id }); }
app.get("/users/:id", getUser);
async function load() { return fetch("/users/1"); }
module.exports = { app, load };
"""
CLIENT_PY = """import os

import requests

PAYMENTS = os.environ.get("PAYMENTS_URL", "http://payments.internal:8080")


def pay(order):
    return requests.post(f"{PAYMENTS}/orders/{order}/pay")
"""
# Two services: a server and a client that calls it.
USERS_PY = """app = object()


@app.get("/users/{id}")
def get_user(id):
    return {"id": id}
"""
WEB_PY = """import requests


def one():
    return requests.get("http://users:8000/users/1")


def two():
    return requests.get("http://users:8000/users/2")
"""

type Link = tuple[str, str, str]


def _index(ingestor: MemgraphIngestor, root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    ingestor.flush_all()


def _resource_links(data: GraphData) -> set[Link]:
    # Every relationship of the file between two Resources, by name.
    names: dict[object, str] = {}
    for node in data["nodes"]:
        labels, props = node["labels"], node["properties"]
        assert isinstance(labels, list)
        assert isinstance(props, dict)
        if cs.NodeLabel.RESOURCE.value in labels:
            names[node["node_id"]] = str(props[cs.KEY_QUALIFIED_NAME])
    return {
        (str(rel["type"]), names[rel["from_id"]], names[rel["to_id"]])
        for rel in data["relationships"]
        if rel["from_id"] in names and rel["to_id"] in names
    }


def _graph_resource_links(ingestor: MemgraphIngestor) -> set[Link]:
    rows = ingestor.fetch_all(
        "MATCH (a:Resource)-[r]->(b:Resource) "
        "RETURN type(r) AS type, a.qualified_name AS a, b.qualified_name AS b"
    )
    return {(str(r["type"]), str(r["a"]), str(r["b"])) for r in rows}


def _ids(data: GraphData) -> set[object]:
    return {node["node_id"] for node in data["nodes"]}


def test_a_project_export_keeps_its_resolves_to_and_flows_to(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(
        memgraph_ingestor,
        tmp_path / "envflow",
        {"server.js": SERVER_JS, "client.py": CLIENT_PY},
    )
    links = _resource_links(memgraph_ingestor.export_graph_to_dict(["envflow"]))
    kinds = {kind for kind, _a, _b in links}
    assert {
        cs.RelationshipType.RESOLVES_TO.value,
        cs.RelationshipType.FLOWS_TO.value,
    } <= kinds, links
    # The file is the induced subgraph: every link the graph holds between
    # its resources (here all of them) is in it.
    assert links == _graph_resource_links(memgraph_ingestor)


def test_a_client_exported_with_its_server_keeps_the_links_between_them(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(memgraph_ingestor, tmp_path / "users", {"api.py": USERS_PY})
    _index(memgraph_ingestor, tmp_path / "web", {"client.py": WEB_PY})

    links = _resource_links(memgraph_ingestor.export_graph_to_dict(["users", "web"]))

    resolves = {
        (a, b) for kind, a, b in links if kind == cs.RelationshipType.RESOLVES_TO.value
    }
    assert len(resolves) == 2, links
    assert links == _graph_resource_links(memgraph_ingestor)


def test_a_client_exported_alone_still_holds_both_ends_of_every_link(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Negative: the server's endpoint is not in the client's file, so the
    # RESOLVES_TO into it stays out, and every relationship keeps both ends.
    _index(memgraph_ingestor, tmp_path / "users", {"api.py": USERS_PY})
    _index(memgraph_ingestor, tmp_path / "web", {"client.py": WEB_PY})

    scoped = memgraph_ingestor.export_graph_to_dict(["web"])

    assert not {
        link
        for link in _resource_links(scoped)
        if link[0] == cs.RelationshipType.RESOLVES_TO.value
    }
    ends = {rel["from_id"] for rel in scoped["relationships"]} | {
        rel["to_id"] for rel in scoped["relationships"]
    }
    assert ends <= _ids(scoped)
