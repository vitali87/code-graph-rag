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


def annotates_receiver(call: Node, receiver: str) -> bool:
    """Whether the def enclosing `call` declares `receiver` with an annotation.

    `def typed(self: Other)` types the receiver from the def's entry, so that
    type holds for a call no rebinding precedes, unlike the one a body
    `self = Other()` puts in the method-wide map.
    """
    return receiver_annotation(call, receiver) is not None


def receiver_annotation(call: Node, receiver: str) -> str | None:
    """The class name the annotation of `receiver` in the def enclosing
    `call` spells, unquoted and without its arguments or module (`Other` for
    `self: "pkg.Other"`), or None when the parameter has none."""
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
            text = safe_decode_text(param.child_by_field_name(cs.TS_FIELD_TYPE))
            spelled = (text or "").strip().strip(cs.PY_ANNOTATION_QUOTES)
            head = spelled.split(cs.PY_ANNOTATION_ARGS_OPEN, 1)[0]
            return head.rsplit(cs.SEPARATOR_DOT, 1)[-1].strip()
    return None


def instance_attribute_names(class_node: Node) -> frozenset[str]:
    """The attributes a class's methods assign on their receiver
    (`self.m = callback`): an instance attribute is found before the class's
    method of that name, so `self.m()` then calls the value (CodeRabbit, PR
    #2908). A class nested in this one assigns its own."""
    names: set[str] = set()
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    stack = list(body.children) if body is not None else []
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CLASS_DEFINITION:
            continue
        if node.type in (cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT):
            target = node.child_by_field_name(cs.TS_FIELD_LEFT)
            receiver = (
                target.child_by_field_name(cs.FIELD_OBJECT)
                if target is not None and target.type == cs.TS_PY_ATTRIBUTE
                else None
            )
            attribute = (
                target.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
                if target is not None and receiver is not None
                else None
            )
            if (
                receiver is not None
                and safe_decode_text(receiver) in cs.PY_RECEIVER_NAMES
                and (name := safe_decode_text(attribute))
            ):
                names.add(name)
        stack.extend(node.children)
    return frozenset(names)


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
