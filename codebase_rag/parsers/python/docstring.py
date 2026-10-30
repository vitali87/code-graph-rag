"""Python's docstring: the string literal opening a module's or a
definition's body, which the definition pass reads where every other
language reads a comment above the declaration."""

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_with_fallback


def python_docstring(node: Node) -> str | None:
    if node.type == cs.TS_PY_MODULE:
        # A module node has no `body` field: its statements are direct
        # children, one level shallower than a class or function body.
        statements = node.children
    else:
        body_node = node.child_by_field_name(cs.FIELD_BODY)
        if not body_node:
            return None
        statements = body_node.children
    if not statements:
        return None
    first_statement = statements[0]
    if (
        first_statement.type == cs.TS_PY_EXPRESSION_STATEMENT
        and first_statement.children
        and first_statement.children[0].type == cs.TS_PY_STRING
    ):
        text = first_statement.children[0].text
        if text is not None:
            result: str = safe_decode_with_fallback(first_statement.children[0]).strip(
                cs.DOCSTRING_STRIP_CHARS
            )
            return result
    return None
