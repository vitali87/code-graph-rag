from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_with_fallback


def decode_node_stripped(node: Node) -> str:
    return safe_decode_with_fallback(node).strip() if node.text else ""


def find_child_by_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.children if c.type == node_type), None)


def csharp_has_override_modifier(method_node: Node) -> bool:
    # A C# member declares `override` via a `modifier` child that wraps
    # exactly one keyword; its stripped text IS the keyword. Its presence is
    # what separates a real base override from an explicit `new` hide (which
    # must not become OVERRIDES).
    for child in method_node.children:
        if child.type == cs.TS_CSHARP_MODIFIER and (
            decode_node_stripped(child) == cs.TS_CSHARP_MODIFIER_OVERRIDE
        ):
            return True
    return False
