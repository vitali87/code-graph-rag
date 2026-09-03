"""Locate the string literal a dynamic call dispatches through (issue #1526).

A trace-only edge has no call expression in the caller's source; the call
went through `getattr(obj, "name")`, a registry keyed by `"name"`, or
something the static pass cannot see. When the callee's name appears as a
string literal inside the caller's own body in one of those shapes, its site
is what a rewrite would have to touch, so the dynamic edge records it. A
nested `def`/`class` is another callable's body and is not searched, and two
candidate literals cannot be told apart statically (an unrelated `d["name"]`
looks exactly like a registry lookup), so the site is recorded only when the
candidate is unique; otherwise the edge is marked unlocatable.
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
_NESTED_SCOPES = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION})


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
        return parent.child_by_field_name(cs.FIELD_KEY) == node
    return True


def locate_dispatch_literal(
    repo_root: Path, path: str, start_line: int, end_line: int, callee_name: str
) -> PropertyDict | None:
    """Site props of the one literal naming `callee_name` in the caller's body.

    Python only for now: other languages' dynamic dispatch has no single
    literal shape worth guessing at. Returns None when the file is not
    Python, cannot be read, holds no such literal in the caller's own body
    (nested definitions excluded), or holds more than one, since the scan
    cannot tell the traced dispatch site from an unrelated same-named
    literal and must not point a rewrite at the wrong one.
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
    # (node, inside_caller): the first definition that begins inside the
    # span is the caller itself; any definition met below it is a nested
    # callable whose literals belong to that callable, not to this edge.
    stack: list[tuple[Node, bool]] = [(root, False)]
    found: list[Node] = []
    while stack:
        node, inside_caller = stack.pop()
        if node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line:
            continue
        if node.type in _NESTED_SCOPES and node.start_point[0] + 1 >= start_line:
            if inside_caller:
                continue
            inside_caller = True
        if _literal_text(node) == callee_name and _is_dispatch_literal(node):
            found.append(node)
        stack.extend((child, inside_caller) for child in node.children)
    if len(found) != 1:
        return None
    return node_site_properties(found[0])
