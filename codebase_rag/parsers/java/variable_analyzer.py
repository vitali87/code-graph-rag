from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode
from ..utils import safe_decode_text
from .utils import (
    extract_class_info,
    extract_field_info,
    extract_method_call_info,
    get_root_node_from_module_qn,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol


class JavaVariableAnalyzerMixin:
    __slots__ = ()
    ast_cache: ASTCacheProtocol
    module_qn_to_file_path: dict[str, Path]
    class_inheritance: dict[str, list[str]]
    _lookup_cache: dict[str, str | None]
    _lookup_in_progress: set[str]

    @abstractmethod
    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str: ...

    @abstractmethod
    def _resolve_java_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None: ...

    @abstractmethod
    def _find_containing_java_class(self, node: ASTNode) -> ASTNode | None: ...

    @abstractmethod
    def build_variable_type_map(
        self, scope_node: ASTNode, module_qn: str
    ) -> dict[str, str]: ...

    def _collect_all_variable_types(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._analyze_java_parameters(scope_node, local_var_types, module_qn)
        self._analyze_java_local_variables(scope_node, local_var_types, module_qn)
        self._analyze_java_class_fields(scope_node, local_var_types, module_qn)
        self._analyze_java_constructor_assignments(
            scope_node, local_var_types, module_qn
        )
        self._analyze_java_enhanced_for_loops(scope_node, local_var_types, module_qn)

    def _analyze_java_parameters(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        params_node = scope_node.child_by_field_name(cs.FIELD_PARAMETERS)
        if not params_node:
            return

        for child in params_node.children:
            match child.type:
                case cs.TS_FORMAL_PARAMETER:
                    self._process_formal_parameter(child, local_var_types, module_qn)
                case cs.TS_SPREAD_PARAMETER:
                    self._process_spread_parameter(child, local_var_types, module_qn)

    def _process_formal_parameter(
        self, param_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        param_name_node = param_node.child_by_field_name(cs.FIELD_NAME)
        param_type_node = param_node.child_by_field_name(cs.FIELD_TYPE)

        if not param_name_node or not param_type_node:
            return

        param_name = safe_decode_text(param_name_node)
        param_type = safe_decode_text(param_type_node)

        if param_name and param_type:
            resolved_type = self._resolve_java_type_name(param_type, module_qn)
            local_var_types[param_name] = resolved_type
            logger.debug(ls.JAVA_PARAM, name=param_name, type=resolved_type)

    def _process_spread_parameter(
        self, param_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        param_name = None
        param_type = None

        for subchild in param_node.children:
            if subchild.type == cs.TS_TYPE_IDENTIFIER:
                if decoded_text := safe_decode_text(subchild):
                    param_type = f"{decoded_text}{cs.JAVA_ARRAY_SUFFIX}"
            elif subchild.type == cs.TS_VARIABLE_DECLARATOR:
                if name_node := subchild.child_by_field_name(cs.FIELD_NAME):
                    param_name = safe_decode_text(name_node)

        if param_name and param_type:
            resolved_type = self._resolve_java_type_name(param_type, module_qn)
            local_var_types[param_name] = resolved_type
            logger.debug(ls.JAVA_VARARGS_PARAM, name=param_name, type=resolved_type)

    def _analyze_java_local_variables(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for_local_variables(scope_node, local_var_types, module_qn)

    def _traverse_for_local_variables(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if node.type == cs.TS_LOCAL_VARIABLE_DECLARATION:
            self._process_java_variable_declaration(node, local_var_types, module_qn)

        for child in node.children:
            self._traverse_for_local_variables(child, local_var_types, module_qn)

    def _process_java_variable_declaration(
        self, decl_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if not (type_node := decl_node.child_by_field_name(cs.FIELD_TYPE)):
            return

        if not (declared_type := safe_decode_text(type_node)):
            return

        if not (declarator_node := decl_node.child_by_field_name(cs.FIELD_DECLARATOR)):
            return

        if declarator_node.type == cs.TS_VARIABLE_DECLARATOR:
            self._process_variable_declarator(
                declarator_node, declared_type, local_var_types, module_qn
            )
        else:
            for child in declarator_node.children:
                if child.type == cs.TS_VARIABLE_DECLARATOR:
                    self._process_variable_declarator(
                        child, declared_type, local_var_types, module_qn
                    )

    def _process_variable_declarator(
        self,
        declarator_node: ASTNode,
        declared_type: str,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if not (name_node := declarator_node.child_by_field_name(cs.FIELD_NAME)):
            return

        if not (var_name := safe_decode_text(name_node)):
            return

        if value_node := declarator_node.child_by_field_name(cs.FIELD_VALUE):
            if inferred_type := self._infer_java_type_from_expression(
                value_node, module_qn
            ):
                resolved_type = self._resolve_java_type_name(inferred_type, module_qn)
                local_var_types[var_name] = resolved_type
                logger.debug(
                    ls.JAVA_LOCAL_VAR_INFERRED, name=var_name, type=resolved_type
                )
                return

        resolved_type = self._resolve_java_type_name(declared_type, module_qn)
        local_var_types[var_name] = resolved_type
        logger.debug(ls.JAVA_LOCAL_VAR_DECLARED, name=var_name, type=resolved_type)

    def _analyze_java_class_fields(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if not (containing_class := self._find_containing_java_class(scope_node)):
            return

        if not (body_node := containing_class.child_by_field_name(cs.FIELD_BODY)):
            return

        for child in body_node.children:
            if child.type == cs.TS_FIELD_DECLARATION:
                field_info = extract_field_info(child)
                if field_info.get(cs.FIELD_NAME) and field_info.get(cs.FIELD_TYPE):
                    field_name = field_info[cs.FIELD_NAME]
                    field_type = field_info[cs.FIELD_TYPE]

                    this_field_ref = (
                        f"{cs.JAVA_KEYWORD_THIS}{cs.SEPARATOR_DOT}{field_name}"
                    )
                    resolved_type = self._resolve_java_type_name(
                        str(field_type), module_qn
                    )
                    local_var_types[this_field_ref] = resolved_type

                    if str(field_name) not in local_var_types:
                        local_var_types[str(field_name)] = resolved_type
                    logger.debug(
                        ls.JAVA_CLASS_FIELD, name=field_name, type=resolved_type
                    )

    def _analyze_java_constructor_assignments(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for_assignments(scope_node, local_var_types, module_qn)

    def _traverse_for_assignments(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if node.type == cs.TS_ASSIGNMENT_EXPRESSION:
            self._process_java_assignment(node, local_var_types, module_qn)

        for child in node.children:
            self._traverse_for_assignments(child, local_var_types, module_qn)

    def _process_java_assignment(
        self, assignment_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name(cs.FIELD_LEFT)
        right_node = assignment_node.child_by_field_name(cs.FIELD_RIGHT)

        if not left_node or not right_node:
            return

        if not (var_name := self._extract_java_variable_reference(left_node)):
            return

        if inferred_type := self._infer_java_type_from_expression(
            right_node, module_qn
        ):
            resolved_type = self._resolve_java_type_name(inferred_type, module_qn)
            local_var_types[var_name] = resolved_type
            logger.debug(ls.JAVA_ASSIGNMENT, name=var_name, type=resolved_type)

    def _extract_java_variable_reference(self, node: ASTNode) -> str | None:
        match node.type:
            case cs.TS_IDENTIFIER:
                return safe_decode_text(node)
            case cs.TS_FIELD_ACCESS:
                object_node = node.child_by_field_name(cs.FIELD_OBJECT)
                field_node = node.child_by_field_name(cs.FIELD_FIELD)

                if object_node and field_node:
                    object_name = safe_decode_text(object_node)
                    field_name = safe_decode_text(field_node)

                    if object_name and field_name:
                        return f"{object_name}{cs.SEPARATOR_DOT}{field_name}"
            case _:
                pass

        return None

    def _analyze_java_enhanced_for_loops(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for_enhanced_for_loops(scope_node, local_var_types, module_qn)

    def _traverse_for_enhanced_for_loops(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if node.type == cs.TS_ENHANCED_FOR_STATEMENT:
            self._process_enhanced_for_statement(node, local_var_types, module_qn)

        for child in node.children:
            self._traverse_for_enhanced_for_loops(child, local_var_types, module_qn)

    def _process_enhanced_for_statement(
        self, for_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        type_node = for_node.child_by_field_name(cs.FIELD_TYPE)
        name_node = for_node.child_by_field_name(cs.FIELD_NAME)

        if type_node and name_node:
            self._register_for_loop_variable(
                type_node, name_node, local_var_types, module_qn
            )
        else:
            self._extract_for_loop_variable_from_children(
                for_node, local_var_types, module_qn
            )

    def _register_for_loop_variable(
        self,
        type_node: ASTNode,
        name_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if (var_type := safe_decode_text(type_node)) and (
            var_name := safe_decode_text(name_node)
        ):
            resolved_type = self._resolve_java_type_name(var_type, module_qn)
            local_var_types[var_name] = resolved_type
            logger.debug(ls.JAVA_ENHANCED_FOR_VAR, name=var_name, type=resolved_type)

    def _extract_for_loop_variable_from_children(
        self, for_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for child in for_node.children:
            if child.type != cs.TS_VARIABLE_DECLARATOR:
                continue

            if not (name_node := child.child_by_field_name(cs.FIELD_NAME)):
                continue

            if not (var_name := safe_decode_text(name_node)):
                continue

            if not (parent := child.parent):
                continue

            for sibling in parent.children:
                if sibling.type == cs.TS_TYPE_IDENTIFIER:
                    if var_type := safe_decode_text(sibling):
                        resolved_type = self._resolve_java_type_name(
                            var_type, module_qn
                        )
                        local_var_types[var_name] = resolved_type
                        logger.debug(
                            ls.JAVA_ENHANCED_FOR_VAR_ALT,
                            name=var_name,
                            type=resolved_type,
                        )
                        break

    def _infer_java_type_from_expression(
        self, expr_node: ASTNode, module_qn: str
    ) -> str | None:
        match expr_node.type:
            case cs.TS_OBJECT_CREATION_EXPRESSION:
                if type_node := expr_node.child_by_field_name(cs.FIELD_TYPE):
                    return safe_decode_text(type_node)

            case cs.TS_METHOD_INVOCATION:
                return self._infer_java_method_return_type(expr_node, module_qn)

            case cs.TS_IDENTIFIER:
                if var_name := safe_decode_text(expr_node):
                    return self._lookup_variable_type(var_name, module_qn)

            case cs.TS_FIELD_ACCESS:
                return self._infer_java_field_access_type(expr_node, module_qn)

            case cs.TS_STRING_LITERAL:
                return cs.JAVA_TYPE_STRING

            case cs.TS_INTEGER_LITERAL:
                return cs.JAVA_TYPE_INT

            case cs.TS_DECIMAL_FLOATING_POINT_LITERAL:
                return cs.JAVA_TYPE_DOUBLE

            case cs.TS_TRUE | cs.TS_FALSE:
                return cs.JAVA_TYPE_BOOLEAN

            case cs.TS_ARRAY_CREATION_EXPRESSION:
                if type_node := expr_node.child_by_field_name(cs.FIELD_TYPE):
                    if base_type := safe_decode_text(type_node):
                        return f"{base_type}{cs.JAVA_ARRAY_SUFFIX}"

            case _:
                pass

        return None

    def _infer_java_method_return_type(
        self, method_call_node: ASTNode, module_qn: str
    ) -> str | None:
        call_info = extract_method_call_info(method_call_node)
        if not call_info:
            return None

        method_name = call_info[cs.FIELD_NAME]
        if not method_name:
            return None

        object_ref = call_info[cs.FIELD_OBJECT]
        call_string = (
            f"{object_ref}{cs.SEPARATOR_DOT}{method_name}"
            if object_ref
            else str(method_name)
        )
        return self._resolve_java_method_return_type(call_string, module_qn)

    def _infer_java_field_access_type(
        self, field_access_node: ASTNode, module_qn: str
    ) -> str | None:
        object_node = field_access_node.child_by_field_name(cs.FIELD_OBJECT)
        field_node = field_access_node.child_by_field_name(cs.FIELD_FIELD)

        if not object_node or not field_node:
            return None

        field_name = safe_decode_text(field_node)
        if not field_name:
            return None

        # (H) A nested receiver (`obj.address.zipCode`) has a field_access as its object;
        # (H) recurse to infer that inner type before looking up the outer field, so
        # (H) multi-level field access resolves rather than failing on a non-variable name.
        if object_node.type == cs.TS_FIELD_ACCESS:
            object_type = self._infer_java_field_access_type(object_node, module_qn)
        elif object_name := safe_decode_text(object_node):
            object_type = self._resolve_field_access_base_type(
                object_name, field_access_node, module_qn
            )
        else:
            object_type = None

        if object_type:
            return self._lookup_java_field_type(object_type, field_name, module_qn)
        return None

    def _resolve_field_access_base_type(
        self, object_name: str, field_access_node: ASTNode, module_qn: str
    ) -> str | None:
        # (H) `this`/`super` are receiver keywords, not variables: resolve them to the
        # (H) containing class (or its superclass) so nested chains rooted at them
        # (H) (e.g. `var c = this.address.city`) infer a type instead of failing.
        if object_name in (cs.JAVA_KEYWORD_THIS, cs.JAVA_KEYWORD_SUPER):
            if not (class_node := self._find_containing_java_class(field_access_node)):
                return None
            class_info = extract_class_info(class_node)
            class_name = class_info.get(cs.FIELD_NAME)
            if object_name == cs.JAVA_KEYWORD_THIS:
                return class_name
            # (H) `super`: return the fully-qualified parent from class_inheritance so a
            # (H) nested superclass (`Outer.Base`) resolves; the relative name from the
            # (H) AST would be treated as an absolute class key by the field lookup.
            if class_name:
                own_qn = self._resolve_java_type_name(class_name, module_qn)
                if cs.SEPARATOR_DOT not in own_qn:
                    own_qn = f"{module_qn}{cs.SEPARATOR_DOT}{own_qn}"
                if parents := self.class_inheritance.get(own_qn):
                    return parents[0]
            return class_info.get(cs.FIELD_SUPERCLASS)
        return self._lookup_variable_type(object_name, module_qn)

    def _lookup_variable_type(self, var_name: str, module_qn: str) -> str | None:
        if not var_name or not module_qn:
            return None

        cache_key = f"{module_qn}{cs.SEPARATOR_COLON}{var_name}"
        if cache_key in self._lookup_cache:
            return self._lookup_cache[cache_key]

        if cache_key in self._lookup_in_progress:
            return None

        self._lookup_in_progress.add(cache_key)

        try:
            result = self._do_variable_type_lookup(var_name, module_qn)
            self._lookup_cache[cache_key] = result
            return result

        finally:
            self._lookup_in_progress.discard(cache_key)

    def _do_variable_type_lookup(self, var_name: str, module_qn: str) -> str | None:
        root_node = get_root_node_from_module_qn(
            module_qn, self.module_qn_to_file_path, self.ast_cache
        )
        if not root_node:
            return None

        variable_types = self.build_variable_type_map(root_node, module_qn)

        this_var = f"{cs.JAVA_KEYWORD_THIS}{cs.SEPARATOR_DOT}{var_name}"
        return variable_types.get(var_name) or variable_types.get(this_var)

    def _lookup_java_field_type(
        self, class_type: str, field_name: str, module_qn: str
    ) -> str | None:
        if not class_type or not field_name:
            return None

        resolved = self._resolve_java_type_name(class_type, module_qn)
        class_qn: str | None = (
            resolved
            if cs.SEPARATOR_DOT in resolved
            else f"{module_qn}{cs.SEPARATOR_DOT}{resolved}"
        )

        # (H) Walk the inheritance chain using authoritative qualified parents from
        # (H) class_inheritance: a field accessed on a subclass may be declared on a
        # (H) superclass, including a nested one like `Outer.Base`. Seen-guarded.
        seen: set[str] = set()
        while class_qn and class_qn not in seen:
            seen.add(class_qn)
            if located := self._locate_class(class_qn):
                root_node, class_path, target_module_qn = located
                if field_type := self._find_field_type_in_nested_class(
                    root_node, class_path, field_name, target_module_qn
                ):
                    return field_type
            parents = self.class_inheritance.get(class_qn)
            class_qn = parents[0] if parents else None

        return None

    def _locate_class(self, class_qn: str) -> tuple[ASTNode, list[str], str] | None:
        # (H) The file module is the longest registered prefix of the class qn; the
        # (H) remaining segments are the (possibly nested) class path within that file,
        # (H) so `proj.pkg.Outer.Base` resolves to file `proj.pkg` + path [Outer, Base].
        parts = class_qn.split(cs.SEPARATOR_DOT)
        for split in range(len(parts) - 1, 0, -1):
            module_candidate = cs.SEPARATOR_DOT.join(parts[:split])
            file_path = self.module_qn_to_file_path.get(module_candidate)
            if file_path is not None and file_path in self.ast_cache:
                root_node, _ = self.ast_cache[file_path]
                return root_node, parts[split:], module_candidate
        return None

    def _find_field_type_in_class(
        self, root_node: ASTNode, class_name: str, field_name: str, module_qn: str
    ) -> str | None:
        return self._find_field_type_in_nested_class(
            root_node, [class_name], field_name, module_qn
        )

    def _find_field_type_in_nested_class(
        self,
        root_node: ASTNode,
        class_path: list[str],
        field_name: str,
        module_qn: str,
    ) -> str | None:
        children = root_node.children
        body: ASTNode | None = None
        for class_name in class_path:
            class_node = next(
                (
                    child
                    for child in children
                    if child.type == cs.TS_CLASS_DECLARATION
                    and extract_class_info(child).get(cs.FIELD_NAME) == class_name
                ),
                None,
            )
            if class_node is None or not (
                body := class_node.child_by_field_name(cs.FIELD_BODY)
            ):
                return None
            children = body.children

        if body is None:
            return None

        for field_child in body.children:
            if field_child.type == cs.TS_FIELD_DECLARATION:
                field_info = extract_field_info(field_child)
                if field_info.get(cs.FIELD_NAME) == field_name:
                    if field_type := field_info.get(cs.FIELD_TYPE):
                        return self._resolve_java_type_name(str(field_type), module_qn)
        return None
