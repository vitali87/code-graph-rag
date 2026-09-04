"""Locate the string literal a dynamic call dispatches through (issue #1526).

A trace-only edge has no call expression in the caller's source; the call
went through `getattr(obj, "name")`, a registry keyed by `"name"`, or
something the static pass cannot see. The invariant, stated once: a
literal is the site of a dynamic edge only when its lookup expression is
INVOKED in the caller's body, either directly (`getattr(obj, "name")()`,
`table["name"]()`) or through a name bound from it and called later
(`fn = table["name"]; fn()`). A literal that is merely present (a dict key,
a lookup never called) is not a site. A nested `def`/`class`/`lambda` is
another callable's body and is not searched. Two candidate sites cannot be
told apart statically, and a computed dispatch (`getattr(obj, attr)`,
`table[key]()`, or a name bound from one and called, including one captured
from an enclosing scope unless the caller rebinds that name) could have
carried the call itself; in either case the edge is marked unlocatable.
"""

from __future__ import annotations

from pathlib import Path

from tree_sitter import Node

from .. import constants as cs
from ..language_spec import get_language_for_extension
from ..parser_loader import load_parsers
from ..parsers.utils import node_site_properties, safe_decode_text
from ..types_defs import PropertyDict

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


def _lookup_of(literal: Node) -> Node | None:
    """The lookup expression a dispatch-shaped literal keys: the `getattr`
    call it is the second argument of, or the subscript it indexes."""
    parent = literal.parent
    if parent is None:
        return None
    if parent.type == cs.TS_ARGUMENT_LIST:
        call = parent.parent
        func = call.child_by_field_name(cs.FIELD_FUNCTION) if call is not None else None
        if func is None or safe_decode_text(func) != cs.PY_BUILTIN_GETATTR:
            return None
        named = parent.named_children
        return call if len(named) >= 2 and named[1] == literal else None
    if parent.type == cs.TS_PY_SUBSCRIPT:
        key = parent.child_by_field_name(cs.TS_PY_FIELD_SUBSCRIPT)
        return parent if key == literal else None
    return None


def _is_invoked(expr: Node) -> bool:
    """`expr` is the function of a call: `expr(...)`."""
    parent = expr.parent
    return (
        parent is not None
        and parent.type == cs.TS_PY_CALL
        and parent.child_by_field_name(cs.FIELD_FUNCTION) == expr
    )


def _bound_name(expr: Node) -> str | None:
    """`name` when `expr` is the whole right side of `name = expr`."""
    parent = expr.parent
    if parent is None or parent.type != cs.TS_PY_ASSIGNMENT:
        return None
    left = parent.child_by_field_name(cs.FIELD_LEFT)
    right = parent.child_by_field_name(cs.FIELD_RIGHT)
    if left is None or right != expr or left.type != cs.TS_PY_IDENTIFIER:
        return None
    return safe_decode_text(left)


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
    """The invoked dispatch literals naming `callee_name` in the caller's body.

    None when the body holds a computed dispatch, which could have carried
    the call itself.
    """
    # (node, inside_caller): the first definition that begins inside the
    # span is the caller itself; any definition met below it is a nested
    # callable whose literals belong to that callable, not to this edge.
    stack: list[tuple[Node, bool]] = [(root, False)]
    direct: list[Node] = []
    # name -> the literal whose lookup bound it (`fn = table["name"]`).
    bound_literal: dict[str, Node] = {}
    # Computed names: bound from a non-literal lookup, in the body or in an
    # enclosing scope; a body assignment of any other kind masks an outer one.
    stored_outer: set[str] = set()
    stored_inner: set[str] = set()
    rebound_inner: set[str] = set()
    called: set[str] = set()
    while stack:
        node, inside_caller = stack.pop()
        outside = (
            node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > end_line
        )
        if outside:
            # A sibling scope's body is another callable's; an enclosing
            # scope's statements are visible to the caller, so a computed
            # callable captured from there still counts as stored.
            if node.type in _NESTED_SCOPES:
                continue
            if (target := _computed_lookup_target(node)) is not None:
                stored_outer.add(target)
            stack.extend((child, inside_caller) for child in node.children)
            continue
        if node.type in _NESTED_SCOPES and node.start_point[0] + 1 >= start_line:
            if inside_caller:
                continue
            inside_caller = True
        if _is_computed_dispatch(node):
            return None
        if (target := _computed_lookup_target(node)) is not None:
            stored_inner.add(target)
        elif node.type == cs.TS_PY_ASSIGNMENT:
            left = node.child_by_field_name(cs.FIELD_LEFT)
            if left is not None and left.type == cs.TS_PY_IDENTIFIER:
                rebound_inner.add(safe_decode_text(left) or "")
        if (name := _called_identifier(node)) is not None:
            called.add(name)
        if _literal_text(node) == callee_name and (lookup := _lookup_of(node)):
            if _is_invoked(lookup):
                direct.append(node)
            elif (bound := _bound_name(lookup)) is not None:
                bound_literal[bound] = node
        stack.extend((child, inside_caller) for child in node.children)
    computed = stored_inner | (stored_outer - rebound_inner)
    if computed & called:
        return None
    return direct + [lit for name, lit in bound_literal.items() if name in called]
