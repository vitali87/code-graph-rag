from __future__ import annotations

import re

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..types_defs import FunctionRegistryTrieProtocol, NodeType
from .import_processor import ImportProcessor
from .py import resolve_class_name
from .type_inference import TypeInferenceEngine


class CallResolver:
    def __init__(
        self,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        class_inheritance: dict[str, list[str]],
    ) -> None:
        self.function_registry = function_registry
        self.import_processor = import_processor
        self.type_inference = type_inference
        self.class_inheritance = class_inheritance

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

    def resolve_function_call(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
    ) -> tuple[str, str] | None:
        if result := self._try_resolve_iife(call_name, module_qn):
            return result

        if self._is_super_call(call_name):
            return self._resolve_super_call(call_name, class_context)

        if cs.SEPARATOR_DOT in call_name and self._is_method_chain(call_name):
            return self._resolve_chained_call(call_name, module_qn, local_var_types)

        if result := self._try_resolve_via_imports(
            call_name, module_qn, local_var_types
        ):
            return result

        if result := self._try_resolve_same_module(call_name, module_qn):
            return result

        return self._try_resolve_via_trie(call_name, module_qn)

    def _try_resolve_iife(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not call_name:
            return None
        if not (
            call_name.startswith(cs.IIFE_FUNC_PREFIX)
            or call_name.startswith(cs.IIFE_ARROW_PREFIX)
        ):
            return None
        iife_qn = f"{module_qn}.{call_name}"
        if iife_qn in self.function_registry:
            return self.function_registry[iife_qn], iife_qn
        return None

    def _is_super_call(self, call_name: str) -> bool:
        return (
            call_name == cs.KEYWORD_SUPER
            or call_name.startswith(f"{cs.KEYWORD_SUPER}.")
            or call_name.startswith(f"{cs.KEYWORD_SUPER}()")
        )

    def _try_resolve_via_imports(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if module_qn not in self.import_processor.import_mapping:
            return None

        import_map = self.import_processor.import_mapping[module_qn]

        if result := self._try_resolve_direct_import(call_name, import_map):
            return result

        if result := self._try_resolve_qualified_call(
            call_name, import_map, module_qn, local_var_types
        ):
            return result

        return self._try_resolve_wildcard_imports(call_name, import_map)

    def _try_resolve_direct_import(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        if call_name not in import_map:
            return None
        imported_qn = import_map[call_name]
        if imported_qn in self.function_registry:
            logger.debug(
                ls.CALL_DIRECT_IMPORT.format(call_name=call_name, qn=imported_qn)
            )
            return self.function_registry[imported_qn], imported_qn
        return None

    def _try_resolve_qualified_call(
        self,
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if not self._has_separator(call_name):
            return None

        separator = self._get_separator(call_name)
        parts = call_name.split(separator)

        if len(parts) == 2:
            if result := self._resolve_two_part_call(
                parts, call_name, separator, import_map, module_qn, local_var_types
            ):
                return result

        if len(parts) >= 3 and parts[0] == cs.KEYWORD_SELF:
            return self._resolve_self_attribute_call(
                parts, call_name, import_map, module_qn, local_var_types
            )

        return self._resolve_multi_part_call(
            parts, call_name, import_map, module_qn, local_var_types
        )

    def _has_separator(self, call_name: str) -> bool:
        return (
            cs.SEPARATOR_DOT in call_name
            or cs.SEPARATOR_DOUBLE_COLON in call_name
            or cs.SEPARATOR_COLON in call_name
        )

    def _get_separator(self, call_name: str) -> str:
        if cs.SEPARATOR_DOUBLE_COLON in call_name:
            return cs.SEPARATOR_DOUBLE_COLON
        if cs.SEPARATOR_COLON in call_name:
            return cs.SEPARATOR_COLON
        return cs.SEPARATOR_DOT

    def _try_resolve_wildcard_imports(
        self, call_name: str, import_map: dict[str, str]
    ) -> tuple[str, str] | None:
        for local_name, imported_qn in import_map.items():
            if not local_name.startswith("*"):
                continue
            if result := self._try_wildcard_qns(call_name, imported_qn):
                return result
        return None

    def _try_wildcard_qns(
        self, call_name: str, imported_qn: str
    ) -> tuple[str, str] | None:
        potential_qns = []
        if cs.SEPARATOR_DOUBLE_COLON not in imported_qn:
            potential_qns.append(f"{imported_qn}.{call_name}")
        potential_qns.append(f"{imported_qn}{cs.SEPARATOR_DOUBLE_COLON}{call_name}")

        for wildcard_qn in potential_qns:
            if wildcard_qn in self.function_registry:
                logger.debug(
                    ls.CALL_WILDCARD.format(call_name=call_name, qn=wildcard_qn)
                )
                return self.function_registry[wildcard_qn], wildcard_qn
        return None

    def _try_resolve_same_module(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        same_module_func_qn = f"{module_qn}.{call_name}"
        if same_module_func_qn in self.function_registry:
            logger.debug(
                ls.CALL_SAME_MODULE.format(call_name=call_name, qn=same_module_func_qn)
            )
            return self.function_registry[same_module_func_qn], same_module_func_qn
        return None

    def _try_resolve_via_trie(
        self, call_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        search_name = re.split(r"[.:]|::", call_name)[-1]
        possible_matches = self.function_registry.find_ending_with(search_name)
        if not possible_matches:
            logger.debug(ls.CALL_UNRESOLVED.format(call_name=call_name))
            return None

        possible_matches.sort(
            key=lambda qn: self._calculate_import_distance(qn, module_qn)
        )
        best_candidate_qn = possible_matches[0]
        logger.debug(
            ls.CALL_TRIE_FALLBACK.format(call_name=call_name, qn=best_candidate_qn)
        )
        return self.function_registry[best_candidate_qn], best_candidate_qn

    def _resolve_two_part_call(
        self,
        parts: list[str],
        call_name: str,
        separator: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        object_name, method_name = parts

        if local_var_types and object_name in local_var_types:
            var_type = local_var_types[object_name]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
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

                matching_qns = self.function_registry.find_ending_with(class_name)
                for qn in matching_qns:
                    if self.function_registry.get(qn) == NodeType.CLASS:
                        class_qn = qn
                        break

            potential_class_qn = f"{class_qn}.{object_name}"
            test_method_qn = f"{potential_class_qn}{separator}{method_name}"
            if test_method_qn in self.function_registry:
                class_qn = potential_class_qn

            registry_separator = (
                separator if separator == cs.SEPARATOR_COLON else cs.SEPARATOR_DOT
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
                ls.CALL_OBJECT_METHOD.format(call_name=call_name, method_qn=method_qn)
            )
            return self.function_registry[method_qn], method_qn

        return None

    def _resolve_self_attribute_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        attribute_ref = cs.SEPARATOR_DOT.join(parts[:-1])
        method_name = parts[-1]

        if local_var_types and attribute_ref in local_var_types:
            var_type = local_var_types[attribute_ref]
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
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

        return None

    def _resolve_multi_part_call(
        self,
        parts: list[str],
        call_name: str,
        import_map: dict[str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
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
            if class_qn := self._resolve_class_qn_from_type(
                var_type, import_map, module_qn
            ):
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

        return None

    def resolve_builtin_call(self, call_name: str) -> tuple[str, str] | None:
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

    def resolve_cpp_operator_call(
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

        if (
            object_type
            := self.type_inference.python_type_inference._infer_expression_return_type(
                object_expr, module_qn, local_var_types
            )
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

    def resolve_java_method_call(
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
