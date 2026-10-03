"""Methods a Python class body binds to a second member name (issue #2620).

`run = _plain` or `__call__ = _fast if FAST else _slow` in a class body makes
the method a member under another name. The assignment is a use of the method
(an implementation reachable only through its alias is not dead), and a call
through the alias (`obj.run()`, `obj()`) runs the method it names.

The scan follows the class namespace in source order, the way Python builds
it: a right-hand name binds the method only if the body has already defined
it, and a later `def` or plain assignment of the alias name replaces the
alias. A statement under `if`/`try`/`with`/`for`/`match` may or may not run,
so it adds candidates to a name instead of replacing them: `run = _impl`
followed by a conditional `def run` leaves both as candidates.

The scan also reports the names the body binds, unconditionally, to a value
that is no method (`run = None`). Attribute lookup stops at the first class
whose body binds the name, so such a binding hides a base class's `run`.
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text

_TARGET_PATTERNS = frozenset(
    {cs.TS_PY_PATTERN_LIST, cs.TS_PY_TUPLE_PATTERN, cs.TS_PY_LIST_PATTERN}
)


class AliasReference(NamedTuple):
    """A name on an alias's right-hand side and the method it names."""

    site: Node
    method: Node


class ClassBodyAliases(NamedTuple):
    # Alias name -> the `function_definition` nodes it may hold once the body
    # has run; more than one when a conditional picks between methods, and
    # then the name's own `def` is one of them when it may survive.
    members: dict[str, tuple[Node, ...]]
    references: list[AliasReference]
    non_method: frozenset[str]


def scan_class_body_aliases(body: Node) -> ClassBodyAliases:
    scan = _ClassBodyScan()
    scan.block(body, conditional=False)
    return ClassBodyAliases(scan.members(), scan.references, frozenset(scan.values))


def _definition_name(node: Node) -> str | None:
    return safe_decode_text(node.child_by_field_name(cs.FIELD_NAME))


def _result_operands(value: Node) -> list[Node]:
    # The names the expression can evaluate to: either branch of a
    # conditional (never its truthiness-tested condition) and either operand
    # of `or` / `and`, through any parentheses.
    operands: list[Node] = []
    pending = [value]
    while pending:
        node = pending.pop()
        match node.type:
            case cs.TS_PY_IDENTIFIER:
                operands.append(node)
            case cs.TS_PY_PARENTHESIZED_EXPRESSION:
                pending.extend(node.named_children)
            case cs.TS_PY_BOOLEAN_OPERATOR:
                pending.extend(
                    operand
                    for operand in (
                        node.child_by_field_name(cs.TS_FIELD_LEFT),
                        node.child_by_field_name(cs.TS_FIELD_RIGHT),
                    )
                    if operand is not None
                )
            case cs.TS_PY_CONDITIONAL_EXPRESSION:
                # tree-sitter-python gives the operands no fields: they are
                # positional [body, condition, alternative].
                branches = node.named_children
                pending.extend(
                    [branches[0], branches[2]] if len(branches) == 3 else branches
                )
    return sorted(operands, key=lambda operand: operand.start_byte)


class _ClassBodyScan:
    def __init__(self) -> None:
        # What each name in the class namespace may hold: the methods it
        # names, or nothing for a value that is not a method.
        self._bound: dict[str, tuple[Node, ...]] = {}
        self._aliases: set[str] = set()
        # Names that certainly hold a value which is no method once the body
        # has run; a conditional binding never makes a name certain.
        self.values: set[str] = set()
        self.references: list[AliasReference] = []

    def members(self) -> dict[str, tuple[Node, ...]]:
        # A name whose only candidate is its own `def` is a method, not an
        # alias. Beside another method its `def` stays a candidate, so a call
        # through the name reaches both.
        members: dict[str, tuple[Node, ...]] = {}
        for name in sorted(self._aliases):
            methods = self._bound.get(name, ())
            if any(_definition_name(method) != name for method in methods):
                members[name] = methods
        return members

    def block(self, block: Node, conditional: bool) -> None:
        for statement in block.named_children:
            self._statement(statement, conditional)

    def _statement(self, node: Node, conditional: bool) -> None:
        match node.type:
            case cs.TS_PY_FUNCTION_DEFINITION:
                self._define(node, (node,), conditional)
            case cs.TS_PY_DECORATED_DEFINITION:
                if (inner := node.child_by_field_name(cs.FIELD_DEFINITION)) is None:
                    return
                methods = (inner,) if inner.type == cs.TS_PY_FUNCTION_DEFINITION else ()
                self._define(inner, methods, conditional)
            case cs.TS_PY_CLASS_DEFINITION:
                # A nested class is a node of its own, which lookup finds
                # before any value, so it is recorded as neither.
                self._define(node, (), conditional, is_value=False)
            case cs.TS_PY_EXPRESSION_STATEMENT:
                for child in node.named_children:
                    if child.type == cs.TS_PY_ASSIGNMENT:
                        self._assignment(child, conditional)
            case _:
                self._compound(node)

    def _compound(self, node: Node) -> None:
        for child in node.named_children:
            if child.type == cs.TS_PY_BLOCK:
                self.block(child, conditional=True)
            elif child.type.endswith(cs.TS_PY_CLAUSE_SUFFIX):
                self._compound(child)

    def _define(
        self,
        definition: Node,
        methods: tuple[Node, ...],
        conditional: bool,
        is_value: bool = True,
    ) -> None:
        if name := _definition_name(definition):
            self._bind(name, methods, conditional, is_alias=False, is_value=is_value)

    def _assignment(self, node: Node, conditional: bool) -> None:
        # `a = b = _impl` nests the second assignment as the first's value.
        names: list[str] = []
        unpacked: list[str] = []
        value: Node | None = node
        while value is not None and value.type == cs.TS_PY_ASSIGNMENT:
            left = value.child_by_field_name(cs.TS_FIELD_LEFT)
            if left is not None and left.type == cs.TS_PY_IDENTIFIER:
                if name := safe_decode_text(left):
                    names.append(name)
            elif left is not None and left.type in _TARGET_PATTERNS:
                unpacked.extend(
                    name
                    for child in left.named_children
                    if child.type == cs.TS_PY_IDENTIFIER
                    and (name := safe_decode_text(child))
                )
            value = value.child_by_field_name(cs.TS_FIELD_RIGHT)
        if value is None:
            # An annotation alone (`x: int`) binds nothing.
            return
        methods: list[Node] = []
        for operand in _result_operands(value):
            for method in self._bound.get(safe_decode_text(operand) or "", ()):
                self.references.append(AliasReference(operand, method))
                if method not in methods:
                    methods.append(method)
        for name in names:
            self._bind(name, tuple(methods), conditional, is_alias=bool(methods))
        for name in unpacked:
            self._bind(name, (), conditional, is_alias=False)

    def _bind(
        self,
        name: str,
        methods: tuple[Node, ...],
        conditional: bool,
        is_alias: bool,
        is_value: bool = True,
    ) -> None:
        if conditional:
            held = self._bound.get(name, ())
            self._bound[name] = held + tuple(m for m in methods if m not in held)
            if is_alias:
                self._aliases.add(name)
            if methods:
                self.values.discard(name)
            return
        self._bound[name] = methods
        if is_alias:
            self._aliases.add(name)
        else:
            self._aliases.discard(name)
        if is_value and not methods:
            self.values.add(name)
        else:
            self.values.discard(name)
