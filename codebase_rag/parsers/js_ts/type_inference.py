from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs as ls
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, NodeType
from ..utils import get_cached_query, safe_decode_text
from . import utils as ut

# Callable node types whose bodies own their locals and returns; the shared
# JS_TS_FUNCTION_NODES tuple lacks the generator EXPRESSION form.
_JS_NESTED_CALLABLE_TYPES = frozenset(cs.JS_TS_FUNCTION_NODES) | {
    cs.TS_GENERATOR_FUNCTION
}

# Ancestor node types that bound a `let`/`const` declaration's scope: the
# nearest one governs the binding (a for-header declaration scopes to the for
# statement, a case-level one to the whole switch body).
_JS_SCOPE_CONTAINER_TYPES = frozenset(
    {
        cs.TS_STATEMENT_BLOCK,
        cs.TS_JS_SWITCH_BODY,
        cs.TS_JS_FOR_STATEMENT,
        cs.TS_JS_FOR_IN_STATEMENT,
    }
)

# Declarations of a name: (scope start_byte, scope end_byte, `new` value node
# when the declarator's own initialiser constructs, else None). Assignments:
# (site byte, `new` value node).
_CtorDecls = dict[str, list[tuple[int, int, "ASTNode | None"]]]
_CtorAssigns = dict[str, list[tuple[int, "ASTNode"]]]
_CtorBindingIndex = tuple[_CtorDecls, _CtorAssigns]

if TYPE_CHECKING:
    from ...types_defs import LanguageQueries
    from ..import_processor import ImportProcessor

_JS_DECLARATOR_QUERY = "(variable_declarator) @declarator"


class JsTypeInferenceEngine:
    __slots__ = (
        "import_processor",
        "function_registry",
        "project_name",
        "_find_method_ast_node",
        "_queries",
    )

    def __init__(
        self,
        import_processor: ImportProcessor,
        function_registry: FunctionRegistryTrieProtocol,
        project_name: str,
        find_method_ast_node_func: Callable[[str], ASTNode | None],
        queries: Mapping[cs.SupportedLanguage, LanguageQueries] | None = None,
    ):
        self.import_processor = import_processor
        self.function_registry = function_registry
        self.project_name = project_name
        self._find_method_ast_node = find_method_ast_node_func
        self._queries = queries

    def _get_declarators_via_query(
        self, caller_node: ASTNode, language: cs.SupportedLanguage | None = None
    ) -> list[Node] | None:
        if self._queries is None:
            return None
        # sorted: frozenset order varies across runs (str-hash randomization) and
        # the first language with queries wins, so keep it deterministic.
        langs = [language] if language is not None else sorted(cs.JS_TS_LANGUAGES)
        for lang in langs:
            lang_queries = self._queries.get(lang)
            if lang_queries and "language" in lang_queries:
                try:
                    q = get_cached_query(lang_queries["language"], _JS_DECLARATOR_QUERY)
                    cursor = QueryCursor(q)
                    captures = cursor.captures(caller_node)
                    return captures.get("declarator", [])
                except Exception:
                    continue
        return None

    def build_local_variable_type_map(
        self,
        caller_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> dict[str, str]:
        local_var_types: dict[str, str] = {}
        declarator_count = 0

        declarator_nodes = self._get_declarators_via_query(caller_node, language)
        if declarator_nodes is not None:
            for current in declarator_nodes:
                declarator_count += 1
                name_node = current.child_by_field_name("name")
                value_node = current.child_by_field_name("value")
                if name_node and value_node:
                    var_name_text = name_node.text
                    if var_name_text:
                        var_name = safe_decode_text(name_node)
                        if var_name is not None:
                            logger.debug(
                                ls.JS_VAR_DECLARATOR_FOUND,
                                var_name=var_name,
                                module_qn=module_qn,
                            )
                            if var_type := self._infer_js_variable_type_from_value(
                                value_node, module_qn
                            ):
                                local_var_types[var_name] = var_type
                                logger.debug(
                                    ls.JS_VAR_INFERRED,
                                    var_name=var_name,
                                    var_type=var_type,
                                )
                            else:
                                logger.debug(ls.JS_VAR_INFER_FAILED, var_name=var_name)
        else:
            stack: list[ASTNode] = [caller_node]
            while stack:
                current = stack.pop()
                if current.type == cs.TS_VARIABLE_DECLARATOR:
                    declarator_count += 1
                    name_node = current.child_by_field_name("name")
                    value_node = current.child_by_field_name("value")
                    if name_node and value_node:
                        var_name_text = name_node.text
                        if var_name_text:
                            var_name = safe_decode_text(name_node)
                            if var_name is not None:
                                logger.debug(
                                    ls.JS_VAR_DECLARATOR_FOUND,
                                    var_name=var_name,
                                    module_qn=module_qn,
                                )
                                if var_type := self._infer_js_variable_type_from_value(
                                    value_node, module_qn
                                ):
                                    local_var_types[var_name] = var_type
                                    logger.debug(
                                        ls.JS_VAR_INFERRED,
                                        var_name=var_name,
                                        var_type=var_type,
                                    )
                                else:
                                    logger.debug(
                                        ls.JS_VAR_INFER_FAILED, var_name=var_name
                                    )
                stack.extend(reversed(current.children))

        logger.debug(
            ls.JS_VAR_TYPE_MAP_BUILT,
            count=len(local_var_types),
            declarator_count=declarator_count,
        )
        return local_var_types

    def _infer_js_variable_type_from_value(
        self, value_node: ASTNode, module_qn: str
    ) -> str | None:
        logger.debug(ls.JS_INFER_VALUE_NODE, node_type=value_node.type)

        if value_node.type == cs.TS_NEW_EXPRESSION:
            if class_name := ut.extract_constructor_name(value_node):
                class_qn = self._resolve_js_class_name(class_name, module_qn)
                return class_qn or class_name

        elif value_node.type == cs.TS_CALL_EXPRESSION:
            func_node = value_node.child_by_field_name("function")
            func_type = func_node.type if func_node else cs.STR_NONE
            logger.debug(ls.JS_CALL_EXPR_FUNC_NODE, func_type=func_type)

            if func_node and func_node.type == cs.TS_MEMBER_EXPRESSION:
                method_call_text = ut.extract_method_call(func_node)
                logger.debug(ls.JS_EXTRACTED_METHOD_CALL, method_call=method_call_text)
                if method_call_text:
                    if inferred_type := self._infer_js_method_return_type(
                        method_call_text, module_qn
                    ):
                        logger.debug(
                            ls.JS_TYPE_INFERRED,
                            method_call=method_call_text,
                            inferred_type=inferred_type,
                        )
                        return inferred_type
                    logger.debug(
                        ls.JS_RETURN_TYPE_INFER_FAILED, method_call=method_call_text
                    )

            elif func_node and func_node.type == cs.TS_IDENTIFIER:
                func_name = func_node.text
                if func_name:
                    return safe_decode_text(func_node)

        logger.debug(ls.JS_NO_PATTERN_MATCHED, node_type=value_node.type)
        return None

    def _infer_js_method_return_type(
        self, method_call: str, module_qn: str
    ) -> str | None:
        parts = method_call.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            logger.debug(ls.JS_METHOD_CALL_INVALID, method_call=method_call)
            return None

        class_name, method_name = parts

        class_qn = self._resolve_js_class_name(class_name, module_qn)
        if not class_qn:
            logger.debug(
                ls.JS_CLASS_RESOLVE_FAILED, class_name=class_name, module_qn=module_qn
            )
            return None

        logger.debug(ls.JS_CLASS_RESOLVED, class_name=class_name, class_qn=class_qn)

        method_qn = f"{class_qn}{cs.SEPARATOR_DOT}{method_name}"
        logger.debug(ls.JS_LOOKING_FOR_METHOD, method_qn=method_qn)

        method_node = self._find_method_ast_node(method_qn)
        if not method_node:
            logger.debug(ls.JS_METHOD_AST_NOT_FOUND, method_qn=method_qn)
            return None

        return_type = self._analyze_return_statements(method_node, method_qn)
        logger.debug(
            ls.JS_RETURN_ANALYZED, method_qn=method_qn, return_type=return_type
        )
        return return_type

    def _resolve_js_class_name(self, class_name: str, module_qn: str) -> str | None:
        if module_qn in self.import_processor.import_mapping:
            import_map = self.import_processor.import_mapping[module_qn]
            if class_name in import_map:
                imported_qn = import_map[class_name]

                full_class_qn = f"{imported_qn}{cs.SEPARATOR_DOT}{class_name}"
                if (
                    full_class_qn in self.function_registry
                    and self.function_registry[full_class_qn] == NodeType.CLASS
                ):
                    return full_class_qn

                return imported_qn

        local_class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_name}"
        if (
            local_class_qn in self.function_registry
            and self.function_registry[local_class_qn] == NodeType.CLASS
        ):
            return local_class_qn

        return None

    def _get_language_obj(self) -> object | None:
        if self._queries is None:
            return None
        for lang in sorted(cs.JS_TS_LANGUAGES):
            lang_queries = self._queries.get(lang)
            if lang_queries and "language" in lang_queries:
                return lang_queries["language"]
        return None

    def _analyze_return_statements(
        self, method_node: ASTNode, method_qn: str
    ) -> str | None:
        return_nodes: list[ASTNode] = []
        ut.find_return_statements(method_node, return_nodes, self._get_language_obj())

        # One O(body) scan shared by every `return <identifier>` in this
        # method; deliberately NOT stored on the engine: a tree-sitter node id
        # is a recycled heap address across parses, and holding Node values
        # would pin whole trees against the bounded AST cache.
        ctor_index: _CtorBindingIndex | None = None

        for return_node in return_nodes:
            # Nested callables own their returns: a callback's `return new
            # Foo()` (or `return x`) is the callback's value, never the
            # method's. The ONE exception is an IIFE whose call value the
            # method itself returns (`return (function () { return new C()
            # })()`): its direct-expression returns ARE the method's value.
            owner_is_method = self._return_belongs_to(return_node, method_node)
            if not owner_is_method and not self._iife_return_of(
                return_node, method_node
            ):
                continue
            for child in return_node.children:
                if child.type == cs.TS_RETURN:
                    continue

                if inferred_type := ut.analyze_return_expression(child, method_qn):
                    return inferred_type

                if not owner_is_method:
                    # An IIFE's `return x` reads the IIFE's OWN locals; the
                    # method-body binding index says nothing about them.
                    continue

                # `return x` where the METHOD'S OWN body binds `x = new
                # C(...)` (the cache-then-construct factory, fastify's
                # ContentType.from, issue #992): the CONSTRUCTED class types
                # the return when every construction reaching THIS return's
                # variable agrees. Bindings resolve by SCOPE SPAN: the
                # innermost declarator whose scope encloses the return is the
                # variable, so a nested-block shadow neither erases an outer
                # construction nor inherits it. Unknown-value assignments
                # (the cache hit) do not veto; a second DIFFERENT class does.
                if child.type == cs.TS_IDENTIFIER and (name := safe_decode_text(child)):
                    if ctor_index is None:
                        ctor_index = self._js_ctor_binding_index(method_node)
                    constructed = self._js_constructed_for(
                        name, child.start_byte, ctor_index
                    )
                    if constructed is None:
                        continue
                    ctor = ut.extract_constructor_name(constructed)
                    if ctor:
                        own_qn = ut.analyze_return_expression(constructed, method_qn)
                        own_leaf = (
                            own_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1] if own_qn else None
                        )
                        # analyze_return_expression resolves a NEW to the
                        # method's OWN class; keep that qn precision only
                        # when the constructed class IS the own class.
                        # Otherwise resolve the constructed class in the
                        # FACTORY'S module (where the construction names it),
                        # falling back to the bare name.
                        if ctor == own_leaf:
                            return own_qn
                        if own_qn and cs.SEPARATOR_DOT in own_qn:
                            factory_module = own_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
                            if resolved := self._resolve_js_class_name(
                                ctor, factory_module
                            ):
                                return resolved
                        return ctor

        return None

    @staticmethod
    def _return_belongs_to(return_node: ASTNode, method_node: ASTNode) -> bool:
        current = return_node.parent
        while current is not None:
            if current.type in _JS_NESTED_CALLABLE_TYPES:
                return current.id == method_node.id
            current = current.parent
        return False

    def _iife_return_of(self, return_node: ASTNode, method_node: ASTNode) -> bool:
        # True when the return's owning callable is immediately invoked and
        # the call's value is returned by the method itself, so the inner
        # return IS the method's return value. A callback ARGUMENT never
        # qualifies: its owner is not the callee of any enclosing call.
        owner = self._js_owning_callable(return_node)
        if owner is None or owner.id == method_node.id:
            return False
        call = self._js_enclosing_invocation(owner)
        if call is None:
            return False
        consumer = self._js_value_consumer_return(call)
        return consumer is not None and self._return_belongs_to(consumer, method_node)

    @staticmethod
    def _js_owning_callable(return_node: ASTNode) -> ASTNode | None:
        current = return_node.parent
        while current is not None:
            if current.type in _JS_NESTED_CALLABLE_TYPES:
                return current
            current = current.parent
        return None

    @staticmethod
    def _js_enclosing_invocation(owner: ASTNode) -> ASTNode | None:
        # Climb value-transparent wrappers; at a call, require the wrapped
        # owner to BE the callee (not an argument, not one operand of many).
        # The one argument-position exception: the JS grammar parses
        # `await (X)()` as a call to an identifier spelled `await` with X as
        # its sole argument, so that call IS a transparent await of X.
        node = owner
        current = owner.parent
        while current is not None:
            if (
                current.type == cs.TS_PARENTHESIZED_EXPRESSION
                or current.type in cs.TS_CAST_WRAPPER_TYPES
            ):
                node = current
                current = current.parent
                continue
            if current.type == cs.TS_ARGUMENTS:
                call = current.parent
                func = (
                    call.child_by_field_name(cs.FIELD_FUNCTION)
                    if call is not None and call.type == cs.TS_CALL_EXPRESSION
                    else None
                )
                if (
                    call is not None
                    and func is not None
                    and func.type == cs.TS_IDENTIFIER
                    and safe_decode_text(func) == cs.JS_AWAIT_IDENTIFIER
                ):
                    node = call
                    current = call.parent
                    continue
                return None
            if current.type == cs.TS_CALL_EXPRESSION:
                func = current.child_by_field_name(cs.FIELD_FUNCTION)
                if func is not None and func.id == node.id:
                    return current
                return None
            return None
        return None

    @staticmethod
    def _js_value_consumer_return(call: ASTNode) -> ASTNode | None:
        current = call.parent
        while current is not None:
            if (
                current.type == cs.TS_PARENTHESIZED_EXPRESSION
                or current.type in cs.TS_CAST_WRAPPER_TYPES
                or current.type == cs.TS_AWAIT_EXPRESSION
            ):
                current = current.parent
                continue
            if current.type == cs.TS_RETURN_STATEMENT:
                return current
            return None
        return None

    def _js_ctor_binding_index(self, method_node: ASTNode) -> _CtorBindingIndex:
        # One scan of the method body (nested callables excluded: their
        # locals are their own) collecting every DECLARATION of every name
        # with the byte span of the scope it governs, plus every
        # `x = new C(...)` assignment site. Scope spans, not name equality,
        # decide which construction reaches which return.
        decls: _CtorDecls = {}
        assigns: _CtorAssigns = {}
        stack: list[ASTNode] = list(method_node.children)
        while stack:
            node = stack.pop()
            if node.type in _JS_NESTED_CALLABLE_TYPES:
                continue
            if node.type == cs.TS_VARIABLE_DECLARATOR:
                self._index_ctor_declarator(node, method_node, decls)
            elif node.type == cs.TS_JS_ASSIGNMENT_EXPRESSION:
                self._index_ctor_assignment(node, assigns)
            elif node.type == cs.TS_JS_FOR_IN_STATEMENT:
                self._index_ctor_loop_binding(node, method_node, decls)
            elif node.type == cs.TS_JS_CATCH_CLAUSE:
                self._index_ctor_catch_binding(node, decls)
            stack.extend(node.children)
        return decls, assigns

    def _index_ctor_declarator(
        self, node: ASTNode, method_node: ASTNode, decls: _CtorDecls
    ) -> None:
        target = node.child_by_field_name(cs.FIELD_NAME)
        if target is None:
            return
        value = node.child_by_field_name(cs.FIELD_VALUE)
        ctor_value = (
            value if value is not None and value.type == cs.TS_NEW_EXPRESSION else None
        )
        is_lexical = (
            node.parent is not None and node.parent.type == cs.TS_LEXICAL_DECLARATION
        )
        span = (
            self._js_scope_span(node, method_node)
            if is_lexical
            else (method_node.start_byte, method_node.end_byte)
        )
        # A destructuring pattern introduces its names with an unknowable
        # value (the construction cannot be attributed to one of them).
        single = target.type == cs.TS_IDENTIFIER
        for bound_name in self._js_binding_names(target):
            decls.setdefault(bound_name, []).append(
                (span[0], span[1], ctor_value if single else None)
            )

    @staticmethod
    def _index_ctor_assignment(node: ASTNode, assigns: _CtorAssigns) -> None:
        target = node.child_by_field_name(cs.FIELD_LEFT)
        value = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if (
            target is not None
            and value is not None
            and target.type == cs.TS_IDENTIFIER
            and value.type == cs.TS_NEW_EXPRESSION
            and (bound_name := safe_decode_text(target))
        ):
            assigns.setdefault(bound_name, []).append((node.start_byte, value))

    def _index_ctor_loop_binding(
        self, node: ASTNode, method_node: ASTNode, decls: _CtorDecls
    ) -> None:
        # `for (const x of xs)` binds x over the loop; `for (var x of xs)`
        # hoists it; `for (x of xs)` (no kind) assigns an existing binding
        # and introduces nothing.
        kind = node.child_by_field_name(cs.FIELD_KIND)
        if kind is None:
            return
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        span = (
            (method_node.start_byte, method_node.end_byte)
            if kind.type == cs.TS_JS_VAR_KIND
            else (node.start_byte, node.end_byte)
        )
        for bound_name in self._js_binding_names(left):
            decls.setdefault(bound_name, []).append((span[0], span[1], None))

    def _index_ctor_catch_binding(self, node: ASTNode, decls: _CtorDecls) -> None:
        param = node.child_by_field_name(cs.FIELD_PARAMETER)
        if param is None:
            return
        for bound_name in self._js_binding_names(param):
            decls.setdefault(bound_name, []).append(
                (node.start_byte, node.end_byte, None)
            )

    @staticmethod
    def _js_binding_names(target: ASTNode) -> list[str]:
        # Only BINDING positions introduce names: a pattern default's right
        # side, a computed key's expression, and a pair's key are READS of
        # the enclosing scope and must not shadow it.
        names: list[str] = []
        stack: list[ASTNode] = [target]
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in (
                cs.TS_IDENTIFIER,
                cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
            ):
                if name := safe_decode_text(node):
                    names.append(name)
            elif node_type in (
                cs.TS_ASSIGNMENT_PATTERN,
                cs.TS_OBJECT_ASSIGNMENT_PATTERN,
            ):
                if (left := node.child_by_field_name(cs.FIELD_LEFT)) is not None:
                    stack.append(left)
            elif node_type == cs.TS_PAIR_PATTERN:
                if (value := node.child_by_field_name(cs.FIELD_VALUE)) is not None:
                    stack.append(value)
            elif node_type in (
                cs.TS_OBJECT_PATTERN,
                cs.TS_ARRAY_PATTERN,
                cs.TS_REST_PATTERN,
            ):
                stack.extend(node.named_children)
        return names

    @staticmethod
    def _js_scope_span(node: ASTNode, method_node: ASTNode) -> tuple[int, int]:
        # The scope a `let`/`const` governs: the nearest enclosing block-like
        # ancestor (a for-header declaration governs the for statement, a
        # case-level one the switch body), or the whole method.
        current = node.parent
        while current is not None and current.id != method_node.id:
            if current.type in _JS_SCOPE_CONTAINER_TYPES:
                return (current.start_byte, current.end_byte)
            current = current.parent
        return (method_node.start_byte, method_node.end_byte)

    @staticmethod
    def _js_innermost_scope(
        entries: list[tuple[int, int, ASTNode | None]], pos: int
    ) -> tuple[int, int] | None:
        enclosing = [(s, e) for s, e, _v in entries if s <= pos < e]
        if not enclosing:
            return None
        # Nested spans: the innermost starts latest; ties are the same span.
        return max(enclosing, key=lambda span: (span[0], -span[1]))

    def _js_constructed_for(
        self, name: str, pos: int, index: _CtorBindingIndex
    ) -> ASTNode | None:
        # The variable `return x` reads is the innermost declaration of x
        # whose scope encloses the return (no declaration: the name is a
        # parameter, function-scoped). The constructions reaching it are its
        # own initialiser plus every assignment site resolving to the SAME
        # declaration; they must all agree on one class.
        decls, assigns = index
        entries = decls.get(name, [])
        scope = self._js_innermost_scope(entries, pos)
        constructions: list[ASTNode] = []
        if scope is not None:
            constructions.extend(
                value
                for s, e, value in entries
                if (s, e) == scope and value is not None
            )
        for site, value in assigns.get(name, []):
            if self._js_innermost_scope(entries, site) == scope:
                constructions.append(value)
        # A construction whose class name cannot be extracted (`new
        # registry.Cached(v)`) is an UNKNOWN class: it vetoes exactly like a
        # different named class, never silently loses to one.
        ctor_names = {ut.extract_constructor_name(value) for value in constructions}
        if len(ctor_names) != 1:
            return None
        chosen = ctor_names.pop()
        if chosen is None:
            return None
        return next(
            value
            for value in constructions
            if ut.extract_constructor_name(value) == chosen
        )
