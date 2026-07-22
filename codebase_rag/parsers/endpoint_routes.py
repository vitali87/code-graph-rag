"""Call-registered route extraction (issue #886).

JS and Go frameworks register routes through calls rather than decorators:
Express-style ``app.get('/path', handler)`` and ``app.route('/p').get(fn)``
chains, Go ``http.HandleFunc("/path", h)`` (including Go 1.22 ``"GET /p"``
patterns) and echo/gin/chi-style verb methods (``e.GET("/p", h)``). This
walker recognises them in cached module ASTs and yields ``METHOD /template``
registrations so they become ENDPOINT resources like Python decorators do.

The evidence gate is a literal path argument opening with ``/`` (or a
``VERB /path`` pattern); everything else is ignored. EXPOSES attribution is
a ladder: a bare-identifier handler defined in the module, else the
registering call's enclosing function, else the module itself, so the
endpoint always stays anchored and the trace lands at the wiring site.

Ceilings (each simply yields nothing, never a wrong template): sub-router
mounting (``app.use('/prefix', router)``), gorilla mux ``.Methods("GET")``
chains, handlers referenced through imports or attributes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from tree_sitter import Node

METHOD_ANY = "ANY"

# Modules whose route registrations this pass can read.
ROUTE_CALL_LANGUAGES = frozenset(
    {cs.SupportedLanguage.JS, cs.SupportedLanguage.TS, cs.SupportedLanguage.GO}
)

CYPHER_PROJECT_MODULES = (
    "MATCH (m:Module) WHERE m.qualified_name STARTS WITH $project_prefix "
    "RETURN m.qualified_name AS qualified_name, m.path AS path"
)

_JS_VERBS = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "delete": "DELETE",
    "head": "HEAD",
    "options": "OPTIONS",
    "all": METHOD_ANY,
}
_JS_ROUTE_CHAIN = "route"
_GO_VERB_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)
_GO_HANDLE_METHODS = frozenset({"HandleFunc", "Handle"})
# Go 1.22 ServeMux patterns: "GET /products/{id}".
_GO_PATTERN_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS) (/\S*)$")

_CHAIN_DEPTH_LIMIT = 8


@dataclass(frozen=True)
class RouteRegistration:
    method: str
    path: str
    handler_name: str | None  # bare-identifier handler, when present
    scope: str  # enclosing function chain, '' at module level


def _decode(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.decode(cs.ENCODING_UTF8)


def _literal_path(node: Node | None, string_type: str, content_type: str) -> str | None:
    # A plain string literal only; no gluing of fragments across escapes is
    # needed for route paths in practice.
    if node is None or node.type != string_type:
        return None
    parts = [
        _decode(child) or ""
        for child in node.named_children
        if child.type == content_type
    ]
    value = "".join(parts)
    return value if value else None


def _call_args(call: Node) -> list[Node]:
    args = call.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
    if args is None:
        return []
    return [c for c in args.named_children if c.type != cs.TS_COMMENT]


def _handler_identifier(node: Node | None, identifier_type: str) -> str | None:
    if node is not None and node.type == identifier_type:
        return _decode(node)
    return None


def _js_chain_path(call: Node, depth: int = 0) -> str | None:
    # `app.route('/todos').get(list).post(create)`: walk the receiver chain
    # down to the `.route('/p')` call and return its literal path.
    if depth > _CHAIN_DEPTH_LIMIT:
        return None
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return None
    prop = _decode(fn.child_by_field_name(cs.FIELD_PROPERTY))
    if prop == _JS_ROUTE_CHAIN:
        args = _call_args(call)
        return _literal_path(
            args[0] if args else None, cs.TS_STRING, cs.TS_STRING_FRAGMENT
        )
    if prop in _JS_VERBS:
        receiver = fn.child_by_field_name(cs.TS_FIELD_OBJECT)
        if receiver is not None and receiver.type == cs.TS_CALL_EXPRESSION:
            return _js_chain_path(receiver, depth + 1)
    return None


def _js_registration(call: Node, scope: str) -> RouteRegistration | None:
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_MEMBER_EXPRESSION:
        return None
    prop = _decode(fn.child_by_field_name(cs.FIELD_PROPERTY))
    method = _JS_VERBS.get(prop or "")
    if method is None:
        return None
    args = _call_args(call)
    direct_path = _literal_path(
        args[0] if args else None, cs.TS_STRING, cs.TS_STRING_FRAGMENT
    )
    if direct_path is not None and direct_path.startswith("/"):
        handler = _handler_identifier(
            args[1] if len(args) > 1 else None, cs.TS_PY_IDENTIFIER
        )
        return RouteRegistration(method, direct_path, handler, scope)
    receiver = fn.child_by_field_name(cs.TS_FIELD_OBJECT)
    if receiver is not None and receiver.type == cs.TS_CALL_EXPRESSION:
        chain_path = _js_chain_path(receiver)
        if chain_path is not None and chain_path.startswith("/"):
            handler = _handler_identifier(
                args[0] if args else None, cs.TS_PY_IDENTIFIER
            )
            return RouteRegistration(method, chain_path, handler, scope)
    return None


def _go_registration(call: Node, scope: str) -> RouteRegistration | None:
    fn = call.child_by_field_name(cs.FIELD_FUNCTION)
    if fn is None or fn.type != cs.TS_GO_SELECTOR_EXPRESSION:
        return None
    field = _decode(fn.child_by_field_name(cs.FIELD_FIELD))
    args = _call_args(call)
    path = _literal_path(
        args[0] if args else None,
        cs.TS_GO_INTERPRETED_STRING,
        cs.TS_GO_INTERPRETED_STRING_CONTENT,
    )
    if path is None:
        return None
    handler = _handler_identifier(
        args[1] if len(args) > 1 else None, cs.TS_PY_IDENTIFIER
    )
    if field in _GO_VERB_METHODS and path.startswith("/"):
        return RouteRegistration(field, path, handler, scope)
    if field in _GO_HANDLE_METHODS:
        pattern = _GO_PATTERN_RE.match(path)
        if pattern is not None:
            return RouteRegistration(pattern.group(1), pattern.group(2), handler, scope)
        if path.startswith("/"):
            return RouteRegistration(METHOD_ANY, path, handler, scope)
    return None


_JS_SCOPE_TYPES = frozenset({cs.TS_FUNCTION_DECLARATION, cs.TS_METHOD_DEFINITION})
_GO_SCOPE_TYPES = frozenset({"function_declaration", "method_declaration"})


def collect_route_registrations(
    root: Node, language: cs.SupportedLanguage
) -> list[RouteRegistration]:
    """All call-registered routes in one module AST."""
    if language is cs.SupportedLanguage.GO:
        scope_types = _GO_SCOPE_TYPES
        call_type = cs.TS_GO_CALL_EXPRESSION
        extract = _go_registration
    else:
        scope_types = _JS_SCOPE_TYPES
        call_type = cs.TS_CALL_EXPRESSION
        extract = _js_registration
    out: list[RouteRegistration] = []
    stack: list[tuple[Node, str]] = [(root, "")]
    while stack:
        node, scope = stack.pop()
        if node.type in scope_types:
            name = _decode(node.child_by_field_name(cs.TS_FIELD_NAME)) or ""
            inner = (
                f"{scope}{cs.SEPARATOR_DOT}{name}"
                if scope and name
                else (name or scope)
            )
            stack.extend((child, inner) for child in node.named_children)
            continue
        if node.type == call_type:
            registration = extract(node, scope)
            if registration is not None:
                out.append(registration)
        stack.extend((child, scope) for child in node.named_children)
    return out
