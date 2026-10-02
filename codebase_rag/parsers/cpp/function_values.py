"""Where a C translation unit uses a function as a value (issue #2529).

Callbacks, vtables and ops tables store a function in a pointer and call it
through the pointer later, so the graph never sees a call to it. The sites
that store it (an initializer list, an assignment, a call argument) are the
only evidence that it is reachable.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from . import utils as cpp_utils


class CFileScope(NamedTuple):
    """What one C/C++ source file declares at file scope."""

    # `static` functions: internal linkage, so no other file can name them.
    tu_local_functions: frozenset[str]
    # Variables, function pointers included: in this file the bare name
    # denotes the variable, never a function defined in another file.
    objects: frozenset[str]


def c_function_value_identifiers(
    scope: Node, boundary_types: frozenset[str]
) -> Iterator[Node]:
    """Identifiers under `scope` that stand where a function pointer value
    goes. Whether one names a function is for the caller to resolve."""
    stack = list(scope.children)
    while stack:
        node = stack.pop()
        if node.type in boundary_types:
            continue
        for value in _value_slots(node):
            yield from _designated_identifiers(value)
        stack.extend(node.children)


def _value_slots(node: Node) -> list[Node]:
    match node.type:
        case cs.TS_CPP_INITIALIZER_LIST | cs.TS_CPP_ARGUMENT_LIST:
            # An initializer_pair or nested initializer_list entry is not an
            # identifier; the walk reaches its value when it visits it.
            return node.named_children
        case cs.TS_CPP_INITIALIZER_PAIR | cs.CppNodeType.INIT_DECLARATOR:
            field = cs.FIELD_VALUE
        case cs.TS_CPP_ASSIGNMENT_EXPRESSION:
            field = cs.FIELD_RIGHT
        case _:
            return []
    value = node.child_by_field_name(field)
    return [] if value is None else [value]


def _designated_identifiers(value: Node) -> Iterator[Node]:
    pending = [value]
    while pending:
        node = pending.pop()
        match node.type:
            case cs.TS_CPP_IDENTIFIER:
                yield node
            case cs.TS_PARENTHESIZED_EXPRESSION:
                pending.extend(node.named_children[:1])
            case cs.TS_CPP_CAST_EXPRESSION:
                pending.extend(_fields(node, cs.FIELD_VALUE))
            case cs.TS_CPP_CONDITIONAL_EXPRESSION:
                # The condition is only tested; either branch is the value.
                pending.extend(
                    _fields(node, cs.FIELD_CONSEQUENCE, cs.FIELD_ALTERNATIVE)
                )
            case cs.TS_CPP_POINTER_EXPRESSION if (
                safe_decode_text(node.child_by_field_name(cs.FIELD_OPERATOR))
                == cs.CPP_OP_ADDRESS_OF
            ):
                pending.extend(_fields(node, cs.CPP_FIELD_ARGUMENT))


def _fields(node: Node, *names: str) -> list[Node]:
    return [child for name in names if (child := node.child_by_field_name(name))]


def c_file_scope(root: Node) -> CFileScope:
    tu_local: set[str] = set()
    objects: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == cs.CppNodeType.FUNCTION_DEFINITION:
            _record_function_definition(node, tu_local)
        elif node.type == cs.CppNodeType.DECLARATION:
            _record_declaration(node, tu_local, objects)
        elif node.type in cs.C_FILE_SCOPE_CONTAINER_TYPES:
            stack.extend(node.named_children)
    return CFileScope(frozenset(tu_local), frozenset(objects))


def _record_function_definition(node: Node, tu_local: set[str]) -> None:
    if not cpp_utils.cpp_declaration_has_internal_linkage(node):
        return
    if name := cpp_utils.extract_function_name(node):
        tu_local.add(name)


def _record_declaration(node: Node, tu_local: set[str], objects: set[str]) -> None:
    # `static int cmp(...);` makes the later plain `int cmp(...) {}`
    # internal too, so a static prototype counts like a definition.
    internal = cpp_utils.cpp_declaration_has_internal_linkage(node)
    for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
        name, is_function = _declared_name(declarator)
        if not name:
            continue
        if not is_function:
            objects.add(name)
        elif internal:
            tu_local.add(name)


def _declared_name(declarator: Node) -> tuple[str | None, bool]:
    # (name, whether it declares a function rather than a variable). A
    # function_declarator names a function only when it wraps the name
    # directly: `int (*fp)(int)` wraps a parenthesized pointer, a variable.
    current: Node | None = declarator
    while current is not None:
        match current.type:
            case cs.TS_CPP_IDENTIFIER:
                return safe_decode_text(current), False
            case cs.CppNodeType.FUNCTION_DECLARATOR:
                inner = current.child_by_field_name(cs.FIELD_DECLARATOR)
                if inner is not None and inner.type == cs.TS_CPP_IDENTIFIER:
                    return safe_decode_text(inner), True
                current = inner
            case cs.CppNodeType.PARENTHESIZED_DECLARATOR:
                current = current.named_child(0)
            case _:
                current = current.child_by_field_name(cs.FIELD_DECLARATOR)
    return None, False
