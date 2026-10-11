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
is a different function and keeps its own node.

A class body spells its overloads `method_signature`, which the function
query never captures as a member there (issue #2524), and an interface's or
abstract class's repeated member registers once
(`js_ts.utils.repeats_member_signature`); `rename` reads the signatures of
all of these through `overload_signatures`, so what it rewrites is exactly
what the graph folded.
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
_SIGNATURE_ONLY = frozenset({cs.TS_FUNCTION_SIGNATURE})
_BLOCK_FUNCTIONS = _IMPLEMENTATIONS | _SIGNATURE_ONLY
# What may stand between two overloads of one member: a comment, and in a
# class body the implementation's decorators.
_BETWEEN_OVERLOADS = frozenset({cs.TS_COMMENT, cs.TS_DECORATOR})


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
    implementation is every same-named signature of its scope.
    """
    name = _name(declaration)
    if name is None:
        return []
    match declaration.type:
        case cs.TS_FUNCTION_SIGNATURE:
            block = _statement_of(declaration).parent
            members = [] if block is None else _declarations(block)
            return _same_named(declaration, name, members, _SIGNATURE_ONLY)
        case cs.TS_METHOD_SIGNATURE | cs.TS_ABSTRACT_METHOD_SIGNATURE:
            body = declaration.parent
            members = [] if body is None else body.named_children
            return _same_named(declaration, name, members, cs.TS_BODILESS_METHOD_TYPES)
        case _:
            return implementation_signatures(declaration)


def implementation_signatures(declaration: Node) -> list[Node]:
    """The signatures written directly above the implementation `declaration`,
    in source order; none for any other node.

    Reads only the run above it, so a caller may ask of every function.
    """
    name = _name(declaration)
    if name is None:
        return []
    match declaration.type:
        case cs.TS_FUNCTION_DECLARATION | cs.TS_GENERATOR_FUNCTION_DECLARATION:
            return _leading(_statement_of(declaration), cs.TS_FUNCTION_SIGNATURE, name)
        case cs.TS_METHOD_DEFINITION:
            return _leading(declaration, cs.TS_METHOD_SIGNATURE, name)
        case _:
            return []


def _same_named(
    declaration: Node, name: str, members: list[Node], kinds: frozenset[str]
) -> list[Node]:
    return [
        member
        for member in members
        if member.type in kinds
        and member.start_byte != declaration.start_byte
        and _name(member) == name
    ]


def _folded(block: Node) -> Iterator[int]:
    declarations = [
        (node, name)
        for node in _declarations(block)
        if node.type in _BLOCK_FUNCTIONS and (name := _name(node)) is not None
    ]
    implemented = {name for node, name in declarations if node.type in _IMPLEMENTATIONS}
    seen: set[str] = set()
    for node, name in declarations:
        if node.type != cs.TS_FUNCTION_SIGNATURE:
            continue
        if name in implemented or name in seen:
            yield node.start_byte
        seen.add(name)


def _leading(statement: Node, signature_type: str, name: str) -> list[Node]:
    # Walks up from the implementation through same-named signatures, so a
    # different declaration (or a same-named one of another kind) ends it.
    found: list[Node] = []
    sibling = statement.prev_named_sibling
    while sibling is not None:
        if sibling.type not in _BETWEEN_OVERLOADS:
            declaration = _unwrapped(sibling)
            if declaration.type != signature_type or _name(declaration) != name:
                break
            found.append(declaration)
        sibling = sibling.prev_named_sibling
    found.reverse()
    return found


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


def _name(declaration: Node) -> str | None:
    return safe_decode_text(declaration.child_by_field_name(cs.FIELD_NAME))
