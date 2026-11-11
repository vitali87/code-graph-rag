"""`endpoint_callers` resolves its target the way a caller means it (#3186).

The target was compared to the stored identity as an exact string, and an
empty answer was returned as it was. The documented example form, `GET
/users/{id}`, named no Express (`:id`) or Flask (`<id>`) route and answered
`[]`, as did a typo'd handler, a concrete URL and a function exposing
nothing: an agent was told nobody calls a route three services call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import PropertyParams, ResultRow

P = "users"
HANDLER = f"{P}.server.getUser"
LISTER = f"{P}.server.listUsers"
PINGER = f"{P}.server.ping"
RPC_HANDLER = f"{P}.rpc.UserService.GetUser"
FLASK_HANDLER = f"{P}.flask_app.item_tag"
# One route per framework spelling, as the extractors store them.
EXPOSED: list[tuple[str, str]] = [
    ("GET /users/:id", HANDLER),
    ("GET /users", LISTER),
    ("GET /flask/<int:item_id>/tags/<tag>", FLASK_HANDLER),
    ("GET /ping", PINGER),
    ("users.UserService/GetUser", RPC_HANDLER),
]
CALLERS = ["web.client.one", "web.client.two", "web.client.three"]
CALLERS_OF = {HANDLER: CALLERS, FLASK_HANDLER: ["web.client.tagged"]}


class FakeGraph:
    """The exposed endpoints and their callers, with the Cypher's own match:
    `$qn` is the handler or the stored identity, compared as written."""

    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        params = params or {}
        if query == cq.CYPHER_GRAPH_EXPOSED_ENDPOINTS:
            assert params[cs.KEY_PROJECT_PREFIX] == f"{P}."
            return [{"endpoint": e, "handler": h} for e, h in EXPOSED]
        if query == cq.CYPHER_GRAPH_ENDPOINT_CALLERS:
            qn = params[cs.KEY_QN]
            return [
                {
                    "label": "Function",
                    "qualified_name": caller,
                    "path": "client.js",
                    "url": f"http://users/users/{i}",
                    "direction": "READS_FROM",
                    "endpoint": endpoint,
                    "handler": handler,
                }
                for endpoint, handler in EXPOSED
                if qn in (endpoint, handler)
                for i, caller in enumerate(CALLERS_OF.get(handler, []), 1)
            ]
        if query == cq.CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS:
            qn = params[cs.KEY_QN]
            return [
                {
                    "label": "Method",
                    "qualified_name": "web.rpc.Client.get_user",
                    "path": "rpc.py",
                    "url": endpoint,
                    "direction": "READS_FROM",
                    "endpoint": endpoint,
                    "handler": handler,
                }
                for endpoint, handler in EXPOSED
                if qn in (endpoint, handler) and handler == RPC_HANDLER
            ]
        return []


def _callers(target: str) -> list[str]:
    return [
        r["qualified_name"]
        for r in graph_query.endpoint_callers(FakeGraph(), P, target)
    ]


@pytest.mark.parametrize(
    "target",
    [
        "GET /users/:id",
        HANDLER,
        "GET /users/{id}",
        "GET /users/<id>",
        "get /users/{id}/",
    ],
    ids=["as-stored", "handler", "braces", "angle", "lowercase-method-trailing-slash"],
)
def test_any_spelling_of_the_route_finds_its_callers(target: str) -> None:
    assert _callers(target) == sorted(CALLERS)


def test_a_flask_converter_route_matches_any_parameter_syntax() -> None:
    # `<int:item_id>` and `<tag>` are each one parameter segment.
    assert _callers("GET /flask/{item_id}/tags/:tag") == ["web.client.tagged"]


def test_a_concrete_request_finds_the_route_serving_it() -> None:
    assert _callers("GET /users/42") == sorted(CALLERS)


@pytest.mark.parametrize(
    ("target", "tail"),
    [
        (f"{P}.server.getUsr", f" Did you mean: {HANDLER},"),
        ("POST /users/{id}", " Did you mean: GET /users/:id,"),
        (f"{P}.server.helper", f" Did you mean: {P}.server."),
        ("GET /orders/42", " Did you mean: GET /users"),
        ("no.such.thing", cs.MCP_UNKNOWN_ENDPOINT_HINT),
    ],
    ids=["typo", "other-method", "exposes-nothing", "no-route-serves-it", "unknown"],
)
def test_a_target_naming_no_endpoint_is_refused(target: str, tail: str) -> None:
    # The closest identity or handler comes first; with none close, the
    # hint points at `endpoints`.
    graph = FakeGraph()
    with pytest.raises(graph_query.UnknownEndpointError) as refused:
        graph_query.endpoint_callers(graph, P, target)
    head = cs.MCP_UNKNOWN_ENDPOINT.format(target=target, project=P)
    assert str(refused.value).startswith(head + tail), str(refused.value)


def test_an_endpoint_nobody_calls_is_an_empty_list() -> None:
    # Negative: the route exists, so `[]` is the answer, not a refusal.
    assert graph_query.endpoint_callers(FakeGraph(), P, "GET /ping") == []
    assert graph_query.endpoint_callers(FakeGraph(), P, PINGER) == []


def test_an_rpc_identity_matches_only_as_written() -> None:
    # Negative: a non-HTTP identity has no path parameters to unify.
    assert _callers("users.UserService/GetUser") == ["web.rpc.Client.get_user"]
    graph = FakeGraph()
    with pytest.raises(graph_query.UnknownEndpointError):
        graph_query.endpoint_callers(graph, P, "users.userservice/getuser")


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=FakeGraph())
    ingestor.list_projects.return_value = [P]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        return MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )


async def test_the_mcp_tool_answers_the_documented_form(
    registry: MCPToolsRegistry,
) -> None:
    rows = await registry.endpoint_callers("GET /users/{id}", project=P)
    assert isinstance(rows, list)
    assert [r["qualified_name"] for r in rows] == sorted(CALLERS)


async def test_the_mcp_tool_refuses_a_typo_like_callers_does(
    registry: MCPToolsRegistry,
) -> None:
    result = await registry.endpoint_callers(f"{P}.server.getUsr", project=P)
    # The refusal itself, not a "graph query failed" wrapping it.
    assert isinstance(result, dict)
    assert set(result) == {cs.DICT_KEY_ERROR}
    target = f"{P}.server.getUsr"
    assert result[cs.DICT_KEY_ERROR].startswith(
        cs.MCP_UNKNOWN_ENDPOINT.format(target=target, project=P)
        + f" Did you mean: {HANDLER},"
    )


NESTED = f"{P}.admin"
NESTED_HANDLER = f"{NESTED}.server.getUser"


class NestedProjectGraph(FakeGraph):
    """`users` beside `users.admin`, whose route `users.` also selects."""

    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        params = params or {}
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: P}, {cs.KEY_NAME: NESTED}]
        if query == cq.CYPHER_GRAPH_EXPOSED_ENDPOINTS:
            return [
                *super().__call__(query, params),
                {"endpoint": "GET /users/<id>", "handler": NESTED_HANDLER},
            ]
        rows = super().__call__(query, params)
        if query == cq.CYPHER_GRAPH_ENDPOINT_CALLERS and params[cs.KEY_QN] in (
            "GET /users/<id>",
            NESTED_HANDLER,
        ):
            rows.append(
                {
                    "label": "Function",
                    "qualified_name": "web.admin.load",
                    "path": "admin.js",
                    "url": "/users/7",
                    "direction": "READS_FROM",
                    "endpoint": "GET /users/<id>",
                    "handler": NESTED_HANDLER,
                }
            )
        return rows


def test_a_nested_projects_route_is_not_the_projects_own() -> None:
    # Negative: `users.admin` is its own registered project; its `GET
    # /users/<id>` matches the concrete request too, but answers for it.
    rows = graph_query.endpoint_callers(NestedProjectGraph(), P, "GET /users/42")
    assert [r["qualified_name"] for r in rows] == sorted(CALLERS)
    assert {r["handler"] for r in rows} == {HANDLER}
    graph = NestedProjectGraph()
    with pytest.raises(graph_query.UnknownEndpointError):
        graph_query.endpoint_callers(graph, P, NESTED_HANDLER)


class RoutesGraph:
    """`routes` (identity, handler, caller) and the callers the linker joined
    to each route through a client-facing URL."""

    def __init__(self, routes: list[tuple[str, str, str]]) -> None:
        self.routes = routes

    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        params = params or {}
        if query == cq.CYPHER_GRAPH_EXPOSED_ENDPOINTS:
            return [{"endpoint": e, "handler": h} for e, h, _c in self.routes]
        if query == cq.CYPHER_GRAPH_ENDPOINT_CALLERS:
            return [
                {
                    "label": "Function",
                    "qualified_name": caller,
                    "path": "client.js",
                    "url": "/api/whatever",
                    "direction": "READS_FROM",
                    "endpoint": endpoint,
                    "handler": handler,
                }
                for endpoint, handler, caller in self.routes
                if params[cs.KEY_QN] in (endpoint, handler)
            ]
        return []


_PROXIED = [
    ("GET /cases", f"{P}.cases.list_cases", "web.client.cases"),
    ("POST /admin/api/v1/otp/verify", f"{P}.otp.verify", "web.client.otp"),
]


@pytest.mark.parametrize(
    ("target", "caller"),
    [
        ("GET /api/cases", "web.client.cases"),
        ("POST /api/v1/otp/verify", "web.client.otp"),
    ],
    ids=["proxy-lead", "router-mount"],
)
def test_a_client_facing_path_finds_the_route_the_linker_inferred(
    target: str, caller: str
) -> None:
    # The linker joins `/api/cases` to `GET /cases` (a proxy lead, #911) and
    # `/api/v1/otp/verify` to `POST /admin/api/v1/otp/verify` (a mount, #923).
    rows = graph_query.endpoint_callers(RoutesGraph(_PROXIED), P, target)
    assert [r["qualified_name"] for r in rows] == [caller]


@pytest.mark.parametrize(
    ("routes", "target"),
    [
        (_PROXIED, "POST /api/cases"),
        (
            [
                ("GET /cases", f"{P}.cases.list_cases", "web.client.a"),
                ("GET /{tenant}/cases", f"{P}.cases.tenant_cases", "web.client.b"),
            ],
            "GET /api/t1/cases",
        ),
    ],
    ids=["other-method", "tie"],
)
def test_an_inferred_route_keeps_the_linkers_bounds(
    routes: list[tuple[str, str, str]], target: str
) -> None:
    # Negative: the method still has to match, and two different routes
    # reached by inference are a tie the linker drops, not a guess.
    graph = RoutesGraph(routes)
    with pytest.raises(graph_query.UnknownEndpointError):
        graph_query.endpoint_callers(graph, P, target)
