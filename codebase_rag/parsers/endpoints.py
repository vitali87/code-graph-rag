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
from .endpoint_prefixes import UNKNOWN_LEAD_SEGMENT

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..services import IngestorProtocol, QueryProtocol

    # (label, handler qn, route decorators, module qn)
    PendingEndpoint = tuple[cs.NodeLabel, str, list[str], str | None]
    PrefixResolver = Callable[[str, str], "list[str] | None"]

# Anchoring on live sink/EXPOSES edges keeps resources whose caller or
# handler was deleted from relinking; delete-then-relink makes the pass
# idempotent so changed URLs or routes drop their stale RESOLVES_TO edges.
CYPHER_LIVE_NETWORK_RESOURCES = (
    "MATCH ()-[e:READS_FROM|WRITES_TO]->(r:Resource {kind: 'NETWORK'}) "
    "RETURN r.qualified_name AS qualified_name, "
    "r.name AS name, r.kind AS kind, "
    "collect(DISTINCT type(e)) AS directions"
)
CYPHER_LIVE_ENDPOINT_RESOURCES = (
    "MATCH ()-[:EXPOSES]->(r:Resource {kind: 'ENDPOINT'}) "
    "RETURN DISTINCT r.qualified_name AS qualified_name, "
    "r.name AS name, r.kind AS kind, r.project AS project"
)
CYPHER_DELETE_RESOLVES_TO = "MATCH ()-[r:RESOLVES_TO]->() DELETE r"

KEY_PROJECT = "project"
_PROJECT_HASH_SEPARATOR = "__"

_HTTP_METHOD_NAMES = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}
)
_ROUTE_NAME = "route"
_DEFAULT_ROUTE_METHOD = "GET"

# ponytail: decorators arrive as raw text, so this is a text parse, not an
# AST walk. The literal must open the argument list (a leading quote), which
# rejects computed paths like (prefix + "/users"). The receiver names the
# router variable the route hangs off, for mount-prefix resolution.
_DECORATOR_CALL_RE = re.compile(
    r"^@(?:(?P<receiver>\w+(?:\.\w+)*)\.)?(?P<name>\w+)"
    r"\(\s*(?P<quote>['\"])(?P<path>/[^'\"]*)(?P=quote)",
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


def decorator_receiver(decorator_text: str) -> str | None:
    """The variable a route decorator hangs off (``router`` in ``@router.get``)."""
    match = _DECORATOR_CALL_RE.match(decorator_text.strip())
    return match.group("receiver") if match is not None else None


# The client side has no parsed method, so the sink edge type is the only
# direction evidence: READS_FROM for get-style calls, WRITES_TO for
# post/put/patch/delete-style calls (issue #878).
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_KEY_DIRECTIONS = "directions"


def _direction_compatible(directions: frozenset[str], method: str) -> bool:
    """True when the URL's sink directions can reach a METHOD endpoint.

    An empty set means the graph predates the aggregated query (or a fake
    omitted it) and stays permissive.
    """
    if not directions:
        return True
    required = (
        cs.RelationshipType.WRITES_TO.value
        if method in _WRITE_METHODS
        else cs.RelationshipType.READS_FROM.value
    )
    return required in directions


# FastAPI-style {id} and Flask-style <user_id> / <int:user_id> variables.
_TEMPLATE_PARAM_RE = re.compile(r"^(\{[^/]+\}|<[^/]+>)$")


def url_matches_template(url: str, template: str) -> bool:
    """Match a literal request URL's path against a route template.

    Template segments like ``{id}`` match exactly one path segment; the
    comparison ignores scheme, host, port, query, and a trailing slash. A
    template opening with the unknown-lead marker (``/**/users/{id}``) has
    an unresolvable mount prefix and matches the URL path's tail instead.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return False
    url_segments = [s for s in parsed.path.split("/") if s]
    template_segments = [s for s in template.split("/") if s]
    if template_segments and template_segments[0] == UNKNOWN_LEAD_SEGMENT:
        template_segments = template_segments[1:]
        if len(url_segments) < len(template_segments):
            return False
        url_segments = url_segments[len(url_segments) - len(template_segments) :]
    elif len(url_segments) != len(template_segments):
        return False
    return all(
        _TEMPLATE_PARAM_RE.match(expected) or expected == actual
        for actual, expected in zip(url_segments, template_segments, strict=True)
    )


def _has_literal_segment(template: str) -> bool:
    """True when at least one path segment is not a template parameter.

    An all-parameter template (``/{id}/``, ``/<path:path>``, or the bare
    root ``/``) matches any URL of the right shape, so linking it to a
    literal URL carries no evidence and only fabricates traces. The
    unknown-lead marker is not evidence either.
    """
    return any(
        not _TEMPLATE_PARAM_RE.match(segment)
        for segment in template.split("/")
        if segment and segment != UNKNOWN_LEAD_SEGMENT
    )


def queue_endpoints(
    pending: list[PendingEndpoint],
    label: cs.NodeLabel,
    qualified_name: str,
    decorators: object,
    module_qn: str | None,
) -> None:
    """Defer a handler's endpoint emission until mount prefixes can resolve.

    Only handlers with at least one route decorator queue; everything else
    is dropped here so the pending list stays proportional to real routes.
    """
    if not isinstance(decorators, list):
        return
    route_decorators = [
        d for d in decorators if isinstance(d, str) and parse_route_decorator(d)
    ]
    if route_decorators:
        pending.append((label, qualified_name, route_decorators, module_qn))


def emit_endpoints(
    ingestor: IngestorProtocol,
    label: cs.NodeLabel,
    qualified_name: str,
    decorators: object,
    *,
    module_qn: str | None = None,
    prefix_resolver: PrefixResolver | None = None,
) -> None:
    """Emit an ENDPOINT Resource plus an EXPOSES edge per route decorator.

    With a resolver, each decorator's receiver variable is looked up and the
    route path is prepended with every mount prefix of that router (a route
    mounted twice yields one endpoint per mount). An unknown receiver keeps
    the bare decorator path.
    """
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
        pairs = parse_route_decorator(decorator)
        if not pairs:
            continue
        prefixes = [""]
        if prefix_resolver is not None and module_qn is not None:
            receiver = decorator_receiver(decorator)
            if receiver is not None:
                resolved = prefix_resolver(module_qn, receiver)
                if resolved:
                    prefixes = resolved
        # The qn is scoped by owning project (EXPOSES always comes from
        # within one), so same-template endpoints in different services stay
        # distinct nodes and host-aware linking can tell them apart (#879).
        project = qualified_name.split(cs.SEPARATOR_DOT, 1)[0]
        for method, path in pairs:
            for prefix in prefixes:
                identity = f"{method} {prefix}{path}"
                resource_qn = RESOURCE_QN_FORMAT.format(
                    kind=ResourceKind.ENDPOINT.value,
                    identity=f"{project}::{identity}",
                )
                ingestor.ensure_node_batch(
                    cs.NodeLabel.RESOURCE,
                    {
                        cs.KEY_QUALIFIED_NAME: resource_qn,
                        cs.KEY_NAME: identity,
                        KEY_KIND: ResourceKind.ENDPOINT.value,
                        KEY_PROJECT: project,
                    },
                )
                ingestor.ensure_relationship_batch(
                    (label, cs.KEY_QUALIFIED_NAME, qualified_name),
                    cs.RelationshipType.EXPOSES,
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, resource_qn),
                )


def _collect_live_resources(
    ingestor: QueryProtocol,
) -> tuple[dict[str, tuple[str, frozenset[str]]], dict[str, tuple[str, str | None]]]:
    from .io_access.constants import DYNAMIC_TARGET, KEY_KIND, ResourceKind

    networks: dict[str, tuple[str, frozenset[str]]] = {}
    endpoints: dict[str, tuple[str, str | None]] = {}
    for query in (CYPHER_LIVE_NETWORK_RESOURCES, CYPHER_LIVE_ENDPOINT_RESOURCES):
        for row in ingestor.fetch_all(query):
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            name = row.get(cs.KEY_NAME)
            if not isinstance(qn, str) or not isinstance(name, str):
                continue
            kind = row.get(KEY_KIND)
            if kind == ResourceKind.NETWORK.value and name != DYNAMIC_TARGET:
                raw_directions = row.get(_KEY_DIRECTIONS)
                directions = (
                    frozenset(d for d in raw_directions if isinstance(d, str))
                    if isinstance(raw_directions, list)
                    else frozenset()
                )
                networks[qn] = (name, directions)
            elif kind == ResourceKind.ENDPOINT.value:
                project = row.get(KEY_PROJECT)
                endpoints[qn] = (name, project if isinstance(project, str) else None)
    return networks, endpoints


def _host_stem(url: str) -> str | None:
    host = urlparse(url).hostname
    return host.lower().replace("_", "-") if host else None


def _project_stem(project: str) -> str:
    # `user-service__2adc9027` deploys as host `user-service` (compose and
    # cluster DNS use the service name); underscores and dashes are
    # interchangeable across compose file and directory conventions.
    return project.split(_PROJECT_HASH_SEPARATOR, 1)[0].lower().replace("_", "-")


def link_endpoints(ingestor: QueryProtocol) -> int:
    """Resolve literal client request URLs to matching ENDPOINT resources.

    Endpoint identities are ``METHOD /path/template``; a NETWORK resource
    links to every endpoint whose template matches its URL path and whose
    method is reachable from the URL's sink directions (a read-only URL
    cannot hit a write-only route). Templates without a literal segment are
    skipped entirely; they would match any same-length URL path. When the
    URL's hostname names an indexed project, only that project's endpoints
    are candidates; an unmatched host keeps the full fan-out. Returns the
    number of edges emitted.
    """
    ingestor.execute_write(CYPHER_DELETE_RESOLVES_TO)
    networks, endpoints = _collect_live_resources(ingestor)

    # The live ingestor both queries and writes; QueryProtocol alone types
    # the read side, so the single write goes through an ingestor view.
    writer = cast("IngestorProtocol", ingestor)
    created = 0
    for network_qn, (url, directions) in networks.items():
        host = _host_stem(url)
        owned = {
            qn
            for qn, (_identity, project) in endpoints.items()
            if project is not None and _project_stem(project) == host
        }
        candidates = owned if owned else set(endpoints)
        for endpoint_qn in candidates:
            identity, _project = endpoints[endpoint_qn]
            method, _, template = identity.partition(" ")
            if (
                template
                and _direction_compatible(directions, method)
                and _has_literal_segment(template)
                and url_matches_template(url, template)
            ):
                writer.ensure_relationship_batch(
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, network_qn),
                    cs.RelationshipType.RESOLVES_TO,
                    (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, endpoint_qn),
                )
                created += 1
    return created
