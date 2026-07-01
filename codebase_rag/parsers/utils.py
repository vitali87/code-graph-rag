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


def extract_modifiers_and_decorators(
    node: ASTNode, lang_queries: LanguageQueries
) -> tuple[list[str], list[str]]:
    query = lang_queries.get("highlights")
    if not query:
        return [], []

    cursor = get_query_cursor(query)

    body_node = node.child_by_field_name("body")
    header_end_byte = body_node.start_byte if body_node else node.end_byte

    target_node = node
    if node.parent and node.parent.type in ("decorated_definition", "export_statement"):
        target_node = node.parent

    cursor.set_byte_range(target_node.start_byte, header_end_byte)

    captures = sorted_captures(cursor, target_node)

    modifiers: list[str] = []
    decorators: list[str] = []

    for name, nodes in captures.items():
        if name.startswith("keyword.modifier") or name == "keyword":
            for n in nodes:
                text = safe_decode_text(n)
                if text and text not in modifiers:
                    modifiers.append(text)
        elif name.startswith("attribute") or name.startswith("function.decorator"):
            for n in nodes:
                text = safe_decode_text(n)
                if text and text not in decorators:
                    decorators.append(text)

    return modifiers, decorators


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
        decorator.lstrip("@#[]() ").split(cs.SEPARATOR_DOT)[-1].rstrip(")]")
        for decorator in decorators
    }


def _is_property_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.PROPERTY_DECORATORS)


def _is_abstract_decorator(decorators: list[str]) -> bool:
    return bool(_decorator_tail_names(decorators) & cs.ABSTRACT_DECORATORS)


_PY_NAMED_PARAMETERS = frozenset(
    {cs.TS_PY_DEFAULT_PARAMETER, cs.TS_PY_TYPED_DEFAULT_PARAMETER}
)
_PY_SCOPE_BOUNDARIES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)


def _python_parameter_name(param_node: Node) -> str | None:
    if param_node.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(param_node)
    if param_node.type in _PY_NAMED_PARAMETERS:
        name_node = param_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and name_node.type == cs.TS_PY_IDENTIFIER:
            return safe_decode_text(name_node)
        return None
    if param_node.type == cs.TS_PY_TYPED_PARAMETER:
        for child in param_node.children:
            if child.type == cs.TS_PY_IDENTIFIER:
                return safe_decode_text(child)
    return None


def _python_invoked_parameter_names(body_node: Node, candidates: set[str]) -> set[str]:
    invoked: set[str] = set()
    stack = [body_node]
    while stack:
        node = stack.pop()
        if node.type == cs.TS_PY_CALL:
            fn = node.child_by_field_name(cs.FIELD_FUNCTION)
            if (
                fn is not None
                and fn.type == cs.TS_PY_IDENTIFIER
                and (name := safe_decode_text(fn)) in candidates
            ):
                invoked.add(name)
        for child in node.children:
            # (H) Nested def/class bodies rebind the param name, so do not let an
            # (H) inner call to a same-named local masquerade as the outer param.
            if child.type not in _PY_SCOPE_BOUNDARIES:
                stack.append(child)
    return invoked


def python_parameter_names(func_node: Node) -> list[str]:
    # (H) Ordered parameter names with a leading self/cls dropped, so positions line
    # (H) up with how call-site arguments map to parameters for bound methods.
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    names: list[str] = []
    for child in params_node.named_children:
        if (name := _python_parameter_name(child)) is not None:
            names.append(name)
    if names and names[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
        names = names[1:]
    return names


def callable_parameter_indices(
    func_node: Node, language: cs.SupportedLanguage | None
) -> dict[str, int]:
    # (H) Maps each parameter that is invoked as a call inside the function body
    # (H) to its positional index in the call-site argument list (self/cls
    # (H) dropped so the index lines up with how bound methods are invoked).
    if language != cs.SupportedLanguage.PYTHON:
        return {}
    body_node = func_node.child_by_field_name(cs.FIELD_BODY)
    if body_node is None or not (names := python_parameter_names(func_node)):
        return {}

    invoked = _python_invoked_parameter_names(body_node, set(names))
    if not invoked:
        return {}
    return {name: index for index, name in enumerate(names) if name in invoked}


def _js_ts_field_member_name(
    node: ASTNode, language: cs.SupportedLanguage | None
) -> str | None:
    # (H) The binding name of a JS/TS class-field arrow / fn-expr whose enclosing
    # (H) field definition holds it as its `value` (`helper = () => ...`), so the
    # (H) member is modelled as class_qn.helper. None for other languages/shapes.
    if language not in (cs.SupportedLanguage.JS, cs.SupportedLanguage.TS):
        return None
    if node.type not in (cs.TS_ARROW_FUNCTION, cs.TS_FUNCTION_EXPRESSION):
        return None
    parent = node.parent
    # (H) `==` not `is`: py-tree-sitter returns a fresh Node wrapper on each access,
    # (H) so identity comparison always fails; Node equality compares the node id.
    if parent is None or parent.child_by_field_name(cs.FIELD_VALUE) != node:
        return None
    name_node = parent.child_by_field_name(cs.FIELD_NAME)
    if name_node is None or name_node.type not in (
        cs.TS_IDENTIFIER,
        cs.TS_PROPERTY_IDENTIFIER,
    ):
        return None
    return safe_decode_text(name_node)


def ingest_method(
    method_node: ASTNode,
    container_qn: str,
    container_type: cs.NodeLabel,
    ingestor: IngestorProtocol,
    function_registry: FunctionRegistryTrieProtocol,
    simple_name_lookup: SimpleNameLookup,
    get_docstring_func: Callable[[ASTNode], str | None],
    language: cs.SupportedLanguage | None = None,
    lang_queries: LanguageQueries | None = None,
    method_qualified_name: str | None = None,
    file_path: Path | None = None,
    repo_path: Path | None = None,
) -> None:
    if language == cs.SupportedLanguage.CPP:
        from .cpp import utils as cpp_utils

        method_name = cpp_utils.extract_function_name(method_node)
        if not method_name:
            return
    elif (method_name_node := method_node.child_by_field_name(cs.FIELD_NAME)) is None:
        # (H) A JS/TS class-field arrow / fn-expr (`helper = () => ...`) has no name
        # (H) field on the function node; take the binding name from the enclosing
        # (H) field definition so it is modelled as a member instead of dropped.
        if not (method_name := _js_ts_field_member_name(method_node, language)):
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

    decorators = []
    modifiers = []
    if lang_queries:
        modifiers, decorators = extract_modifiers_and_decorators(method_node, lang_queries)

    method_props: PropertyDict = {
        cs.KEY_QUALIFIED_NAME: method_qn,
        cs.KEY_NAME: method_name,
        cs.KEY_MODIFIERS: modifiers,
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
    function_registry.mark_callable_params(
        method_qn, callable_parameter_indices(method_node, language)
    )
    simple_name_lookup[method_name].add(method_qn)

    # (H) The DEFINES_METHOD parent is matched in the graph by LABEL +
    # (H) qualified_name, so it must carry the container's real node label. Callers
    # (H) pass Class by default, but a trait/interface (Interface) or enum (Enum)
    # (H) container would then never match, dropping the containment edge. Prefer
    # (H) the label the container was actually registered with.
    container_label = container_type
    registered = function_registry.get(container_qn)
    if registered is not None and registered != NodeType.METHOD:
        container_label = cs.NodeLabel(registered.value)

    ingestor.ensure_relationship_batch(
        (container_label, cs.KEY_QUALIFIED_NAME, container_qn),
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
    # (H) The definition pass already ingests an exported function / const-arrow at
    # (H) its natural qn. Re-registering here would collide and mint a spurious
    # (H) `qn@line` duplicate node, onto which call resolution then binds (mangling
    # (H) the callee qn). If the natural qn already exists, the node is done.
    if function_qn in function_registry:
        return
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
