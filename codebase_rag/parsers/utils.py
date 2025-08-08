"""Common utility functions for all parser components."""

from tree_sitter import Node


def safe_decode_text(node: Node | None) -> str | None:
    """Safely decode text from a tree-sitter node.

    Args:
        node: Tree-sitter node to decode text from, can be None.

    Returns:
        Decoded text or None if node or its text is None.
    """
    if node is None or node.text is None:
        return None
    text_bytes = node.text
    if isinstance(text_bytes, bytes):
        return text_bytes.decode("utf-8")
    return str(text_bytes)


def safe_decode_with_fallback(node: Node | None, fallback: str = "") -> str:
    """Safely decode node.text to string with fallback."""
    result = safe_decode_text(node)
    return result if result is not None else fallback


def contains_node(parent: Node, target: Node) -> bool:
    """Check if parent node contains target node in its subtree.

    Args:
        parent: The parent node to search within.
        target: The target node to search for.

    Returns:
        True if target is found within parent's subtree, False otherwise.
    """
    if parent == target:
        return True
    for child in parent.children:
        if contains_node(child, target):
            return True
    return False
