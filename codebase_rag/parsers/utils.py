from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node, QueryCursor

from ..constants import ENCODING_UTF8, SupportedLanguage
from ..types_defs import (
    ASTNode,
    LanguageQueries,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
    TreeSitterNodeProtocol,
)

if TYPE_CHECKING:
    from tree_sitter import Query

    from ..language_spec import LanguageSpec
    from ..services import IngestorProtocol
    from ..types_defs import FunctionRegistryTrieProtocol


class FunctionCapturesResult(NamedTuple):
    lang_config: "LanguageSpec"
    captures: dict[str, list[ASTNode]]


def get_function_captures(
    root_node: ASTNode,
    language: SupportedLanguage,
    queries: dict[SupportedLanguage, LanguageQueries],
) -> FunctionCapturesResult | None:
    lang_queries = queries[language]
    lang_config = lang_queries["config"]

    query = lang_queries["functions"]
    if not query:
        return None

    cursor = QueryCursor(query)
    captures = cursor.captures(root_node)
    return FunctionCapturesResult(lang_config, captures)


@lru_cache(maxsize=10000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    return text_bytes.decode(ENCODING_UTF8)


def safe_decode_text(node: ASTNode | TreeSitterNodeProtocol | None) -> str | None:
    if node is None or node.text is None:
        return None
    text_bytes = node.text
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def get_query_cursor(query: "Query") -> QueryCursor:
    return QueryCursor(query)


def safe_decode_with_fallback(node: ASTNode | None, fallback: str = "") -> str:
    result = safe_decode_text(node)
    return result if result is not None else fallback


def contains_node(parent: ASTNode, target: ASTNode) -> bool:
    if parent == target:
        return True
    return any(contains_node(child, target) for child in parent.children)


def ingest_method(
    method_node: ASTNode,
    container_qn: str,
    container_type: str,
    ingestor: "IngestorProtocol",
    function_registry: "FunctionRegistryTrieProtocol",
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    language: str = "",
    extract_decorators_func: Callable[[ASTNode], list[str]] | None = None,
    method_qualified_name: str | None = None,
) -> None:
    if language == "cpp":
        from .cpp import utils as cpp_utils

        method_name = cpp_utils.extract_function_name(method_node)
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

    method_props: PropertyDict = {
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
    function_node: ASTNode,
    function_name: str,
    module_qn: str,
    export_type: str,
    ingestor: "IngestorProtocol",
    function_registry: "FunctionRegistryTrieProtocol",
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    is_export_inside_function_func: Callable[[ASTNode], bool],
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


def is_method_node(func_node: ASTNode, lang_config: "LanguageSpec") -> bool:
    current = func_node.parent
    if not isinstance(current, Node):
        return False

    while current and current.type not in lang_config.module_node_types:
        if current.type in lang_config.class_node_types:
            return True
        current = current.parent
    return False
