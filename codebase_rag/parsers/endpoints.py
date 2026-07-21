"""Route-decorator parsing and URL/template matching (issue #425 phase 3).

Handler decorators are stored verbatim on Function/Method nodes (e.g.
``@app.get("/users/{id}")``); this module turns them into
``(METHOD, /path/template)`` pairs and matches literal client URLs against
those templates so cross-project request edges can resolve to handlers.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse

from .. import constants as cs

if TYPE_CHECKING:
    from ..services import IngestorProtocol, QueryProtocol

# Anchoring on live sink/EXPOSES edges keeps resources whose caller or
# handler was deleted from relinking; delete-then-relink makes the pass
# idempotent so changed URLs or routes drop their stale RESOLVES_TO edges.
CYPHER_LIVE_NETWORK_RESOURCES = (
    "MATCH ()-[:READS_FROM|WRITES_TO]->(r:Resource {kind: 'NETWORK'}) "
    "RETURN DISTINCT r.qualified_name AS qualified_name, "
    "r.name AS name, r.kind AS kind"
)
CYPHER_LIVE_ENDPOINT_RESOURCES = (
    "MATCH ()-[:EXPOSES]->(r:Resource {kind: 'ENDPOINT'}) "
    "RETURN DISTINCT r.qualified_name AS qualified_name, "
    "r.name AS name, r.kind AS kind"
)
CYPHER_DELETE_RESOLVES_TO = "MATCH ()-[r:RESOLVES_TO]->() DELETE r"

_HTTP_METHOD_NAMES = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}
)
_ROUTE_NAME = "route"
_DEFAULT_ROUTE_METHOD = "GET"

# ponytail: decorators arrive as raw text, so this is a text parse, not an
# AST walk. The literal must open the argument list (a leading quote), which
# rejects computed paths like (prefix + "/users").
_DECORATOR_CALL_RE = re.compile(
    r"^@[\w.]*?(?P<name>\w+)\(\s*(?P<quote>['\"])(?P<path>/[^'\"]*)(?P=quote)",
)
_METHODS_KWARG_RE = re.compile(r"methods\s*=\s*[\[({](?P<items>[^\])}]*)[\])}]")
_METHOD_ITEM_RE = re.compile(r"['\"](\w+)['\"]")


def parse_route_decorator(decorator_text: str) -> list[tuple[str, str]]:
    """Return ``(METHOD, path_template)`` pairs for a route decorator.

    Non-route decorators, computed paths, and pathless calls yield ``[]``.
    """
    match = _DECORATOR_CALL_RE.match(decorator_text.strip())
    if match is None:
        return []
    name = match.group("name").lower()
    path = match.group("path")
    if name in _HTTP_METHOD_NAMES:
        return [(name.upper(), path)]
    if name != _ROUTE_NAME:
        return []
    methods_match = _METHODS_KWARG_RE.search(decorator_text)
    if methods_match is None:
        return [(_DEFAULT_ROUTE_METHOD, path)]
    methods = _METHOD_ITEM_RE.findall(methods_match.group("items"))
    return [(m.upper(), path) for m in methods]


# FastAPI-style {id} and Flask-style <user_id> / <int:user_id> variables.
_TEMPLATE_PARAM_RE = re.compile(r"^(\{[^/]+\}|<[^/]+>)$")


def url_matches_template(url: str, template: str) -> bool:
    """Match a literal request URL's path against a route template.

    Template segments like ``{id}`` match exactly one path segment; the
    comparison ignores scheme, host, port, query, and a trailing slash.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    url_segments = [s for s in parsed.path.split("/") if s]
    template_segments = [s for s in template.split("/") if s]
    if len(url_segments) != len(template_segments):
        return False
    return all(
        _TEMPLATE_PARAM_RE.match(expected) or expected == actual
        for actual, expected in zip(url_segments, template_segments, strict=True)
    )


def emit_endpoints(
    ingestor: IngestorProtocol,
    label: cs.NodeLabel,
    qualified_name: str,
    decorators: object,
) -> None:
    """Emit an ENDPOINT Resource plus an EXPOSES edge per route decorator."""
    # Imported lazily: parsers.utils imports this module, and the io_access
    # package init pulls extract, which imports parsers.utils back.
    from .io_access.constants import KEY_KIND, RESOURCE_QN_FORMAT, ResourceKind

    # A filtering sink that would drop the EXPOSES edge must not receive the
    # Resource node either, or selective capture leaves an orphaned endpoint.
    rel_gate = getattr(ingestor, "rel_enabled", None)
    if callable(rel_gate) and not rel_gate(cs.RelationshipType.EXPOSES):
        return
    if not isinstance(decorators, list):
        return
    for decorator in decorators:
        if not isinstance(decorator, str):
            continue
        for method, path in parse_route_decorator(decorator):
            identity = f"{method} {path}"
            resource_qn = RESOURCE_QN_FORMAT.format(
                kind=ResourceKind.ENDPOINT.value, identity=identity
            )
            ingestor.ensure_node_batch(
                cs.NodeLabel.RESOURCE,
                {
                    cs.KEY_QUALIFIED_NAME: resource_qn,
                    cs.KEY_NAME: identity,
                    KEY_KIND: ResourceKind.ENDPOINT.value,
                },
            )
            ingestor.ensure_relationship_batch(
                (label, cs.KEY_QUALIFIED_NAME, qualified_name),
                cs.RelationshipType.EXPOSES,
                (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, resource_qn),
            )


def link_endpoints(ingestor: QueryProtocol) -> int:
    """Resolve literal client request URLs to matching ENDPOINT resources.

    Endpoint identities are ``METHOD /path/template``; a NETWORK resource
    links to every endpoint whose template matches its URL path. Matching
    is method-agnostic: the request method lives on the sink edge, not in
    the Resource identity. Returns the number of edges emitted.
    """
    from .io_access.constants import DYNAMIC_TARGET, KEY_KIND, ResourceKind

    ingestor.execute_write(CYPHER_DELETE_RESOLVES_TO)
    networks: dict[str, str] = {}
    endpoints: dict[str, str] = {}
    for query in (CYPHER_LIVE_NETWORK_RESOURCES, CYPHER_LIVE_ENDPOINT_RESOURCES):
        for row in ingestor.fetch_all(query):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            name = row.get(cs.KEY_NAME)
            if not isinstance(qn, str) or not isinstance(name, str):
                continue
            kind = row.get(KEY_KIND)
            if kind == ResourceKind.NETWORK.value and name != DYNAMIC_TARGET:
                networks[qn] = name
            elif kind == ResourceKind.ENDPOINT.value:
                endpoints[qn] = name

    # The live ingestor both queries and writes; QueryProtocol alone types
    # the read side, so the single write goes through an ingestor view.
    writer = cast("IngestorProtocol", ingestor)
    created = 0
    for network_qn, url in networks.items():
        for endpoint_qn, identity in endpoints.items():
            _, _, template = identity.partition(" ")
            if template and url_matches_template(url, template):
                writer.ensure_relationship_batch(
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, network_qn),
                    cs.RelationshipType.RESOLVES_TO,
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, endpoint_qn),
                )
                created += 1
    return created
