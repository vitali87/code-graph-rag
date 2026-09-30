from __future__ import annotations

from collections.abc import Collection

from tree_sitter import Node

from ... import constants as cs
from ...types_defs import FunctionRegistryTrieProtocol, NodeType
from ..utils import safe_decode_text

# What a Go type name can declare: a struct (Class), a named or alias type,
# or an interface. Receiver and constructor-return types name one of them.
TYPE_DECLARATION_TYPES = frozenset({NodeType.CLASS, NodeType.TYPE, NodeType.INTERFACE})


def package_level_definitions(
    registry: FunctionRegistryTrieProtocol,
    package_qn: str,
    name: str,
    labels: Collection[str],
) -> list[str]:
    # A Go package spans its directory's files, and cgr files each package-level
    # declaration under its FILE module (`pkg.file.Name`), a segment the source
    # never writes. So `Name` of package `pkg` is any `labels` entry exactly one
    # segment below `pkg`: deeper qns are methods or another package's files.
    prefix = f"{package_qn}{cs.SEPARATOR_DOT}"
    depth = package_qn.count(cs.SEPARATOR_DOT) + 2
    return [
        qn
        for qn in registry.find_ending_with(name)
        if qn.startswith(prefix)
        and qn.count(cs.SEPARATOR_DOT) == depth
        and registry.get(qn) in labels
    ]


# Statements whose `left` (or a type switch's `alias`) list declares names:
# `a, b := ...`, `for k, v := range m`, `case v := <-ch`, `switch t := x.(type)`.
_GO_BINDING_LIST_FIELDS = {
    cs.TS_GO_SHORT_VAR_DECLARATION: cs.FIELD_LEFT,
    cs.TS_GO_RANGE_CLAUSE: cs.FIELD_LEFT,
    cs.TS_GO_RECEIVE_STATEMENT: cs.FIELD_LEFT,
    cs.TS_GO_TYPE_SWITCH_STATEMENT: cs.FIELD_GO_ALIAS,
}
# Declarations whose `name` identifiers are the names they bind.
_GO_NAMED_BINDING_TYPES = frozenset(
    {
        cs.TS_GO_VAR_SPEC,
        cs.TS_GO_CONST_SPEC,
        cs.TS_GO_PARAMETER_DECLARATION,
        cs.TS_GO_VARIADIC_PARAMETER_DECLARATION,
    }
)


def local_binding_names(func_node: Node) -> frozenset[str]:
    # Every name a function or method binds for itself: receiver, parameters,
    # locals, and those of the closures inside it. A bare call to one of them
    # calls that value, whatever package-level function shares its name. The
    # set ignores block scoping, so it may hold a name that is out of scope at
    # a given call, never miss one that is in scope.
    names: set[str] = set()
    stack = [func_node]
    while stack:
        node = stack.pop()
        if (field := _GO_BINDING_LIST_FIELDS.get(node.type)) is not None:
            if (bound := node.child_by_field_name(field)) is not None:
                names.update(
                    name
                    for child in bound.named_children
                    if child.type == cs.TS_GO_IDENTIFIER
                    and (name := safe_decode_text(child))
                )
        elif node.type in _GO_NAMED_BINDING_TYPES:
            names.update(
                name
                for child in node.children_by_field_name(cs.FIELD_NAME)
                if child.type == cs.TS_GO_IDENTIFIER
                and (name := safe_decode_text(child))
            )
        stack.extend(node.named_children)
    return frozenset(names)


def extract_package_name(root: Node) -> str | None:
    # The `package foo` clause names the Go package a file belongs to;
    # membership is (directory, package name), not directory alone.
    for child in root.named_children:
        if child.type != cs.TS_GO_PACKAGE_CLAUSE:
            continue
        for ident in child.named_children:
            if ident.type == cs.TS_GO_PACKAGE_IDENTIFIER:
                return safe_decode_text(ident)
    return None


def is_receiver_method(node: Node) -> bool:
    return (
        node.type == cs.TS_GO_METHOD_DECLARATION
        and node.child_by_field_name(cs.FIELD_RECEIVER) is not None
    )


def extract_receiver_type_name(node: Node) -> str | None:
    receiver = node.child_by_field_name(cs.FIELD_RECEIVER)
    if receiver is None:
        return None
    for param in receiver.children:
        if param.type != cs.TS_GO_PARAMETER_DECLARATION:
            continue
        type_node = param.child_by_field_name(cs.FIELD_TYPE)
        if type_node is not None:
            return type_identifier_text(type_node)
    return None


def extract_return_type_name(node: Node) -> str | None:
    # Bare name of a Go function/method's single return type (`Root() *Command`
    # -> "Command"), for chained-call resolution. A parameter_list result
    # (multiple/named returns) is ambiguous for chaining, so it is skipped.
    result = node.child_by_field_name(cs.FIELD_RESULT)
    if result is None or result.type == cs.TS_GO_PARAMETER_LIST:
        return None
    return _return_type_identifier(result)


def extract_first_return_type_name(node: Node) -> str | None:
    # FIRST return type of a Go function, for typing `v, err := f()` bindings under
    # the (T, error) idiom. Unlike extract_return_type_name (chaining, where a
    # multi-return callee is uncallable so the skip is correct), a parameter_list
    # result contributes its first declared type, and a qualified `pkg.T` keeps its
    # dotted text so a local bound to an external package's type stays typed rather
    # than trie-guessed.
    result = node.child_by_field_name(cs.FIELD_RESULT)
    if result is None:
        return None
    if result.type == cs.TS_GO_PARAMETER_LIST:
        for param in result.children:
            if param.type != cs.TS_GO_PARAMETER_DECLARATION:
                continue
            type_node = param.child_by_field_name(cs.FIELD_TYPE)
            return _first_return_identifier(type_node) if type_node else None
        return None
    return _first_return_identifier(result)


def _first_return_identifier(type_node: Node) -> str | None:
    if type_node.type == cs.TS_GO_QUALIFIED_TYPE:
        return safe_decode_text(type_node)
    if type_node.type == cs.TS_GO_POINTER_TYPE:
        for child in type_node.named_children:
            return _first_return_identifier(child)
        return None
    return _return_type_identifier(type_node)


def _return_type_identifier(type_node: Node) -> str | None:
    # Like type_identifier_text but does NOT unwrap composite types: a
    # `[]Command`/`map[k]Command`/`chan Command` return is a container, and a chained
    # call lands on the container, not the element, so it must not be unwrapped to
    # "Command" (which would emit a false edge). Only a plain type_identifier, a
    # pointer to one (`*Command`), or a generic base resolves.
    if type_node.type in cs.TS_GO_CONTAINER_TYPES:
        return None
    if type_node.type == cs.TS_TYPE_IDENTIFIER and type_node.text:
        return safe_decode_text(type_node)
    if type_node.type in (cs.TS_GO_POINTER_TYPE, cs.TS_GENERIC_TYPE):
        for child in type_node.children:
            if name := _return_type_identifier(child):
                return name
    return None


def type_identifier_text(type_node: Node) -> str | None:
    if type_node.type == cs.TS_TYPE_IDENTIFIER and type_node.text:
        return safe_decode_text(type_node)
    # Unwrap pointer (*T) and generic (T[P]) receivers to the base name.
    for child in type_node.children:
        if name := type_identifier_text(child):
            return name
    return None
