"""TypeScript overload signatures, folded into the function they declare.

An overloaded TypeScript function is one function: bodiless signatures, then
the implementation that every call runs. Registered as definitions of their
own, the signatures took the plain qualified name and pushed the
implementation to `name@line`, so dead code reported one function once per
signature, every call fanned out to all of them as `overload`, and `rename`
rewrote one declaration and left the rest under the old name, which `tsc`
rejects (TS2389, issue #2457). A signature is therefore no function of its
own, as Python's `@overload` stubs are not (issue #2590): beside a same-named
implementation in its block it is dropped, and a set with no implementation
(an ambient one, in a `.d.ts`) keeps its first signature as the function.

Folding is per block, the scope TypeScript merges declarations in: a
same-named function in another function body, namespace or `declare module`
is a different function and keeps its own node. Within a class body a
`static` member and an instance member of one name live in different
declaration spaces too, so every set here is keyed by name and staticness:
an abstract `f` beside an overloaded `static f` is two functions, and
renaming one must not touch the other's signatures (bot review, PR #3357).

A class body spells its overloads `method_signature`, which the function
query never captures as a member there (issue #2524), and an interface's or
abstract class's repeated member registers once
(`js_ts.utils.repeats_member_signature`, by name alone, which is enough
there: an abstract member cannot be `static` (TS1243) and an interface has
no static side); `rename` reads the signatures of all of these through
`overload_signatures`, so what it rewrites is exactly what the graph folded.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text

# Each wraps one declaration: `export ...`, `declare ...`, `export declare ...`.
_WRAPPERS = frozenset({cs.TS_EXPORT_STATEMENT, cs.TS_AMBIENT_DECLARATION})
_IMPLEMENTATIONS = frozenset(
    {cs.TS_FUNCTION_DECLARATION, cs.TS_GENERATOR_FUNCTION_DECLARATION}
)
_BLOCK_FUNCTIONS = _IMPLEMENTATIONS | {cs.TS_FUNCTION_SIGNATURE}
# What may stand between two overloads of one member: a comment, and in a
# class body the implementation's decorators.
_BETWEEN_OVERLOADS = frozenset({cs.TS_COMMENT, cs.TS_DECORATOR})

# What makes two declarations one function's: the name and whether it is
# `static`.
type _Key = tuple[str | None, bool]


def folded_overload_signatures(functions: Iterable[Node]) -> frozenset[int]:
    """Start bytes of the `function_signature`s among `functions` that fold
    into another declaration of their function.

    Each block holding a signature is read once, however many it holds.
    """
    blocks: dict[int, Node] = {}
    for node in functions:
        if node.type == cs.TS_FUNCTION_SIGNATURE:
            block = _statement_of(node).parent
            if block is not None:
                blocks.setdefault(block.id, block)
    return frozenset(start for block in blocks.values() for start in _folded(block))


def overload_signatures(declaration: Node) -> list[Node]:
    """The other declarations of the function `declaration` registers as.

    An implementation's signatures are the ones written directly above it,
    since TypeScript accepts no other placement (TS2391). A set without an
    implementation is every signature of its scope with the same key and
    node type: an abstract set is all abstract (TS2512), and an abstract
    getter and setter are one property's.
    """
    match declaration.type:
        case cs.TS_FUNCTION_SIGNATURE:
            block = _statement_of(declaration).parent
            members = [] if block is None else _declarations(block)
        case cs.TS_METHOD_SIGNATURE | cs.TS_ABSTRACT_METHOD_SIGNATURE:
            body = declaration.parent
            members = [] if body is None else body.named_children
        case _:
            return implementation_signatures(declaration)
    key = _key(declaration)
    if key[0] is None:
        return []
    return [
        member
        for member in members
        if member.type == declaration.type
        and member.start_byte != declaration.start_byte
        and _key(member) == key
    ]


def implementation_signatures(declaration: Node) -> list[Node]:
    """The signatures written directly above the implementation `declaration`,
    in source order; none for any other node.

    Reads only the run above it, so a caller may ask of every function.
    """
    match declaration.type:
        case cs.TS_FUNCTION_DECLARATION | cs.TS_GENERATOR_FUNCTION_DECLARATION:
            statement = _statement_of(declaration)
            signature_type = cs.TS_FUNCTION_SIGNATURE
        case cs.TS_METHOD_DEFINITION:
            statement = declaration
            signature_type = cs.TS_METHOD_SIGNATURE
        case _:
            return []
    key = _key(declaration)
    if key[0] is None:
        return []
    found: list[Node] = []
    sibling = statement.prev_named_sibling
    # A different declaration, or one of another key, ends the run.
    while sibling is not None:
        if sibling.type not in _BETWEEN_OVERLOADS:
            signature = _unwrapped(sibling)
            if signature.type != signature_type or _key(signature) != key:
                break
            found.append(signature)
        sibling = sibling.prev_named_sibling
    found.reverse()
    return found


def _folded(block: Node) -> Iterator[int]:
    declarations = [
        (node, key)
        for node in _declarations(block)
        if node.type in _BLOCK_FUNCTIONS and (key := _key(node))[0] is not None
    ]
    implemented = {key for node, key in declarations if node.type in _IMPLEMENTATIONS}
    seen: set[_Key] = set()
    for node, key in declarations:
        if node.type != cs.TS_FUNCTION_SIGNATURE:
            continue
        if key in implemented or key in seen:
            yield node.start_byte
        seen.add(key)


def _declarations(block: Node) -> list[Node]:
    return [_unwrapped(statement) for statement in block.named_children]


def _statement_of(declaration: Node) -> Node:
    # The statement a declaration is written as, its wrappers included.
    while declaration.parent is not None and declaration.parent.type in _WRAPPERS:
        declaration = declaration.parent
    return declaration


def _unwrapped(statement: Node) -> Node:
    while statement.type in _WRAPPERS:
        inner = next(
            (
                child
                for child in statement.named_children
                if child.type not in _BETWEEN_OVERLOADS
            ),
            None,
        )
        if inner is None:
            break
        statement = inner
    return statement


def _key(declaration: Node) -> _Key:
    # `static` is an anonymous token among the member's own children.
    return (
        safe_decode_text(declaration.child_by_field_name(cs.FIELD_NAME)),
        any(child.type == cs.TS_STATIC for child in declaration.children),
    )
