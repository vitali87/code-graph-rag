from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ..utils import safe_decode_text


def enclosing_namespace(node: Node) -> str | None:
    """The dotted PHP namespace that lexically encloses `node`, or None in
    the global namespace.

    Read per declaration, not per file: a file may declare several
    namespaces, and each declaration belongs to the one around it (issue
    #2472). A bracketed `namespace X { ... }` holds its declarations in its
    body; a statement `namespace X;` covers every top-level statement after
    it, up to the next such statement.
    """
    current = node
    while (parent := current.parent) is not None:
        if parent.type == cs.TS_PHP_NAMESPACE_DEFINITION:
            return _namespace_name(parent)
        if parent.parent is None:
            return _statement_namespace(parent, current)
        current = parent
    return None


def _statement_namespace(root: Node, statement: Node) -> str | None:
    namespace = None
    for child in root.children:
        if child.start_byte >= statement.start_byte:
            break
        if (
            child.type == cs.TS_PHP_NAMESPACE_DEFINITION
            and child.child_by_field_name(cs.FIELD_BODY) is None
        ):
            namespace = _namespace_name(child)
    return namespace


def _namespace_name(definition: Node) -> str | None:
    # `namespace { ... }` is the global namespace: it has no name.
    name_node = definition.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or not (name := safe_decode_text(name_node)):
        return None
    return name.replace(cs.PHP_NAMESPACE_SEPARATOR, cs.SEPARATOR_DOT)
