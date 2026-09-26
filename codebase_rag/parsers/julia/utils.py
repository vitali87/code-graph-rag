"""Name extraction for tree-sitter-julia nodes.

Julia's grammar has no uniform `name` field on its definition nodes, so
every site that names a Julia function routes through these helpers or the
definition and call passes mint different qns for the same node.
"""

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def _first_named(node: Node | None) -> Node | None:
    if node is None:
        return None
    named = node.named_children
    return named[0] if named else None


def _last_named(node: Node | None) -> Node | None:
    if node is None:
        return None
    named = node.named_children
    return named[-1] if named else None


def _single_line_or_none(text: str | None) -> str | None:
    # A multi-line span is a grammar error-recovery span standing in for a
    # name, not a name (no Julia name spans lines, issue #1882 review).
    if text is None or "\n" in text:
        return None
    return text


def _unwrap_head(head: Node | None) -> Node | None:
    """Unwrap `where`/return-type decoration; the callee is the first
    named child in every wrapper."""
    while head is not None and head.type in (
        cs.TS_JULIA_WHERE_EXPRESSION,
        cs.TS_JULIA_TYPED_EXPRESSION,
    ):
        head = _first_named(head)
    return head


def _dotted_text(node: Node) -> str | None:
    """A dotted callee's text; interpolated segments (`LogExpFunctions.$(f)`) reduce
    to the interpolated expression's name, and a segment that reduces to
    nothing keeps the raw text."""
    text = safe_decode_text(node)
    if text is None:
        return None
    if "$" not in text:
        return _single_line_or_none(text)
    parts: list[str] = []
    for child in node.named_children:
        if child.type == cs.TS_JULIA_INTERPOLATION_EXPRESSION:
            inner = julia_callee_name(_first_named(child))
            if not inner:
                return _single_line_or_none(text)
        else:
            inner = safe_decode_text(child)
            if inner is not None and "\n" in inner:
                # A segment that spans lines is error-recovered, not a
                # part: refuse the whole name.
                return _single_line_or_none(text)
        if inner:
            parts.append(inner)
    return cs.SEPARATOR_DOT.join(parts) if parts else _single_line_or_none(text)


def julia_callee_name(node: Node | None) -> str | None:
    """Reduce any call-position expression to the name it stands for.

    Reducing BOTH definition heads and call sites through this one helper
    is what keeps definition qns and call edges agreeing (a parameter list,
    a return type, or an interpolated marker in the name would mint an
    unaddressable qn).
    """
    if node is None or not node.text:
        return None
    node_type = node.type
    if node_type in (
        cs.TS_JULIA_IDENTIFIER,
        cs.TS_JULIA_FIELD_EXPRESSION,
        cs.TS_JULIA_SCOPED_IDENTIFIER,
    ):
        return _dotted_text(node)
    if node_type in (
        cs.TS_JULIA_PARENTHESIZED_EXPRESSION,
        cs.TS_JULIA_PARAMETRIZED_TYPE_EXPRESSION,
        cs.TS_JULIA_INTERPOLATION_EXPRESSION,
        cs.TS_JULIA_CALL_EXPRESSION,
    ):
        # The real callee is the first named child; peel to a fixpoint.
        return julia_callee_name(_first_named(node))
    if node_type == cs.TS_JULIA_TYPED_EXPRESSION:
        # `(D::Differential)(x)`: a closure method on a type — the type is
        # the callee's name (the value side is the receiver, not a name).
        type_node = node.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None:
            named = node.named_children
            type_node = named[-1] if named else None
        return julia_callee_name(type_node)
    if node_type == cs.TS_JULIA_UNARY_TYPED_EXPRESSION:
        # `(::T)` — an anonymous-type closure head; the type side carries
        # the best available stand-in name.
        return julia_callee_name(_first_named(node))
    # Anything else (a bare quoted operator, a string callee): the whole
    # text is the best available name.
    return _single_line_or_none(safe_decode_text(node))


def _callee_name(head: Node) -> str | None:
    """The callee name of a call-shaped head, dotted for qualified heads.

    `add` -> `add`; `Base.show` -> the full dotted
    text; `Arr{T, N}(ex)` -> `Arr` (the constructor's type, parameter list
    stripped) — see julia_callee_name for the full reduction.
    """
    return julia_callee_name(_first_named(head))


def julia_function_head_name(node: Node) -> str | None:
    """The name a Julia function-defining node declares, else None.

    Handles `function_definition`, `macro_definition` (a parameterless macro
    is `macro build ... end`, whose signature is a bare identifier), and the
    concise method `assignment` (whose left side is the call head; a plain
    `x = 1` assignment names nothing).
    """
    if node.type in (cs.TS_JULIA_FUNCTION_DEFINITION, cs.TS_JULIA_MACRO_DEFINITION):
        signature = next(
            (c for c in node.children if c.type == cs.TS_JULIA_SIGNATURE), None
        )
        if signature is None:
            return None
        first = _first_named(signature)
        if first is None or not first.text:
            return None
        if first.type == cs.TS_JULIA_ARGUMENT_LIST:
            # `function (x) ... end` is anonymous: the argument list is not a name.
            return None
        if first.type == cs.TS_JULIA_IDENTIFIER:
            # A parameterless macro: `macro build ... end`.
            return safe_decode_text(first)
        head = _unwrap_head(first)
        if head is None:
            return None
    elif node.type == cs.TS_JULIA_ASSIGNMENT:
        first = _first_named(node)
        if first is None:
            return None
        # `f(x) = ...`, `f(x)::Int = ...` and `f(x)::Int where T = ...` all
        # wrap the call head in where/typed decoration on the LEFT side.
        head = _unwrap_head(first)
        # Only a call head on the left defines a concise method; a plain
        # `x = 1`, `x::Int = 1` or `x = f()` assignment names no function.
        if head is None or head.type != cs.TS_JULIA_CALL_EXPRESSION:
            return None
    else:
        return None

    if head.type == cs.TS_JULIA_IDENTIFIER and head.text:
        return safe_decode_text(head)
    return _callee_name(head)


def _same_span(a: Node, b: Node) -> bool:
    # py-tree-sitter returns a fresh wrapper per access chain, so identity
    # is unreliable; the byte span is the node's real identity.
    return (
        a.start_byte == b.start_byte and a.end_byte == b.end_byte and a.type == b.type
    )


def _climb_head_wrappers(call_node: Node) -> Node:
    """Climb the where/typed decoration wrapping a definition head.

    The climb stops at the first wrapper in an expression position
    (`y = f(x)::Int`, `g(f(x) where T)`) — those are real call sites,
    not heads.
    """
    current = call_node
    while (parent := current.parent) is not None and parent.type in (
        cs.TS_JULIA_TYPED_EXPRESSION,
        cs.TS_JULIA_WHERE_EXPRESSION,
    ):
        first = next(iter(parent.named_children), None)
        if first is None or not _same_span(first, current):
            break
        current = parent
    return current


def julia_is_signature_head(call_node: Node) -> bool:
    """Whether a call_expression is a function definition's own head.

    Julia's grammar parses definition heads as call_expressions, and both
    are captured by the bare `(call_expression) @call` pattern, so the call
    pass drops heads here.
    """
    head = _climb_head_wrappers(call_node)
    parent = head.parent
    if parent is None or parent.type not in (
        cs.TS_JULIA_SIGNATURE,
        cs.TS_JULIA_ASSIGNMENT,
    ):
        return False
    # Only the head position (first named child) is a definition.
    first = next(iter(parent.named_children), None)
    return first is not None and _same_span(first, head)


def julia_arrow_assigned_name(node: Node) -> str | None:
    """The binding name of an arrow function, from its enclosing assignment.

    Only a DIRECT binding counts (the arrow must BE the assigned value):
    `ys = map(x -> x + 1, xs)` names nothing (issue #1882 review).
    """
    current = node.parent
    while current is not None and current.type != cs.TS_JULIA_ASSIGNMENT:
        current = current.parent
    if current is None:
        return None
    value = _last_named(current)
    # Peel the decoration a bare arrow value may wear; any other container
    # (a call, a binary expression, ...) is not a transparent wrapper.
    while value is not None and value.type in (
        cs.TS_JULIA_TYPED_EXPRESSION,
        cs.TS_JULIA_WHERE_EXPRESSION,
        cs.TS_JULIA_PARENTHESIZED_EXPRESSION,
    ):
        value = _first_named(value)
    if value is None or not _same_span(value, node):
        return None
    lhs = _first_named(current)
    if lhs is not None and lhs.type == cs.TS_JULIA_TYPED_EXPRESSION:
        lhs = _first_named(lhs)
    if lhs is None or not lhs.text:
        return None
    if lhs.type in (cs.TS_JULIA_IDENTIFIER, cs.TS_JULIA_FIELD_EXPRESSION):
        return _single_line_or_none(safe_decode_text(lhs))
    return None


def julia_call_name(call_node: Node) -> str | None:
    """The callee name of a Julia call-site node (callee = first named
    child; a macrocall names its `macro_identifier` minus the `@`)."""
    if call_node.type == cs.TS_JULIA_MACROCALL_EXPRESSION:
        macro_id = next(
            (c for c in call_node.children if c.type == cs.TS_JULIA_MACRO_IDENTIFIER),
            None,
        )
        if macro_id is None or not macro_id.text:
            return None
        name = safe_decode_text(macro_id)
        return name.lstrip("@") if name is not None else None
    if call_node.type in (
        cs.TS_JULIA_CALL_EXPRESSION,
        cs.TS_JULIA_BROADCAST_CALL_EXPRESSION,
    ):
        # The same reduction a definition head gets (julia_callee_name): a
        # call `Arr{T, N}(x)` names the constructor `Arr`, exactly as its
        # definition `function Arr{T, N}(x)` registers.
        return julia_callee_name(_first_named(call_node))
    return None
