from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def _normalize_type_name(text: str) -> str:
    # (H) Strip generic arguments (`List<int>` -> `List`) and whitespace so a
    # (H) parameter signature is stable and matches the registered, generic-free
    # (H) type names. Array brackets are kept (they distinguish overloads).
    return text.split(cs.CHAR_ANGLE_OPEN, 1)[0].strip()


def extract_parameter_type_names(method_node: Node) -> list[str]:
    # (H) The declared type of each parameter, in order, for the method-qn
    # (H) signature that keeps C# overloads distinct. A `params object[]` tail is
    # (H) not wrapped in a `parameter` node (grammar quirk); its `array_type`
    # (H) sits directly under the parameter_list, so capture that too.
    param_list = method_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if param_list is None:
        return []
    types: list[str] = []
    for child in param_list.children:
        type_node: Node | None = None
        if child.type == cs.TS_CSHARP_PARAMETER:
            type_node = child.child_by_field_name(cs.FIELD_TYPE)
        elif child.type == cs.TS_CSHARP_ARRAY_TYPE:
            type_node = child
        if type_node is not None and type_node.text:
            if name := safe_decode_text(type_node):
                types.append(_normalize_type_name(name))
    return types


def extract_method_signature(method_node: Node) -> tuple[str | None, list[str]]:
    # (H) (method name, parameter type names). The name is the `name` field, the
    # (H) same leaf ingest_method registers, so the signatured qn stays consistent.
    # (H) Operators/destructors have no `name` field -> (None, ...), and the
    # (H) caller leaves them with their bare synthesized name.
    name_node = method_node.child_by_field_name(cs.FIELD_NAME)
    name = safe_decode_text(name_node) if name_node and name_node.text else None
    return name, extract_parameter_type_names(method_node)
