"""The signature that tells one C++ member overload from another (issue #2455).

A member is keyed `Class.name`, so `f(int)` and `f(double)` shared one node
and the last one ingested overwrote the rest. Its declaration and its
out-of-class definition must still share one node, and the two are often
spelled differently. The signature is the parameter TYPES as written, plus
the cv and ref qualifiers that make `at(int)` and `at(int) const` two
overloads, with only what can never tell two overloads apart dropped:
parameter names, default arguments, spacing, `struct`/`class`/`typename`
keywords and a leading global `::`. Namespace qualifiers stay, since
`f(a::T)` and `f(b::T)` are two members; `utils.cpp_signatures` pairs
`std::string` with `string` when nothing closer matches.
"""

import re

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import OverloadSignature
from ..utils import safe_decode_with_fallback

_DECLARATOR_WRAPPERS = frozenset(
    {
        cs.CppNodeType.POINTER_DECLARATOR,
        cs.CppNodeType.REFERENCE_DECLARATOR,
        cs.CppNodeType.PARENTHESIZED_DECLARATOR,
    }
)
_NAME_WRAPPERS = frozenset(
    {
        cs.CppNodeType.REFERENCE_DECLARATOR,
        cs.CppNodeType.PARENTHESIZED_DECLARATOR,
        cs.CppNodeType.VARIADIC_DECLARATOR,
    }
)
_NAMES = frozenset({cs.CppNodeType.IDENTIFIER, cs.CppNodeType.FIELD_IDENTIFIER})
_PARAMETERS = frozenset(
    {
        cs.CppNodeType.PARAMETER_DECLARATION,
        cs.CppNodeType.OPTIONAL_PARAMETER_DECLARATION,
        cs.CppNodeType.VARIADIC_PARAMETER_DECLARATION,
    }
)
_TEMPLATE_BODIES = frozenset(
    {
        cs.CppNodeType.FUNCTION_DEFINITION,
        cs.CppNodeType.DECLARATION,
        cs.CppNodeType.FIELD_DECLARATION,
        cs.CppNodeType.TEMPLATE_DECLARATION,
    }
)
_QUALIFIERS = frozenset({cs.TS_CPP_TYPE_QUALIFIER, cs.CppNodeType.REF_QUALIFIER})
# Where a walk up from a member stops: a definition at namespace scope, or
# the body of a function, is no class body.
_OUTSIDE_CLASS = frozenset(
    {cs.CppNodeType.TRANSLATION_UNIT, cs.CppNodeType.COMPOUND_STATEMENT}
)

# `struct S&` and `S&` name one parameter type.
_ELABORATED_RE = re.compile(r"\b(?:struct|class|enum|union|typename)\s+")
_SPACE_RE = re.compile(r"\s+")
# `::` joins in the set so `std :: string`, as the tokens are rejoined, reads
# `std::string`.
_PUNCTUATION_SPACE_RE = re.compile(r"\s*([*&<>,()\[\]:])\s*")
# A leading `::` (the global namespace) names what the bare name names: not
# after a name or a template's `>`, where it qualifies.
_GLOBAL_SCOPE_RE = re.compile(r"(?<![\w>])::")


def cpp_overload_signature(method_node: Node) -> OverloadSignature | None:
    """`(int,const std::string&) const` for `f(int a, const std::string &s) const`.

    None when no parameter list can be found (a declaration error recovery
    mangled), so the caller keeps the plain name.
    """
    declarator = _function_declarator(method_node)
    if declarator is None:
        return None
    parameters = declarator.child_by_field_name(cs.FIELD_PARAMETERS)
    if parameters is None:
        return None
    types = [
        _parameter_type(child)
        for child in parameters.children
        if child.type in _PARAMETERS or child.type == cs.LANG_ELLIPSIS
    ]
    if types == [cs.CPP_VOID_PARAMETER]:
        types = []
    qualifiers = [
        _normalize(safe_decode_with_fallback(child))
        for child in declarator.children
        if child.type in _QUALIFIERS
    ]
    text = f"{cs.CHAR_PAREN_OPEN}{cs.CHAR_COMMA.join(types)}{cs.CHAR_PAREN_CLOSE}"
    return OverloadSignature(
        text=cs.CHAR_SPACE.join((text, *qualifiers)), arity=len(types)
    )


def declared_in_class_body(method_node: Node) -> bool:
    """True for a member written inside its class, declared or defined there.

    Every out-of-class definition must match one of these, which is what lets
    a definition spelled differently from its declaration still find it.
    """
    current = method_node.parent
    while current is not None and current.type not in _OUTSIDE_CLASS:
        if current.type == cs.CppNodeType.FIELD_DECLARATION_LIST:
            return True
        current = current.parent
    return False


def _function_declarator(node: Node) -> Node | None:
    if node.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        inner = next(
            (child for child in node.named_children if child.type in _TEMPLATE_BODIES),
            None,
        )
        return _function_declarator(inner) if inner is not None else None
    current = node.child_by_field_name(cs.FIELD_DECLARATOR)
    # The member's own declarator is the first function_declarator down the
    # declarator spine; a return type of `int&` or `T*` wraps it.
    while current is not None:
        if current.type == cs.CppNodeType.FUNCTION_DECLARATOR:
            return current
        if current.type not in _DECLARATOR_WRAPPERS:
            return None
        current = current.child_by_field_name(cs.FIELD_DECLARATOR) or next(
            (
                child
                for child in current.named_children
                if child.type in _DECLARATOR_WRAPPERS
                or child.type == cs.CppNodeType.FUNCTION_DECLARATOR
            ),
            None,
        )
    return None


def _declared_name(declarator: Node | None) -> Node | None:
    # Only the declarator spine names THIS parameter: an identifier off it is
    # an array bound or an inner function pointer's parameter.
    if declarator is None or declarator.type in _NAMES:
        return declarator
    if (inner := declarator.child_by_field_name(cs.FIELD_DECLARATOR)) is not None:
        return _declared_name(inner)
    if declarator.type in _NAME_WRAPPERS:
        for child in declarator.named_children:
            if (name := _declared_name(child)) is not None:
                return name
    return None


def _parameter_type(parameter: Node) -> str:
    if parameter.type == cs.LANG_ELLIPSIS:
        return cs.LANG_ELLIPSIS
    name = _declared_name(parameter.child_by_field_name(cs.FIELD_DECLARATOR))
    tokens: list[str] = []
    for child in parameter.children:
        # Everything after `=` is the default argument, which a definition
        # never repeats.
        if child.type == cs.CHAR_EQUALS:
            break
        tokens.extend(_tokens(child, name))
    return _normalize(cs.CHAR_SPACE.join(tokens))


def _tokens(node: Node, name: Node | None) -> list[str]:
    if node.type == cs.TS_COMMENT or (
        name is not None
        and node.type == name.type
        and node.start_byte == name.start_byte
        and node.end_byte == name.end_byte
    ):
        return []
    if not node.children:
        return [safe_decode_with_fallback(node)]
    return [token for child in node.children for token in _tokens(child, name)]


def _normalize(text: str) -> str:
    text = _ELABORATED_RE.sub("", text)
    text = _SPACE_RE.sub(cs.CHAR_SPACE, text)
    text = _PUNCTUATION_SPACE_RE.sub(r"\1", text).strip()
    return _GLOBAL_SCOPE_RE.sub("", text)
