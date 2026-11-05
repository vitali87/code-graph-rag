"""PHP callable shapes that name a method without calling it (issue #3117).

A string callable is an absolute FQCN: `'App\\Resolver::m'` is `\\App\\Resolver`
even inside namespace App, and a `use` alias is not applied. `Foo::class` and
`new Foo` are compiler names: imports, then the enclosing namespace, and a
leading `\\` is absolute. The two resolvers must not be shared.
"""

import math
import re
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..call_resolver import _php_fold
from ..utils import safe_decode_text

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_STRING_CALLABLE = re.compile(
    rf"^\\?(?P<class>{_IDENT}(?:\\{_IDENT})*)::(?P<method>{_IDENT})$"
)
_METHOD_NAME = re.compile(rf"^{_IDENT}$")
# PHP casts a string key to int only when the text is already the canonical
# decimal form. "0" and "1" are indexes 0 and 1; "01" and "object" are not.
_CANONICAL_INT_KEY = re.compile(r"0|-?[1-9][0-9]*")
_VALUE_CALLS = frozenset(
    {
        cs.TS_PHP_FUNCTION_CALL_EXPRESSION,
        cs.TS_PHP_MEMBER_CALL_EXPRESSION,
        cs.TS_PHP_SCOPED_CALL_EXPRESSION,
        cs.TS_PHP_NULLSAFE_MEMBER_CALL_EXPRESSION,
        cs.TS_PHP_OBJECT_CREATION_EXPRESSION,
    }
)
# A stored closure has no caller pass, so the enclosing method must see a
# callable written inside it. A named function and a class do have their own.
_VALUE_WALK_STOP = frozenset(
    {
        cs.TS_PHP_FUNCTION_DEFINITION,
        cs.TS_PHP_METHOD_DECLARATION,
        cs.TS_CLASS_DECLARATION,
        cs.TS_INTERFACE_DECLARATION,
        cs.TS_PHP_TRAIT_DECLARATION,
        cs.TS_ENUM_DECLARATION,
        cs.TS_PHP_ANONYMOUS_CLASS,
    }
)
_SINGLE_ESCAPES = {"\\": "\\", "'": "'"}
_DOUBLE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "$": "$",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


def value_walk_stop(node_type: str) -> bool:
    return node_type in _VALUE_WALK_STOP


class _ParamClass(NamedTuple):
    # `declared` is false when this function has no parameter of that name.
    # An untyped parameter is declared and has no node: it hides an outer one.
    declared: bool
    node: Node | None


def relative_scope(text: str) -> cs.PhpRelativeScope | None:
    folded = _php_fold(text)
    if folded not in cs.PhpRelativeScope:
        return None
    return cs.PhpRelativeScope(folded)


def relative_new(node: Node) -> cs.PhpRelativeScope | None:
    """`self` / `static` / `parent` for `new self`, else None.

    Those keywords are a `name` child, not a `relative_scope`. A variable
    (`new $class`) and an anonymous class are not names.
    """
    target = _creation_target(node)
    if target is None or target.type != cs.TS_PHP_NAME:
        return None
    text = safe_decode_text(target)
    if text is None:
        return None
    return relative_scope(text)


def is_dynamic_or_anonymous_new(node: Node) -> bool:
    target = _creation_target(node)
    return target is not None and target.type in (
        cs.TS_PHP_VARIABLE_NAME,
        cs.TS_PHP_ANONYMOUS_CLASS,
    )


def relative_scoped_call(node: Node) -> tuple[cs.PhpRelativeScope, str] | None:
    """`(self|static|parent, method)` for `self::m()`. Other scopes are None."""
    if node.type != cs.TS_PHP_SCOPED_CALL_EXPRESSION:
        return None
    scope = node.child_by_field_name(cs.FIELD_SCOPE)
    name = node.child_by_field_name(cs.FIELD_NAME)
    if (
        scope is None
        or name is None
        or scope.type != cs.TS_PHP_RELATIVE_SCOPE
        or name.type != cs.TS_PHP_NAME
    ):
        return None
    scope_text = safe_decode_text(scope)
    method = safe_decode_text(name)
    if scope_text is None or method is None or not _METHOD_NAME.fullmatch(method):
        return None
    scope_name = relative_scope(scope_text)
    if scope_name is None:
        return None
    return scope_name, method


def callable_array(node: Node) -> tuple[Node, str] | None:
    """Receiver node and method name of `[$recv, 'm']` or `array($recv, 'm')`.

    PHP reads a callable array at indexes 0 and 1, whatever order the
    elements are written in. A string key (`'action' => 'delete'`) is not
    one of those indexes. A spread or a non-literal method name is not
    this shape either.
    """
    if node.type != cs.TS_PHP_ARRAY_CREATION_EXPRESSION:
        return None
    elements = [
        child
        for child in node.named_children
        if child.type == cs.TS_PHP_ARRAY_ELEMENT_INITIALIZER
    ]
    if len(elements) != 2 or len(node.named_children) != 2:
        return None
    slots: dict[int, Node] = {}
    next_key = 0
    for element in elements:
        key, value = _element_slot(element, next_key)
        if key is None or value is None or key in slots:
            return None
        next_key = max(next_key, key + 1)
        slots[key] = value
    receiver = slots.get(0)
    method_node = slots.get(1)
    if receiver is None or method_node is None or set(slots) != {0, 1}:
        return None
    if receiver.type == cs.TS_PHP_VARIADIC_UNPACKING:
        return None
    method = php_literal_text(method_node)
    if method is None or not _METHOD_NAME.fullmatch(method):
        return None
    return _unwrap_parens(receiver), method


def string_callable(node: Node) -> tuple[str, str] | None:
    """`(dotted FQCN, method)` for `'App\\\\Resolver::m'`, else None.

    The class is absolute whether or not the literal has a leading slash.
    Interpolation is rejected whole: one variable makes the string dynamic.
    """
    text = php_literal_text(node)
    if text is None:
        return None
    match = _STRING_CALLABLE.fullmatch(text)
    if match is None:
        return None
    dotted = match.group("class").replace(cs.PHP_NAMESPACE_SEPARATOR, cs.SEPARATOR_DOT)
    return dotted, match.group("method")


def class_const_scope(node: Node) -> Node | None:
    """The scope of `Foo::class` / `self::class`. `Foo::BAR` is not `class`."""
    if node.type != cs.TS_PHP_CLASS_CONSTANT_ACCESS_EXPRESSION:
        return None
    named = node.named_children
    if len(named) < 2 or named[-1].type != cs.TS_PHP_NAME:
        return None
    const = safe_decode_text(named[-1])
    if const is None or _php_fold(const) != cs.PHP_CLASS_CONST:
        return None
    return named[0]


def in_value_position(node: Node) -> bool:
    """True when `node` is stored, returned, passed, or used as a callee.

    A callable has to be a value. The same text in a comparison or an echo
    is not one, and a direct-argument closure is a different issue (#2925).
    """
    climbed = node
    while (
        climbed.parent is not None
        and climbed.parent.type == cs.TS_PARENTHESIZED_EXPRESSION
    ):
        climbed = climbed.parent
    parent = climbed.parent
    if parent is None:
        return False
    # A ternary's results are values when the ternary itself is stored,
    # returned, or passed. The condition is not. Elvis has no body, so the
    # condition is a result too.
    if parent.type == cs.TS_PHP_CONDITIONAL_EXPRESSION:
        return _conditional_result(parent, climbed) and in_value_position(parent)
    # tree-sitter builds a fresh Node object per lookup, so `is` never
    # matches a field against the node the walk is holding. Equality does.
    if parent.type == cs.TS_ASSIGNMENT_EXPRESSION:
        return parent.child_by_field_name(cs.TS_FIELD_RIGHT) == climbed
    if parent.type == cs.TS_RETURN_STATEMENT:
        return climbed in parent.named_children
    if parent.type == cs.TS_PHP_ARGUMENT:
        return True
    if parent.type == cs.TS_PHP_ARRAY_ELEMENT_INITIALIZER:
        named = parent.named_children
        return bool(named) and named[-1] == climbed
    if parent.type in _VALUE_CALLS:
        return parent.child_by_field_name(cs.FIELD_FUNCTION) == climbed
    # `fn() => [$this, 'm']` has no return statement. The body is the value.
    if parent.type == cs.TS_PHP_ARROW_FUNCTION:
        return parent.child_by_field_name(cs.FIELD_BODY) == climbed
    return False


def _conditional_result(parent: Node, climbed: Node) -> bool:
    body = parent.child_by_field_name(cs.FIELD_BODY)
    alternative = parent.child_by_field_name(cs.FIELD_ALTERNATIVE)
    if body is not None:
        return climbed in (body, alternative)
    condition = parent.child_by_field_name(cs.TS_FIELD_CONDITION)
    return climbed in (condition, alternative)


def parameter_class_type(var_node: Node) -> Node | None:
    """The one class type of the parameter named exactly `var_node`.

    PHP has no typed locals. A union is used only when exactly one member is
    a class; `?Resolver` unwraps. Two classes (`Resolver|Child`) are ambiguous.
    A closure does not declare the variables it captures, so the search
    continues outward. A parameter of the same name, typed or not, stops it.
    """
    if var_node.type != cs.TS_PHP_VARIABLE_NAME:
        return None
    wanted = safe_decode_text(var_node)
    if not wanted:
        return None
    owner = var_node.parent
    while owner is not None:
        if owner.type in cs.FQN_PHP_FUNCTION_TYPES:
            found = _parameter_class_on(owner, wanted)
            if found.declared:
                return found.node
        owner = owner.parent
    return None


def _parameter_class_on(owner: Node, wanted: str) -> _ParamClass:
    params = owner.child_by_field_name(cs.FIELD_PARAMETERS)
    if params is None:
        return _ParamClass(False, None)
    for param in params.named_children:
        name = param.child_by_field_name(cs.FIELD_NAME)
        if name is None or safe_decode_text(name) != wanted:
            continue
        type_node = param.child_by_field_name(cs.FIELD_TYPE)
        if type_node is None:
            return _ParamClass(True, None)
        classes = _class_types(type_node)
        if classes is None or len(classes) != 1:
            return _ParamClass(True, None)
        children = classes[0].named_children
        return _ParamClass(True, children[0] if children else None)
    return _ParamClass(False, None)


def php_literal_text(node: Node) -> str | None:
    """Decoded PHP string literal, or None when the literal is dynamic."""
    if node.type == cs.TS_STRING:
        text = safe_decode_text(node)
        if text is None or len(text) < 2 or text[0] != "'" or text[-1] != "'":
            return None
        return _decode_single(text[1:-1])
    if node.type == cs.TS_PHP_ENCAPSED_STRING:
        return _decode_encapsed(node)
    return None


def _creation_target(node: Node) -> Node | None:
    if node.type != cs.TS_PHP_OBJECT_CREATION_EXPRESSION:
        return None
    for child in node.named_children:
        if child.type != cs.FIELD_ARGUMENTS:
            return child
    return None


def _element_slot(element: Node, implicit: int) -> tuple[int | None, Node | None]:
    # The value is the last named child. A missing key takes the next integer
    # index, which moves past an earlier explicit integer key.
    named = element.named_children
    if not named or named[-1].type == cs.TS_PHP_VARIADIC_UNPACKING:
        return None, None
    if len(named) == 1:
        return implicit, named[-1]
    key = _array_key(named[0])
    if not isinstance(key, int):
        return None, None
    return key, named[-1]


def _array_key(node: Node) -> int | str | None:
    # PHP casts the key before it stores the element. A callable reads
    # indexes 0 and 1 after that cast, so the literal's spelling has to
    # become the same integer PHP would store.
    if node.type == cs.TS_PHP_UNARY_OP_EXPRESSION:
        return _unary_int_key(node)
    if node.type == cs.TS_PHP_INTEGER:
        return _integer_literal(safe_decode_text(node))
    if node.type == cs.TS_PHP_FLOAT:
        return _float_key(safe_decode_text(node))
    if node.type == cs.TS_PHP_BOOLEAN:
        return _boolean_key(safe_decode_text(node))
    text = php_literal_text(node)
    if text is None:
        return None
    if _CANONICAL_INT_KEY.fullmatch(text):
        return int(text)
    return text


def _unary_int_key(node: Node) -> int | None:
    # `+1` and `-0`. The sign is not part of the integer token. `-0` is 0.
    op = next((child.type for child in node.children if child.type in {"+", "-"}), None)
    operand = next(
        (
            child
            for child in node.named_children
            if child.type in {cs.TS_PHP_INTEGER, cs.TS_PHP_FLOAT}
        ),
        None,
    )
    if op is None or operand is None:
        return None
    if operand.type == cs.TS_PHP_INTEGER:
        value = _integer_literal(safe_decode_text(operand))
    else:
        value = _float_key(safe_decode_text(operand))
    if value is None:
        return None
    return -value if op == "-" else value


def _integer_literal(text: str | None) -> int | None:
    if not text:
        return None
    body = text.replace("_", "").lower()
    for prefix, base in (("0x", 16), ("0b", 2), ("0o", 8)):
        if not body.startswith(prefix):
            continue
        digits = body[len(prefix) :]
        if not digits:
            return None
        try:
            return int(digits, base)
        except ValueError:
            return None
    if not body.isdigit():
        return None
    # A leading zero is still octal. `01` is 1; a digit 8 or 9 is not a key.
    if len(body) > 1 and body[0] == "0":
        try:
            return int(body, 8)
        except ValueError:
            return None
    return int(body)


def _float_key(text: str | None) -> int | None:
    if not text:
        return None
    try:
        value = float(text.replace("_", ""))
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return int(value)


def _boolean_key(text: str | None) -> int | None:
    folded = _php_fold(text or "")
    if folded == "true":
        return 1
    if folded == "false":
        return 0
    return None


def _unwrap_parens(node: Node) -> Node:
    while node.type == cs.TS_PARENTHESIZED_EXPRESSION and node.named_children:
        node = node.named_children[0]
    return node


def _class_types(type_node: Node) -> list[Node] | None:
    if type_node.type == cs.TS_PHP_OPTIONAL_TYPE:
        children = type_node.named_children
        if len(children) != 1:
            return None
        return _class_types(children[0])
    if type_node.type == cs.TS_PHP_NAMED_TYPE:
        return [type_node]
    if type_node.type == cs.TS_PHP_PRIMITIVE_TYPE:
        return []
    if type_node.type != cs.TS_PHP_UNION_TYPE:
        return None
    classes: list[Node] = []
    for child in type_node.named_children:
        if child.type == cs.TS_PHP_PRIMITIVE_TYPE:
            continue
        if child.type != cs.TS_PHP_NAMED_TYPE:
            return None
        classes.append(child)
    return classes


def _decode_single(interior: str) -> str:
    # Only `\\` and `\'` are escapes. `\R` is a backslash and an R.
    out: list[str] = []
    index = 0
    while index < len(interior):
        char = interior[index]
        if char == "\\" and index + 1 < len(interior):
            nxt = interior[index + 1]
            if nxt in _SINGLE_ESCAPES:
                out.append(_SINGLE_ESCAPES[nxt])
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _decode_encapsed(node: Node) -> str | None:
    parts: list[str] = []
    for child in node.named_children:
        if child.type == cs.TS_PHP_STRING_CONTENT:
            text = safe_decode_text(child)
            if text is None:
                return None
            parts.append(text)
            continue
        if child.type != cs.TS_ESCAPE_SEQUENCE:
            return None
        raw = safe_decode_text(child)
        if raw is None or len(raw) != 2 or raw[0] != "\\":
            return None
        decoded = _DOUBLE_ESCAPES.get(raw[1])
        if decoded is None:
            return None
        parts.append(decoded)
    return "".join(parts)
