from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


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
    # FIRST return type of a Go function, for typing `v, err := f()`
    # bindings under the (T, error) idiom. Unlike extract_return_type_name
    # (chaining, where a multi-return callee is uncallable so the skip is
    # correct), a parameter_list result contributes its first declared
    # type, and a qualified `pkg.T` keeps its dotted text so a local bound
    # to an external package's type stays typed rather than trie-guessed.
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
    # `[]Command`/`map[k]Command`/`chan Command` return is a container, and a
    # chained call lands on the container, not the element, so it must not be
    # unwrapped to "Command" (which would emit a false edge). Only a plain
    # type_identifier, a pointer to one (`*Command`), or a generic base resolves.
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
    # Unwrap pointer (*T) and generic (T[P]) receivers down to the base name.
    for child in type_node.children:
        if name := type_identifier_text(child):
            return name
    return None
