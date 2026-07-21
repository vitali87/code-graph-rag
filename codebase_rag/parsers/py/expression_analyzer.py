from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node

from ... import constants as cs
from ... import logs as lg
from ...decorators import recursion_guard
from ...types_defs import FunctionRegistryTrieProtocol, NodeType, SimpleNameLookup
from ..import_processor import ImportProcessor
from ..utils import follow_reexports, safe_decode_text
from .utils import resolve_class_name

if TYPE_CHECKING:
    from pathlib import Path

    from ..factory import ASTCacheProtocol

    class _ExpressionAnalyzerDeps(Protocol):
        def _analyze_self_assignments(
            self, node: Node, local_var_types: dict[str, str], module_qn: str
        ) -> None: ...

        def build_local_variable_type_map(
            self, caller_node: Node, module_qn: str
        ) -> dict[str, str]: ...

        def _find_method_ast_node(self, method_qn: str) -> Node | None: ...

        def _find_function_ast_node(self, fn_qn: str) -> Node | None: ...

        def _analyze_method_return_statements(
            self, method_node: Node, method_qn: str, module_qn: str | None = None
        ) -> str | None: ...

    _ExprBase: type = _ExpressionAnalyzerDeps
else:
    _ExprBase = object


class PythonExpressionAnalyzerMixin(_ExprBase):
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    simple_name_lookup: SimpleNameLookup
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol

    _method_return_type_cache: dict[str, str | None]
    _self_assignment_cache: dict[tuple[Node, str], dict[str, str] | None]

    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func_node
                and func_node.type == cs.TS_PY_IDENTIFIER
                and func_node.text is not None
                and (class_name := safe_decode_text(func_node))
                and class_name[0].isupper()
            ):
                return class_name

            if (
                func_node
                and func_node.type == cs.TS_PY_ATTRIBUTE
                and (method_call_text := self._extract_full_method_call(func_node))
            ):
                if inferred := self._infer_method_call_return_type(
                    method_call_text, module_qn, None
                ):
                    return inferred
                return self._attribute_constructor_type(method_call_text)

        elif node.type == cs.TS_PY_LIST_COMPREHENSION:
            if body_node := node.child_by_field_name(cs.TS_FIELD_BODY):
                return self._infer_type_from_expression(body_node, module_qn)

        return None

    def _attribute_constructor_type(self, method_call_text: str) -> str | None:
        # alias.ClassName(...) is an attribute CONSTRUCTOR (pd.DataFrame,
        # genai.Client), not a method call: the variable's type is the dotted class
        # itself. Without this the receiver stays untyped, known-external suppression
        # cannot fire, and a later member call (df.apply) rebinds by bare name to an
        # unrelated first-party method. Mirrors the bare `ClassName(...)` heuristic.
        simple_name = method_call_text.rsplit(cs.SEPARATOR_DOT, 1)[-1]
        if simple_name and simple_name[0].isupper():
            return method_call_text
        return None

    def _infer_type_from_expression_simple(
        self, node: Node, module_qn: str
    ) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func_node
                and func_node.type == cs.TS_PY_IDENTIFIER
                and func_node.text is not None
                and (class_name := safe_decode_text(func_node))
                and class_name[0].isupper()
            ):
                return class_name

        elif node.type == cs.TS_PY_LIST_COMPREHENSION:
            if body_node := node.child_by_field_name(cs.TS_FIELD_BODY):
                return self._infer_type_from_expression_simple(body_node, module_qn)

        return None

    def _infer_type_from_expression_complex(
        self, node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None:
        if node.type == cs.TS_PY_CALL:
            func_node = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func_node
                and func_node.type == cs.TS_PY_ATTRIBUTE
                and (method_call_text := self._extract_full_method_call(func_node))
            ):
                if inferred := self._infer_method_call_return_type(
                    method_call_text, module_qn, local_var_types
                ):
                    return inferred
                return self._attribute_constructor_type(method_call_text)

            if (
                func_node
                and func_node.type == cs.TS_PY_IDENTIFIER
                and func_node.text is not None
                and (callee := safe_decode_text(func_node))
            ):
                # `r = make_widget()`: type the local from the free-function
                # factory's return so the later `r.run()` does not fall back to
                # the bare-name trie.
                return self._infer_free_function_return_type(callee, module_qn)

        return None

    def _extract_full_method_call(self, attr_node: Node) -> str | None:
        return safe_decode_text(attr_node) if attr_node.text else None

    @recursion_guard(
        key_func=lambda self, method_call, module_qn, *_, **__: (
            f"{module_qn}:{method_call}"
        ),
        guard_name=cs.ATTR_TYPE_INFERENCE_IN_PROGRESS,
    )
    def _infer_method_call_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if cs.SEPARATOR_DOT in method_call and self._is_method_chain(method_call):
            return self._infer_chained_call_return_type_fixed(
                method_call, module_qn, local_var_types
            )

        if cs.SEPARATOR_DOT not in method_call and cs.CHAR_PAREN_OPEN in method_call:
            # A bare call as chain receiver (`get_resolver()._is_callback()`,
            # django) is a free-function factory or a constructor; type it from
            # the callee's return.
            callee = method_call.split(cs.CHAR_PAREN_OPEN, 1)[0].strip()
            if callee.isidentifier():
                return self._infer_free_function_return_type(callee, module_qn)
            return None

        return self._infer_method_return_type(method_call, module_qn, local_var_types)

    def _infer_free_function_return_type(
        self, callee: str, module_qn: str
    ) -> str | None:
        # Resolve a bare callee in the caller's scope only (same module, then
        # imports); Python scoping admits nothing else for a bare name, and a global
        # simple-name fallback would rebind common names. A CLASS callee is a
        # constructor: the returned type is the class itself.
        local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{callee}"
        candidates = [local_qn]
        if imported := self.import_processor.import_mapping.get(module_qn, {}).get(
            callee
        ):
            # The import may name a package re-export hop (django's
            # `from django.urls import get_resolver`), not the definition.
            candidates.append(
                follow_reexports(
                    imported,
                    self.import_processor.import_mapping,
                    self.function_registry,
                )
            )
        for qn in candidates:
            match self.function_registry.get(qn):
                case NodeType.CLASS:
                    return qn
                case NodeType.FUNCTION:
                    return self._get_function_return_type_from_ast(qn)
                case _:
                    continue
        return None

    @recursion_guard(
        key_func=lambda self, fn_qn: fn_qn,
        guard_name=cs.ATTR_TYPE_INFERENCE_IN_PROGRESS,
    )
    def _get_function_return_type_from_ast(self, fn_qn: str) -> str | None:
        if fn_qn in self._method_return_type_cache:
            return self._method_return_type_cache[fn_qn]

        fn_module_qn = fn_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        fn_node = self._find_function_ast_node(fn_qn)
        result = (
            self._analyze_method_return_statements(fn_node, fn_qn, fn_module_qn)
            if fn_node
            else None
        )
        if result and cs.SEPARATOR_DOT not in result:
            # Resolve a bare class name in the FACTORY's module scope before it
            # crosses into a caller module where the class is not imported.
            result = self._resolve_class_name(result, fn_module_qn) or result
        self._method_return_type_cache[fn_qn] = result
        return result

    def _is_method_chain(self, call_name: str) -> bool:
        return (
            cs.CHAR_PAREN_OPEN in call_name
            and cs.CHAR_PAREN_CLOSE in call_name
            and bool(re.search(cs.REGEX_METHOD_CHAIN_SUFFIX, call_name))
        )

    def _infer_chained_call_return_type_fixed(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        match = re.search(cs.REGEX_FINAL_METHOD_CAPTURE, call_name)
        if not match:
            return None

        final_method = match[1]

        object_expr = call_name[: match.start()]

        object_type = self._infer_object_type_for_chained_call(
            object_expr, module_qn, local_var_types
        )
        if not object_type:
            return None

        full_object_type = (
            self._resolve_class_name(object_type, module_qn)
            if cs.SEPARATOR_DOT not in object_type
            else None
        ) or object_type

        method_qn = f"{full_object_type}{cs.SEPARATOR_DOT}{final_method}"
        return self._get_method_return_type_from_ast(method_qn)

    def _infer_object_type_for_chained_call(
        self,
        object_expr: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if (
            cs.CHAR_PAREN_OPEN not in object_expr
            and local_var_types
            and (var_type := local_var_types.get(object_expr))
        ):
            return var_type

        if cs.CHAR_PAREN_OPEN in object_expr and cs.CHAR_PAREN_CLOSE in object_expr:
            return self._infer_method_call_return_type(
                object_expr, module_qn, local_var_types
            )

        return None

    def _infer_expression_return_type(
        self,
        expression: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if (
            cs.CHAR_PAREN_OPEN not in expression
            and local_var_types
            and (var_type := local_var_types.get(expression))
        ):
            import_map = self.import_processor.import_mapping.get(module_qn, {})
            if resolved := import_map.get(var_type):
                return resolved
            return self._resolve_class_name(var_type, module_qn)

        return self._infer_method_call_return_type(
            expression, module_qn, local_var_types
        )

    @recursion_guard(
        key_func=lambda self, method_qn: method_qn,
        guard_name=cs.ATTR_TYPE_INFERENCE_IN_PROGRESS,
    )
    def _get_method_return_type_from_ast(self, method_qn: str) -> str | None:
        if method_qn in self._method_return_type_cache:
            return self._method_return_type_cache[method_qn]

        method_node = self._find_method_ast_node(method_qn)
        result = (
            self._analyze_method_return_statements(method_node, method_qn)
            if method_node
            else None
        )
        self._method_return_type_cache[method_qn] = result
        return result

    def _infer_method_return_type(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        try:
            if (
                method_qn := self._resolve_method_qualified_name(
                    method_call, module_qn, local_var_types
                )
            ) and (method_node := self._find_method_ast_node(method_qn)):
                return self._analyze_method_return_statements(method_node, method_qn)
            return None
        except Exception as e:
            logger.debug(lg.PY_INFER_RETURN_FAILED, method=method_call, error=e)
            return None

    def _resolve_method_qualified_name(
        self,
        method_call: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        if cs.SEPARATOR_DOT not in method_call:
            return None

        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) < 2:
            return None

        if len(parts) == 2:
            class_name, method_name_with_args = parts

            method_name = (
                method_name_with_args.split(cs.CHAR_PAREN_OPEN)[0]
                if cs.CHAR_PAREN_OPEN in method_name_with_args
                else method_name_with_args
            )

            if local_var_types and (var_type := local_var_types.get(class_name)):
                return self._resolve_class_method(var_type, method_name, module_qn)

            return self._resolve_class_method(class_name, method_name, module_qn)

        if parts[0] == cs.PY_KEYWORD_SELF and len(parts) >= 3:
            attribute_name = parts[1]
            method_name = parts[-1]

            if attribute_type := self._infer_attribute_type(attribute_name, module_qn):
                return self._resolve_class_method(
                    attribute_type, method_name, module_qn
                )

        if len(parts) >= 3:
            potential_class = parts[-2]
            method_name = parts[-1]
            return self._resolve_class_method(potential_class, method_name, module_qn)

        return None

    def _resolve_class_method(
        self, class_name: str, method_name: str, module_qn: str
    ) -> str | None:
        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if result := self._try_resolve_method(local_class_qn, method_name):
            return result

        import_mapping = self.import_processor.import_mapping.get(module_qn, {})
        if (imported_class_qn := import_mapping.get(class_name)) and (
            result := self._try_resolve_method(imported_class_qn, method_name)
        ):
            return result

        for qn in self.simple_name_lookup.get(class_name, []):
            if result := self._try_resolve_method(qn, method_name):
                logger.debug(
                    lg.PY_RESOLVED_METHOD,
                    class_name=class_name,
                    method_name=method_name,
                    method_qn=result,
                )
                return result

        return None

    def _try_resolve_method(self, class_qn: str, method_name: str) -> str | None:
        if self.function_registry.get(class_qn) != NodeType.CLASS:
            return None
        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        if self.function_registry.get(method_qn) == NodeType.METHOD:
            return method_qn
        return None

    def _infer_attribute_type(self, attribute_name: str, module_qn: str) -> str | None:
        if result := self._try_infer_from_self_assignments(attribute_name, module_qn):
            return result

        class_name = (
            "".join(
                word.capitalize() for word in attribute_name.split(cs.CHAR_UNDERSCORE)
            )
            if cs.CHAR_UNDERSCORE in attribute_name
            else attribute_name.capitalize()
        )
        return self._find_class_in_scope(class_name, module_qn)

    def _try_infer_from_self_assignments(
        self, attribute_name: str, module_qn: str
    ) -> str | None:
        try:
            file_path = self.module_qn_to_file_path.get(module_qn)
            if not file_path or not (entry := self.ast_cache.load(file_path)):
                return None

            root_node, language = entry
            if language != cs.SupportedLanguage.PYTHON:
                return None

            cache_key = (root_node, module_qn)
            if cache_key in self._self_assignment_cache:
                instance_vars = self._self_assignment_cache[cache_key]
            else:
                instance_vars = {}
                self._analyze_self_assignments(root_node, instance_vars, module_qn)
                self._self_assignment_cache[cache_key] = instance_vars or None

            if not instance_vars:
                return None

            full_attr_name = f"{cs.PY_SELF_PREFIX}{attribute_name}"
            return instance_vars.get(full_attr_name)

        except Exception as e:
            logger.debug(lg.PY_INFER_ATTR_FAILED, attr=attribute_name, error=e)
            return None

    def _find_class_in_scope(self, class_name: str, module_qn: str) -> str | None:
        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if self.function_registry.get(local_class_qn) == NodeType.CLASS:
            return class_name

        import_mapping = self.import_processor.import_mapping.get(module_qn, {})
        if (
            imported_qn := import_mapping.get(class_name)
        ) and self.function_registry.get(imported_qn) == NodeType.CLASS:
            return class_name

        if any(
            self.function_registry.get(qn) == NodeType.CLASS
            for qn in self.simple_name_lookup.get(class_name, [])
        ):
            return class_name

        return None

    def _resolve_class_name(self, class_name: str, module_qn: str) -> str | None:
        return resolve_class_name(
            class_name, module_qn, self.import_processor, self.function_registry
        )
