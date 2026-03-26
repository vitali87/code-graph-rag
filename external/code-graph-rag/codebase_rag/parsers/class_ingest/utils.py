from __future__ import annotations

from tree_sitter import Node

from ..utils import safe_decode_with_fallback


def decode_node_stripped(node: Node) -> str:
    return safe_decode_with_fallback(node).strip() if node.text else ""


def find_child_by_type(node: Node, node_type: str) -> Node | None:
    return next((c for c in node.children if c.type == node_type), None)
