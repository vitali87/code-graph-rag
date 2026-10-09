"""A rootful relative URL is a resource of the project that requests it (#3190).

`fetch("/carts/7")` is same-origin: it reaches the issuing project's own
backend. Its NETWORK node was keyed by the URL alone, so every project in
the shared graph requesting `/carts/7` shared it, the "projects that issued
it" became all of them, and each client was linked to every project's
handler. An absolute URL names one shared service and stays shared.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

PROJECT = "alpha"
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])

_SERVER_JS = """\
const express = require("express");
const app = express();
function getCart(req, res) { res.json({ id: req.params.id }); }
app.get("/carts/:id", getCart);
async function load() { return fetch("/carts/7"); }
async function remote() { return fetch("http://carts.internal/carts/7"); }
async function cdn() { return fetch("//cdn.example.com/carts/7"); }
module.exports = { app, load, remote, cdn };
"""
_CLIENT_PY = """\
import os

import requests


def submit():
    return requests.post("/orders", data=os.environ["ORDER_TOKEN"])
"""

_Edges = set[tuple[str, str, str]]


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> tuple[_Edges, dict[str, dict]]:
    root = tmp_path_factory.mktemp("rootful") / PROJECT
    root.mkdir()
    (root / "server.js").write_text(_SERVER_JS, encoding="utf-8")
    (root / "client.py").write_text(_CLIENT_PY, encoding="utf-8")
    mock = MagicMock()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=mock,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        capture=_CAPTURE_IO,
    ).run()
    edges = {
        (str(c.args[0][2]), str(c.args[1]), str(c.args[2][2]))
        for c in mock.ensure_relationship_batch.call_args_list
    }
    resources = {
        str(c.args[1][cs.KEY_QUALIFIED_NAME]): dict(c.args[1])
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.RESOURCE.value
    }
    return edges, resources


def _target(edges: _Edges, caller: str, rel: cs.RelationshipType) -> set[str]:
    return {to for frm, kind, to in edges if frm == caller and kind == rel.value}


def test_a_rootful_url_is_a_resource_of_its_project(
    graph: tuple[_Edges, dict[str, dict]],
) -> None:
    edges, resources = graph
    qn = f"resource::NETWORK::{PROJECT}::/carts/7"
    assert _target(edges, f"{PROJECT}.server.load", cs.RelationshipType.READS_FROM) == {
        qn
    }
    # The name stays the URL, for display and for matching it to a route.
    assert resources[qn][cs.KEY_NAME] == "/carts/7"


@pytest.mark.parametrize(
    ("caller", "url"),
    [
        ("remote", "http://carts.internal/carts/7"),
        ("cdn", "//cdn.example.com/carts/7"),
    ],
    ids=["absolute", "protocol-relative"],
)
def test_a_url_naming_its_host_stays_shared(
    graph: tuple[_Edges, dict[str, dict]], caller: str, url: str
) -> None:
    # Negative: a host names one service every project reaches alike.
    edges, _resources = graph
    assert _target(
        edges, f"{PROJECT}.server.{caller}", cs.RelationshipType.READS_FROM
    ) == {f"resource::NETWORK::{url}"}


def test_a_flow_into_a_rootful_url_reaches_the_same_node(
    graph: tuple[_Edges, dict[str, dict]],
) -> None:
    # The env var flows into the very node the request writes to, not into
    # an unscoped twin of it.
    edges, _resources = graph
    url = f"resource::NETWORK::{PROJECT}::/orders"
    assert _target(
        edges, f"{PROJECT}.client.submit", cs.RelationshipType.WRITES_TO
    ) == {url}
    assert ("resource::ENV::ORDER_TOKEN", cs.RelationshipType.FLOWS_TO.value, url) in (
        edges
    )
