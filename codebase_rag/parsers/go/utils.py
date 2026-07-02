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
    # (H) Bare name of a Go function/method's single return type (`Root() *Command`
    # (H) -> "Command"), for chained-call resolution. A parameter_list result
    # (H) (multiple/named returns) is ambiguous for chaining, so it is skipped.
    result = node.child_by_field_name(cs.FIELD_RESULT)
    if result is None or result.type == cs.TS_GO_PARAMETER_LIST:
        return None
    return _return_type_identifier(result)


def _return_type_identifier(type_node: Node) -> str | None:
    # (H) Like type_identifier_text but does NOT unwrap composite types: a
    # (H) `[]Command`/`map[k]Command`/`chan Command` return is a container, and a
    # (H) chained call lands on the container, not the element, so it must not be
    # (H) unwrapped to "Command" (which would emit a false edge). Only a plain
    # (H) type_identifier, a pointer to one (`*Command`), or a generic base resolves.
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
    # (H) Unwrap pointer (*T) and generic (T[P]) receivers down to the base name.
    for child in type_node.children:
        if name := type_identifier_text(child):
            return name
    return None
