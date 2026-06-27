from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

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
from .utils import (
    get_function_captures,
    is_method_node,
    python_parameter_names,
    safe_decode_text,
    sorted_captures,
)


class _CallableFlowArg(NamedTuple):
    # (H) One call-site argument that may carry a callable: bound either to a concrete
    # (H) function (source_concrete) or to a parameter of the caller (source_caller +
    # (H) source_param), keyed to the callee parameter by position or keyword.
    callee_qn: str
    position: int
    keyword: str
    source_concrete: str
    source_caller: str
    source_param: str


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
    __slots__ = (
        "ingestor",
        "repo_path",
        "project_name",
        "_resolver",
        "_flow_param_names",
        "_flow_args",
    )

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
        # (H) Inter-procedural callable-parameter flow: ordered params per function and
        # (H) the per-call-site argument bindings, resolved to a fixpoint in finalize.
        self._flow_param_names: dict[str, list[str]] = {}
        self._flow_args: list[_CallableFlowArg] = []

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

    def _filter_top_level_calls(
        self,
        all_call_nodes: list[Node],
        call_starts: list[int],
        func_nodes: list[Node],
    ) -> list[Node]:
        # (H) Calls lexically inside a function/method belong to that function,
        # (H) not the module; only genuine top-level calls (module-load time,
        # (H) including `if __name__ == "__main__"` blocks) are module-attributed.
        nested_starts: set[int] = set()
        for func_node in func_nodes:
            for call in self._filter_calls_in_node(
                all_call_nodes, call_starts, func_node
            ):
                nested_starts.add(call.start_byte)
        return [c for c in all_call_nodes if c.start_byte not in nested_starts]

    def _module_qn(self, relative_path: Path, file_name: str) -> str:
        if file_name in (cs.INIT_PY, cs.MOD_RS):
            return cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        return cs.SEPARATOR_DOT.join(
            [self.project_name] + list(relative_path.with_suffix("").parts)
        )

    def collect_callable_field_bindings(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        # (H) Pre-pass: record which functions are bound to a class's callable
        # (H) fields (FQNSpec(get_name=_python_get_name, ...)). Runs before call
        # (H) resolution so a field invocation can resolve regardless of which
        # (H) file the construction site lives in. Keyword bindings only;
        # (H) positional callable args would need declared field order.
        if language != cs.SupportedLanguage.PYTHON:
            return
        try:
            module_qn = self._module_qn(
                cached_relative_path(file_path, self.repo_path), file_path.name
            )
            if (
                func_class_captures_cache is not None
                and file_path in func_class_captures_cache
            ):
                call_nodes = func_class_captures_cache[file_path].get(cs.CAPTURE_CALL)
            else:
                call_nodes = None
            if call_nodes is None:
                call_nodes, _ = self._collect_all_call_nodes(
                    root_node, language, queries
                )
            resolver = self._resolver
            registry = resolver.function_registry
            callable_labels = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            for call_node in call_nodes:
                _positional, keyword = self._parse_call_arguments(call_node)
                if not keyword:
                    continue
                name = self._get_call_target_name(call_node)
                if not name:
                    continue
                callee = resolver.resolve_function_call(name, module_qn)
                if not callee or callee[0] != cs.NodeLabel.CLASS:
                    continue
                for field, value_node in keyword.items():
                    if not (value_text := safe_decode_text(value_node)):
                        continue
                    bound = resolver.resolve_function_call(value_text, module_qn)
                    if bound and bound[0] in callable_labels and bound[1] in registry:
                        resolver.record_callable_field_binding(
                            callee[1], field, bound[1]
                        )
        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

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
            module_qn = self._module_qn(relative_path, file_path.name)

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
                call_starts: list[int] | None = None
            else:
                all_call_nodes, call_starts = self._collect_all_call_nodes(
                    root_node, language, queries
                )

            sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)
            if sorted_func_nodes or combined_captures.get(cs.CAPTURE_CLASS):
                if cached_calls is not None:
                    call_starts = [n.start_byte for n in all_call_nodes]
                func_node_starts = (
                    [n.start_byte for n in sorted_func_nodes]
                    if sorted_func_nodes
                    else None
                )
            else:
                call_starts = None
                func_node_starts = None

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
            if not all_call_nodes:
                return
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
            if sorted_func_nodes and call_starts is not None:
                module_calls = self._filter_top_level_calls(
                    all_call_nodes, call_starts, sorted_func_nodes
                )
            else:
                module_calls = all_call_nodes
            self._ingest_function_calls(
                root_node,
                module_qn,
                cs.NodeLabel.MODULE,
                module_qn,
                language,
                queries,
                call_nodes=module_calls,
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
            has_classes = bool(combined_captures.get(cs.CAPTURE_CLASS))
        else:
            result = get_function_captures(root_node, language, queries)
            if not result:
                return
            lang_config, captures = result
            func_nodes = captures.get(cs.CAPTURE_FUNCTION, [])
            has_classes = bool(captures.get(cs.CAPTURE_CLASS))
        for func_node in func_nodes:
            if has_classes and self._is_method(func_node, lang_config):
                continue

            if language == cs.SupportedLanguage.CPP:
                func_name = cpp_utils.extract_function_name(func_node)
            else:
                func_name = self._get_node_name(func_node)
            if not func_name:
                continue
            # (H) An out-of-line C++ method definition (`Ret Class::method() {...}`
            # (H) at namespace/file scope) is bound by the definition pass to its
            # (H) class node (qn `class_qn.method`). Attribute its body's calls to
            # (H) that method node, not a phantom module-rooted free-function qn,
            # (H) so the CALLS edges join to a real node.
            if language == cs.SupportedLanguage.CPP and (
                bound := self._cpp_out_of_class_method_caller(
                    func_node, func_name, module_qn
                )
            ):
                caller_qn, class_qn = bound
                filtered = (
                    self._filter_calls_in_node(all_call_nodes, call_starts, func_node)
                    if all_call_nodes is not None and call_starts is not None
                    else None
                )
                self._ingest_function_calls(
                    func_node,
                    caller_qn,
                    cs.NodeLabel.METHOD,
                    module_qn,
                    language,
                    queries,
                    class_qn,
                    call_nodes=filtered,
                    call_name_cache=call_name_cache,
                )
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

    def _cpp_out_of_class_method_caller(
        self, func_node: Node, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        # (H) Resolve an out-of-line C++ method definition to its (method_qn,
        # (H) class_qn), mirroring the definition pass's class binding. The leaf
        # (H) class name resolves the class across files (header-declared classes);
        # (H) `endswith(normalized)` guards against a leaf collision binding to the
        # (H) wrong class, and the registry membership check ensures the method node
        # (H) actually exists before overriding the default attribution.
        if not cpp_utils.is_out_of_class_method_definition(func_node):
            return None
        class_name = cpp_utils.extract_class_name_from_out_of_class_method(func_node)
        if not class_name:
            return None
        normalized = class_name.replace(cs.SEPARATOR_DOUBLE_COLON, cs.SEPARATOR_DOT)
        leaf = normalized.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        class_qn = self._resolver._resolve_class_name(leaf, module_qn)
        if not class_qn or not class_qn.endswith(normalized):
            return None
        caller_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        if caller_qn in self._resolver.function_registry:
            return caller_qn, class_qn
        return None

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
                n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end
            ]
        else:
            method_query = queries[language][cs.QUERY_FUNCTIONS]
            if not method_query:
                return
            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)
            method_nodes = method_captures.get(cs.CAPTURE_FUNCTION, [])
        for method_node in method_nodes:
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
        if combined_captures is not None:
            class_nodes = combined_captures.get(cs.CAPTURE_CLASS, [])
        else:
            query = queries[language][cs.QUERY_CLASSES]
            if not query:
                return
            cursor = QueryCursor(query)
            captures = sorted_captures(cursor, root_node)
            class_nodes = captures.get(cs.CAPTURE_CLASS, [])

        for class_node in class_nodes:
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
        # (H) A macro-internal call (Rust `name(args)` inside a token_tree) is
        # (H) captured as the bare identifier node; its text is the callee name.
        if call_node.type == cs.TS_IDENTIFIER and call_node.text is not None:
            return call_node.text.decode(cs.ENCODING_UTF8)
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
                        return func_child.text.decode(cs.ENCODING_UTF8)
                case cs.TS_GENERIC_FUNCTION:
                    # (H) turbofish: unwrap to the underlying callee identifier
                    inner = func_child.child_by_field_name(cs.TS_FIELD_FUNCTION)
                    if inner and inner.text:
                        return inner.text.decode(cs.ENCODING_UTF8)
                case cs.TS_CPP_FIELD_EXPRESSION:
                    field_node = func_child.child_by_field_name(cs.FIELD_FIELD)
                    if field_node and field_node.text:
                        return field_node.text.decode(cs.ENCODING_UTF8)
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
                    method_name = name_node.text.decode(cs.ENCODING_UTF8)
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = object_node.text.decode(cs.ENCODING_UTF8)
                    return f"{object_text}{cs.SEPARATOR_DOT}{method_name}"

        if name_node := call_node.child_by_field_name(cs.FIELD_NAME):
            if name_node.text is not None:
                return name_node.text.decode(cs.ENCODING_UTF8)

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

        caller_spec = (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn)

        caller_params: frozenset[str] = frozenset()
        if language == cs.SupportedLanguage.PYTHON:
            ordered_params = python_parameter_names(caller_node)
            self._flow_param_names[caller_qn] = ordered_params
            caller_params = frozenset(ordered_params)

        # (H) Runs independently of call_nodes: a getter access is an attribute, not
        # (H) a call, so callers that read a property but make no other call must
        # (H) still reach this pass before the early return below.
        if language == cs.SupportedLanguage.PYTHON and (
            prop_names := self._resolver.function_registry.property_names()
        ):
            self._ingest_property_accesses(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                prop_names,
            )

        # (H) Operator syntax (k in r, r[k], r[k]=v, len(r)) dispatches to dunder
        # (H) methods; emit those edges when the operand is a first-party type.
        if language == cs.SupportedLanguage.PYTHON:
            self._ingest_operator_dispatch_calls(
                caller_node, caller_spec, module_qn, local_var_types
            )

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
        is_js_ts = language in (cs.SupportedLanguage.JS, cs.SupportedLanguage.TS)
        is_cpp = language == cs.SupportedLanguage.CPP
        method_invocation_type = cs.TS_METHOD_INVOCATION
        resolver = self._resolver
        resolve_func = resolver.resolve_function_call
        resolve_builtin = resolver.resolve_builtin_call if is_js_ts else None
        resolve_cpp_op = resolver.resolve_cpp_operator_call if is_cpp else None
        get_target = self._get_call_target_name
        class_label = cs.NodeLabel.CLASS
        ensure_rel = self.ingestor.ensure_relationship_batch
        calls_rel = cs.RelationshipType.CALLS
        qn_key = cs.KEY_QUALIFIED_NAME
        _id = id
        is_python = language == cs.SupportedLanguage.PYTHON
        alias_map: dict[str, str] | None = None

        for call_node in call_nodes:
            node_id = _id(call_node)
            if call_name_cache is not None and node_id in call_name_cache:
                call_name = call_name_cache[node_id]
            else:
                call_name = get_target(call_node)
                if call_name_cache is not None:
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
            if not callee_info and resolve_builtin is not None:
                callee_info = resolve_builtin(call_name)
            if not callee_info and resolve_cpp_op is not None:
                callee_info = resolve_cpp_op(call_name, module_qn)
            if not callee_info and is_python and cs.SEPARATOR_DOT not in call_name:
                # (H) A bare name that resolves to nothing may be a local alias of a
                # (H) callable (do = self._start; do()). Resolve the assignment's
                # (H) right-hand side and treat the alias call as a call to it.
                if alias_map is None:
                    alias_map = self._build_local_alias_map(
                        caller_node, queries[language][cs.QUERY_CONFIG], module_qn
                    )
                if (rhs := alias_map.get(call_name)) is not None:
                    callee_info = resolve_func(
                        rhs, module_qn, local_var_types, class_context
                    )

            if not callee_info and is_python and cs.SEPARATOR_DOT in call_name:
                # (H) recv.field(...) where field is a callable struct field:
                # (H) resolve to the functions bound to it at construction sites.
                self._ingest_callable_field_calls(
                    call_name, caller_spec, local_var_types, ensure_rel
                )

            if is_python and call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in (
                cs.HIGHER_ORDER_BUILTINS
            ):
                # (H) sorted(xs, key=f) and friends invoke f synchronously in this
                # (H) frame, so the trace attributes the call to the enclosing fn.
                self._ingest_higher_order_builtin_calls(
                    call_node,
                    caller_spec,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                )

            if not callee_info:
                continue

            callee_type, callee_qn = callee_info

            if is_python:
                self._collect_callable_flow(
                    call_node,
                    callee_qn,
                    caller_qn,
                    caller_params,
                    module_qn,
                    local_var_types,
                    class_context,
                )

            if is_python and (
                dispatch_targets := resolver.protocol_dispatch_targets(callee_qn)
            ):
                # (H) The call resolved to a Protocol stub; the stub never runs, so emit
                # (H) edges to the method on every conformer instead of the stub.
                for conformer_type, conformer_qn in dispatch_targets:
                    for target_qn in resolver.function_registry.variants(conformer_qn):
                        ensure_rel(
                            caller_spec,
                            calls_rel,
                            (conformer_type, qn_key, target_qn),
                        )
                continue

            if is_python:
                # (H) f(...) invoked through a parameter: the edge runs from the
                # (H) callee to whatever each call site binds to that parameter.
                self._ingest_callable_param_calls(
                    call_node,
                    callee_type,
                    callee_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                )

            if callee_type == class_label:
                # (H) Instantiating a class is a call to its __init__ at runtime;
                # (H) redirect to the constructor when the class defines one.
                init_qn = f"{callee_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                if init_qn not in resolver.function_registry:
                    continue
                callee_type = cs.NodeLabel.METHOD
                callee_qn = init_qn

            for target_qn in resolver.function_registry.variants(callee_qn):
                ensure_rel(
                    caller_spec,
                    calls_rel,
                    (callee_type, qn_key, target_qn),
                )

    def _ingest_operator_dispatch_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        boundary = (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary:
                continue
            match node.type:
                case cs.TS_PY_SUBSCRIPT:
                    parent = node.parent
                    left = (
                        parent.child_by_field_name(cs.TS_FIELD_LEFT)
                        if parent is not None and parent.type == cs.TS_PY_ASSIGNMENT
                        else None
                    )
                    is_write = left is not None and left.id == node.id
                    self._emit_operator_dunder(
                        node.child_by_field_name(cs.FIELD_VALUE),
                        cs.PY_DUNDER_SETITEM if is_write else cs.PY_DUNDER_GETITEM,
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case cs.TS_PY_COMPARISON_OPERATOR:
                    operators = node.child_by_field_name(cs.TS_FIELD_OPERATORS)
                    if (
                        operators is not None
                        and (op_text := safe_decode_text(operators))
                        and cs.PY_OP_IN in op_text.split()
                        and node.named_children
                    ):
                        self._emit_operator_dunder(
                            node.named_children[-1],
                            cs.PY_DUNDER_CONTAINS,
                            caller_spec,
                            module_qn,
                            local_var_types,
                        )
                case cs.TS_PY_CALL:
                    func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
                    args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
                    if (
                        func is not None
                        and safe_decode_text(func) == cs.PY_BUILTIN_LEN
                        and args is not None
                        and len(args.named_children) == 1
                    ):
                        self._emit_operator_dunder(
                            args.named_children[0],
                            cs.PY_DUNDER_LEN,
                            caller_spec,
                            module_qn,
                            local_var_types,
                        )
                case cs.TS_PY_BOOLEAN_OPERATOR:
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_LEFT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_RIGHT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case cs.TS_PY_NOT_OPERATOR:
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_ARGUMENT),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
                case (
                    cs.TS_PY_IF_STATEMENT
                    | cs.TS_PY_WHILE_STATEMENT
                    | cs.TS_PY_ELIF_CLAUSE
                    | cs.TS_PY_CONDITIONAL_EXPRESSION
                ):
                    # (H) A bare object as a condition is tested for truthiness; nested
                    # (H) boolean/not operators are handled when the walk reaches them.
                    self._emit_truthiness(
                        node.child_by_field_name(cs.TS_FIELD_CONDITION),
                        caller_spec,
                        module_qn,
                        local_var_types,
                    )
            stack.extend(node.children)

    def _emit_truthiness(
        self,
        operand: Node | None,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # (H) Truthiness of an object calls __bool__ if defined, else __len__. Only a
        # (H) bare name/attribute operand names an object (a comparison/call is already
        # (H) a bool and is handled elsewhere); try __bool__ first, then __len__.
        if operand is None or operand.type not in (
            cs.TS_PY_IDENTIFIER,
            cs.TS_PY_ATTRIBUTE,
        ):
            return
        for dunder in (cs.PY_DUNDER_BOOL, cs.PY_DUNDER_LEN):
            if self._emit_operator_dunder(
                operand, dunder, caller_spec, module_qn, local_var_types
            ):
                return

    def _emit_operator_dunder(
        self,
        operand: Node | None,
        dunder: str,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        # (H) Resolve the implied <operand>.__dunder__ call; resolution only succeeds
        # (H) for a first-party class that defines the dunder, so builtin containers
        # (H) (dict/list) yield no edge. Restrict to simple attribute/name operands.
        # (H) Returns whether an edge was emitted (truthiness tries __bool__ then __len__).
        if operand is None or not (operand_text := safe_decode_text(operand)):
            return False
        if any(ch in operand_text for ch in cs.PY_OPERAND_REJECT_CHARS):
            return False
        targets = self._resolver.operator_dunder_targets(
            operand_text, dunder, module_qn, local_var_types
        )
        if not targets:
            return False
        for callee_type, callee_qn in targets:
            for target_qn in self._resolver.function_registry.variants(callee_qn):
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (callee_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
        return True

    def _parse_call_arguments(
        self, call_node: Node
    ) -> tuple[list[Node], dict[str, Node]]:
        positional: list[Node] = []
        keyword: dict[str, Node] = {}
        args_node = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if args_node is None:
            return positional, keyword
        for child in args_node.named_children:
            if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                name_node = child.child_by_field_name(cs.FIELD_NAME)
                value_node = child.child_by_field_name(cs.FIELD_VALUE)
                if (
                    name_node is not None
                    and value_node is not None
                    and (name := safe_decode_text(name_node)) is not None
                ):
                    keyword[name] = value_node
            else:
                positional.append(child)
        return positional, keyword

    def _emit_callback_edge(
        self,
        source_spec: tuple[str, str, str],
        arg_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
    ) -> None:
        if not (arg_text := safe_decode_text(arg_node)):
            return
        if not (
            resolved := resolve_func(
                arg_text, module_qn, local_var_types, class_context
            )
        ):
            return
        res_type, res_qn = resolved
        registry = self._resolver.function_registry
        if res_type == cs.NodeLabel.CLASS:
            init_qn = f"{res_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
            if init_qn not in registry:
                return
            res_type = cs.NodeLabel.METHOD
            res_qn = init_qn
        for target_qn in registry.variants(res_qn):
            ensure_rel(
                source_spec,
                cs.RelationshipType.CALLS,
                (res_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def _ingest_callable_param_calls(
        self,
        call_node: Node,
        callee_type: str,
        callee_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
    ) -> None:
        if not (params := self._resolver.function_registry.callable_params(callee_qn)):
            return
        positional, keyword = self._parse_call_arguments(call_node)
        source_spec = (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn)
        for param_name, index in params.items():
            arg_node = keyword.get(param_name)
            if arg_node is None and index < len(positional):
                arg_node = positional[index]
            if arg_node is not None:
                self._emit_callback_edge(
                    source_spec,
                    arg_node,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    ensure_rel,
                )

    def _collect_callable_flow(
        self,
        call_node: Node,
        callee_qn: str,
        caller_qn: str,
        caller_params: frozenset[str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        # (H) Record, for each call-site argument that names a callable, whether it is a
        # (H) concrete function or a parameter of the caller (a pass-through). The
        # (H) fixpoint in finalize propagates concretes through pass-through params to
        # (H) the functions that actually invoke them.
        positional, keyword = self._parse_call_arguments(call_node)
        items: list[tuple[int, str, Node]] = [
            (index, "", node) for index, node in enumerate(positional)
        ]
        items.extend((-1, name, node) for name, node in keyword.items())
        callable_labels = (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
            cs.NodeLabel.CLASS,
        )
        for position, keyword_name, arg_node in items:
            if arg_node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
                continue
            arg_text = safe_decode_text(arg_node)
            if not arg_text:
                continue
            if arg_node.type == cs.TS_PY_IDENTIFIER and arg_text in caller_params:
                self._flow_args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, "", caller_qn, arg_text
                    )
                )
                continue
            resolved = self._resolver.resolve_function_call(
                arg_text, module_qn, local_var_types, class_context
            )
            if resolved is not None and resolved[0] in callable_labels:
                self._flow_args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, resolved[1], "", ""
                    )
                )

    def finalize_callable_param_flow(self) -> None:
        # (H) Resolve the recorded call-site argument bindings to a fixpoint and emit a
        # (H) CALLS edge from every function that invokes a callable parameter to each
        # (H) concrete function that can reach it (directly or via pass-through params).
        registry = self._resolver.function_registry
        seeds: dict[tuple[str, str], set[str]] = defaultdict(set)
        edges: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for arg in self._flow_args:
            if arg.keyword:
                param_name = arg.keyword
            else:
                callee_params = self._flow_param_names.get(arg.callee_qn)
                if callee_params is None or not (
                    0 <= arg.position < len(callee_params)
                ):
                    continue
                param_name = callee_params[arg.position]
            slot = (arg.callee_qn, param_name)
            if arg.source_concrete:
                seeds[slot].add(arg.source_concrete)
            else:
                edges[slot].add((arg.source_caller, arg.source_param))

        bindings: dict[tuple[str, str], set[str]] = {
            k: set(v) for k, v in seeds.items()
        }
        for slot in edges:
            bindings.setdefault(slot, set())
        changed = True
        while changed:
            changed = False
            for slot, sources in edges.items():
                for source in sources:
                    if (reachable := bindings.get(source)) and not reachable.issubset(
                        bindings[slot]
                    ):
                        bindings[slot] |= reachable
                        changed = True

        ensure_rel = self.ingestor.ensure_relationship_batch
        for func_qn, invoked in (
            (qn, registry.callable_params(qn)) for qn in self._flow_param_names
        ):
            if not invoked or (func_type := registry.get(func_qn)) is None:
                continue
            source_spec = (func_type, cs.KEY_QUALIFIED_NAME, func_qn)
            for param_name in invoked:
                for target_qn in bindings.get((func_qn, param_name), ()):
                    target_type = registry.get(target_qn)
                    if target_type is None:
                        continue
                    for variant in registry.variants(target_qn):
                        ensure_rel(
                            source_spec,
                            cs.RelationshipType.CALLS,
                            (target_type, cs.KEY_QUALIFIED_NAME, variant),
                        )

    def _ingest_callable_field_calls(
        self,
        call_name: str,
        caller_spec: tuple[str, str, str],
        local_var_types: dict[str, str] | None,
        ensure_rel,
    ) -> None:
        recv, sep, field = call_name.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return
        recv_type = local_var_types.get(recv) if local_var_types else None
        targets = self._resolver.callable_field_targets(field, recv_type)
        if not targets:
            return
        registry = self._resolver.function_registry
        for target_qn in targets:
            if target_qn in registry:
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (registry[target_qn], cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _ingest_higher_order_builtin_calls(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func,
        ensure_rel,
    ) -> None:
        positional, keyword = self._parse_call_arguments(call_node)
        for arg_node in (*positional, *keyword.values()):
            self._emit_callback_edge(
                caller_spec,
                arg_node,
                module_qn,
                local_var_types,
                class_context,
                resolve_func,
                ensure_rel,
            )

    def _build_local_alias_map(
        self, caller_node: Node, lang_config: LanguageSpec, module_qn: str
    ) -> dict[str, str]:
        identifier = cs.TS_PY_IDENTIFIER
        attribute = cs.TS_PY_ATTRIBUTE
        assignment = cs.TS_PY_ASSIGNMENT
        left_field = cs.TS_FIELD_LEFT
        right_field = cs.TS_FIELD_RIGHT
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        aliases: dict[str, str] = {}
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == assignment:
                left = node.child_by_field_name(left_field)
                right = node.child_by_field_name(right_field)
                if (
                    left is not None
                    and left.type == identifier
                    and (left_text := left.text) is not None
                    and right is not None
                    and (
                        target := self._alias_reference_text(
                            right, identifier, attribute, module_qn
                        )
                    )
                    is not None
                ):
                    aliases.setdefault(left_text.decode(cs.ENCODING_UTF8), target)
            stack.extend(node.children)
        return aliases

    def _alias_reference_text(
        self, right: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        # (H) An alias rhs is a plain name/attribute, a conditional that picks one
        # (H) (resolve_builtin_call if is_js_ts else None), or getattr(recv, name)
        # (H) dynamic dispatch. Take the name/attribute branch (consequence or
        # (H) alternative, never the condition) or build recv.<name> for getattr.
        if right.type in (identifier, attribute):
            return right.text.decode(cs.ENCODING_UTF8) if right.text else None
        if right.type == cs.TS_PY_CONDITIONAL_EXPRESSION and right.named_children:
            for branch in (right.named_children[0], right.named_children[-1]):
                if branch.type in (identifier, attribute) and branch.text:
                    return branch.text.decode(cs.ENCODING_UTF8)
        if right.type == cs.TS_PY_CALL:
            return self._getattr_reference_text(right, identifier, attribute, module_qn)
        return None

    def _getattr_reference_text(
        self, call: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if (
            func is None
            or safe_decode_text(func) != cs.PY_BUILTIN_GETATTR
            or args is None
            or len(args.named_children) < 2
        ):
            return None
        receiver, name_node = args.named_children[0], args.named_children[1]
        if receiver.type not in (identifier, attribute):
            return None
        if (name := self._resolve_str_const(name_node, module_qn)) is None:
            return None
        return f"{safe_decode_text(receiver)}{cs.SEPARATOR_DOT}{name}"

    def _resolve_str_const(self, node: Node, module_qn: str) -> str | None:
        # (H) Resolve a getattr name argument to its string value: a string literal
        # (H) directly, or a module-level constant (cs.METHOD_X / METHOD_X) read from
        # (H) the defining module's AST.
        if node.type == cs.TS_PY_STRING:
            content = next(
                (c for c in node.children if c.type == cs.TS_PY_STRING_CONTENT), None
            )
            return safe_decode_text(content) if content is not None else None
        if node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
            return None
        name_text = safe_decode_text(node)
        if not name_text:
            return None
        import_map = self._resolver.import_processor.import_mapping.get(module_qn, {})
        prefix, _, const_name = name_text.rpartition(cs.SEPARATOR_DOT)
        if not prefix:
            mapped = import_map.get(const_name)
            const_module_qn = (
                mapped.rsplit(cs.SEPARATOR_DOT, 1)[0] if mapped else module_qn
            )
        elif (mapped_module := import_map.get(prefix)) is not None:
            const_module_qn = mapped_module
        else:
            const_module_qn = prefix
        return self._module_string_constant(const_module_qn, const_name)

    def _module_string_constant(self, module_qn: str, const_name: str) -> str | None:
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or file_path not in type_inference.ast_cache:
            return None
        root_node, _ = type_inference.ast_cache[file_path]
        for child in root_node.children:
            if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.children:
                continue
            assignment = child.children[0]
            if assignment.type != cs.TS_PY_ASSIGNMENT:
                continue
            left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
            right = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and safe_decode_text(left) == const_name
                and right is not None
                and right.type == cs.TS_PY_STRING
            ):
                return self._resolve_str_const(right, module_qn)
        return None

    def _ingest_property_accesses(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # (H) Accessing an @property getter invokes the getter method at runtime, but
        # (H) tree-sitter sees a plain attribute, not a call. Resolve attribute
        # (H) accesses whose tail names a known property and emit a CALLS edge to the
        # (H) getter (skipping the attribute that is itself a call's function, which
        # (H) the call path above already resolves).
        resolver = self._resolver
        resolve_func = resolver.resolve_function_call
        registry = resolver.function_registry
        ensure_rel = self.ingestor.ensure_relationship_batch
        calls_rel = cs.RelationshipType.CALLS
        qn_key = cs.KEY_QUALIFIED_NAME
        method_label = cs.NodeLabel.METHOD
        attr_type = cs.TS_PY_ATTRIBUTE
        call_type = cs.TS_PY_CALL
        func_field = cs.TS_FIELD_FUNCTION
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == attr_type and (text := node.text) is not None:
                attr_text = text.decode(cs.ENCODING_UTF8)
                if attr_text.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
                    parent = node.parent
                    is_call_target = (
                        parent is not None
                        and parent.type == call_type
                        and parent.child_by_field_name(func_field) is node
                    )
                    if not is_call_target and (
                        callee_info := resolve_func(
                            attr_text, module_qn, local_var_types, class_context
                        )
                    ):
                        callee_qn = callee_info[1]
                        if (
                            registry.is_property(callee_qn)
                            and callee_qn != caller_qn
                            and callee_qn not in seen
                        ):
                            seen.add(callee_qn)
                            for target_qn in registry.variants(callee_qn):
                                ensure_rel(
                                    caller_spec,
                                    calls_rel,
                                    (method_label, qn_key, target_qn),
                                )
            stack.extend(node.children)

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
