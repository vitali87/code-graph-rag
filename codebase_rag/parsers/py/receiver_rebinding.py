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

from collections.abc import Iterator
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from .ast_analyzer import _parameter_names, _walrus_nodes_under, _walrus_parts

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
            return _annotated_class(text or "")
    return None


def _annotated_class(text: str) -> str:
    # The class an annotation names, without quotes or arguments and keeping
    # a qualifier: `type["A"]` / `typing.Type[A]` name the class `A` itself,
    # as a classmethod's `cls` holds it (CodeRabbit, PR #2908).
    spelled = text.strip().strip(cs.PY_ANNOTATION_QUOTES).strip()
    head, _, rest = spelled.partition(cs.PY_ANNOTATION_ARGS_OPEN)
    head = head.strip()
    if rest and head.rsplit(cs.SEPARATOR_DOT, 1)[-1] in cs.PY_CLASS_OBJECT_TYPES:
        return _annotated_class(rest.rsplit(cs.PY_ANNOTATION_ARGS_CLOSE, 1)[0])
    return head


def instance_attribute_names(class_node: Node) -> frozenset[str]:
    """The attributes a class's methods assign on their bound receiver,
    whatever it is named (`self.m = callback`, `this.m = callback`), by an
    assignment, a `for` target or a `with ... as` target: an instance
    attribute is found before the class's method of that name, so `self.m()`
    then calls the value (CodeRabbit, Greptile, PR #2908). A class nested in
    this one assigns its own."""
    names: set[str] = set()
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    for method in _methods(body) if body is not None else ():
        if (receiver := _receiver_parameter(method)) is None:
            continue
        stack = list(method.children)
        while stack:
            node = stack.pop()
            if node.type == cs.TS_PY_CLASS_DEFINITION:
                continue
            for target in _written_targets(node):
                names.update(_receiver_attributes(target, receiver))
            stack.extend(node.children)
    return frozenset(names)


def bound_receiver(caller: Node) -> str | None:
    """The name `caller` reaches its class's instance (or, in a
    classmethod, the class) by: the first parameter of the method around
    it, unless that is a static method, which binds none, or a def or
    lambda in between takes a parameter of the name (`def run(self, cls)`
    binds only `self`; Greptile, PR #2908)."""
    hidden: set[str] = set()
    node: Node | None = caller
    while node is not None and node.type != cs.TS_PY_CLASS_DEFINITION:
        if node.type == cs.TS_PY_FUNCTION_DEFINITION and _is_method(node):
            first = _receiver_parameter(node)
            return None if first is None or first in hidden else first
        if node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA):
            hidden.update(_parameter_names(node))
        node = node.parent
    return None


def receiver_hidden_spans(caller: Node, receiver: str) -> tuple[tuple[int, int], ...]:
    """The byte spans inside `caller` where `receiver` names something
    else: the body of a nested def or lambda taking a parameter of that name
    (`(lambda self: self.name)(None)`) or of a lambda binding it with `:=`,
    and a comprehension whose `for` binds it. A nested class holds its
    own."""
    spans: list[tuple[int, int]] = []
    stack = list(caller.named_children)
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CLASS_DEFINITION:
            continue
        if (
            node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA)
            and (body := node.child_by_field_name(cs.FIELD_BODY)) is not None
            and _scope_binds(node, body, receiver)
        ):
            spans.append((body.start_byte, body.end_byte))
            continue
        if node.type in cs.PY_COMPREHENSION_TYPES and (
            hidden := _comprehension_hides(node, receiver)
        ):
            spans.extend(hidden)
        stack.extend(node.named_children)
    return tuple(sorted(spans))


def _scope_binds(scope: Node, body: Node, receiver: str) -> bool:
    # A def or lambda taking a parameter of the name, or a lambda whose body
    # binds it with `:=` (`lambda: ((self := make()), self.m())`), which makes
    # it the lambda's own name throughout that body (Greptile, PR #2908). A
    # nested def is its own caller, whose scope facts read its body's binds.
    if receiver in set(_parameter_names(scope)):
        return True
    return scope.type == cs.TS_PY_LAMBDA and any(
        _walrus_parts(walrus)[0] == receiver for walrus in _walrus_nodes_under(body)
    )


def _comprehension_hides(comprehension: Node, receiver: str) -> list[tuple[int, int]]:
    # `[self.m() for self in items]` binds `self` to each item for the whole
    # comprehension but its first iterable, which runs in the enclosing
    # scope (Greptile, PR #2908).
    clauses = [
        child
        for child in comprehension.named_children
        if child.type == cs.TS_PY_FOR_IN_CLAUSE
    ]
    if not any(receiver in _target_names(clause) for clause in clauses):
        return []
    first = clauses[0].child_by_field_name(cs.FIELD_RIGHT)
    if first is None:
        return [(comprehension.start_byte, comprehension.end_byte)]
    return [
        (comprehension.start_byte, first.start_byte),
        (first.end_byte, comprehension.end_byte),
    ]


def _target_names(clause: Node) -> set[str]:
    # Every name a `for ... in` clause's target binds (`self`, `a, self`).
    # An attribute or subscript target (`for self.slot in items`) writes
    # into an object and binds no name, so the walk stops there (Greptile,
    # PR #2908).
    target = clause.child_by_field_name(cs.FIELD_LEFT)
    names: set[str] = set()
    stack = [target] if target is not None else []
    while stack:
        node = stack.pop()
        if node.type in (cs.TS_PY_ATTRIBUTE, cs.TS_PY_SUBSCRIPT):
            continue
        if node.type == cs.TS_PY_IDENTIFIER and (name := safe_decode_text(node)):
            names.add(name)
        stack.extend(node.named_children)
    return names


def _methods(body: Node) -> Iterator[Node]:
    # The defs of a class body, including those under an `if`/`try`/`with`
    # block in it (Greptile, PR #2908); a nested class holds its own.
    stack = list(body.named_children)
    while stack:
        child = stack.pop()
        if child.type == cs.TS_PY_DECORATED_DEFINITION:
            child = child.child_by_field_name(cs.FIELD_DEFINITION) or child
        if child.type == cs.TS_PY_FUNCTION_DEFINITION:
            yield child
        elif child.type != cs.TS_PY_CLASS_DEFINITION:
            stack.extend(child.named_children)


def _is_method(function: Node) -> bool:
    parent = function.parent
    if parent is not None and parent.type == cs.TS_PY_DECORATED_DEFINITION:
        parent = parent.parent
    return (
        parent is not None
        and parent.type == cs.TS_PY_BLOCK
        and parent.parent is not None
        and parent.parent.type == cs.TS_PY_CLASS_DEFINITION
    )


def _receiver_parameter(method: Node) -> str | None:
    # A method's first parameter, or None for a static method.
    decorated = method.parent
    if decorated is not None and decorated.type == cs.TS_PY_DECORATED_DEFINITION:
        for decorator in decorated.named_children:
            text = (safe_decode_text(decorator) or "").lstrip(cs.DECORATOR_AT)
            head = text.split(cs.CHAR_PAREN_OPEN, 1)[0].strip()
            if (
                decorator.type == cs.TS_PY_DECORATOR
                and head.rsplit(cs.SEPARATOR_DOT, 1)[-1] in cs.STATIC_DECORATORS
            ):
                return None
    return next(iter(_parameter_names(method)), None)


def _written_targets(node: Node) -> Iterator[Node]:
    # The targets one statement writes: an assignment's, a `for`'s, and a
    # `with ... as` item's.
    if node.type in (
        cs.TS_PY_ASSIGNMENT,
        cs.TS_PY_AUGMENTED_ASSIGNMENT,
        cs.TS_PY_FOR_STATEMENT,
    ):
        if (target := node.child_by_field_name(cs.TS_FIELD_LEFT)) is not None:
            yield target
    elif node.type == cs.TS_PY_AS_PATTERN_TARGET:
        yield from node.named_children


def _receiver_attributes(target: Node, receiver: str) -> Iterator[str]:
    # The receiver attributes an assignment target writes, through tuple,
    # list and parenthesised targets (`self.m, self.x = cb, 0`; CodeRabbit,
    # PR #2908). An attribute's own object is a read, never a write.
    pending = [target]
    while pending:
        node = pending.pop()
        if node.type == cs.TS_PY_ATTRIBUTE:
            obj = node.child_by_field_name(cs.FIELD_OBJECT)
            if (
                obj is not None
                and safe_decode_text(obj) == receiver
                and (
                    name := safe_decode_text(
                        node.child_by_field_name(cs.TS_PY_FIELD_ATTRIBUTE)
                    )
                )
            ):
                yield name
        elif node.type in cs.PY_ATTRIBUTE_TARGET_CONTAINERS:
            pending.extend(node.named_children)


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
