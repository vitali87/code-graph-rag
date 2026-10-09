"""`cgr doctor` accepts the graph cgr writes for an HTTP service.

Endpoint linking scopes every ENDPOINT resource by its project and stores
that on the node (`project`, read back to scope endpoint lookups), but the
`Resource` schema did not list it. So the structural audit failed on any
graph indexed with `--capture io` that held one route, and `cgr doctor`
exited 1 on a graph cgr itself had just written (issue #2730).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_audit as ga
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

_APP = """\
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health() -> dict:
    return {"ok": True}
"""


def _records(mock: MagicMock) -> tuple[list[GraphNodeRecord], list[GraphRelRecord]]:
    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock.ensure_relationship_batch.call_args_list
    ]
    return nodes, rels


def test_a_one_route_service_passes_the_audit(temp_repo: Path) -> None:
    (temp_repo / "app.py").write_text(_APP, encoding="utf-8")
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=temp_repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture(["io"]),
    ).run(force=True)
    nodes, rels = _records(mock)
    endpoints = [
        n
        for n in nodes
        if n.label == cs.NodeLabel.RESOURCE
        and str(n.properties.get(cs.KEY_QUALIFIED_NAME, "")).startswith(
            "resource::ENDPOINT::"
        )
    ]
    # The fixture must exercise the property the schema missed.
    assert endpoints, nodes
    assert all("project" in n.properties for n in endpoints), nodes
    assert ga.collect_violations(nodes, rels) == []


def test_the_live_audit_accepts_a_resource_project() -> None:
    rows = {cq.CYPHER_AUDIT_LABEL_PROPS: [{"label": "Resource", "key": "project"}]}
    assert ga.collect_live_violations(lambda query: rows.get(query, [])) == []


def test_other_undocumented_resource_properties_are_still_flagged() -> None:
    # Negatives: declaring `project` documents that one key, on Resource only.
    rows = {
        cq.CYPHER_AUDIT_LABEL_PROPS: [
            {"label": "Resource", "key": "flavour"},
            {"label": "Module", "key": "project"},
        ]
    }
    violations = ga.collect_live_violations(lambda query: rows.get(query, []))
    assert sorted(v.detail for v in violations) == sorted(
        [
            "Resource nodes carry undocumented property 'flavour'",
            "Module nodes carry undocumented property 'project'",
        ]
    ), violations
