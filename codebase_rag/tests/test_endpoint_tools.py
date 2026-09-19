"""Cross-service edges as questions (issue #1603).

`EXPOSES` and `RESOLVES_TO` were written and never read: no tool could ask
"who calls this endpoint" or "what does this service depend on", and
`dead-code` rooted every route handler by its decorator, so an endpoint
nobody calls was never reported. Three deterministic tools and one
dead-code switch consume the edges. Every read is project-scoped by the
HANDLER; the callers are counted across the whole graph by design.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.dead_code import (
    collect_dead_code,
    dead_code_from_graph,
    default_dead_code_config,
)
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools import tool_descriptions as td
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "users"
HANDLER = f"{P}.api.get_user"
ENDPOINT = "GET /users/{id}"
CALLER = "orders.client.fetch_user"


class FakeGraph:
    """Answers the four cross-service reads from explicit rows."""

    def __init__(self) -> None:
        self.rows: dict[str, list[ResultRow]] = {}
        self.calls: list[tuple[str, PropertyDict | None]] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        self.calls.append((query, params))
        if query in self.rows:
            return list(self.rows[query])
        raise AssertionError(f"unexpected query: {query[:60]}")


def test_endpoints_carry_their_handler_and_caller_count() -> None:
    graph = FakeGraph()
    graph.rows[cq.CYPHER_GRAPH_ENDPOINTS] = [
        {
            "endpoint": ENDPOINT,
            "kind": "ENDPOINT",
            "label": "Function",
            "handler": HANDLER,
            "path": "api.py",
            "callers": 2,
        },
        {
            "endpoint": "POST /users",
            "kind": "ENDPOINT",
            "label": "Function",
            "handler": f"{P}.api.create_user",
            "path": "api.py",
            "callers": 0,
        },
    ]
    rows = graph_query.endpoints(graph.fetch_all, P)
    assert [(r["endpoint"], r["handler"], r["callers"]) for r in rows] == [
        (ENDPOINT, HANDLER, 2),
        ("POST /users", f"{P}.api.create_user", 0),
    ]
    assert graph.calls[0][1] == {cs.KEY_PROJECT_PREFIX: f"{P}."}


def test_endpoint_callers_join_the_resolved_and_the_direct_rows() -> None:
    graph = FakeGraph()
    graph.rows[cq.CYPHER_GRAPH_ENDPOINT_CALLERS] = [
        {
            "label": "Function",
            "qualified_name": CALLER,
            "path": "client.py",
            "url": "http://users:8000/users/42",
            "direction": "READS_FROM",
            "endpoint": ENDPOINT,
            "handler": HANDLER,
        }
    ]
    graph.rows[cq.CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS] = [
        {
            "label": "Method",
            "qualified_name": "billing.rpc.Client.get_user",
            "path": "rpc.py",
            "url": ENDPOINT,
            "direction": "READS_FROM",
            "endpoint": ENDPOINT,
            "handler": HANDLER,
        }
    ]
    rows = graph_query.endpoint_callers(graph.fetch_all, P, ENDPOINT)
    assert [r["qualified_name"] for r in rows] == [
        "billing.rpc.Client.get_user",
        CALLER,
    ]
    assert rows[1]["url"] == "http://users:8000/users/42"
    # The target is passed as `$qn`, matched against handler OR identity.
    assert all(params[cs.KEY_QN] == ENDPOINT for _q, params in graph.calls)


def test_remote_dependencies_keep_unresolved_rows() -> None:
    graph = FakeGraph()
    graph.rows[cq.CYPHER_GRAPH_REMOTE_DEPENDENCIES] = [
        {
            "label": "Function",
            "qualified_name": CALLER,
            "path": "client.py",
            "url": "http://users:8000/users/42",
            "direction": "READS_FROM",
            "endpoint": ENDPOINT,
            "handler": HANDLER,
            "handler_project": P,
        },
        {
            "label": "Function",
            "qualified_name": "orders.client.ping",
            "path": "client.py",
            "url": "http://unknown/health",
            "direction": "READS_FROM",
            "endpoint": None,
            "handler": None,
            "handler_project": None,
        },
    ]
    rows = graph_query.remote_dependencies(graph.fetch_all, "orders")
    assert [(r["url"], r["handler"], r["handler_project"]) for r in rows] == [
        ("http://users:8000/users/42", HANDLER, P),
        ("http://unknown/health", None, None),
    ]


def test_the_reads_are_scoped_by_the_handler_and_count_with_a_count() -> None:
    for query in (
        cq.CYPHER_GRAPH_ENDPOINTS,
        cq.CYPHER_GRAPH_ENDPOINT_CALLERS,
        cq.CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS,
        cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS,
    ):
        assert "h.qualified_name STARTS WITH $project_prefix" in query
        assert "EXPOSES" in query
    for query in (cq.CYPHER_GRAPH_ENDPOINTS, cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS):
        assert "OPTIONAL MATCH" in query and "count(DISTINCT" in query
    assert "RESOLVES_TO" in cq.CYPHER_GRAPH_ENDPOINT_CALLERS
    assert "RESOLVES_TO" not in cq.CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS
    assert "c.qualified_name STARTS WITH $project_prefix" in (
        cq.CYPHER_GRAPH_REMOTE_DEPENDENCIES
    )
    assert "e.project AS handler_project" in cq.CYPHER_GRAPH_REMOTE_DEPENDENCIES


def test_the_three_tools_are_advertised_and_read_the_graph(tmp_path: Path) -> None:
    ingestor = MagicMock()
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )
    schemas = {s.name: s for s in registry.get_tool_schemas()}
    for name in (
        cs.MCPToolName.ENDPOINTS,
        cs.MCPToolName.ENDPOINT_CALLERS,
        cs.MCPToolName.REMOTE_DEPENDENCIES,
    ):
        assert schemas[name].description == td.MCP_TOOLS[name]
        assert cs.MCPParamName.PROJECT in schemas[name].inputSchema["properties"]
        assert registry._tools[name].returns_json is True
    assert schemas[cs.MCPToolName.ENDPOINT_CALLERS].inputSchema["required"] == [
        cs.MCPParamName.TARGET
    ]


@pytest.mark.anyio
async def test_endpoint_callers_goes_through_the_project_guard(tmp_path: Path) -> None:
    ingestor = MagicMock()
    ingestor.list_projects.return_value = [P]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        registry = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )
    refused = await registry.endpoint_callers(ENDPOINT, project="nope")
    assert "Unknown project" in refused["error"]


# --- dead endpoints ----------------------------------------------------------

_FUNCTION = cs.NodeLabel.FUNCTION.value


def _handler(qn: str) -> tuple[tuple[str, str], PropertyDict]:
    return (
        (_FUNCTION, qn),
        {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_NAME: qn.rsplit(".", 1)[-1],
            cs.KEY_PATH: "api.py",
            cs.KEY_DECORATORS: ["@app.get('/users/{id}')"],
        },
    )


def test_a_route_handler_nobody_calls_is_dead_only_with_endpoint_roots_off() -> None:
    nodes = dict([_handler(HANDLER), _handler(f"{P}.api.create_user")])
    links = {HANDLER: 0, f"{P}.api.create_user": 1}
    on = default_dead_code_config(include_tests=True, include_classes=False)
    assert dead_code_from_graph(nodes, [], f"{P}.", on, links) == set()
    off = on._replace(endpoint_roots=False)
    assert dead_code_from_graph(nodes, [], f"{P}.", off, links) == {HANDLER}
    # Without the links (the collector did not fetch them) nothing changes:
    # the switch cannot report a handler dead on missing evidence.
    assert dead_code_from_graph(nodes, [], f"{P}.", off, None) == set()


def test_a_decorated_non_handler_stays_a_root_with_endpoint_roots_off() -> None:
    (_key, props) = _handler(f"{P}.cli.main")
    props[cs.KEY_DECORATORS] = ["@app.command()"]
    nodes = {(_FUNCTION, f"{P}.cli.main"): props}
    off = default_dead_code_config(include_tests=True, include_classes=False)._replace(
        endpoint_roots=False
    )
    assert dead_code_from_graph(nodes, [], f"{P}.", off, {}) == set()


class _Ingestor:
    def __init__(self, links: list[ResultRow]) -> None:
        self.links = links
        self.queries: list[str] = []

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        self.queries.append(query)
        if query == cq.CYPHER_DEAD_CODE_NODES:
            return [
                {
                    "label": _FUNCTION,
                    "qualified_name": HANDLER,
                    "name": "get_user",
                    "path": "api.py",
                    "decorators": ["@app.get('/users/{id}')"],
                }
            ]
        if query == cq.CYPHER_DEAD_CODE_RELS:
            return []
        if query == cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS:
            return list(self.links)
        raise AssertionError(query[:60])


def test_the_collector_fetches_the_links_only_when_asked() -> None:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    store = _Ingestor([{"handler": HANDLER, "endpoint": ENDPOINT, "callers": 0}])
    assert collect_dead_code(store, P, config) == []  # type: ignore[arg-type]
    assert cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS not in store.queries
    rows = collect_dead_code(store, P, config._replace(endpoint_roots=False))  # type: ignore[arg-type]
    assert [r["qualified_name"] for r in rows] == [HANDLER]
    assert cq.CYPHER_DEAD_CODE_ENDPOINT_LINKS in store.queries
    # Two endpoints on one handler add up: one caller anywhere keeps it live.
    store = _Ingestor(
        [
            {"handler": HANDLER, "endpoint": ENDPOINT, "callers": 0},
            {"handler": HANDLER, "endpoint": "HEAD /users/{id}", "callers": 1},
        ]
    )
    assert collect_dead_code(store, P, config._replace(endpoint_roots=False)) == []  # type: ignore[arg-type]


def test_the_cli_config_carries_the_switch() -> None:
    from codebase_rag.cli import _dead_code_config

    assert _dead_code_config(True, False, [], []).endpoint_roots is True
    assert _dead_code_config(True, False, [], [], None, False).endpoint_roots is False


def test_every_resource_kind_is_documented() -> None:
    """The kinds and the two cross-service edges were undocumented (issue
    #1603); the enum and the doc table cannot drift again."""
    from codebase_rag.parsers.io_access.constants import ResourceKind

    doc = (
        Path(__file__).resolve().parents[2] / "docs/architecture/graph-schema.md"
    ).read_text()
    table = doc[
        doc.index("## Resource Kinds") : doc.index("## I/O and Data-Flow Edges")
    ]
    documented = {
        line.split("|")[1].strip()
        for line in table.splitlines()
        if line.startswith("| ")
    }
    documented.discard("Kind")
    assert documented == {kind.value for kind in ResourceKind}
    for rel in (
        cs.RelationshipType.EXPOSES.value,
        cs.RelationshipType.RESOLVES_TO.value,
    ):
        assert rel in table
