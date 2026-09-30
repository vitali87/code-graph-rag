"""`typing.overload` stubs, folded into the implementation that follows them.

An `@overload` stub exists only for the type checker: the implementation
defined after it rebinds the name, so every call runs the implementation.
Registered as definitions of their own, the stubs took the bare qualified
name (the first one) and pushed the implementation to `name@line`, and every
call fanned out to all of them as `overload` (issue #2590). A stub is
therefore dropped when an implementation follows it in the same block,
which is the rule C/C++ header prototypes already follow beside their
definition (issue #893); stubs with no implementation (a Protocol's) are all
there is to point at and keep their nodes.

The ingest pass and `rename` both read the stubs off the syntax tree with the
helpers below, so the stubs a rename rewrites are exactly the ones the graph
folded.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def folded_overload_stubs(root: Node) -> frozenset[int]:
    """Start bytes of the stub `function_definition`s an implementation owns."""
    spellings = overload_spellings(root)
    if not spellings:
        return frozenset()
    return frozenset(
        start for block in _blocks(root) for start in _folded_in(block, spellings)
    )


def overload_stub_names(function: Node, root: Node) -> list[Node]:
    """Name tokens of the stubs folded into the implementation `function`.

    The stubs precede their implementation in its block; the walk back stops
    at an earlier same-named plain def, whose own stubs those are.
    """
    spellings = overload_spellings(root)
    statement = _statement_of(function)
    name = _name(function)
    if not spellings or name is None or _is_stub(statement, spellings):
        return []
    names: list[Node] = []
    sibling = statement.prev_named_sibling
    while sibling is not None:
        other = _function_of(sibling)
        if other is not None and _name(other) == name:
            if not _is_stub(sibling, spellings):
                break
            if (token := other.child_by_field_name(cs.FIELD_NAME)) is not None:
                names.append(token)
        sibling = sibling.prev_named_sibling
    return names


def overload_spellings(root: Node) -> frozenset[str]:
    """Every decorator spelling that names typing's `overload` in this module.

    Read from the module's own imports, so a local `def overload` or another
    library's `overload` (a real rebinding decorator) never matches.
    """
    spellings: set[str] = set()
    for block in _blocks(root):
        for statement in block.named_children:
            if statement.type == cs.TS_PY_IMPORT_STATEMENT:
                spellings.update(_module_import_spellings(statement))
            elif statement.type == cs.TS_PY_IMPORT_FROM_STATEMENT:
                spellings.update(_from_import_spellings(statement))
    return frozenset(spellings)


def _module_import_spellings(statement: Node) -> Iterator[str]:
    # `import typing` / `import typing as t` -> `typing.overload` / `t.overload`.
    for imported in statement.children_by_field_name(cs.FIELD_NAME):
        module, alias = _imported(imported)
        if module in cs.PY_OVERLOAD_MODULES:
            yield f"{alias or module}{cs.SEPARATOR_DOT}{cs.PY_OVERLOAD}"


def _from_import_spellings(statement: Node) -> Iterator[str]:
    # `from typing import overload [as o]`, or a star import that binds it.
    module = safe_decode_text(statement.child_by_field_name(cs.FIELD_MODULE_NAME))
    if module not in cs.PY_OVERLOAD_MODULES:
        return
    if any(child.type == cs.TS_WILDCARD_IMPORT for child in statement.named_children):
        yield cs.PY_OVERLOAD
    for imported in statement.children_by_field_name(cs.FIELD_NAME):
        name, alias = _imported(imported)
        if name == cs.PY_OVERLOAD:
            yield alias or name


def _imported(node: Node) -> tuple[str | None, str | None]:
    if node.type == cs.TS_PY_ALIASED_IMPORT:
        return (
            safe_decode_text(node.child_by_field_name(cs.FIELD_NAME)),
            safe_decode_text(node.child_by_field_name(cs.FIELD_ALIAS)),
        )
    return safe_decode_text(node), None


def _blocks(root: Node) -> Iterator[Node]:
    # The module and every statement block under it. Only statements are
    # descended into (never expressions), so the walk costs the statement
    # count, not the node count.
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_PY_MODULE, cs.TS_PY_BLOCK):
            yield node
        stack.extend(
            child
            for child in node.named_children
            if child.type in cs.PY_STATEMENT_CONTAINERS
        )


def _folded_in(block: Node, spellings: frozenset[str]) -> Iterator[int]:
    # Walked backwards so "a plain def of this name follows" is known on
    # reaching each stub: that def is the implementation the stub describes.
    implemented: set[str] = set()
    for statement in reversed(block.named_children):
        function = _function_of(statement)
        if function is None or (name := _name(function)) is None:
            continue
        if not _is_stub(statement, spellings):
            implemented.add(name)
        elif name in implemented:
            yield function.start_byte


def _function_of(statement: Node) -> Node | None:
    if statement.type == cs.TS_PY_DECORATED_DEFINITION:
        statement = statement.child_by_field_name(cs.FIELD_DEFINITION) or statement
    return statement if statement.type == cs.TS_PY_FUNCTION_DEFINITION else None


def _statement_of(function: Node) -> Node:
    parent = function.parent
    if parent is not None and parent.type == cs.TS_PY_DECORATED_DEFINITION:
        return parent
    return function


def _name(function: Node) -> str | None:
    return safe_decode_text(function.child_by_field_name(cs.FIELD_NAME))


def _is_stub(statement: Node, spellings: frozenset[str]) -> bool:
    if statement.type != cs.TS_PY_DECORATED_DEFINITION:
        return False
    return any(
        "".join((safe_decode_text(expression) or "").split()) in spellings
        for decorator in statement.named_children
        if decorator.type == cs.TS_PY_DECORATOR
        for expression in decorator.named_children[:1]
    )
