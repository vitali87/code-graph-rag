from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger
from tree_sitter import Node, Query, QueryCursor

from .. import constants as cs
from .. import logs
from ..types_defs import (
    ASTNode,
    LanguageQueries,
    NodeType,
    PropertyDict,
    SimpleNameLookup,
    TreeSitterNodeProtocol,
)
from ..utils.path_utils import cached_relative_path, cached_resolve_posix

if TYPE_CHECKING:
    from ..language_spec import LanguageSpec
    from ..services import IngestorProtocol
    from ..types_defs import FunctionRegistryTrieProtocol

_QUERY_CACHE: dict[tuple[int, str], Query] = {}
_QUERY_LAST: tuple[tuple[int, str], Query] | None = None


def get_cached_query(language_obj, query_text: str) -> Query:
    global _QUERY_LAST
    key = (id(language_obj), query_text)
    if _QUERY_LAST is not None and _QUERY_LAST[0] == key:
        return _QUERY_LAST[1]
    if key not in _QUERY_CACHE:
        _QUERY_CACHE[key] = Query(language_obj, query_text)
    result = _QUERY_CACHE[key]
    _QUERY_LAST = (key, result)
    return result


class FunctionCapturesResult(NamedTuple):
    lang_config: LanguageSpec
    captures: dict[str, list[ASTNode]]


def sorted_captures(cursor: QueryCursor, node: ASTNode) -> dict[str, list[ASTNode]]:
    # (H) tree-sitter v0.25 captures() returns nodes in non-deterministic order
    # (H) across process invocations; sort by start_byte for reproducibility
    raw = cursor.captures(node)
    result: dict[str, list[ASTNode]] = {}
    for name, nodes in raw.items():
        if len(nodes) <= 1:
            result[name] = nodes
        else:
            is_sorted = True
            prev_byte = nodes[0].start_byte
            for i in range(1, len(nodes)):
                cur_byte = nodes[i].start_byte
                if cur_byte < prev_byte:
                    is_sorted = False
                    break
                prev_byte = cur_byte
            result[name] = nodes if is_sorted else sorted(nodes, key=_start_byte_key)
    return result


def _start_byte_key(n: ASTNode) -> int:
    return n.start_byte


def get_function_captures(
    root_node: ASTNode,
    language: cs.SupportedLanguage,
    queries: dict[cs.SupportedLanguage, LanguageQueries],
) -> FunctionCapturesResult | None:
    lang_queries = queries[language]
    lang_config = lang_queries[cs.QUERY_CONFIG]

    if not (query := lang_queries[cs.QUERY_FUNCTIONS]):
        return None

    cursor = QueryCursor(query)
    captures = sorted_captures(cursor, root_node)
    return FunctionCapturesResult(lang_config, captures)


@lru_cache(maxsize=50000)
def _cached_decode_bytes(text_bytes: bytes) -> str:
    return text_bytes.decode(cs.ENCODING_UTF8)


def safe_decode_text(node: ASTNode | TreeSitterNodeProtocol | None) -> str | None:
    if node is None or (text_bytes := node.text) is None:
        return None
    if isinstance(text_bytes, bytes):
        return _cached_decode_bytes(text_bytes)
    return str(text_bytes)


def get_query_cursor(query: Query) -> QueryCursor:
    return QueryCursor(query)


def safe_decode_with_fallback(node: ASTNode | None, fallback: str = "") -> str:
    return result if (result := safe_decode_text(node)) is not None else fallback


def contains_node(parent: ASTNode, target: ASTNode) -> bool:
    return parent == target or any(
        contains_node(child, target) for child in parent.children
    )


def _decorator_tail_names(decorators: list[str]) -> set[str]:
    return {
        decorator.lstrip(cs.DECORATOR_AT).split(cs.SEPARATOR_DOT)[-1]
        for decorator in decorators
    }


def _is_property_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.PROPERTY_DECORATORS)


def _is_abstract_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.ABSTRACT_DECORATORS)


def ingest_method(
    method_node: ASTNode,
    container_qn: str,
    container_type: cs.NodeLabel,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    language: cs.SupportedLanguage | None = None,
    extract_decorators_func: Callable[[ASTNode], list[str]] | None = None,
    method_qualified_name: str | None = None,
    file_path: Path | None = None,
    repo_path: Path | None = None,
) -> None:
    if language == cs.SupportedLanguage.CPP:
        from .cpp import utils as cpp_utils

        method_name = cpp_utils.extract_function_name(method_node)
        if not method_name:
            return
    elif not (method_name_node := method_node.child_by_field_name(cs.FIELD_NAME)):
        return
    elif (text := method_name_node.text) is None:
        return
    else:
        method_name = text.decode(cs.ENCODING_UTF8)

    method_qn = method_qualified_name or f"{container_qn}.{method_name}"
    if language != cs.SupportedLanguage.CPP:
        method_qn = function_registry.register_unique_qn(
            method_qn, method_node.start_point[0] + 1
        )

    decorators = extract_decorators_func(method_node) if extract_decorators_func else []

    method_props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: method_qn,
        cs.KEY_NAME: method_name,
        cs.KEY_DECORATORS: decorators,
        cs.KEY_START_LINE: method_node.start_point[0] + 1,
        cs.KEY_END_LINE: method_node.end_point[0] + 1,
        cs.KEY_DOCSTRING: get_docstring_func(method_node),
    }
    if file_path is not None and repo_path is not None:
        method_props[cs.KEY_PATH] = cached_relative_path(
            file_path, repo_path
        ).as_posix()
        method_props[cs.KEY_ABSOLUTE_PATH] = cached_resolve_posix(file_path)

    logger.info(logs.METHOD_FOUND.format(name=method_name, qn=method_qn))
    ingestor.ensure_node_batch(cs.NodeLabel.METHOD, method_props)
    function_registry[method_qn] = NodeType.METHOD
    if _is_property_decorator(decorators):
        function_registry.mark_property(method_qn)
    if _is_abstract_decorator(decorators):
        function_registry.mark_abstract(method_qn)
    simple_name_lookup[method_name].add(method_qn)

    ingestor.ensure_relationship_batch(
        (container_type, cs.KEY_QUALIFIED_NAME, container_qn),
        cs.RelationshipType.DEFINES_METHOD,
        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, method_qn),
    )


def ingest_exported_function(
    function_node: ASTNode,
    function_name: str,
    module_qn: str,
    export_type: str,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    is_export_inside_function_func: Callable[[ASTNode], bool],
) -> None:
    if is_export_inside_function_func(function_node):
        return

    function_qn = f"{module_qn}.{function_name}"
    function_qn = function_registry.register_unique_qn(
        function_qn, function_node.start_point[0] + 1
    )

    function_props = {
        cs.KEY_QUALIFIED_NAME: function_qn,
        cs.KEY_NAME: function_name,
        cs.KEY_START_LINE: function_node.start_point[0] + 1,
        cs.KEY_END_LINE: function_node.end_point[0] + 1,
        cs.KEY_DOCSTRING: get_docstring_func(function_node),
    }

    logger.info(
        logs.EXPORT_FOUND.format(
            export_type=export_type, name=function_name, qn=function_qn
        )
    )
    ingestor.ensure_node_batch(cs.NodeLabel.FUNCTION, function_props)
    function_registry[function_qn] = NodeType.FUNCTION
    simple_name_lookup[function_name].add(function_qn)


def is_method_node(func_node: ASTNode, lang_config: LanguageSpec) -> bool:
    current = func_node.parent
    if not isinstance(current, Node):
        return False

    class_types = lang_config.class_node_types
    func_types = lang_config.function_node_types
    module_types = lang_config.module_node_types
    body_field = cs.FIELD_BODY

    while current is not None:
        current_type = current.type
        if current_type in module_types:
            return False
        if current_type in class_types:
            return True
        if (
            current_type in func_types
            and current.child_by_field_name(body_field) is not None
        ):
            return False
        current = current.parent
    return False
