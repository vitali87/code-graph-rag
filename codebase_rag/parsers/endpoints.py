"""Route-decorator parsing and URL/template matching (issue #425 phase 3).

Handler decorators are stored verbatim on Function/Method nodes (e.g.
``@app.get("/users/{id}")``); this module turns them into
``(METHOD, /path/template)`` pairs and matches literal client URLs against
those templates so cross-project request edges can resolve to handlers.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

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
_METHODS_KWARG_RE = re.compile(r"methods\s*=\s*\[(?P<items>[^\]]*)\]")
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


_TEMPLATE_PARAM_RE = re.compile(r"^\{[^/]+\}$")


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
