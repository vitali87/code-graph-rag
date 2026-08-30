"""Locate the string literal a dynamic call dispatches through (issue #1526).

A trace-only edge has no call expression in the caller's source; the call
went through `getattr(obj, "name")`, a registry keyed by `"name"`, or
something the static pass cannot see. When the callee's name appears as a
string literal inside the caller's span in one of those shapes, its site is
what a rewrite would have to touch, so the dynamic edge records it; when no
such literal exists the edge is marked unlocatable instead.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.utils import node_site_properties, safe_decode_text
from ..types_defs import PropertyDict

_LITERAL_PARENTS = frozenset({cs.TS_ARGUMENT_LIST, cs.TS_PY_PAIR, cs.TS_PY_SUBSCRIPT})


def _literal_text(node: Node) -> str | None:
    if node.type != cs.TS_PY_STRING:
        return None
    for child in node.named_children:
        if child.type == cs.TS_PY_STRING_CONTENT:
            return safe_decode_text(child)
    return None


def _is_dispatch_literal(node: Node) -> bool:
    parent = node.parent
    if parent is None or parent.type not in _LITERAL_PARENTS:
        return False
    if parent.type == cs.TS_ARGUMENT_LIST:
        call = parent.parent
        func = call.child_by_field_name(cs.FIELD_FUNCTION) if call is not None else None
        return func is not None and safe_decode_text(func) == cs.PY_BUILTIN_GETATTR
    if parent.type == cs.TS_PY_PAIR:
        return parent.child_by_field_name(cs.FIELD_KEY) is node
    return True


def locate_dispatch_literal(
    repo_root: Path, path: str, start_line: int, end_line: int, callee_name: str
) -> PropertyDict | None:
    """Site props of the literal naming `callee_name` inside the caller's span.

    Python only for now: other languages' dynamic dispatch has no single
    literal shape worth guessing at. Returns None when the file is not
    Python, cannot be read, or holds no such literal in the span.
    """
    file_path = repo_root / path
    if get_language_for_extension(file_path.suffix) != cs.SupportedLanguage.PYTHON:
        return None
    try:
        source = file_path.read_bytes()
    except OSError:
        return None
    parsers, _queries = load_parsers()
    parser = parsers.get(cs.SupportedLanguage.PYTHON)
    if parser is None:
        return None
    root = parser.parse(source).root_node
    stack = [root]
    found: list[Node] = []
    while stack:
        node = stack.pop()
        if node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line:
            continue
        if _literal_text(node) == callee_name and _is_dispatch_literal(node):
            found.append(node)
        stack.extend(node.children)
    if not found:
        return None
    first = min(found, key=lambda n: (n.start_point[0], n.start_point[1]))
    return node_site_properties(first)
