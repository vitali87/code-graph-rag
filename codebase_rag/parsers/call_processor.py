from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..language_spec import LanguageSpec
from ..services import IngestorProtocol
from ..types_defs import LanguageQueries, NodeType
from .cpp_utils import convert_operator_symbol_to_name, extract_cpp_function_name
from .import_processor import ImportProcessor
from .python_utils import resolve_class_name
from .type_inference import TypeInferenceEngine

if TYPE_CHECKING:
    from ..graph_updater import FunctionRegistryTrie


class CallProcessor:
    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrie,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance

    def _get_node_name(self, node: Node, field: str = "name") -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        if text is None:
            return None
        return text.decode(cs.ENCODING_UTF8)

    def _resolve_class_qn_from_type(
        self, var_type: str, import_map: dict[str, str], module_qn: str
    ) -> str:
        if cs.SEPARATOR_DOT in var_type:
            return var_type
        if var_type in import_map:
            return import_map[var_type]
        return self._resolve_class_name(var_type, module_qn) or ""

    def _try_resolve_method(
        self, class_qn: str, method_name: str, separator: str = cs.SEPARATOR_DOT
    ) -> tuple[str, str] | None:
        method_qn = f"{class_qn}{separator}{method_name}"
        if method_qn in self.function_registry:
            return self.function_registry[method_qn], method_qn
        return self._resolve_inherited_method(class_qn, method_name)

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

        local_var_types = self.type_inference.build_local_variable_type_map(
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
                callee_info = self._resolve_java_method_call(
                    call_node, module_qn, local_var_types
                )
            else:
                callee_info = self._resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                )
            if callee_info:
                callee_type, callee_qn = callee_info
            elif builtin_info := self._resolve_builtin_call(call_name):
                callee_type, callee_qn = builtin_info
            elif operator_info := self._resolve_cpp_operator_call(call_name, module_qn):
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

    def _process_nested_calls_in_node(
        self,
        call_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        func_child = call_node.child_by_field_name("function")
        if not func_child:
            return

        if func_child.type == "attribute":
            self._find_and_process_nested_calls(
                func_child,
                caller_qn,
                caller_type,
                module_qn,
                local_var_types,
                class_context,
            )

    def _find_and_process_nested_calls(
        self,
        node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        if node.type == "call":
            self._process_nested_calls_in_node(
                node, caller_qn, caller_type, module_qn, local_var_types, class_context
            )

            if call_name := self._get_call_target_name(node):
                if callee_info := self._resolve_function_call(
                    call_name, module_qn, local_var_types, class_context
                ):
                    callee_type, callee_qn = callee_info
                    logger.debug(
                        ls.CALL_NESTED_FOUND.format(
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

        for child in node.children:
            self._find_and_process_nested_calls(
                child, caller_qn, caller_type, module_qn, local_var_types, class_context
            )

    def _resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        if call_name and (
            call_name.startswith(cs.IIFE_FUNC_PREFIX)
            or call_name.startswith(cs.IIFE_ARROW_PREFIX)
        ):
            iife_qn = f"{module_qn}.{call_name}"
            if iife_qn in self.function_registry:
                return self.function_registry[iife_qn], iife_qn

        if (
            call_name == cs.KEYWORD_SUPER
            or call_name.startswith(f"{cs.KEYWORD_SUPER}.")
            or call_name.startswith(f"{cs.KEYWORD_SUPER}()")
        ):
            return self._resolve_super_call(call_name, class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]

            if call_name in import_map:
                imported_qn = import_map[call_name]
                if imported_qn in self.function_registry:
                    logger.debug(
                        ls.CALL_DIRECT_IMPORT.format(
                            call_name=call_name, qn=imported_qn
                        )
                    )
                    return self.function_registry[imported_qn], imported_qn

            if (
                cs.SEPARATOR_DOT in call_name
                or cs.SEPARATOR_DOUBLE_COLON in call_name
                or cs.SEPARATOR_COLON in call_name
            ):
                if cs.SEPARATOR_DOUBLE_COLON in call_name:
                    separator = cs.SEPARATOR_DOUBLE_COLON
                elif cs.SEPARATOR_COLON in call_name:
                    separator = cs.SEPARATOR_COLON
                else:
                    separator = cs.SEPARATOR_DOT
                parts = call_name.split(separator)

                if len(parts) == 2:
                    object_name, method_name = parts

                    if local_var_types and object_name in local_var_types:
                        var_type = local_var_types[object_name]
                        class_qn = self._resolve_class_qn_from_type(
                            var_type, import_map, module_qn
                        )

                        if class_qn:
                            method_qn = f"{class_qn}{separator}{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    ls.CALL_TYPE_INFERRED.format(
                                        call_name=call_name,
                                        method_qn=method_qn,
                                        obj=object_name,
                                        var_type=var_type,
                                    )
                                )
                                return self.function_registry[method_qn], method_qn

                            if inherited_method := self._resolve_inherited_method(
                                class_qn, method_name
                            ):
                                logger.debug(
                                    ls.CALL_TYPE_INFERRED_INHERITED.format(
                                        call_name=call_name,
                                        method_qn=inherited_method[1],
                                        obj=object_name,
                                        var_type=var_type,
                                    )
                                )
                                return inherited_method

                        if var_type in cs.JS_BUILTIN_TYPES:
                            return (
                                cs.NodeLabel.FUNCTION,
                                f"{cs.BUILTIN_PREFIX}.{var_type}.prototype.{method_name}",
                            )

                    if object_name in import_map:
                        class_qn = import_map[object_name]

                        if cs.SEPARATOR_DOUBLE_COLON in class_qn:
                            rust_parts = class_qn.split(cs.SEPARATOR_DOUBLE_COLON)
                            class_name = rust_parts[-1]

                            matching_qns = self.function_registry.find_ending_with(
                                class_name
                            )
                            for qn in matching_qns:
                                if self.function_registry.get(qn) == NodeType.CLASS:
                                    class_qn = qn
                                    break

                        potential_class_qn = f"{class_qn}.{object_name}"
                        test_method_qn = f"{potential_class_qn}{separator}{method_name}"
                        if test_method_qn in self.function_registry:
                            class_qn = potential_class_qn

                        registry_separator = (
                            separator
                            if separator == cs.SEPARATOR_COLON
                            else cs.SEPARATOR_DOT
                        )
                        method_qn = f"{class_qn}{registry_separator}{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                ls.CALL_IMPORT_STATIC.format(
                                    call_name=call_name, method_qn=method_qn
                                )
                            )
                            return self.function_registry[method_qn], method_qn

                    method_qn = f"{module_qn}.{method_name}"
                    if method_qn in self.function_registry:
                        logger.debug(
                            ls.CALL_OBJECT_METHOD.format(
                                call_name=call_name, method_qn=method_qn
                            )
                        )
                        return self.function_registry[method_qn], method_qn

                if len(parts) >= 3 and parts[0] == cs.KEYWORD_SELF:
                    attribute_ref = cs.SEPARATOR_DOT.join(parts[:-1])
                    method_name = parts[-1]

                    if local_var_types and attribute_ref in local_var_types:
                        var_type = local_var_types[attribute_ref]
                        class_qn = self._resolve_class_qn_from_type(
                            var_type, import_map, module_qn
                        )

                        if class_qn:
                            method_qn = f"{class_qn}.{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    ls.CALL_INSTANCE_ATTR.format(
                                        call_name=call_name,
                                        method_qn=method_qn,
                                        attr_ref=attribute_ref,
                                        var_type=var_type,
                                    )
                                )
                                return self.function_registry[method_qn], method_qn

                            if inherited_method := self._resolve_inherited_method(
                                class_qn, method_name
                            ):
                                logger.debug(
                                    ls.CALL_INSTANCE_ATTR_INHERITED.format(
                                        call_name=call_name,
                                        method_qn=inherited_method[1],
                                        attr_ref=attribute_ref,
                                        var_type=var_type,
                                    )
                                )
                                return inherited_method
                else:
                    class_name = parts[0]
                    method_name = cs.SEPARATOR_DOT.join(parts[1:])

                    if class_name in import_map:
                        class_qn = import_map[class_name]
                        method_qn = f"{class_qn}.{method_name}"
                        if method_qn in self.function_registry:
                            logger.debug(
                                ls.CALL_IMPORT_QUALIFIED.format(
                                    call_name=call_name, method_qn=method_qn
                                )
                            )
                            return self.function_registry[method_qn], method_qn

                    if local_var_types and class_name in local_var_types:
                        var_type = local_var_types[class_name]
                        class_qn = self._resolve_class_qn_from_type(
                            var_type, import_map, module_qn
                        )

                        if class_qn:
                            method_qn = f"{class_qn}.{method_name}"
                            if method_qn in self.function_registry:
                                logger.debug(
                                    ls.CALL_INSTANCE_QUALIFIED.format(
                                        call_name=call_name,
                                        method_qn=method_qn,
                                        class_name=class_name,
                                        var_type=var_type,
                                    )
                                )
                                return self.function_registry[method_qn], method_qn

                            if inherited_method := self._resolve_inherited_method(
                                class_qn, method_name
                            ):
                                logger.debug(
                                    ls.CALL_INSTANCE_INHERITED.format(
                                        call_name=call_name,
                                        method_qn=inherited_method[1],
                                        class_name=class_name,
                                        var_type=var_type,
                                    )
                                )
                                return inherited_method

            for local_name, imported_qn in import_map.items():
                if local_name.startswith("*"):
                    potential_qns = []

                    if cs.SEPARATOR_DOUBLE_COLON not in imported_qn:
                        potential_qns.append(f"{imported_qn}.{call_name}")
                    potential_qns.append(
                        f"{imported_qn}{cs.SEPARATOR_DOUBLE_COLON}{call_name}"
                    )
                    for wildcard_qn in potential_qns:
                        if wildcard_qn in self.function_registry:
                            logger.debug(
                                ls.CALL_WILDCARD.format(
                                    call_name=call_name, qn=wildcard_qn
                                )
                            )
                            return self.function_registry[wildcard_qn], wildcard_qn

        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                ls.CALL_SAME_MODULE.format(call_name=call_name, qn=same_module_func_qn)
            )
            return (
                self.function_registry[same_module_func_qn],
                same_module_func_qn,
            )

        search_name = re.split(r"[.:]|::", call_name)[-1]

        if possible_matches := self.function_registry.find_ending_with(search_name):
            possible_matches.sort(
                key=lambda qn: self._calculate_import_distance(qn, module_qn)
            )
            best_candidate_qn = possible_matches[0]
            logger.debug(
                ls.CALL_TRIE_FALLBACK.format(call_name=call_name, qn=best_candidate_qn)
            )
            return (
                self.function_registry[best_candidate_qn],
                best_candidate_qn,
            )

        logger.debug(ls.CALL_UNRESOLVED.format(call_name=call_name))
        return None

    def _resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
        if call_name in cs.JS_BUILTIN_PATTERNS:
            return (cs.NodeLabel.FUNCTION, f"{cs.BUILTIN_PREFIX}.{call_name}")

        for suffix in (".bind", ".call", ".apply"):
            if call_name.endswith(suffix):
                method = suffix[1:]
                return (
                    cs.NodeLabel.FUNCTION,
                    f"{cs.BUILTIN_PREFIX}.Function.prototype.{method}",
                )

        if ".prototype." in call_name and (
            call_name.endswith(".call") or call_name.endswith(".apply")
        ):
            base_call = call_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
            return (cs.NodeLabel.FUNCTION, base_call)

        return None

    def _resolve_cpp_operator_call(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name.startswith(cs.OPERATOR_PREFIX):
            return None

        if call_name in cs.CPP_OPERATORS:
            return (cs.NodeLabel.FUNCTION, cs.CPP_OPERATORS[call_name])

        if possible_matches := self.function_registry.find_ending_with(call_name):
            same_module_ops = [
                qn
                for qn in possible_matches
                if qn.startswith(module_qn) and call_name in qn
            ]
            candidates = same_module_ops or possible_matches
            candidates.sort(key=lambda qn: (len(qn), qn))
            best = candidates[0]
            return (self.function_registry[best], best)

        return None

    def _is_method_chain(self, call_name: str) -> bool:
        if "(" in call_name and ")" in call_name:
            parts = call_name.split(cs.SEPARATOR_DOT)
            method_calls = sum("(" in part and ")" in part for part in parts)
            return method_calls >= 1 and len(parts) >= 2
        return False

    def _resolve_chained_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        match = re.search(r"\.([^.()]+)$", call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        if object_type := self.type_inference._infer_expression_return_type(
            object_expr, module_qn, local_var_types
        ):
            full_object_type = object_type
            if cs.SEPARATOR_DOT not in object_type:
                if resolved_class := self._resolve_class_name(object_type, module_qn):
                    full_object_type = resolved_class

            method_qn = f"{full_object_type}.{final_method}"

            if method_qn in self.function_registry:
                logger.debug(
                    ls.CALL_CHAINED.format(
                        call_name=call_name,
                        method_qn=method_qn,
                        obj_expr=object_expr,
                        obj_type=object_type,
                    )
                )
                return self.function_registry[method_qn], method_qn

            if inherited_method := self._resolve_inherited_method(
                full_object_type, final_method
            ):
                logger.debug(
                    ls.CALL_CHAINED_INHERITED.format(
                        call_name=call_name,
                        method_qn=inherited_method[1],
                        obj_expr=object_expr,
                        obj_type=object_type,
                    )
                )
                return inherited_method

        return None

    def _resolve_super_call(
        self, call_name: str, class_context: str | None = None
    ) -> tuple[str, str] | None:
        match call_name:
            case _ if call_name == cs.KEYWORD_SUPER:
                method_name = cs.KEYWORD_CONSTRUCTOR
            case _ if cs.SEPARATOR_DOT in call_name:
                method_name = call_name.split(cs.SEPARATOR_DOT, 1)[1]
            case _:
                return None

        current_class_qn = class_context
        if not current_class_qn:
            logger.debug(ls.CALL_SUPER_NO_CONTEXT.format(call_name=call_name))
            return None

        if current_class_qn not in self.class_inheritance:
            logger.debug(ls.CALL_SUPER_NO_INHERITANCE.format(class_qn=current_class_qn))
            return None

        parent_classes = self.class_inheritance[current_class_qn]
        if not parent_classes:
            logger.debug(ls.CALL_SUPER_NO_PARENTS.format(class_qn=current_class_qn))
            return None

        if result := self._resolve_inherited_method(current_class_qn, method_name):
            callee_type, parent_method_qn = result
            logger.debug(
                ls.CALL_SUPER_RESOLVED.format(
                    call_name=call_name, method_qn=parent_method_qn
                )
            )
            return callee_type, parent_method_qn

        logger.debug(
            ls.CALL_SUPER_UNRESOLVED.format(
                call_name=call_name, class_qn=current_class_qn
            )
        )
        return None

    def _resolve_inherited_method(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        if class_qn not in self.class_inheritance:
            return None

        queue = list(self.class_inheritance.get(class_qn, []))
        visited = set(queue)

        while queue:
            parent_class_qn = queue.pop(0)
            parent_method_qn = f"{parent_class_qn}.{method_name}"

            if parent_method_qn in self.function_registry:
                return (
                    self.function_registry[parent_method_qn],
                    parent_method_qn,
                )

            if parent_class_qn in self.class_inheritance:
                for grandparent_qn in self.class_inheritance[parent_class_qn]:
                    if grandparent_qn not in visited:
                        visited.add(grandparent_qn)
                        queue.append(grandparent_qn)

        return None

    def _calculate_import_distance(
        self, candidate_qn: str, caller_module_qn: str
    ) -> int:
        caller_parts = caller_module_qn.split(cs.SEPARATOR_DOT)
        candidate_parts = candidate_qn.split(cs.SEPARATOR_DOT)

        common_prefix = 0
        for i in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[i] == candidate_parts[i]:
                common_prefix += 1
            else:
                break

        base_distance = max(len(caller_parts), len(candidate_parts)) - common_prefix

        if candidate_qn.startswith(
            cs.SEPARATOR_DOT.join(caller_parts[:-1]) + cs.SEPARATOR_DOT
        ):
            base_distance -= 1

        return base_distance

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
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

    def _resolve_java_method_call(
        self,
        call_node: Node,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> tuple[str, str] | None:
        java_engine = self.type_inference.java_type_inference

        result = java_engine.resolve_java_method_call(
            call_node, local_var_types, module_qn
        )

        if result:
            call_text = (
                call_node.text.decode(cs.ENCODING_UTF8) if call_node.text else "unknown"
            )
            logger.debug(
                ls.CALL_JAVA_RESOLVED.format(call_text=call_text, method_qn=result[1])
            )

        return result
