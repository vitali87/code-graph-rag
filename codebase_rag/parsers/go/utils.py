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


def type_identifier_text(type_node: Node) -> str | None:
    if type_node.type == cs.TS_TYPE_IDENTIFIER and type_node.text:
        return safe_decode_text(type_node)
    # (H) Unwrap pointer (*T) and generic (T[P]) receivers down to the base name.
    for child in type_node.children:
        if name := type_identifier_text(child):
            return name
    return None
