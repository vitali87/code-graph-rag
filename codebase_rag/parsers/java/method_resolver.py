from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...decorators import recursion_guard
from ...types_defs import ASTNode, NodeType
from ..utils import safe_decode_text
from .utils import extract_method_call_info, get_class_context_from_qn

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol, FunctionRegistryTrieProtocol
    from ..import_processor import ImportProcessor


class JavaMethodResolverMixin:
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    project_name: str
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    class_inheritance: dict[str, list[str]]
    _fqn_to_module_qn: dict[str, list[str]]

    @abstractmethod
    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str: ...

    @abstractmethod
    def _rank_module_candidates(
        self, candidates: list[str], class_qn: str, current_module_qn: str | None
    ) -> list[str]: ...

    @abstractmethod
    def _find_registry_entries_under(
        self, prefix: str
    ) -> Iterable[tuple[str, str]]: ...

    @abstractmethod
    def _get_superclass_name(self, class_qn: str) -> str | None: ...

    @abstractmethod
    def _get_implemented_interfaces(self, class_qn: str) -> list[str]: ...

    @abstractmethod
    def _get_current_class_name(self, module_qn: str) -> str | None: ...

    @abstractmethod
    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None: ...

    def _resolve_java_object_type(
        self, object_ref: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if object_ref in local_var_types:
            return local_var_types[object_ref]

        # (H) Check for 'this' reference - find the containing class (using trie for O(k) lookup)
        if object_ref == cs.JAVA_KEYWORD_THIS:
            return next(
                (
                    str(qn)
                    for qn, entity_type in self.function_registry.find_with_prefix(
                        module_qn
                    )
                    if entity_type == NodeType.CLASS
                ),
                None,
            )

        # (H) Check for 'super' reference - for super calls, look at parent classes (using trie for O(k) lookup)
        if object_ref == cs.JAVA_KEYWORD_SUPER:
            for qn, entity_type in self.function_registry.find_with_prefix(module_qn):
                if entity_type == NodeType.CLASS:
                    if parent_qn := self._find_parent_class(qn):
                        return parent_qn
            return None

        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if object_ref in import_map:
                return import_map[object_ref]

        simple_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_ref}"
        if (
            simple_class_qn in self.function_registry
            and self.function_registry[simple_class_qn] == NodeType.CLASS
        ):
            return simple_class_qn

        return None

    def _find_parent_class(self, class_qn: str) -> str | None:
        parent_classes = self.class_inheritance.get(class_qn, [])
        return parent_classes[0] if parent_classes else None

    def _resolve_static_or_local_method(
        self, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        return next(
            (
                (entity_type, qn)
                for qn, entity_type in self.function_registry.find_with_prefix(
                    module_qn
                )
                if entity_type in cs.JAVA_CALLABLE_ENTITY_TYPES
                and qn.split(cs.CHAR_PAREN_OPEN)[0].endswith(
                    f"{cs.SEPARATOR_DOT}{method_name}"
                )
            ),
            None,
        )

    def _resolve_instance_method(
        self, object_type: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        resolved_type = self._resolve_java_type_name(object_type, module_qn)

        if method_result := self._find_method_with_any_signature(
            resolved_type, method_name, module_qn
        ):
            return method_result

        if inherited_result := self._find_inherited_method(
            resolved_type, method_name, module_qn
        ):
            return inherited_result

        return self._find_interface_method(resolved_type, method_name, module_qn)

    def _find_method_with_any_signature(
        self, class_qn: str, method_name: str, current_module_qn: str | None = None
    ) -> tuple[str, str] | None:
        if class_qn:
            if result := self._search_method_in_class(class_qn, method_name):
                return result

        if class_qn and not class_qn.startswith(self.project_name):
            return self._search_method_in_alternate_modules(
                class_qn, method_name, current_module_qn
            )

        return None

    def _search_method_in_class(
        self, class_qn: str, method_name: str
    ) -> tuple[str, str] | None:
        for qn, method_type in self._find_registry_entries_under(class_qn):
            if qn == class_qn:
                continue
            suffix = qn[len(class_qn) :]
            if not suffix.startswith(cs.SEPARATOR_DOT):
                continue
            member = suffix[1:]
            if self._is_matching_method(member, method_name):
                return method_type, qn
        return None

    def _search_method_in_alternate_modules(
        self, class_qn: str, method_name: str, current_module_qn: str | None
    ) -> tuple[str, str] | None:
        suffixes = class_qn.split(cs.SEPARATOR_DOT)
        lookup_keys = [
            cs.SEPARATOR_DOT.join(suffixes[i:]) for i in range(len(suffixes))
        ] or [class_qn]

        candidate_modules = self._collect_candidate_modules(lookup_keys)
        ranked_candidates = self._rank_module_candidates(
            candidate_modules, class_qn, current_module_qn
        )

        simple_class_name = class_qn.split(cs.SEPARATOR_DOT)[-1]

        for module_qn in ranked_candidates:
            registry_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{simple_class_name}"
            if result := self._search_method_in_class(registry_class_qn, method_name):
                return result

        return None

    def _collect_candidate_modules(self, lookup_keys: list[str]) -> list[str]:
        candidate_modules: list[str] = []
        seen_modules: set[str] = set()

        for key in lookup_keys:
            if key in self._fqn_to_module_qn:
                for module_candidate in self._fqn_to_module_qn[key]:
                    if module_candidate not in seen_modules:
                        candidate_modules.append(module_candidate)
                        seen_modules.add(module_candidate)

        return candidate_modules

    def _is_matching_method(self, member: str, method_name: str) -> bool:
        return (
            member == method_name
            or member.startswith(f"{method_name}{cs.CHAR_PAREN_OPEN}")
            or member == f"{method_name}{cs.EMPTY_PARENS}"
        )

    @recursion_guard(
        key_func=lambda self, class_qn, *_, **__: class_qn,
        guard_name=cs.GUARD_INHERITED_METHOD,
    )
    def _find_inherited_method(
        self, class_qn: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        if not (superclass_qn := self._get_superclass_name(class_qn)):
            return None

        if method_result := self._find_method_with_any_signature(
            superclass_qn, method_name, module_qn
        ):
            return method_result

        return self._find_inherited_method(superclass_qn, method_name, module_qn)

    def _find_interface_method(
        self, class_qn: str, method_name: str, module_qn: str
    ) -> tuple[str, str] | None:
        for interface_qn in self._get_implemented_interfaces(class_qn):
            if method_result := self._find_method_with_any_signature(
                interface_qn, method_name, module_qn
            ):
                return method_result

        return None

    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        if not method_call:
            return None

        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            method_name = method_call
            if (current_class_qn := self._get_current_class_name(module_qn)) and (
                result := self._find_method_return_type(current_class_qn, method_name)
            ):
                return result
        else:
            object_part = cs.SEPARATOR_DOT.join(parts[:-1])
            method_name = parts[-1]

            if object_part in self.function_registry:
                return self._find_method_return_type(object_part, method_name)

            if object_type := self._lookup_variable_type(object_part, module_qn):
                return self._find_method_return_type(object_type, method_name)

            potential_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{object_part}"
            if potential_class_qn in self.function_registry:
                return self._find_method_return_type(potential_class_qn, method_name)

        return self._heuristic_method_return_type(method_call)

    def _find_method_return_type(self, class_qn: str, method_name: str) -> str | None:
        if not class_qn or not method_name:
            return None

        ctx = get_class_context_from_qn(
            class_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not ctx:
            return None

        return self._find_method_return_type_in_ast(
            ctx.root_node, ctx.target_class_name, method_name, ctx.module_qn
        )

    def _find_method_return_type_in_ast(
        self, node: ASTNode, class_name: str, method_name: str, module_qn: str
    ) -> str | None:
        if node.type == cs.TS_CLASS_DECLARATION:
            if (
                name_node := node.child_by_field_name(cs.KEY_NAME)
            ) and safe_decode_text(name_node) == class_name:
                if body_node := node.child_by_field_name(cs.FIELD_BODY):
                    return self._search_methods_in_class_body(
                        body_node, method_name, module_qn
                    )

        for child in node.children:
            if result := self._find_method_return_type_in_ast(
                child, class_name, method_name, module_qn
            ):
                return result

        return None

    def _search_methods_in_class_body(
        self, body_node: ASTNode, method_name: str, module_qn: str
    ) -> str | None:
        for child in body_node.children:
            if child.type == cs.TS_METHOD_DECLARATION:
                if (
                    name_node := child.child_by_field_name(cs.KEY_NAME)
                ) and safe_decode_text(name_node) == method_name:
                    if (type_node := child.child_by_field_name(cs.KEY_TYPE)) and (
                        return_type := safe_decode_text(type_node)
                    ):
                        return self._resolve_java_type_name(return_type, module_qn)
        return None

    def _heuristic_method_return_type(self, method_call: str) -> str | None:
        method_lower = method_call.lower()
        if cs.JAVA_GETTER_PATTERN in method_lower:
            if cs.JAVA_NAME_PATTERN in method_lower:
                return cs.JAVA_TYPE_STRING_FQN
            if cs.JAVA_ID_PATTERN in method_lower:
                return cs.JAVA_TYPE_LONG
            if (
                cs.JAVA_SIZE_PATTERN in method_lower
                or cs.JAVA_LENGTH_PATTERN in method_lower
            ):
                return cs.JAVA_TYPE_INT

        if (
            cs.JAVA_CREATE_PATTERN in method_lower
            or cs.JAVA_NEW_PATTERN in method_lower
        ):
            parts = method_call.split(cs.SEPARATOR_DOT)
            if len(parts) >= 2:
                method_name_lower = parts[-1].lower()
                if cs.JAVA_USER_PATTERN in method_name_lower:
                    return cs.JAVA_HEURISTIC_USER
                if cs.JAVA_ORDER_PATTERN in method_name_lower:
                    return cs.JAVA_HEURISTIC_ORDER

        if cs.JAVA_IS_PATTERN in method_lower or cs.JAVA_HAS_PATTERN in method_lower:
            return cs.JAVA_TYPE_BOOLEAN

        return None

    def _do_resolve_java_method_call(
        self, call_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> tuple[str, str] | None:
        if call_node.type != cs.TS_METHOD_INVOCATION:
            return None

        call_info = extract_method_call_info(call_node)
        if not call_info:
            return None

        method_name = call_info[cs.FIELD_NAME]
        object_ref = call_info[cs.FIELD_OBJECT]

        if not method_name:
            logger.debug(ls.JAVA_NO_METHOD_NAME)
            return None

        logger.debug(
            ls.JAVA_RESOLVING_CALL.format(method=method_name, object=object_ref)
        )

        if not object_ref:
            logger.debug(ls.JAVA_RESOLVING_STATIC.format(method=method_name))
            result = self._resolve_static_or_local_method(str(method_name), module_qn)
            if result:
                logger.debug(ls.JAVA_FOUND_STATIC.format(result=result))
            else:
                logger.debug(ls.JAVA_STATIC_NOT_FOUND.format(method=method_name))
            return result

        logger.debug(ls.JAVA_RESOLVING_OBJ_TYPE.format(object=object_ref))
        if not (
            object_type := self._resolve_java_object_type(
                str(object_ref), local_var_types, module_qn
            )
        ):
            logger.debug(ls.JAVA_OBJ_TYPE_UNKNOWN.format(object=object_ref))
            return None

        logger.debug(ls.JAVA_OBJ_TYPE_RESOLVED.format(type=object_type))
        result = self._resolve_instance_method(object_type, str(method_name), module_qn)
        if result:
            logger.debug(ls.JAVA_FOUND_INSTANCE.format(result=result))
        else:
            logger.debug(
                ls.JAVA_INSTANCE_NOT_FOUND.format(type=object_type, method=method_name)
            )
        return result
