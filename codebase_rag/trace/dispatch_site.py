"""Locate the string literal a dynamic call dispatches through (issue #1526).

A trace-only edge has no call expression in the caller's source; the call
went through `getattr(obj, "name")`, a registry keyed by `"name"`, or
something the static pass cannot see. When the callee's name appears as a
string literal inside the caller's own body in one of those shapes, its site
is what a rewrite would have to touch, so the dynamic edge records it. A
nested `def`/`class`/`lambda` is another callable's body and is not searched, and two
candidate literals cannot be told apart statically (an unrelated `d["name"]`
looks exactly like a registry lookup), so the site is recorded only when the
candidate is unique and the body holds no computed dispatch that could have
carried the call instead; otherwise the edge is marked unlocatable.
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
# A lambda counts as another callable's body too: the trace resolver drops
# `<lambda>` frames as synthetic, so a call made inside one is never
# attributed to the enclosing function and its literals cannot be that
# function's dispatch site.
_NESTED_SCOPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_LAMBDA}
)


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


def _computed_lookup_target(node: Node) -> str | None:
    """The name bound by `name = table[key]` or `name = getattr(obj, attr)`
    with a non-literal key: a dispatch stored for a later call."""
    if node.type != cs.TS_PY_ASSIGNMENT:
        return None
    left = node.child_by_field_name(cs.FIELD_LEFT)
    right = node.child_by_field_name(cs.FIELD_RIGHT)
    if left is None or right is None or left.type != cs.TS_PY_IDENTIFIER:
        return None
    if right.type == cs.TS_PY_SUBSCRIPT:
        key = right.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        computed = key is not None and key.type != cs.TS_PY_STRING
    elif right.type == cs.TS_PY_CALL:
        func = right.child_by_field_name(cs.FIELD_FUNCTION)
        args = right.child_by_field_name(cs.FIELD_ARGUMENTS)
        named = args.named_children if args is not None else []
        computed = (
            func is not None
            and safe_decode_text(func) == cs.PY_BUILTIN_GETATTR
            and len(named) >= 2
            and named[1].type != cs.TS_PY_STRING
        )
    else:
        computed = False
    return safe_decode_text(left) if computed else None


def _called_identifier(node: Node) -> str | None:
    """The bare name a call invokes (`fn()`), if any."""
    if node.type != cs.TS_PY_CALL:
        return None
    func = node.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None or func.type != cs.TS_PY_IDENTIFIER:
        return None
    return safe_decode_text(func)


def _is_computed_dispatch(node: Node) -> bool:
    """`getattr(obj, name)` with a non-literal name, or `table[key](...)`.

    Either could have carried the traced call, so no literal in the same
    body can be trusted to be its site.
    """
    if node.type != cs.TS_PY_CALL:
        return False
    func = node.child_by_field_name(cs.FIELD_FUNCTION)
    if func is None:
        return False
    if func.type == cs.TS_PY_SUBSCRIPT:
        key = func.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        return key is not None and key.type != cs.TS_PY_STRING
    if safe_decode_text(func) != cs.PY_BUILTIN_GETATTR:
        return False
    args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
    named = args.named_children if args is not None else []
    return len(named) >= 2 and named[1].type != cs.TS_PY_STRING


def locate_dispatch_literal(
    repo_root: Path, path: str, start_line: int, end_line: int, callee_name: str
) -> PropertyDict | None:
    """Site props of the one literal naming `callee_name` in the caller's body.

    Python only for now: other languages' dynamic dispatch has no single
    literal shape worth guessing at. Returns None when the file is not
    Python, cannot be read, holds no such literal in the caller's own body
    (nested definitions excluded), or holds more than one, since the scan
    cannot tell the traced dispatch site from an unrelated same-named
    literal and must not point a rewrite at the wrong one. A computed
    dispatch in the body (`getattr(obj, name)`, `table[key]()`) could have
    carried the call itself, so it makes the edge unlocatable too.
    """
    root = _python_root(repo_root / path)
    if root is None:
        return None
    found = _dispatch_literals(root, start_line, end_line, callee_name)
    if found is None or len(found) != 1:
        return None
    return node_site_properties(found[0])


def _python_root(file_path: Path) -> Node | None:
    """Parsed root of a Python file, or None when it is not one or unreadable."""
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
    return parser.parse(source).root_node


def _dispatch_literals(
    root: Node, start_line: int, end_line: int, callee_name: str
) -> list[Node] | None:
    """Dispatch-shaped literals naming `callee_name` in the caller's own body.

    None when the body holds a computed dispatch, which could have carried
    the call itself.
    """
    # (node, inside_caller): the first definition that begins inside the
    # span is the caller itself; any definition met below it is a nested
    # callable whose literals belong to that callable, not to this edge.
    stack: list[tuple[Node, bool]] = [(root, False)]
    found: list[Node] = []
    # A computed lookup stored in a name (`fn = registry[key]`) and called
    # later (`fn()`) is a computed dispatch too, just split in two.
    stored: set[str] = set()
    called: set[str] = set()
    while stack:
        node, inside_caller = stack.pop()
        outside = (
            node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line
        )
        if outside:
            # A sibling scope's body is another callable's; an enclosing
            # scope's statements are visible to the caller, so a computed
            # callable captured from there (`fn = registry[key]` above a
            # nested `def`) still counts as stored.
            if node.type in _NESTED_SCOPES:
                continue
            if (target := _computed_lookup_target(node)) is not None:
                stored.add(target)
            stack.extend((child, inside_caller) for child in node.children)
            continue
        if node.type in _NESTED_SCOPES and node.start_point[0] + 1 >= start_line:
            if inside_caller:
                continue
            inside_caller = True
        if _is_computed_dispatch(node):
            return None
        if (target := _computed_lookup_target(node)) is not None:
            stored.add(target)
        if (name := _called_identifier(node)) is not None:
            called.add(name)
        if _literal_text(node) == callee_name and _is_dispatch_literal(node):
            found.append(node)
        stack.extend((child, inside_caller) for child in node.children)
    if stored & called:
        return None
    return found
