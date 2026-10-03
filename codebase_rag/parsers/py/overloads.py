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
spelling is surely still the binding of its root name (`overload`, an alias,
or the `t` of `t.overload`) where the stub is defined. Each scope is read in
statement order, and anything that binds the name rebinds it from that
statement on, until typing is imported again: a `def` or `class` of that
name, an assignment to it (unpacked, chained or augmented too), a walrus, a
`for`, `with ... as`, `except ... as` or `case` target naming it, a `type`
alias, a `del`, or an import of it from anywhere else. The bodies of an `if`,
`for`, `while`, `with`, `try` or `match` are no scope of their own, so what
they bind is the enclosing scope's; which branch runs, and whether a loop
body runs again, is unknown, so a name such a statement binds anywhere in it
is typing's within and after it only when every binding of it there gives
typing's too. A class body runs where it is defined, but a def's body runs
only when the def is called, by which time any binding its enclosing scopes
make anywhere (after the def too) may be in force: it starts from all of them
merged by that same rule, and its own bindings shadow them as it runs. A name
a `global` or `nonlocal` declares can be rebound by any call, so it never
spells a stub. A stub whose decorator is not surely typing's is never folded.
Where nothing binds the name before the stub, the module's imports as a
whole decide.

The ingest pass and `rename` both read the stubs off the syntax tree with the
helpers below, so the stubs a rename rewrites are exactly the ones the graph
folded.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Iterable, Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text

# The shapes a target unpacks through to the names it binds: `a, b`, `(a, b)`,
# `[a, b]`, `*a`, and the `tuple` / `list` / parenthesized EXPRESSIONS
# tree-sitter parses a `with ... as` target as, and `del a, b`'s list.
_TARGET_PATTERNS = cs.PY_UNPACKING_TARGET_TYPES | {
    cs.TS_PY_LIST_SPLAT_PATTERN,
    cs.TS_PY_TUPLE,
    cs.TS_PY_LIST,
    cs.TS_PY_PARENTHESIZED_EXPRESSION,
    cs.TS_PY_LIST_SPLAT,
    cs.TS_PY_EXPRESSION_LIST,
}
# Nodes whose `left` field is the target they bind.
_LEFT_BINDERS = frozenset(
    {cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT}
)
# Nodes whose `name` field is the one name they bind: a walrus, a def, a class.
_NAME_BINDERS = frozenset(
    {cs.TS_PY_NAMED_EXPRESSION, cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION}
)
# Nodes every named child of which is a target: `as x`, `del x, y`.
_LIST_BINDERS = frozenset({cs.TS_PY_AS_PATTERN_TARGET, cs.TS_PY_DELETE_STATEMENT})
# A bare name under these is a `case` capture, not a value pattern.
_CAPTURE_PARENTS = frozenset({cs.TS_PY_CASE_PATTERN, cs.TS_PY_KEYWORD_PATTERN})
_IMPORTS = frozenset({cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT})
# A def's or class's body is a scope of its own; every other statement that
# holds blocks binds in the scope it sits in.
_OWN_SCOPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)
_SAME_SCOPE_COMPOUNDS = cs.PY_STATEMENT_CONTAINERS - _OWN_SCOPES - {cs.TS_PY_BLOCK}
_SHARED_DECLARATIONS = frozenset(
    {cs.TS_PY_GLOBAL_STATEMENT, cs.TS_PY_NONLOCAL_STATEMENT}
)

# Root-name bindings, as (name, spelling), that the enclosing scopes make
# anywhere: what a def's body may see when it is finally called.
type _Late = tuple[tuple[str, str], ...]


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
        for _bound, spelling in _import_bindings(statement)
        if spelling
    )


def _overload_stubs(root: Node) -> frozenset[int]:
    # Start bytes of the statements typing's `overload` decorates. Every block
    # is read in statement order from the bindings in force where it starts,
    # {root name: the spelling it gives, "" once it may be anything else}.
    spellings = overload_spellings(root)
    if not spellings:
        return frozenset()
    roots = {spelling.partition(cs.SEPARATOR_DOT)[0] for spelling in spellings}
    hits = _root_words(root, roots)
    shared = _declared_shared(root, roots, hits)
    spellings = frozenset(
        spelling
        for spelling in spellings
        if spelling.partition(cs.SEPARATOR_DOT)[0] not in shared
    )
    stubs: set[int] = set()
    pending: list[tuple[Node, dict[str, str], _Late]] = [
        (root, {}, _scope_late(root, roots, hits))
    ]
    while pending:
        block, bound, late = pending.pop()
        for statement in block.named_children:
            if _is_stub(statement, spellings, bound):
                stubs.add(statement.start_byte)
            pending.extend(_step(statement, bound, late, roots, hits))
    return frozenset(stubs)


def _step(
    statement: Node,
    bound: dict[str, str],
    late: _Late,
    roots: set[str],
    hits: list[int],
) -> list[tuple[Node, dict[str, str], _Late]]:
    # Move `bound` past `statement`, and return the blocks under it, each with
    # the bindings in force where it starts.
    if statement.type in _SAME_SCOPE_COMPOUNDS:
        # Any of its clauses may run, and a loop body runs again after its
        # end, so each of its blocks, and every statement after it, may see
        # any binding made anywhere in it (a `for` target included).
        _merge(bound, _scope_bindings(statement, hits), roots)
    inner = (
        [
            _block_start(block, statement, bound, late, roots, hits)
            for block in _blocks_in(statement)
        ]
        if statement.type in cs.PY_STATEMENT_CONTAINERS
        else []
    )
    if statement.type not in _SAME_SCOPE_COMPOUNDS:
        _rebind(bound, _bindings(statement, hits), roots)
    return inner


def _block_start(
    block: Node,
    statement: Node,
    bound: dict[str, str],
    late: _Late,
    roots: set[str],
    hits: list[int],
) -> tuple[Node, dict[str, str], _Late]:
    # `block` under `statement`, with the bindings it starts from and the late
    # ones a def inside it may see.
    start = dict(bound)
    if _function_of(statement) is None:
        # The same scope, or a class body: either runs right here.
        return block, start, late
    # A def's body runs when it is called, so any binding the enclosing
    # scopes make anywhere, after the def too, may be the one it reads.
    _merge(start, late, roots)
    return block, start, late + _scope_late(block, roots, hits)


def _scope_late(block: Node, roots: set[str], hits: list[int]) -> _Late:
    # Every root-name binding the scope whose body is `block` makes anywhere.
    return tuple(
        (name, spelling)
        for statement in block.named_children
        for name, spelling in _scope_bindings(statement, hits)
        if name is not None and name in roots
    )


def _declared_shared(root: Node, roots: set[str], hits: list[int]) -> set[str]:
    # Root names a `global` or `nonlocal` declares anywhere: the function
    # declaring one can rebind it whenever it is called.
    return {
        name
        for block in _blocks(root)
        for statement in block.named_children
        if statement.type in _SHARED_DECLARATIONS and _spans_word(statement, hits)
        for name in map(safe_decode_text, statement.named_children)
        if name in roots
    }


def _rebind(
    bound: dict[str, str],
    bindings: Iterable[tuple[str | None, str]],
    roots: set[str],
) -> None:
    # A statement that surely runs: each root name it binds means, from here
    # on, what its last binding there gives.
    for name, spelling in bindings:
        if name is not None and name in roots:
            bound[name] = spelling


def _merge(
    bound: dict[str, str],
    bindings: Iterable[tuple[str | None, str]],
    roots: set[str],
) -> None:
    # A statement whose bindings each may or may not run: a root name it binds
    # keeps a spelling only when the binding before it (if any) and every
    # binding of it there agree on that spelling; otherwise it is "", since
    # an uncertain `overload` must never fold a definition.
    meanings: dict[str, set[str]] = {}
    for name, spelling in bindings:
        if name is not None and name in roots:
            prior = {bound[name]} if name in bound else set()
            meanings.setdefault(name, prior).add(spelling)
    for name, spellings in meanings.items():
        bound[name] = spellings.pop() if len(spellings) == 1 else ""


def _scope_bindings(
    statement: Node, hits: list[int]
) -> Iterator[tuple[str | None, str]]:
    # Every binding `statement` makes in its scope: its own syntax's, and that
    # of every statement in its clauses' blocks, at any depth, except inside
    # a def's or class's body (another scope; its name and header still
    # count). A statement with no root-named word in it binds no root name;
    # an import is read anyway, since `from typing import *` names none.
    stack = [statement]
    while stack:
        node = stack.pop()
        yield from _bindings(node, hits)
        if node.type in _SAME_SCOPE_COMPOUNDS:
            stack.extend(
                inner
                for block in _blocks_in(node)
                for inner in block.named_children
                if inner.type in _IMPORTS or _spans_word(inner, hits)
            )


def _bindings(statement: Node, hits: list[int]) -> Iterator[tuple[str | None, str]]:
    # The names `statement` binds in its scope, read off its own syntax (not
    # the blocks under it), each with the spelling of typing's `overload` it
    # now gives ("" for none). Only an import can bind a root name without
    # spelling it (`from typing import *`).
    if statement.type in _IMPORTS:
        yield from _import_bindings(statement)
    elif _spans_word(statement, hits):
        for name in _names_bound(statement, hits):
            yield name, ""


def _names_bound(statement: Node, hits: list[int]) -> Iterator[str | None]:
    # Every name `statement`'s own syntax binds where it runs: its targets,
    # walruses (a comprehension's bind in the enclosing scope too; a
    # lambda's are read as well, which only ever keeps definitions apart), a
    # def's or class's name, `case` captures. The blocks under it are
    # statements of their own or another scope, so they are skipped; so is
    # any node with no root-named word in it, so this costs the root words,
    # not the node count.
    stack = [statement]
    while stack:
        node = stack.pop()
        yield from _bound_here(node)
        stack.extend(
            child
            for child in node.named_children
            if child.type != cs.TS_PY_BLOCK and _spans_word(child, hits)
        )


def _bound_here(node: Node) -> Iterator[str | None]:
    # The names `node` itself binds, by its own kind.
    if node.type in _LEFT_BINDERS:
        yield from _target_names(node.child_by_field_name(cs.FIELD_LEFT))
    elif node.type in _NAME_BINDERS:
        yield _name(node)
    elif node.type in _LIST_BINDERS:
        for target in node.named_children:
            yield from _target_names(target)
    elif node.type == cs.TS_PY_TYPE_ALIAS_STATEMENT:
        yield _type_alias_name(node)
    else:
        yield from _captures(node)


def _target_names(target: Node | None) -> Iterator[str | None]:
    # The names a target binds, unpacked to any depth; `x.y` and `x[i]` bind
    # none.
    if target is None:
        return
    if target.type == cs.TS_PY_IDENTIFIER:
        yield safe_decode_text(target)
    elif target.type in _TARGET_PATTERNS:
        for child in target.named_children:
            yield from _target_names(child)


def _captures(node: Node) -> Iterator[str | None]:
    # A `case` pattern's captures: a bare name (`case x`, `[x]`, `Foo(k=x)`),
    # a `*x` / `**x`, a `... as x`. A dotted `Color.RED` is a value pattern
    # and `Foo` in `Foo()` a class, so neither binds.
    if node.type == cs.TS_PY_DOTTED_NAME:
        parent = node.parent
        if (
            node.named_child_count == 1
            and parent is not None
            and parent.type in _CAPTURE_PARENTS
        ):
            yield safe_decode_text(node)
    elif node.type in (cs.TS_PY_SPLAT_PATTERN, cs.TS_PY_AS_PATTERN):
        # `as_pattern`'s first child is what it matches (or, in `with` and
        # `except`, the context or exception), never a name it binds.
        start = 1 if node.type == cs.TS_PY_AS_PATTERN else 0
        for child in node.named_children[start:]:
            if child.type == cs.TS_PY_IDENTIFIER:
                yield safe_decode_text(child)


def _type_alias_name(statement: Node) -> str | None:
    # `type X = ...` / `type X[T] = ...` binds `X`.
    node = statement.child_by_field_name(cs.FIELD_LEFT)
    while node is not None and node.type != cs.TS_PY_IDENTIFIER:
        node = node.named_child(0)
    return safe_decode_text(node)


def _root_words(root: Node, roots: set[str]) -> list[int]:
    # Sorted start bytes of every whole word in the module spelled as a root
    # name: only syntax spanning one can bind a root name. (A tree that kept
    # no source spells no typing import either, so it never gets here.)
    source = root.text or b""
    words = b"|".join(re.escape(name.encode()) for name in sorted(roots))
    pattern = re.compile(rb"(?<!\w)(?:" + words + rb")(?!\w)")
    return [root.start_byte + match.start() for match in pattern.finditer(source)]


def _spans_word(node: Node, hits: list[int]) -> bool:
    index = bisect_left(hits, node.start_byte)
    return index < len(hits) and hits[index] < node.end_byte


def _import_bindings(statement: Node) -> Iterator[tuple[str | None, str]]:
    # The names an import binds, each with the spelling of typing's
    # `overload` it now gives ("" for none); nothing for other statements.
    match statement.type:
        case cs.TS_PY_IMPORT_STATEMENT:
            yield from _module_import_bindings(statement)
        case cs.TS_PY_IMPORT_FROM_STATEMENT:
            yield from _from_import_bindings(statement)


def _module_import_bindings(statement: Node) -> Iterator[tuple[str | None, str]]:
    # `import typing` / `import typing as t` -> `typing.overload` / `t.overload`.
    # `import a.b` binds the package `a`; `import a.b as c` binds `c` to `a.b`.
    for imported in statement.children_by_field_name(cs.FIELD_NAME):
        module, alias = _imported(imported)
        if alias is None and module is not None:
            module = module.partition(cs.SEPARATOR_DOT)[0]
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
