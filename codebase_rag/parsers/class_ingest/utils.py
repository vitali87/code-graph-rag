from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import TYPE_CHECKING

from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ...config import settings
from ...node_text import safe_decode_with_fallback, sorted_captures
from ..rust import utils as rs_utils

if TYPE_CHECKING:
    from ...language_spec import LanguageSpec
    from ...types_defs import LanguageQueries


def decode_node_stripped(node: Node) -> str:
    return safe_decode_with_fallback(node).strip()


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


def _is_nested_inside_function(
    node: Node, class_body: Node, lang_config: LanguageSpec
) -> bool:
    current = node.parent
    while current and current is not class_body:
        if (
            current.type in lang_config.function_node_types
            and current.child_by_field_name(cs.FIELD_BODY) is not None
        ):
            return True
        current = current.parent
    return False


def _method_belongs_directly(
    method_node: Node, class_node: Node, lang_config: LanguageSpec
) -> bool:
    current = method_node.parent
    while current is not None:
        if current == class_node:
            return True
        if current.type in lang_config.class_node_types or (
            current.type in lang_config.function_node_types
            and current.child_by_field_name(cs.FIELD_BODY) is not None
        ):
            return False
        current = current.parent
    return False


def skip_method(
    method_node: Node, class_node: Node, class_body: Node, lang_config: LanguageSpec
) -> bool:
    # Both walks below stop at a bodied function but cross a Rust const/static
    # initializer without noticing, so an item written in one inside an impl
    # was ingested as a method of that type and took its name (issue #1037).
    if lang_config.language == cs.SupportedLanguage.RUST and rs_utils.is_body_local(
        method_node
    ):
        return True
    if settings.CAPTURE_FUNCTION_LOCAL_DEFINITIONS:
        return not _method_belongs_directly(method_node, class_node, lang_config)
    return _is_nested_inside_function(method_node, class_body, lang_config)


def body_method_nodes(
    body_node: Node,
    lang_queries: LanguageQueries,
    sorted_func_nodes: list[Node] | None,
    func_node_starts: list[int] | None,
) -> list[Node] | None:
    # The function nodes inside an impl body: sliced from the pre-sorted
    # capture list when the caller has one, else queried directly. None when
    # the language has no function query at all.
    if sorted_func_nodes is not None and func_node_starts is not None:
        body_end = body_node.end_byte
        lo = bisect_left(func_node_starts, body_node.start_byte)
        hi = bisect_right(func_node_starts, body_end)
        return [n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end]
    method_query = lang_queries[cs.QUERY_FUNCTIONS]
    if not method_query:
        return None
    method_captures = sorted_captures(QueryCursor(method_query), body_node)
    return method_captures.get(cs.CAPTURE_FUNCTION, [])
