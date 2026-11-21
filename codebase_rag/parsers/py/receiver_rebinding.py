"""Whether a method's `self = Other()` can run before a given call (issue #2620).

The type map is method-wide: one `self = Other()` anywhere in a method types
`self` as `Other` for every call in it. A call that runs before that
assignment still runs on the instance, through its class's own member (an
alias such as `run = _own` included) and any subclass override. So a call
counts the rebinding only when the assignment can run first:

- on every path, when the assignment's statement sits in a block that also
  holds the call, earlier in it (the call runs on `Other` only);
- on some path, when it precedes the call in a branch that may not run, or
  follows it inside a loop that holds both (the call may run on either).
"""

from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .ast_analyzer import _bindings_in, _identifiers_in
from .forward_refs import unquote_forward_refs

# Scopes whose assignments bind their own locals, not the method's receiver.
_NESTED_SCOPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA, cs.TS_PY_CLASS_DEFINITION}
)
_LOOPS = frozenset({cs.TS_PY_FOR_STATEMENT, cs.TS_PY_WHILE_STATEMENT})


class ReceiverRebinding(NamedTuple):
    always: bool
    sometimes: bool


def receiver_rebinding(call: Node, receiver: str) -> ReceiverRebinding:
    function = _enclosing_function(call)
    if function is None:
        return ReceiverRebinding(always=False, sometimes=False)
    always = sometimes = False
    for assignment in _assignments_to(function, receiver):
        if assignment.end_byte <= call.start_byte:
            sometimes = True
            always = always or _contains(_statement_block(assignment), call)
        elif _shares_a_loop(assignment, call, function):
            sometimes = True
    return ReceiverRebinding(always=always, sometimes=sometimes)


def rebound_receivers(function: Node) -> frozenset[str]:
    """The receivers (`self`, `cls`) the def's own body rebinds, by a
    `self = Other()` or any other binding: only a call through one of them
    needs `receiver_bound_otherwise` and `receiver_rebinding`."""
    bound = {safe_decode_text(name) for _binder, name in _bindings_in(function)}
    return frozenset(
        receiver
        for receiver in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
        if receiver in bound
    )


def receiver_bound_otherwise(function: Node, receiver: str) -> bool:
    """Whether the def's own body binds `receiver` other than by a plain
    `self = value`: a `for` or `with` target, an unpacking, a walrus, an
    `except` alias, an import, a `case` capture. `receiver_rebinding` orders
    only plain assignments against a call, so the receiver may then hold
    anything at any call (Greptile review on PR #2951)."""
    for binder, name in _bindings_in(function):
        if safe_decode_text(name) != receiver:
            continue
        left = (
            binder.child_by_field_name(cs.TS_FIELD_LEFT)
            if binder.type == cs.TS_PY_ASSIGNMENT
            else None
        )
        if left is None or left.id != name.id:
            return True
    return False


def receiver_annotation(call: Node, receiver: str) -> str | None:
    """The annotation the def enclosing `call` declares `receiver` with.

    `def typed(self: Other)` types the receiver from the def's entry, so a
    call no rebinding precedes runs on `Other`, whatever later type a body
    `self = Later()` puts in the method-wide map. The text is the one the
    type map records for the parameter.
    """
    function = _enclosing_function(call)
    params = (
        function.child_by_field_name(cs.TS_FIELD_PARAMETERS)
        if function is not None
        else None
    )
    if params is None:
        return None
    for param in params.named_children:
        if param.type == cs.TS_PY_TYPED_PARAMETER:
            name = next(
                (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
            )
        elif param.type == cs.TS_PY_TYPED_DEFAULT_PARAMETER:
            name = param.child_by_field_name(cs.TS_FIELD_NAME)
        else:
            continue
        if name is not None and safe_decode_text(name) == receiver:
            annotation = safe_decode_text(param.child_by_field_name(cs.TS_FIELD_TYPE))
            return unquote_forward_refs(annotation) if annotation else None
    return None


def receiver_is_bound(call: Node, receiver: str) -> bool:
    """Whether `receiver` at `call` holds what a method binds: the instance
    a plain method's first parameter receives, or a `@classmethod`'s class.

    A `@staticmethod` binds nothing, and a nested def or lambda that takes a
    parameter of the name holds whatever its own caller passes, so neither
    names the enclosing class (Greptile review on PR #2951). A closure that
    does not take the name sees its method's.
    """
    current = call.parent
    while current is not None and current.type != cs.TS_PY_CLASS_DEFINITION:
        # `[self.m() for self in items]`: the comprehension's own variable.
        if (
            current.type in cs.PY_COMPREHENSION_TYPES
            and receiver in _comprehension_names(current)
        ):
            return False
        if current.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA):
            names = _parameter_names(current)
            if _is_method(current):
                return names[:1] == [receiver] and not _is_static(current)
            if receiver in names:
                return False
        current = current.parent
    return False


def _comprehension_names(comprehension: Node) -> set[str | None]:
    return {
        safe_decode_text(name)
        for clause in comprehension.named_children
        if clause.type == cs.TS_PY_FOR_IN_CLAUSE
        and (left := clause.child_by_field_name(cs.TS_FIELD_LEFT)) is not None
        for name in _identifiers_in(left)
    }


def _parameter_names(function: Node) -> list[str]:
    params = function.child_by_field_name(cs.TS_FIELD_PARAMETERS)
    names: list[str] = []
    for param in params.named_children if params is not None else ():
        name = (
            param
            if param.type == cs.TS_PY_IDENTIFIER
            else param.child_by_field_name(cs.TS_FIELD_NAME)
            or next(
                (c for c in param.named_children if c.type == cs.TS_PY_IDENTIFIER),
                None,
            )
        )
        if name is not None and (text := safe_decode_text(name)):
            names.append(text)
    return names


def _definition_holder(function: Node) -> Node | None:
    # What holds a def: its `decorated_definition` when it has decorators.
    parent = function.parent
    if parent is not None and parent.type == cs.TS_PY_DECORATED_DEFINITION:
        return parent
    return function


def _is_method(function: Node) -> bool:
    if function.type != cs.TS_PY_FUNCTION_DEFINITION:
        return False
    holder = _definition_holder(function)
    block = holder.parent if holder is not None else None
    return (
        block is not None
        and block.type == cs.TS_PY_BLOCK
        and block.parent is not None
        and block.parent.type == cs.TS_PY_CLASS_DEFINITION
    )


def _is_static(function: Node) -> bool:
    holder = _definition_holder(function)
    if holder is None or holder.type != cs.TS_PY_DECORATED_DEFINITION:
        return False
    return any(
        decorator.type == cs.TS_PY_DECORATOR
        and (text := safe_decode_text(decorator.named_children[0]))
        and text.rpartition(cs.SEPARATOR_DOT)[2] == cs.PY_STATICMETHOD
        for decorator in holder.children
        if decorator.named_children
    )


def _enclosing_function(node: Node) -> Node | None:
    current = node.parent
    while current is not None:
        if current.type == cs.TS_PY_FUNCTION_DEFINITION:
            return current
        if current.type == cs.TS_PY_CLASS_DEFINITION:
            return None
        current = current.parent
    return None


def _assignments_to(function: Node, receiver: str) -> list[Node]:
    found: list[Node] = []
    body = function.child_by_field_name(cs.FIELD_BODY)
    pending = list(body.named_children) if body is not None else []
    while pending:
        node = pending.pop()
        if node.type in _NESTED_SCOPES:
            continue
        if node.type == cs.TS_PY_ASSIGNMENT:
            left = node.child_by_field_name(cs.TS_FIELD_LEFT)
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and safe_decode_text(left) == receiver
            ):
                found.append(node)
        pending.extend(node.named_children)
    return found


def _statement_block(assignment: Node) -> Node | None:
    # `a = self = X` nests the inner assignment, so climb to the statement.
    current = assignment
    while current.parent is not None and current.parent.type != cs.TS_PY_BLOCK:
        current = current.parent
    return current.parent


def _contains(outer: Node | None, inner: Node) -> bool:
    return (
        outer is not None
        and outer.start_byte <= inner.start_byte
        and inner.end_byte <= outer.end_byte
    )


def _shares_a_loop(assignment: Node, call: Node, function: Node) -> bool:
    current = call.parent
    while current is not None and current != function:
        if current.type in _LOOPS and _contains(current, assignment):
            return True
        current = current.parent
    return False
