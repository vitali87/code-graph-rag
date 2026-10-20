"""Where a C translation unit uses a function as a value (issue #2529).

Callbacks, vtables and ops tables store a function in a pointer and call it
through the pointer later, so the graph never sees a call to it. The sites
that store it (an initializer list, an assignment, a call argument) are the
only evidence that it is reachable.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text
from . import utils as cpp_utils
from .overloads import cpp_declarator_signature


class CFileScope(NamedTuple):
    """What one C/C++ source file declares at file scope."""

    # `static` functions: internal linkage, so no other file can name them.
    tu_local_functions: frozenset[str]
    # Variables, function pointers included, and enumeration constants: in
    # this file the bare name denotes that object, never a function defined
    # in another file.
    objects: frozenset[str]
    # The `static` functions again, by name and parameter signature: C++
    # overloads `f(int)` and `f(double)` are two functions, and `static` on
    # one leaves the other's linkage alone (#3154 review). A named
    # namespace's are keyed by their qualified name (`N::f`).
    tu_local_overloads: frozenset[tuple[str, str]] = frozenset()


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
        case cs.TS_RETURN_STATEMENT:
            # `return handler;` hands the function to the caller, which may
            # call it through the returned pointer (#2593 review).
            return node.named_children[:1]
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
    overloads: set[tuple[str, str]] = set()
    objects: set[str] = set()
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in cs.C_FILE_SCOPE_CONTAINER_TYPES:
            stack.extend(node.named_children)
            continue
        if node.type == cs.CppNodeType.FUNCTION_DEFINITION:
            _record_function_definition(node, tu_local, overloads)
        elif node.type == cs.CppNodeType.DECLARATION:
            _record_declaration(node, tu_local, overloads, objects)
        elif node.type == cs.CppNodeType.NAMESPACE_DEFINITION:
            _record_namespace_statics(node, overloads)
        # An enum written anywhere outside a function body (alone, in a
        # typedef, a declaration, or even a struct member) declares its
        # constants at file scope in C (#2593 review).
        objects.update(enumerator_names(node))
    return CFileScope(frozenset(tu_local), frozenset(objects), frozenset(overloads))


def c_definition_has_internal_linkage(
    definition: Node, language: cs.SupportedLanguage
) -> bool:
    """Whether a C/C++ function definition is local to its translation unit.

    `static` need not sit on the definition itself: it can sit on the
    function a template wraps, or on an earlier declaration
    (`static int now(void); int now(void) { ... }` defines a private `now`),
    and an anonymous namespace gives the same internal linkage (#3154 review).
    """
    inner = definition
    if definition.type == cs.CppNodeType.TEMPLATE_DECLARATION:
        inner = next(
            (
                child
                for child in definition.named_children
                if child.type == cs.CppNodeType.FUNCTION_DEFINITION
            ),
            definition,
        )
    if cpp_utils.cpp_declaration_has_internal_linkage(inner):
        return True
    root = inner
    while root.parent is not None:
        root = root.parent
    scope = _cached_file_scope(root)
    if language != cs.SupportedLanguage.CPP:
        # C has no overloads: every declaration of a name is one function.
        name = cpp_utils.extract_function_name(inner)
        return name is not None and name in scope.tu_local_functions
    # In C++ only the same signature, in the same namespace, is the function
    # a `static` declaration made private: `static void f(int);` leaves
    # `void f(double)` and `n::f(int)` extern.
    key = _overload_key(inner.child_by_field_name(cs.FIELD_DECLARATOR))
    namespace = _namespace_scope(definition)
    return (
        key is not None
        and namespace is not None
        and (f"{namespace}{key[0]}", key[1]) in scope.tu_local_overloads
    )


def _namespace_scope(node: Node) -> str | None:
    # `N::` (`A::B::` nested) for a declaration in a named namespace, "" at
    # file scope, None in a class, a function or an anonymous namespace.
    scope = ""
    current = node.parent
    while current is not None:
        if current.type == cs.CppNodeType.NAMESPACE_DEFINITION:
            name = safe_decode_text(current.child_by_field_name(cs.FIELD_NAME))
            if not name:
                return None
            scope = f"{name}{cs.SEPARATOR_DOUBLE_COLON}{scope}"
        elif current.type not in cs.C_FILE_SCOPE_CONTAINER_TYPES:
            return None
        current = current.parent
    return scope


# One file's definitions are ingested together, so the last file's scope is
# the one asked for again.
@lru_cache(maxsize=1)
def _cached_file_scope(root: Node) -> CFileScope:
    return c_file_scope(root)


def enumerator_names(node: Node) -> set[str]:
    """The enumeration constants declared in `node`, not counting those of a
    function body inside it, which are local to that body."""
    names: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.CppNodeType.COMPOUND_STATEMENT:
            continue
        if current.type == cs.TS_ENUMERATOR:
            name_node = current.child_by_field_name(cs.FIELD_NAME)
            if name_node is not None and (name := safe_decode_text(name_node)):
                names.add(name)
            continue
        stack.extend(current.named_children)
    return names


def local_enumerator_names(node: Node) -> set[str]:
    """Enumeration constants a function body declares before `node`, in the
    blocks around it: there they name a constant, never a function."""
    names: set[str] = set()
    current = node.parent
    while current is not None:
        if current.type == cs.CppNodeType.FUNCTION_DEFINITION:
            return names
        for sibling in current.named_children:
            if sibling.start_byte >= node.start_byte:
                break
            names |= enumerator_names(sibling)
        current = current.parent
    # At file scope: c_file_scope already holds every file-scope constant.
    return set()


def _record_function_definition(
    node: Node, tu_local: set[str], overloads: set[tuple[str, str]]
) -> None:
    if not cpp_utils.cpp_declaration_has_internal_linkage(node):
        return
    if name := cpp_utils.extract_function_name(node):
        tu_local.add(name)
    if key := _overload_key(node.child_by_field_name(cs.FIELD_DECLARATOR)):
        overloads.add(key)


def _record_declaration(
    node: Node,
    tu_local: set[str],
    overloads: set[tuple[str, str]],
    objects: set[str],
) -> None:
    # `static int cmp(...);` makes the later plain `int cmp(...) {}`
    # internal too, so a static prototype counts like a definition.
    internal = cpp_utils.cpp_declaration_has_internal_linkage(node)
    for declarator in node.children_by_field_name(cs.FIELD_DECLARATOR):
        name, function = _declared_name(declarator)
        if not name:
            continue
        if function is None:
            objects.add(name)
        elif internal:
            tu_local.add(name)
            if signature := cpp_declarator_signature(function):
                overloads.add((name, signature.text))


def _record_namespace_statics(namespace: Node, overloads: set[tuple[str, str]]) -> None:
    # `namespace N { static int f(); int f() {} }` defines a private `f` too
    # (#3154 review). Only the overload keys: the names and objects the call
    # processor reads stay the file scope's.
    stack = [namespace]
    while stack:
        node = stack.pop()
        if (
            node.type in cs.C_FILE_SCOPE_CONTAINER_TYPES
            or node.type == cs.CppNodeType.NAMESPACE_DEFINITION
        ):
            stack.extend(node.named_children)
        elif (
            node.type == cs.CppNodeType.DECLARATION
            and cpp_utils.cpp_has_static_storage(node)
        ):
            overloads.update(_namespace_overload_keys(node))


def _namespace_overload_keys(declaration: Node) -> Iterator[tuple[str, str]]:
    # (`N::f`, parameter signature) of each function the declaration declares.
    if (scope := _namespace_scope(declaration)) is None:
        return
    for declarator in declaration.children_by_field_name(cs.FIELD_DECLARATOR):
        name, function = _declared_name(declarator)
        signature = None if function is None else cpp_declarator_signature(function)
        if name and signature is not None:
            yield f"{scope}{name}", signature.text


def _overload_key(declarator: Node | None) -> tuple[str, str] | None:
    # (name, parameter signature) of the function a declarator names.
    name, function = _declared_name(declarator)
    if name is None or function is None:
        return None
    signature = cpp_declarator_signature(function)
    return None if signature is None else (name, signature.text)


def _declared_name(declarator: Node | None) -> tuple[str | None, Node | None]:
    # (name, the function_declarator when it declares a function rather than
    # a variable). A function_declarator names a function only when it wraps
    # the name directly: `int (*fp)(int)` wraps a parenthesized pointer, a
    # variable.
    current = declarator
    while current is not None:
        match current.type:
            case cs.TS_CPP_IDENTIFIER:
                return safe_decode_text(current), None
            case cs.CppNodeType.FUNCTION_DECLARATOR:
                inner = current.child_by_field_name(cs.FIELD_DECLARATOR)
                if inner is not None and inner.type == cs.TS_CPP_IDENTIFIER:
                    return safe_decode_text(inner), current
                current = inner
            case cs.CppNodeType.PARENTHESIZED_DECLARATOR:
                current = current.named_child(0)
            case _:
                current = current.child_by_field_name(cs.FIELD_DECLARATOR)
    return None, None
