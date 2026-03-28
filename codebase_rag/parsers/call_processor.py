from __future__ import annotations

from bisect import bisect_left, bisect_right
from pathlib import Path

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..parser_loader import COMBINED_FUNC_CLASS_QUERIES
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from ..utils.path_utils import cached_relative_path
from .call_resolver import CallResolver
from .cpp import utils as cpp_utils
from .import_processor import ImportProcessor
from .type_inference import TypeInferenceEngine
from .utils import get_function_captures, is_method_node, sorted_captures

_TYPED_LANGUAGES = frozenset(
    {
        cs.SupportedLanguage.PYTHON,
        cs.SupportedLanguage.JS,
        cs.SupportedLanguage.TS,
        cs.SupportedLanguage.JAVA,
        cs.SupportedLanguage.LUA,
    }
)


class CallProcessor:
    __slots__ = ("ingestor", "repo_path", "project_name", "_resolver")

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
    ) -> None:
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name

        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            class_inheritance=class_inheritance,
        )

    def _get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        return None if text is None else text.decode(cs.ENCODING_UTF8)

    def _collect_all_call_nodes(
        self,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> tuple[list[Node], list[int]]:
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return [], []
        cursor = QueryCursor(calls_query)
        captures = sorted_captures(cursor, root_node)
        call_nodes = captures.get(cs.CAPTURE_CALL, [])
        call_starts = [n.start_byte for n in call_nodes]
        return call_nodes, call_starts

    def _filter_calls_in_node(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        container: Node,
    ) -> list[Node]:
        start = container.start_byte
        end = container.end_byte
        lo = bisect_left(call_starts, start)
        hi = bisect_right(call_starts, end)
        return [n for n in all_call_nodes[lo:hi] if n.end_byte <= end]

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        relative_path = cached_relative_path(file_path, self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE, path=relative_path)

        try:
            module_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in (cs.INIT_PY, cs.MOD_RS):
                module_qn = cs.SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            call_name_cache: dict[int, str | None] = {}

            if (
                func_class_captures_cache is not None
                and file_path in func_class_captures_cache
            ):
                combined_captures = func_class_captures_cache[file_path]
            else:
                combined_query = COMBINED_FUNC_CLASS_QUERIES.get(language)
                if combined_query:
                    cursor = QueryCursor(combined_query)
                    combined_captures = sorted_captures(cursor, root_node)
                else:
                    combined_captures = {}

            cached_calls = combined_captures.get(cs.CAPTURE_CALL)
            if cached_calls is not None:
                all_call_nodes = cached_calls
                call_starts = [n.start_byte for n in all_call_nodes]
            else:
                all_call_nodes, call_starts = self._collect_all_call_nodes(
                    root_node, language, queries
                )
            if not all_call_nodes:
                return

            sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)
            func_node_starts = (
                [n.start_byte for n in sorted_func_nodes] if sorted_func_nodes else None
            )

            self._process_calls_in_functions(
                root_node,
                module_qn,
                language,
                queries,
                all_call_nodes,
                call_starts,
                call_name_cache=call_name_cache,
                combined_captures=combined_captures or None,
            )
            self._process_calls_in_classes(
                root_node,
                module_qn,
                language,
                queries,
                all_call_nodes,
                call_starts,
                call_name_cache=call_name_cache,
                combined_captures=combined_captures,
                sorted_func_nodes=sorted_func_nodes,
                func_node_starts=func_node_starts,
            )
            self._ingest_function_calls(
                root_node,
                module_qn,
                cs.NodeLabel.MODULE,
                module_qn,
                language,
                queries,
                call_nodes=all_call_nodes,
                call_name_cache=call_name_cache,
            )

        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    def _process_calls_in_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        combined_captures: dict[str, list[Node]] | None = None,
    ) -> None:
        if combined_captures is not None:
            lang_config = queries[language][cs.QUERY_CONFIG]
            func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION, [])
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                continue
            if self._is_method(func_node, lang_config):
                continue

            if language == cs.SupportedLanguage.CPP:
                func_name = cpp_utils.extract_function_name(func_node)
            else:
                func_name = self._get_node_name(func_node)
            if not func_name:
                continue
            if func_qn := self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            ):
                filtered = (
                    self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
                    if all_call_nodes is not None and call_starts is not None
                    else None
                )
                self._ingest_function_calls(
                    func_node,
                    func_qn,
                    cs.NodeLabel.FUNCTION,
                    module_qn,
                    language,
                    queries,
                    call_nodes=filtered,
                    call_name_cache=call_name_cache,
                )

    def _get_rust_impl_class_name(self, class_node: Node) -> str | None:
        class_name = self._get_node_name(class_node, cs.FIELD_TYPE)
        if class_name:
            return class_name
        return next(
            (
                child.text.decode(cs.ENCODING_UTF8)
                for child in class_node.children
                if child.type == cs.TS_TYPE_IDENTIFIER and child.is_named and child.text
            ),
            None,
        )

    def _get_class_name_for_node(
        self, class_node: Node, language: cs.SupportedLanguage
    ) -> str | None:
        if language == cs.SupportedLanguage.RUST and class_node.type == cs.TS_IMPL_ITEM:
            return self._get_rust_impl_class_name(class_node)
        return self._get_node_name(class_node)

    def _process_methods_in_class(
        self,
        body_node: Node,
        class_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        if sorted_func_nodes is not None and func_node_starts is not None:
            body_start = body_node.start_byte
            body_end = body_node.end_byte
            lo = bisect_left(func_node_starts, body_start)
            hi = bisect_right(func_node_starts, body_end)
            method_nodes = [
                n
                for n in sorted_func_nodes[lo:hi]
                if n.end_byte <= body_end and isinstance(n, Node)
            ]
        else:
            method_query = queries[language][cs.QUERY_FUNCTIONS]
            if not method_query:
                return
            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)
            method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
        for method_node in method_nodes:
            if not isinstance(method_node, Node):
                continue
            if language == cs.SupportedLanguage.CPP:
                method_name = cpp_utils.extract_function_name(method_node)
            else:
                method_name = self._get_node_name(method_node)
            if not method_name:
                continue
            method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
            filtered = (
                self._filter_calls_in_node(all_call_nodes, call_starts, method_node)
                if all_call_nodes is not None and call_starts is not None
                else None
            )
            self._ingest_function_calls(
                method_node,
                method_qn,
                cs.NodeLabel.METHOD,
                module_qn,
                language,
                queries,
                class_qn,
                call_nodes=filtered,
                call_name_cache=call_name_cache,
            )

    def _process_calls_in_classes(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        all_call_nodes: list[Node] | None = None,
        call_starts: list[int] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
        combined_captures: dict[str, list] | None = None,
        sorted_func_nodes: list[Node] | None = None,
        func_node_starts: list[int] | None = None,
    ) -> None:
        if combined_captures and cs.CAPTURE_CLASS in combined_captures:
            class_nodes = combined_captures[cs.CAPTURE_CLASS]
        else:
            query = queries[language][cs.QUERY_CLASSES]
            if not query:
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue
            class_name = self._get_class_name_for_node(class_node, language)
            if not class_name:
                continue
            class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
            if body_node := class_node.child_by_field_name(cs.FIELD_BODY):
                self._process_methods_in_class(
                    body_node,
                    class_qn,
                    module_qn,
                    language,
                    queries,
                    all_call_nodes,
                    call_starts,
                    call_name_cache=call_name_cache,
                    sorted_func_nodes=sorted_func_nodes,
                    func_node_starts=func_node_starts,
                )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        if func_child := call_node.child_by_field_name(cs.TS_FIELD_FUNCTION):
            match func_child.type:
                case (
                    cs.TS_IDENTIFIER
                    | cs.TS_ATTRIBUTE
                    | cs.TS_MEMBER_EXPRESSION
                    | cs.CppNodeType.QUALIFIED_IDENTIFIER
                    | cs.TS_SCOPED_IDENTIFIER
                ):
                    if func_child.text is not None:
                        return str(func_child.text.decode(cs.ENCODING_UTF8))
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        return str(field_node.text.decode(cs.ENCODING_UTF8))
                case cs.TS_PARENTHESIZED_EXPRESSION:
                    return self._get_iife_target_name(func_child)

        match call_node.type:
            case (
                cs.TS_CPP_BINARY_EXPRESSION
                | cs.TS_CPP_UNARY_EXPRESSION
                | cs.TS_CPP_UPDATE_EXPRESSION
            ):
                operator_node = call_node.child_by_field_name(cs.FIELD_OPERATOR)
                if operator_node and operator_node.text:
                    operator_text = operator_node.text.decode(cs.ENCODING_UTF8)
                    return cpp_utils.convert_operator_symbol_to_name(operator_text)
            case cs.TS_METHOD_INVOCATION:
                object_node = call_node.child_by_field_name(cs.FIELD_OBJECT)
                name_node = call_node.child_by_field_name(cs.FIELD_NAME)
                if name_node and name_node.text:
                    method_name = str(name_node.text.decode(cs.ENCODING_UTF8))
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = str(object_node.text.decode(cs.ENCODING_UTF8))
                    return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return str(name_node.text.decode(cs.ENCODING_UTF8))

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case cs.TS_FUNCTION_EXPRESSION:
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case cs.TS_ARROW_FUNCTION:
                    return f"{cs.IIFE_ARROW_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
        return None

    def _ingest_function_calls(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None = None,
        call_nodes: list[Node] | None = None,
        call_name_cache: dict[int, str | None] | None = None,
    ) -> None:
        if language in _TYPED_LANGUAGES:
            local_var_types = (
                self._resolver.type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language
                )
            )
        else:
            local_var_types = None

        if call_nodes is None:
            calls_query = queries[language].get(cs.QUERY_CALLS)
            if not calls_query:
                return
            cursor = QueryCursor(calls_query)
            captures = sorted_captures(cursor, caller_node)
            call_nodes = captures.get(cs.CAPTURE_CALL, [])

        if not call_nodes:
            return

        is_java = language == cs.SupportedLanguage.JAVA
        method_invocation_type = cs.TS_METHOD_INVOCATION
        resolver = self._resolver
        resolve_func = resolver.resolve_function_call
        resolve_builtin = resolver.resolve_builtin_call
        resolve_cpp_op = resolver.resolve_cpp_operator_call
        get_target = self._get_call_target_name
        class_label = cs.NodeLabel.CLASS
        ensure_rel = self.ingestor.ensure_relationship_batch
        calls_rel = cs.RelationshipType.CALLS
        qn_key = cs.KEY_QUALIFIED_NAME
        _id = id
        has_cache = call_name_cache is not None

        for call_node in call_nodes:
            node_id = _id(call_node)
            if has_cache and node_id in call_name_cache:
                call_name = call_name_cache[node_id]
            else:
                call_name = get_target(call_node)
                if has_cache:
                    call_name_cache[node_id] = call_name
            if not call_name:
                continue

            if is_java and call_node.type == method_invocation_type:
                callee_info = resolver.resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = resolve_func(
                    call_name, module_qn, local_var_types, class_context
                )
            if not callee_info:
                callee_info = resolve_builtin(call_name)
            if not callee_info:
                callee_info = resolve_cpp_op(call_name, module_qn)
            if not callee_info:
                continue

            callee_type, callee_qn = callee_info
            if callee_type == class_label:
                continue

            ensure_rel(
                (caller_type, qn_key, caller_qn),
                calls_rel,
                (callee_type, qn_key, callee_qn),
            )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts: list[str] = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT, node=func_node, parent_type=type(current)
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name(cs.FIELD_NAME):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode(cs.ENCODING_UTF8))
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}{cs.SEPARATOR_DOT}{cs.SEPARATOR_DOT.join(path_parts)}{cs.SEPARATOR_DOT}{func_name}"
        return f"{module_qn}{cs.SEPARATOR_DOT}{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        return is_method_node(func_node, lang_config)
