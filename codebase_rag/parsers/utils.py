"""Common utility functions for all parser components."""

from functools import lru_cache
from typing import TYPE_CHECKING, Any

from loguru import logger
from tree_sitter import Node, QueryCursor

if TYPE_CHECKING:
    from ..services import IngestorProtocol


@lru_cache(maxsize=10000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    """Cache decoded text to avoid repeated UTF-8 decoding operations.

    This cache significantly improves performance for large codebases where
    the same text content appears in multiple nodes.

    Args:
        text_bytes: Raw bytes to decode

    Returns:
        Decoded UTF-8 string
    """
    return text_bytes.decode("utf-8")


def safe_decode_text(node: Node | None) -> str | None:
    """Safely decode text from a tree-sitter node with performance caching.

    Args:
        node: Tree-sitter node to decode text from, can be None.

    Returns:
        Decoded text or None if node or its text is None.
    """
    if node is None or node.text is None:
        return None
    text_bytes = node.text
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def get_query_cursor(query: Any) -> QueryCursor:
    """Create a query cursor for the given query.

    This is a simple wrapper around QueryCursor construction to provide
    a consistent interface across the codebase.

    Args:
        query: Query object to create cursor with

    Returns:
        A QueryCursor instance for the given query
    """
    return QueryCursor(query)


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


def ingest_method(
    method_node: Node,
    container_qn: str,
    container_type: str,
    ingestor: "IngestorProtocol",
    function_registry: dict[str, str],
    simple_name_lookup: dict[str, set[str]],
    get_docstring_func: Any,
    language: str = "",
    extract_decorators_func: Any = None,
    method_qualified_name: str | None = None,
) -> None:
    """Ingest a method node into the graph database.

    Args:
        method_node: The tree-sitter node representing the method.
        container_qn: The qualified name of the container (class/impl block).
        container_type: The type of container ("Class", "Interface", etc.).
        ingestor: The graph database ingestor.
        function_registry: Registry mapping qualified names to function types.
        simple_name_lookup: Lookup table for simple names to qualified names.
        get_docstring_func: Function to extract docstring from a node.
        language: The programming language (used for C++ specific handling).
        extract_decorators_func: Optional function to extract decorators.
        method_qualified_name: Optional pre-computed qualified name to use instead of generating one.
    """
    # Extract method name based on language
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

    # Build qualified name
    if method_qualified_name is not None:
        # Use pre-computed qualified name (for language-specific handling)
        method_qn = method_qualified_name
    else:
        # Default qualified name construction
        method_qn = f"{container_qn}.{method_name}"

    # Extract decorators if function provided
    decorators = []
    if extract_decorators_func:
        decorators = extract_decorators_func(method_node)

    # Create method properties
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
    function_registry[method_qn] = "Method"
    simple_name_lookup[method_name].add(method_qn)

    # Create relationship between container and method
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
    simple_name_lookup: dict[str, set[str]],
    get_docstring_func: Any,
    is_export_inside_function_func: Any,
) -> None:
    """Ingest an exported function into the graph database.

    This helper eliminates duplication between CommonJS and ES6 export processing.

    Args:
        function_node: The tree-sitter node representing the function.
        function_name: The name of the function.
        module_qn: The qualified name of the module.
        export_type: Description for logging (e.g., "CommonJS Export", "ES6 Export").
        ingestor: The graph database ingestor.
        function_registry: Registry mapping qualified names to function types.
        simple_name_lookup: Lookup table for simple names to qualified names.
        get_docstring_func: Function to extract docstring from a node.
        is_export_inside_function_func: Function to check if export is inside a function.
    """
    # Skip if this export is inside a function (let regular processing handle it)
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
    function_registry[function_qn] = "Function"
    simple_name_lookup[function_name].add(function_qn)
