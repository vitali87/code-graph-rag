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
    """The receivers (`self`, `cls`) a `self = Other()` in the def's own body
    rebinds: only a call through one of them needs `receiver_rebinding`."""
    return frozenset(
        receiver
        for receiver in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
        if _assignments_to(function, receiver)
    )


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
