from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_with_fallback


def decode_node_stripped(node: Node) -> str:
    return safe_decode_with_fallback(node).strip() if node.text else ""


def find_child_by_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.children if c.type == node_type), None)


def csharp_has_override_modifier(method_node: Node) -> bool:
    # (H) A C# member declares `override` via a `modifier` child wrapping the
    # (H) `override` keyword token. Its presence is what separates a real base
    # (H) override from an explicit `new` hide (which must not become OVERRIDES).
    for child in method_node.children:
        if child.type == cs.TS_CSHARP_MODIFIER and any(
            tok.type == cs.TS_CSHARP_MODIFIER_OVERRIDE for tok in child.children
        ):
            return True
    return False
