from __future__ import annotations

from pathlib import Path

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import FunctionRegistryTrieProtocol, LanguageQueries
from .call_resolver import CallResolver
from .cpp_utils import convert_operator_symbol_to_name, extract_cpp_function_name
from .import_processor import ImportProcessor
from .type_inference import TypeInferenceEngine


class CallProcessor:
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

    def _get_node_name(self, node: Node, field: str = "name") -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        if text is None:
            return None
        return text.decode(cs.ENCODING_UTF8)

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        relative_path = file_path.relative_to(self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE.format(path=relative_path))

        try:
            module_qn = cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in (cs.INIT_PY, cs.MOD_RS):
                module_qn = cs.SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_path.parent.parts)
                )

            self._process_calls_in_functions(root_node, module_qn, language, queries)
            self._process_calls_in_classes(root_node, module_qn, language, queries)
            self._process_module_level_calls(root_node, module_qn, language, queries)

        except Exception as e:
            logger.error(ls.CALL_PROCESSING_FAILED.format(path=file_path, error=e))

    def _process_calls_in_functions(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]
        lang_config: LanguageSpec = lang_queries["config"]

        query = lang_queries["functions"]
        if not query:
            return
        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        func_nodes = captures.get("function", [])
        for func_node in func_nodes:
            if not isinstance(func_node, Node):
                continue
            if self._is_method(func_node, lang_config):
                continue

            if language == cs.SupportedLanguage.CPP:
                func_name = extract_cpp_function_name(func_node)
            else:
                func_name = self._get_node_name(func_node)
            if not func_name:
                continue
            if func_qn := self._build_nested_qualified_name(
                func_node, module_qn, func_name, lang_config
            ):
                self._ingest_function_calls(
                    func_node,
                    func_qn,
                    cs.NodeLabel.FUNCTION,
                    module_qn,
                    language,
                    queries,
                )

    def _process_calls_in_classes(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        lang_queries = queries[language]
        query = lang_queries["classes"]
        if not query:
            return

        cursor = QueryCursor(query)
        captures = cursor.captures(root_node)
        class_nodes = captures.get("class", [])

        for class_node in class_nodes:
            if not isinstance(class_node, Node):
                continue

            if language == cs.SupportedLanguage.RUST and class_node.type == "impl_item":
                class_name = self._get_node_name(class_node, "type")
                if not class_name:
                    for child in class_node.children:
                        if (
                            child.type == "type_identifier"
                            and child.is_named
                            and child.text
                        ):
                            class_name = child.text.decode(cs.ENCODING_UTF8)
                            break
            else:
                class_name = self._get_node_name(class_node)
            if not class_name:
                continue
            class_qn = f"{module_qn}.{class_name}"
            body_node = class_node.child_by_field_name("body")
            if not body_node:
                continue

            method_query = lang_queries["functions"]
            if not method_query:
                continue
            method_cursor = QueryCursor(method_query)
            method_captures = method_cursor.captures(body_node)
            method_nodes = method_captures.get("function", [])
            for method_node in method_nodes:
                if not isinstance(method_node, Node):
                    continue
                method_name = self._get_node_name(method_node)
                if not method_name:
                    continue
                method_qn = f"{class_qn}.{method_name}"

                self._ingest_function_calls(
                    method_node,
                    method_qn,
                    cs.NodeLabel.METHOD,
                    module_qn,
                    language,
                    queries,
                    class_qn,
                )

    def _process_module_level_calls(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: dict[cs.SupportedLanguage, LanguageQueries],
    ) -> None:
        self._ingest_function_calls(
            root_node, module_qn, cs.NodeLabel.MODULE, module_qn, language, queries
        )

    def _get_call_target_name(self, call_node: Node) -> str | None:
        if func_child := call_node.child_by_field_name("function"):
            match func_child.type:
                case (
                    "identifier"
                    | "attribute"
                    | "member_expression"
                    | "qualified_identifier"
                    | "scoped_identifier"
                ):
                    if func_child.text is not None:
                        return str(func_child.text.decode(cs.ENCODING_UTF8))
                case "field_expression":
                    field_node = func_child.child_by_field_name("field")
                    if field_node and field_node.text:
                        return str(field_node.text.decode(cs.ENCODING_UTF8))
                case "parenthesized_expression":
                    return self._get_iife_target_name(func_child)

        match call_node.type:
            case "binary_expression" | "unary_expression" | "update_expression":
                operator_node = call_node.child_by_field_name("operator")
                if operator_node and operator_node.text:
                    operator_text = operator_node.text.decode(cs.ENCODING_UTF8)
                    return convert_operator_symbol_to_name(operator_text)
            case "method_invocation":
                object_node = call_node.child_by_field_name("object")
                name_node = call_node.child_by_field_name("name")
                if name_node and name_node.text:
                    method_name = str(name_node.text.decode(cs.ENCODING_UTF8))
                    if not object_node or not object_node.text:
                        return method_name
                    object_text = str(object_node.text.decode(cs.ENCODING_UTF8))
                    return f"{object_text}.{method_name}"

        if name_node := call_node.child_by_field_name("name"):
            if name_node.text is not None:
                return str(name_node.text.decode(cs.ENCODING_UTF8))

        return None

    def _get_iife_target_name(self, parenthesized_expr: Node) -> str | None:
        for child in parenthesized_expr.children:
            match child.type:
                case "function_expression":
                    return f"{cs.IIFE_FUNC_PREFIX}{child.start_point[0]}_{child.start_point[1]}"
                case "arrow_function":
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
    ) -> None:
        calls_query = queries[language].get("calls")
        if not calls_query:
            return

        local_var_types = self._resolver.type_inference.build_local_variable_type_map(
            caller_node, module_qn, language
        )

        cursor = QueryCursor(calls_query)
        captures = cursor.captures(caller_node)
        call_nodes = captures.get("call", [])

        logger.debug(
            ls.CALL_FOUND_NODES.format(
                count=len(call_nodes), language=language, caller=caller_qn
            )
        )

        for call_node in call_nodes:
            if not isinstance(call_node, Node):
                continue

            """(H) We removed _process_nested_calls_in_node because tree-sitter query finds
            ALL call nodes including nested ones. The recursive nested call processing
            was causing O(N*M) complexity, leading to extreme slowdowns on files with
            many nested calls."""

            call_name = self._get_call_target_name(call_node)
            if not call_name:
                continue

            if (
                language == cs.SupportedLanguage.JAVA
                and call_node.type == "method_invocation"
            ):
                callee_info = self._resolver.resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = self._resolver.resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                )
            if callee_info:
                callee_type, callee_qn = callee_info
            elif builtin_info := self._resolver.resolve_builtin_call(call_name):
                callee_type, callee_qn = builtin_info
            elif operator_info := self._resolver.resolve_cpp_operator_call(
                call_name, module_qn
            ):
                callee_type, callee_qn = operator_info
            else:
                continue
            logger.debug(
                ls.CALL_FOUND.format(
                    caller=caller_qn,
                    call_name=call_name,
                    callee_type=callee_type,
                    callee_qn=callee_qn,
                )
            )

            self.ingestor.ensure_relationship_batch(
                (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn),
                cs.RelationshipType.CALLS,
                (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
            )

    def _build_nested_qualified_name(
        self,
        func_node: Node,
        module_qn: str,
        func_name: str,
        lang_config: LanguageSpec,
    ) -> str | None:
        path_parts = []
        current = func_node.parent

        if not isinstance(current, Node):
            logger.warning(
                ls.CALL_UNEXPECTED_PARENT.format(
                    node=func_node, parent_type=type(current)
                )
            )
            return None

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.function_node_types:
                if name_node := current.child_by_field_name("name"):
                    text = name_node.text
                    if text is not None:
                        path_parts.append(text.decode(cs.ENCODING_UTF8))
            elif current.type in lang_config.class_node_types:
                return None

            current = current.parent

        path_parts.reverse()
        if path_parts:
            return f"{module_qn}.{'.'.join(path_parts)}.{func_name}"
        else:
            return f"{module_qn}.{func_name}"

    def _is_method(self, func_node: Node, lang_config: LanguageSpec) -> bool:
        current = func_node.parent
        if not isinstance(current, Node):
            return False

        while current and current.type not in lang_config.module_node_types:
            if current.type in lang_config.class_node_types:
                return True
            current = current.parent
        return False
