from functools import lru_cache
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..constants import ENCODING_UTF8
from ..types_defs import NodeType, SimpleNameLookup

if TYPE_CHECKING:
    from ..services import IngestorProtocol


@lru_cache(maxsize=10000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    return text_bytes.decode(ENCODING_UTF8)


def safe_decode_text(node: Node | None) -> str | None:
    if node is None or node.text is None:
        return None
    text_bytes = node.text
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def get_query_cursor(query: Any) -> QueryCursor:
    return QueryCursor(query)


def safe_decode_with_fallback(node: Node | None, fallback: str = "") -> str:
    result = safe_decode_text(node)
    return result if result is not None else fallback


def contains_node(parent: Node, target: Node) -> bool:
    if parent == target:
        return True
    for child in parent.children:
        if contains_node(child, target):
            return True
    return False


def ingest_method(
    method_node: Node,
    container_qn: str,
    container_type: str,
    ingestor: "IngestorProtocol",
    function_registry: dict[str, str],
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Any,
    language: str = "",
    extract_decorators_func: Any = None,
    method_qualified_name: str | None = None,
) -> None:
    if language == "cpp":
        from .cpp_utils import extract_cpp_function_name

        method_name = extract_cpp_function_name(method_node)
        if not method_name:
            return
    else:
        method_name_node = method_node.child_by_field_name("name")
        if not method_name_node:
            return
        text = method_name_node.text
        if text is None:
            return
        method_name = text.decode("utf8")

    if method_qualified_name is not None:
        method_qn = method_qualified_name
    else:
        method_qn = f"{container_qn}.{method_name}"

    decorators = []
    if extract_decorators_func:
        decorators = extract_decorators_func(method_node)

    method_props: dict[str, Any] = {
        "qualified_name": method_qn,
        "name": method_name,
        "decorators": decorators,
        "start_line": method_node.start_point[0] + 1,
        "end_line": method_node.end_point[0] + 1,
        "docstring": get_docstring_func(method_node),
    }

    logger.info(f"    Found Method: {method_name} (qn: {method_qn})")
    ingestor.ensure_node_batch("Method", method_props)
    function_registry[method_qn] = NodeType.METHOD
    simple_name_lookup[method_name].add(method_qn)

    ingestor.ensure_relationship_batch(
        (container_type, "qualified_name", container_qn),
        "DEFINES_METHOD",
        ("Method", "qualified_name", method_qn),
    )


def ingest_exported_function(
    function_node: Node,
    function_name: str,
    module_qn: str,
    export_type: str,
    ingestor: "IngestorProtocol",
    function_registry: dict[str, str],
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Any,
    is_export_inside_function_func: Any,
) -> None:
    if is_export_inside_function_func(function_node):
        return

    function_qn = f"{module_qn}.{function_name}"

    function_props = {
        "qualified_name": function_qn,
        "name": function_name,
        "start_line": function_node.start_point[0] + 1,
        "end_line": function_node.end_point[0] + 1,
        "docstring": get_docstring_func(function_node),
    }

    logger.info(f"  Found {export_type}: {function_name} (qn: {function_qn})")
    ingestor.ensure_node_batch("Function", function_props)
    function_registry[function_qn] = NodeType.FUNCTION
    simple_name_lookup[function_name].add(function_qn)
