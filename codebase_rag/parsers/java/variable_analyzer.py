from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING

from loguru import logger

from ... import constants as cs
from ... import logs as ls
from ...decorators import depth_exhausted, depth_guard, recursion_guard
from ...types_defs import ASTNode
from ..utils import safe_decode_text
from .utils import (
    extract_class_info,
    extract_field_info,
    extract_method_call_info,
    get_root_node_from_module_qn,
    spread_element_type_node,
)

if TYPE_CHECKING:
    from pathlib import Path

    from ...types_defs import ASTCacheProtocol


# Classes whose declared fields stay cached. Methods are analysed class by
# class, so the classes in use at once are one file's nesting chain (anonymous
# and inner classes included). Small, because every entry keeps its parse tree
# alive.
_CLASS_FIELDS_CACHE_SIZE = 16


@lru_cache(maxsize=_CLASS_FIELDS_CACHE_SIZE)
def _declared_fields(class_node: ASTNode) -> tuple[tuple[str, str], ...]:
    """(name, declared type) of each field the class body declares, in order.

    Every method's variable-type map reads its class's fields, and walking
    the class body for them once per method made a class of N methods cost
    O(N^2) (issue #2453). Read once per class instead, keyed on the class
    node, which tree-sitter compares by tree, so a re-parsed file reads its
    own.
    """
    if not (body_node := class_node.child_by_field_name(cs.FIELD_BODY)):
        return ()
    fields: list[tuple[str, str]] = []
    for child in body_node.children:
        if child.type != cs.TS_FIELD_DECLARATION:
            continue
        field_info = extract_field_info(child)
        field_name = field_info.get(cs.FIELD_NAME)
        field_type = field_info.get(cs.FIELD_TYPE)
        if field_name and field_type:
            fields.append((str(field_name), str(field_type)))
    return tuple(fields)


def _java_literal_type(expr_node: ASTNode) -> str | None:
    # A literal's type is fixed by its node type, and for the numeric ones by
    # its suffix: `1L` is a long, `1.5f` a float.
    node_type = expr_node.type
    if node_type == cs.TS_STRING_LITERAL:
        return cs.JAVA_TYPE_STRING
    if node_type == cs.TS_JAVA_CHARACTER_LITERAL:
        return cs.JAVA_TYPE_CHAR
    if node_type in (cs.TS_TRUE, cs.TS_FALSE):
        return cs.JAVA_TYPE_BOOLEAN
    text = safe_decode_text(expr_node) or ""
    if node_type in cs.TS_JAVA_FLOATING_POINT_LITERALS:
        return (
            cs.JAVA_TYPE_FLOAT
            if text.endswith(cs.JAVA_FLOAT_SUFFIXES)
            else cs.JAVA_TYPE_DOUBLE
        )
    if node_type in cs.TS_JAVA_INTEGER_LITERALS:
        return (
            cs.JAVA_TYPE_LONG_PRIMITIVE
            if text.endswith(cs.JAVA_LONG_SUFFIXES)
            else cs.JAVA_TYPE_INT
        )
    return None


def _java_type_field_text(expr_node: ASTNode) -> str | None:
    # The type an object creation or a cast names, as written.
    if type_node := expr_node.child_by_field_name(cs.FIELD_TYPE):
        return safe_decode_text(type_node)
    return None


def _java_array_creation_type(expr_node: ASTNode) -> str | None:
    if base_type := _java_type_field_text(expr_node):
        return f"{base_type}{cs.JAVA_ARRAY_SUFFIX}"
    return None


def _first_type_identifier_text(parent: ASTNode) -> str | None:
    for sibling in parent.children:
        if sibling.type == cs.TS_TYPE_IDENTIFIER and (
            text := safe_decode_text(sibling)
        ):
            return text
    return None


class JavaVariableAnalyzerMixin:
    __slots__ = ()
    ast_cache: ASTCacheProtocol
    module_qn_to_file_path: dict[str, Path]
    class_inheritance: dict[str, list[str]]
    _lookup_cache: dict[str, str | None]
    _lookup_in_progress: set[str]
    # Annotation only, never a def: this mixin precedes JavaMethodResolverMixin
    # in the MRO, so even an @abstractmethod stub here would SHADOW the real
    # implementation and silently resolve every call to None. `...` because a
    # Callable's parameters are positional-only, and ty rejects that as an
    # override of the resolver's methods, whose parameters have names.
    _do_resolve_java_method_call: Callable[..., tuple[str, str] | None]
    _declared_return_type_of: Callable[..., str | None]
    _resolve_java_method_return_type: Callable[..., str | None]

    @abstractmethod
    def _resolve_java_type_name(self, type_name: str, module_qn: str) -> str: ...

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
        self._analyze_java_try_resources(scope_node, local_var_types, module_qn)
        self._analyze_java_catch_parameters(scope_node, local_var_types, module_qn)

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

        # The element type by shape, not by `type_identifier` alone: a
        # generic, array or primitive varargs bound no local before
        # (issue #1974).
        element = spread_element_type_node(param_node)
        if element is not None and (decoded_text := safe_decode_text(element)):
            param_type = f"{decoded_text}{cs.JAVA_ARRAY_SUFFIX}"
        for subchild in param_node.children:
            if subchild.type == cs.TS_VARIABLE_DECLARATOR:
                if name_node := subchild.child_by_field_name(cs.FIELD_NAME):
                    param_name = safe_decode_text(name_node)

        if param_name and param_type:
            resolved_type = self._resolve_java_type_name(param_type, module_qn)
            local_var_types[param_name] = resolved_type
            logger.debug(ls.JAVA_VARARGS_PARAM, name=param_name, type=resolved_type)

    def _traverse_for(
        self,
        node: ASTNode,
        node_type: str,
        process: Callable[[ASTNode, dict[str, str], str], None],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        # One walker for every "find nodes of type T under this scope and feed
        # them to a processor" pass; the passes differ only in (T, processor).
        pending = [node]
        while pending:
            current = pending.pop()
            if current.type == node_type:
                process(current, local_var_types, module_qn)
            pending.extend(reversed(current.children))

    def _analyze_java_local_variables(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for(
            scope_node,
            cs.TS_LOCAL_VARIABLE_DECLARATION,
            self._process_java_variable_declaration,
            local_var_types,
            module_qn,
        )

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
                value_node, module_qn, local_var_types
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

        for field_name, field_type in _declared_fields(containing_class):
            this_field_ref = f"{cs.JAVA_KEYWORD_THIS}{cs.SEPARATOR_DOT}{field_name}"
            resolved_type = self._resolve_java_type_name(field_type, module_qn)
            local_var_types[this_field_ref] = resolved_type

            if field_name not in local_var_types:
                local_var_types[field_name] = resolved_type
            logger.debug(ls.JAVA_CLASS_FIELD, name=field_name, type=resolved_type)

    def _analyze_java_constructor_assignments(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for(
            scope_node,
            cs.TS_ASSIGNMENT_EXPRESSION,
            self._process_java_assignment,
            local_var_types,
            module_qn,
        )

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
            right_node, module_qn, local_var_types
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

        return None

    def _analyze_java_enhanced_for_loops(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for(
            scope_node,
            cs.TS_ENHANCED_FOR_STATEMENT,
            self._process_enhanced_for_statement,
            local_var_types,
            module_qn,
        )

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

            if var_type := _first_type_identifier_text(parent):
                resolved_type = self._resolve_java_type_name(var_type, module_qn)
                local_var_types[var_name] = resolved_type
                logger.debug(
                    ls.JAVA_ENHANCED_FOR_VAR_ALT,
                    name=var_name,
                    type=resolved_type,
                )

    def _analyze_java_try_resources(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for(
            scope_node,
            cs.TS_JAVA_RESOURCE,
            self._process_java_resource,
            local_var_types,
            module_qn,
        )

    def _process_java_resource(
        self, resource_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # A `resource` binds via the same `name`/`type`/`value` fields as a
        # variable declarator, so it is typed exactly like a local: the
        # constructed type for `var r = new Res()`, else the declared type
        # (issue #3341). A bare Java 9 resource `try (r)` has no `type`
        # field: it declares nothing, so it must not touch the outer
        # variable's type either.
        if not (type_node := resource_node.child_by_field_name(cs.FIELD_TYPE)):
            return
        if not (declared_type := safe_decode_text(type_node)):
            return
        self._process_variable_declarator(
            resource_node, declared_type, local_var_types, module_qn
        )

    def _analyze_java_catch_parameters(
        self, scope_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._traverse_for(
            scope_node,
            cs.TS_CATCH_FORMAL_PARAMETER,
            self._process_catch_formal_parameter,
            local_var_types,
            module_qn,
        )

    def _process_catch_formal_parameter(
        self,
        param_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if not (name_node := param_node.child_by_field_name(cs.FIELD_NAME)):
            return
        if not (var_name := safe_decode_text(name_node)):
            return
        alternatives = self._catch_type_alternatives(param_node)
        if not alternatives:
            return
        if len(alternatives) == 1:
            resolved_type = self._resolve_java_type_name(alternatives[0], module_qn)
            local_var_types[var_name] = resolved_type
            logger.debug(ls.JAVA_CATCH_PARAMETER, name=var_name, type=resolved_type)
            return
        # A multi-catch parameter is typed by the nearest common first-party
        # supertype, if there is one. With none, it stays untyped: writing
        # nothing leaves any outer variable of the same name untouched.
        if common := self._common_first_party_supertype(alternatives, module_qn):
            local_var_types[var_name] = common
            logger.debug(ls.JAVA_CATCH_PARAMETER, name=var_name, type=common)

    def _catch_type_alternatives(self, param_node: ASTNode) -> list[str]:
        # The `catch_type` child holds the parameter's type(s): one type node,
        # or one per `|`-separated alternative of a multi-catch. An ERROR
        # child means the type list did not parse (e.g. annotations on a
        # union alternative, which this grammar rejects): bind nothing
        # rather than read a fragment of it as a type.
        for child in param_node.children:
            if child.type != cs.TS_JAVA_CATCH_TYPE:
                continue
            alternatives: list[str] = []
            for type_child in child.named_children:
                if type_child.type in (
                    cs.TS_ANNOTATION,
                    cs.TS_MARKER_ANNOTATION,
                    cs.TS_ERROR,
                ):
                    return []
                if text := safe_decode_text(type_child):
                    alternatives.append(text)
            return list(dict.fromkeys(alternatives))
        return []

    def _common_first_party_supertype(
        self, alternatives: list[str], module_qn: str
    ) -> str | None:
        # Lowest common ancestor over the superclass chains `class_inheritance`
        # records, restricted to first-party types: a supertype only counts
        # if it is itself a parsed class in the repo. A chain that runs into
        # a JDK type (Exception, Throwable, ...) therefore ends there with
        # no match, instead of guessing that type for the parameter.
        chains = []
        for alternative in alternatives:
            resolved = self._resolve_java_type_name(alternative, module_qn)
            key = self._inheritance_key_for(resolved)
            chains.append(self._java_superclass_chain(key or resolved))
        best: str | None = None
        best_key: tuple[int, int] | None = None
        for candidate in chains[0]:
            if candidate not in self.class_inheritance:
                continue
            positions = []
            for chain in chains:
                if candidate not in chain:
                    break
                positions.append(chain.index(candidate))
            else:
                key = (max(positions), sum(positions))
                if best_key is None or key < best_key:
                    best, best_key = candidate, key
        return best

    def _inheritance_key_for(self, resolved_type: str) -> str | None:
        # The qn a type name resolves to and the qn `class_inheritance` keys
        # its classes by are not always the same string: a resolved
        # same-package name can stay a simple name while the registry keys
        # the class under its doubled file-module form
        # (`<module>.AppError.AppError`). An UNQUALIFIED name may therefore
        # match on its simple name, and only when exactly one parsed class
        # carries it (the same uniqueness rule `_resolve_java_type_name`
        # applies to a nested type). A QUALIFIED name must prove its
        # identity from its package path instead: an exact key, or a key
        # that ends at the written path, in plain or doubled form. A
        # same-named class in another package never stands in for a
        # qualified name — in particular, `java.io.IOException` never
        # resolves to a first-party `IOException`.
        if resolved_type in self.class_inheritance:
            return resolved_type
        if cs.SEPARATOR_DOT not in resolved_type:
            matches = [
                qn
                for qn in self.class_inheritance
                if qn.rpartition(cs.SEPARATOR_DOT)[2] == resolved_type
            ]
            return matches[0] if len(matches) == 1 else None
        simple_name = resolved_type.rpartition(cs.SEPARATOR_DOT)[2]
        matches = [
            qn
            for qn in self.class_inheritance
            if qn.endswith(f"{cs.SEPARATOR_DOT}{resolved_type}")
            or qn.endswith(
                f"{cs.SEPARATOR_DOT}{resolved_type}{cs.SEPARATOR_DOT}{simple_name}"
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _java_superclass_chain(self, class_qn: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = class_qn
        while current and current not in seen:
            seen.add(current)
            chain.append(current)
            parents = self.class_inheritance.get(current)
            current = parents[0] if parents else None
        return chain

    @depth_guard(
        max_depth=cs.JAVA_MAX_INFERENCE_DEPTH,
        guard_name=cs.GUARD_JAVA_INFERENCE_DEPTH,
    )
    def _infer_java_type_from_expression(
        self,
        expr_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        # Literals carry their type in the node itself, with no lookup and no
        # scope: keeping them out of this switch keeps it readable.
        if (literal := _java_literal_type(expr_node)) is not None:
            return literal

        match expr_node.type:
            case cs.TS_OBJECT_CREATION_EXPRESSION:
                return _java_type_field_text(expr_node)

            case cs.TS_METHOD_INVOCATION:
                return self._infer_java_method_return_type(
                    expr_node, module_qn, local_var_types
                )

            case cs.TS_IDENTIFIER:
                return self._infer_java_identifier_type(
                    expr_node, module_qn, local_var_types
                )

            case cs.TS_FIELD_ACCESS:
                return self._infer_java_field_access_type(
                    expr_node, module_qn, local_var_types
                )

            case cs.TS_ARRAY_CREATION_EXPRESSION:
                return _java_array_creation_type(expr_node)

            case cs.TS_JAVA_CAST_EXPRESSION:
                # The cast names the static type the value has from here on,
                # which is the type overload resolution goes by (issue #2548).
                return _java_type_field_text(expr_node)

            case cs.TS_PARENTHESIZED_EXPRESSION:
                return self._infer_java_parenthesized_type(
                    expr_node, module_qn, local_var_types
                )

        return None

    def _infer_java_identifier_type(
        self,
        expr_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> str | None:
        if not (var_name := safe_decode_text(expr_node)):
            return None
        # The caller's own locals first: the module-wide lookup is name-keyed
        # across every method, so a same-named local in another method would
        # answer for this one (issue #1348).
        if local_var_types and var_name in local_var_types:
            return local_var_types[var_name]
        return self._lookup_variable_type(var_name, module_qn)

    def _infer_java_parenthesized_type(
        self,
        expr_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> str | None:
        if inner := next(
            (c for c in expr_node.children if c.type not in cs.DELIMITER_TOKENS),
            None,
        ):
            return self._infer_java_type_from_expression(
                inner, module_qn, local_var_types
            )
        return None

    def _infer_java_method_return_type(
        self,
        method_call_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        call_info = extract_method_call_info(method_call_node)
        if not call_info:
            return None

        method_name = call_info[cs.FIELD_NAME]
        if not method_name:
            return None

        # Resolve the nested call properly first: it is overload-sensitive, and
        # the name-only lookup below takes the FIRST declaration's return type,
        # so `take(make("x"))` could be typed from `make(int)`. The resolved qn
        # carries the SELECTED signature, which reads back the right return
        # type. Needs the caller's variable types, or the receiver of an
        # instance call cannot be typed (issue #1348).
        if (
            resolved := self._resolve_nested_java_call(
                method_call_node, module_qn, local_var_types or {}
            )
        ) and (declared := self._declared_return_type_of(resolved[1])):
            return declared

        object_ref = call_info[cs.FIELD_OBJECT]
        call_string = (
            f"{object_ref}{cs.SEPARATOR_DOT}{method_name}"
            if object_ref
            else str(method_name)
        )
        # A call refused for depth stays untyped: the name-based fallback walks
        # the class AST recursively, which the depth limit exists to prevent.
        if depth_exhausted(cs.GUARD_JAVA_INFERENCE_DEPTH, cs.JAVA_MAX_INFERENCE_DEPTH):
            return None
        return self._resolve_java_method_return_type(call_string, module_qn)

    @recursion_guard(
        key_func=lambda self, call_node, *_, **__: call_node.id,
        guard_name=cs.GUARD_NESTED_JAVA_CALL,
    )
    def _resolve_nested_java_call(
        self,
        call_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str],
    ) -> tuple[str, str] | None:
        # Guarded: resolution infers its ARGUMENT types, and typing an argument
        # comes back here, so a call nested in its own argument list would
        # recurse without a brake.
        return self._do_resolve_java_method_call(call_node, local_var_types, module_qn)

    @depth_guard(
        max_depth=cs.JAVA_MAX_INFERENCE_DEPTH,
        guard_name=cs.GUARD_JAVA_INFERENCE_DEPTH,
    )
    def _infer_java_field_access_type(
        self,
        field_access_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        object_node = field_access_node.child_by_field_name(cs.FIELD_OBJECT)
        field_node = field_access_node.child_by_field_name(cs.FIELD_FIELD)

        if not object_node or not field_node:
            return None

        field_name = safe_decode_text(field_node)
        if not field_name:
            return None

        # A nested receiver (`obj.address.zipCode`) has a field_access as its object;
        # recurse to infer that inner type before the outer field, so multi-level
        # access resolves instead of failing on a non-variable name.
        if object_node.type == cs.TS_FIELD_ACCESS:
            object_type = self._infer_java_field_access_type(
                object_node, module_qn, local_var_types
            )
        elif object_name := safe_decode_text(object_node):
            object_type = self._resolve_field_access_base_type(
                object_name, field_access_node, module_qn, local_var_types
            )
        else:
            object_type = None

        if object_type:
            return self._lookup_java_field_type(object_type, field_name, module_qn)
        return None

    def _resolve_field_access_base_type(
        self,
        object_name: str,
        field_access_node: ASTNode,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
    ) -> str | None:
        # The caller's own locals win over the module-wide, name-keyed map,
        # which cannot tell two methods' same-named variables apart.
        if local_var_types and object_name in local_var_types:
            return local_var_types[object_name]
        # `this`/`super` are receiver keywords, not variables: resolve them to the
        # containing class or its superclass so nested chains rooted at them
        # (e.g. `var c = this.address.city`) infer a type.
        if object_name in (cs.JAVA_KEYWORD_THIS, cs.JAVA_KEYWORD_SUPER):
            if not (class_node := self._find_containing_java_class(field_access_node)):
                return None
            class_info = extract_class_info(class_node)
            class_name = class_info.get(cs.FIELD_NAME)
            if object_name == cs.JAVA_KEYWORD_THIS:
                return class_name
            # `super`: return the fully-qualified parent from class_inheritance so a
            # nested superclass (`Outer.Base`) resolves; the AST's relative name
            # would be treated as an absolute class key by the field lookup.
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

        cache_key = f"{module_qn}{cs.CHAR_COLON}{var_name}"
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

        # Walk the inheritance chain via qualified parents from class_inheritance:
        # a field accessed on a subclass may be declared on a superclass, including
        # a nested one like `Outer.Base`. Seen-guarded.
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
        # The file module is the longest registered prefix of the class qn; the rest
        # are the (possibly nested) class path within it, so `proj.pkg.Outer.Base`
        # resolves to file `proj.pkg` + path [Outer, Base].
        parts = class_qn.split(cs.SEPARATOR_DOT)
        for split in range(len(parts) - 1, 0, -1):
            module_candidate = cs.SEPARATOR_DOT.join(parts[:split])
            file_path = self.module_qn_to_file_path.get(module_candidate)
            if file_path is not None and (entry := self.ast_cache.load(file_path)):
                root_node, _ = entry
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
            if field_child.type != cs.TS_FIELD_DECLARATION:
                continue
            field_info = extract_field_info(field_child)
            if field_info.get(cs.FIELD_NAME) == field_name and (
                field_type := field_info.get(cs.FIELD_TYPE)
            ):
                return self._resolve_java_type_name(str(field_type), module_qn)
        return None
