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

A decorator is typing's `overload` only while the typing import that gave its
spelling is still the binding of its root name (`overload`, an alias, or the
`t` of `t.overload`) where the stub is defined. Each block is read in
statement order: a `def` or `class` of that name, an assignment to it, a `for`
loop target naming it (in the loop's body too), or an import of it from
anywhere else, made earlier in the stub's block or in a block enclosing it,
rebinds it from that statement on, until typing is imported again. Where no
enclosing block binds the name before the stub, the module's imports as a
whole decide.

The ingest pass and `rename` both read the stubs off the syntax tree with the
helpers below, so the stubs a rename rewrites are exactly the ones the graph
folded.
"""

from __future__ import annotations

from collections.abc import Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text

# The unpacking forms a loop target can take: `a, b`, `(a, b)`, `[a, b]`, `*a`.
_TARGET_PATTERNS = cs.PY_UNPACKING_TARGET_TYPES | {cs.TS_PY_LIST_SPLAT_PATTERN}


def folded_overload_stubs(root: Node) -> frozenset[int]:
    """Start bytes of the stub `function_definition`s an implementation owns."""
    stubs = _overload_stubs(root)
    if not stubs:
        return frozenset()
    return frozenset(
        start for block in _blocks(root) for start in _folded_in(block, stubs)
    )


def overload_stub_names(function: Node, root: Node) -> list[Node]:
    """Name tokens of the stubs folded into the implementation `function`.

    The stubs precede their implementation in its block; the walk back stops
    at an earlier same-named plain def, whose own stubs those are.
    """
    stubs = _overload_stubs(root)
    statement = _statement_of(function)
    name = _name(function)
    if not stubs or name is None or statement.start_byte in stubs:
        return []
    names: list[Node] = []
    sibling = statement.prev_named_sibling
    while sibling is not None:
        other = _function_of(sibling)
        if other is not None and _name(other) == name:
            if sibling.start_byte not in stubs:
                break
            if (token := other.child_by_field_name(cs.FIELD_NAME)) is not None:
                names.append(token)
        sibling = sibling.prev_named_sibling
    return names


def overload_spellings(root: Node) -> frozenset[str]:
    """Every decorator spelling a typing import in this module gives `overload`.

    Read from the module's own imports, so another library's `overload` never
    matches; whether that import is still the binding in force at a given
    stub is `_overload_stubs`'s call.
    """
    return frozenset(
        spelling
        for block in _blocks(root)
        for statement in block.named_children
        for _bound, spelling in _bindings(statement)
        if spelling
    )


def _overload_stubs(root: Node) -> frozenset[int]:
    # Start bytes of the statements typing's `overload` decorates. Every block
    # is read in statement order from the bindings in force where it starts,
    # {root name: the spelling it gives, "" once rebound to anything else};
    # only statements are walked, never expressions.
    spellings = overload_spellings(root)
    roots = {spelling.partition(cs.SEPARATOR_DOT)[0] for spelling in spellings}
    stubs: set[int] = set()
    pending: list[tuple[Node, dict[str, str]]] = [(root, {})] if spellings else []
    while pending:
        block, bound = pending.pop()
        for statement in block.named_children:
            if _is_stub(statement, spellings, bound):
                stubs.add(statement.start_byte)
            if statement.type == cs.TS_PY_FOR_STATEMENT:
                # The loop target is bound before the body runs.
                _rebind(statement, roots, bound)
            if statement.type in cs.PY_STATEMENT_CONTAINERS:
                pending.extend((inner, dict(bound)) for inner in _blocks_in(statement))
            _rebind(statement, roots, bound)
    return frozenset(stubs)


def _rebind(statement: Node, roots: set[str], bound: dict[str, str]) -> None:
    # Record what each overload-relevant root name means after `statement`.
    for name, spelling in _bindings(statement):
        if name is not None and name in roots:
            bound[name] = spelling


def _bindings(statement: Node) -> Iterator[tuple[str | None, str]]:
    # The names `statement` binds in its block, each with the spelling of
    # typing's `overload` it now gives ("" for none).
    match statement.type:
        case cs.TS_PY_IMPORT_STATEMENT:
            yield from _module_import_bindings(statement)
        case cs.TS_PY_IMPORT_FROM_STATEMENT:
            yield from _from_import_bindings(statement)
        case (
            cs.TS_PY_DECORATED_DEFINITION
            | cs.TS_PY_FUNCTION_DEFINITION
            | cs.TS_PY_CLASS_DEFINITION
        ):
            definition = statement.child_by_field_name(cs.FIELD_DEFINITION)
            yield _name(definition or statement), ""
        case cs.TS_PY_FOR_STATEMENT:
            # `for name in ...` / `for a, (b, *name) in ...`.
            for name in _target_names(statement.child_by_field_name(cs.FIELD_LEFT)):
                yield name, ""
        case cs.TS_PY_EXPRESSION_STATEMENT:
            # `name = ...` / `name: T = ...`; the value is never read.
            assignment = statement.named_child(0)
            if assignment is not None and assignment.type == cs.TS_PY_ASSIGNMENT:
                target = assignment.child_by_field_name(cs.FIELD_LEFT)
                if target is not None and target.type == cs.TS_PY_IDENTIFIER:
                    yield safe_decode_text(target), ""


def _target_names(target: Node | None) -> Iterator[str | None]:
    # The names a loop target binds; `x.y` and `x[i]` bind none.
    if target is None:
        return
    if target.type == cs.TS_PY_IDENTIFIER:
        yield safe_decode_text(target)
    elif target.type in _TARGET_PATTERNS:
        for child in target.named_children:
            yield from _target_names(child)


def _module_import_bindings(statement: Node) -> Iterator[tuple[str | None, str]]:
    # `import typing` / `import typing as t` -> `typing.overload` / `t.overload`.
    for imported in statement.children_by_field_name(cs.FIELD_NAME):
        module, alias = _imported(imported)
        name = alias or module
        if module in cs.PY_OVERLOAD_MODULES:
            yield name, f"{name}{cs.SEPARATOR_DOT}{cs.PY_OVERLOAD}"
        else:
            yield name, ""  # `import x as overload` rebinds the name.


def _from_import_bindings(statement: Node) -> Iterator[tuple[str | None, str]]:
    # `from typing import overload [as o]`, or a star import that binds it; a
    # name imported from any other module spells nothing. Another module's
    # star import binds names this cannot see, so it binds none here.
    module = safe_decode_text(statement.child_by_field_name(cs.FIELD_MODULE_NAME))
    from_typing = module in cs.PY_OVERLOAD_MODULES
    if from_typing and any(
        child.type == cs.TS_WILDCARD_IMPORT for child in statement.named_children
    ):
        yield cs.PY_OVERLOAD, cs.PY_OVERLOAD
    for imported in statement.children_by_field_name(cs.FIELD_NAME):
        name, alias = _imported(imported)
        bound = alias or name
        if from_typing and name == cs.PY_OVERLOAD:
            yield bound, bound or ""
        else:
            yield bound, ""


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


def _blocks_in(statement: Node) -> Iterator[Node]:
    # The blocks directly under a compound statement, through its clauses.
    for child in statement.named_children:
        if child.type == cs.TS_PY_BLOCK:
            yield child
        elif child.type in cs.PY_STATEMENT_CONTAINERS:
            yield from _blocks_in(child)


def _folded_in(block: Node, stubs: frozenset[int]) -> Iterator[int]:
    # Walked backwards so "a plain def of this name follows" is known on
    # reaching each stub: that def is the implementation the stub describes.
    implemented: set[str] = set()
    for statement in reversed(block.named_children):
        function = _function_of(statement)
        if function is None or (name := _name(function)) is None:
            continue
        if statement.start_byte not in stubs:
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


def _is_stub(statement: Node, spellings: frozenset[str], bound: dict[str, str]) -> bool:
    # A decorator spelled as typing's `overload` whose root name nothing else
    # has rebound by the time `statement` runs.
    if statement.type != cs.TS_PY_DECORATED_DEFINITION:
        return False
    for decorator in statement.named_children:
        if decorator.type != cs.TS_PY_DECORATOR:
            continue
        for expression in decorator.named_children[:1]:
            spelling = "".join((safe_decode_text(expression) or "").split())
            root_name = spelling.partition(cs.SEPARATOR_DOT)[0]
            if spelling in spellings and bound.get(root_name, spelling) == spelling:
                return True
    return False
