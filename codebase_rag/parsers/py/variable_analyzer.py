from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol

from loguru import logger
from tree_sitter import QueryCursor

from ... import constants as cs
from ... import logs as lg
from ...types_defs import ASTNode, FunctionRegistryTrieProtocol, NodeType
from ..import_processor import ImportProcessor
from ..utils import get_cached_query, safe_decode_text
from .utils import resolve_class_name

# Deepest operand chain `_value_leaves` will walk. Each term of `a or b or c`
# or `x + y + z` is one level. Measured over every assignment in this repo the
# deepest chain is 6, so hand-written code never gets near this and only
# generated source reaches it. The cap exists because a RecursionError here is
# SILENT: the caller runs inside a broad `except Exception` that would drop the
# enclosing function's whole type map, which is the very defect #1868 fixes.
_MAX_ALIAS_DEPTH = 32


class _Alias(NamedTuple):
    """What a local variable was assigned, reduced to what it can evaluate to.

    `candidates` are the leaf expressions the right-hand side can take the
    value of, in preference order: names and attributes read the type map, a
    constructor call infers its own. For an operator expression they are the
    LEFT operand's leaves, `dunder` is the forward method (`__truediv__`) and
    `reflected` holds the right operand's leaves, consulted for the reflected
    method (`__rtruediv__`) when no left type supplies the forward one --
    which is Python's own dispatch order.
    """

    candidates: tuple[ASTNode, ...]
    dunder: str | None
    reflected: tuple[ASTNode, ...]


if TYPE_CHECKING:

    class _VariableAnalyzerDeps(Protocol):
        def _infer_type_from_expression(
            self, node: ASTNode, module_qn: str
        ) -> str | None: ...

        def _find_class_node(self, class_qn: str) -> ASTNode | None: ...

        def _find_method_ast_node(self, method_qn: str) -> ASTNode | None: ...

        def _find_class_in_scope(
            self, class_name: str, module_qn: str
        ) -> str | None: ...

        def _infer_method_call_return_type(
            self,
            method_call: str,
            module_qn: str,
            local_var_types: dict[str, str] | None = None,
        ) -> str | None: ...

        def _get_method_return_type_from_ast(self, method_qn: str) -> str | None: ...

    _VarBase: type = _VariableAnalyzerDeps
else:
    _VarBase = object


def _union_members(type_str: str) -> list[str]:
    """The non-None members of a type as written, in order: `A | None | B` -> [A, B]."""
    return [
        member
        for part in type_str.split(cs.PY_UNION_SEPARATOR)
        if (member := part.strip()) and member != cs.PY_NONE
    ]


def _non_none_members(type_str: str) -> frozenset[str]:
    """`Widget | None` and `Widget` name the same receiver; compare them as such."""
    return frozenset(_union_members(type_str))


def _reflected_dunder(dunder: str) -> str:
    """`__truediv__` -> `__rtruediv__`: the method the RIGHT operand supplies."""
    return f"__r{dunder[2:]}"


def _container_element_type(type_str: str | None) -> str | None:
    """The element inside a canonical ``list[<element>]`` marker, else ``None``."""
    if (
        type_str
        and type_str.startswith(cs.PY_LIST_TYPE_PREFIX)
        and type_str.endswith("]")
    ):
        return type_str[len(cs.PY_LIST_TYPE_PREFIX) : -1] or None
    return None


class PythonVariableAnalyzerMixin(_VarBase):
    __slots__ = ()
    import_processor: ImportProcessor
    function_registry: FunctionRegistryTrieProtocol
    queries: dict[cs.SupportedLanguage, object]
    _available_classes_cache: dict[str, list[str]]
    _class_member_type_cache: dict[str, dict[str, str]]
    class_inheritance: dict[str, list[str]]

    def _infer_parameter_types(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        params_node = caller_node.child_by_field_name(cs.TS_FIELD_PARAMETERS)
        if not params_node:
            return

        for param in params_node.children:
            self._process_parameter(param, local_var_types, module_qn)

    def _process_parameter(
        self, param: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        match param.type:
            case cs.TS_PY_IDENTIFIER:
                self._process_untyped_parameter(param, local_var_types, module_qn)
            case cs.TS_PY_TYPED_PARAMETER:
                self._process_typed_parameter(param, local_var_types)
            case cs.TS_PY_TYPED_DEFAULT_PARAMETER:
                self._process_typed_default_parameter(param, local_var_types)

    def _process_untyped_parameter(
        self, param: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if (
            param.text is None
            or (param_name := safe_decode_text(param)) is None
            or not (
                inferred_type := self._infer_type_from_parameter_name(
                    param_name, module_qn
                )
            )
        ):
            return
        local_var_types[param_name] = inferred_type
        logger.debug(lg.PY_PARAM_TYPE_INFERRED, param=param_name, type=inferred_type)

    def _process_typed_parameter(
        self, param: ASTNode, local_var_types: dict[str, str]
    ) -> None:
        param_name_node = next(
            (c for c in param.children if c.type == cs.TS_PY_IDENTIFIER), None
        )
        param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        if not (
            param_name_node
            and param_type_node
            and param_name_node.text
            and param_type_node.text
            and (param_name := safe_decode_text(param_name_node))
            and (param_type := safe_decode_text(param_type_node))
        ):
            return
        local_var_types[param_name] = param_type

    def _process_typed_default_parameter(
        self, param: ASTNode, local_var_types: dict[str, str]
    ) -> None:
        param_name_node = param.child_by_field_name(cs.TS_FIELD_NAME)
        param_type_node = param.child_by_field_name(cs.TS_FIELD_TYPE)
        if not (
            param_name_node
            and param_type_node
            and param_name_node.text
            and param_type_node.text
            and (param_name := safe_decode_text(param_name_node))
            and (param_type := safe_decode_text(param_type_node))
        ):
            return
        local_var_types[param_name] = param_type

    def _infer_type_from_parameter_name(
        self, param_name: str, module_qn: str
    ) -> str | None:
        logger.debug(lg.PY_TYPE_INFER_ATTEMPT, param=param_name, module=module_qn)
        available_class_names = self._collect_available_classes(module_qn)
        logger.debug(lg.PY_AVAILABLE_CLASSES, classes=available_class_names)
        return self._find_best_class_match(param_name, available_class_names)

    def _collect_available_classes(self, module_qn: str) -> list[str]:
        if module_qn in self._available_classes_cache:
            return self._available_classes_cache[module_qn]
        available_class_names: list[str] = []
        for qn, node_type in self.function_registry.find_with_prefix(module_qn):
            if node_type != NodeType.CLASS:
                continue
            if cs.SEPARATOR_DOT.join(qn.split(cs.SEPARATOR_DOT)[:-1]) == module_qn:
                available_class_names.append(qn.split(cs.SEPARATOR_DOT)[-1])

        if module_qn not in self.import_processor.import_mapping:
            self._available_classes_cache[module_qn] = available_class_names
            return available_class_names

        for local_name, imported_qn in self.import_processor.import_mapping[
            module_qn
        ].items():
            if self.function_registry.get(imported_qn) == NodeType.CLASS:
                available_class_names.append(local_name)

        self._available_classes_cache[module_qn] = available_class_names
        return available_class_names

    def _find_best_class_match(
        self, param_name: str, available_class_names: list[str]
    ) -> str | None:
        param_lower = param_name.lower()
        best_match = None
        highest_score = 0

        for class_name in available_class_names:
            score = self._calculate_match_score(param_lower, class_name.lower())
            if score > highest_score:
                highest_score = score
                best_match = class_name

        logger.debug(
            lg.PY_BEST_MATCH, param=param_name, match=best_match, score=highest_score
        )
        return best_match

    def _calculate_match_score(self, param_lower: str, class_lower: str) -> int:
        if param_lower == class_lower:
            return cs.PY_SCORE_EXACT_MATCH
        if class_lower.endswith(param_lower) or param_lower.endswith(class_lower):
            return cs.PY_SCORE_SUFFIX_MATCH
        if class_lower in param_lower:
            return int(
                cs.PY_SCORE_CONTAINS_BASE * (len(class_lower) / len(param_lower))
            )
        return 0

    def _analyze_comprehension(
        self, comp_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        for child in comp_node.children:
            if child.type == cs.TS_PY_FOR_IN_CLAUSE:
                self._analyze_for_clause(child, local_var_types, module_qn)

    def _analyze_for_loop(
        self, for_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        self._analyze_for_clause(for_node, local_var_types, module_qn)

    def _analyze_for_clause(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        if (left_node := node.child_by_field_name(cs.TS_FIELD_LEFT)) and (
            right_node := node.child_by_field_name(cs.TS_FIELD_RIGHT)
        ):
            self._infer_loop_var_from_iterable(
                left_node, right_node, local_var_types, module_qn
            )

    def _infer_loop_var_from_iterable(
        self,
        left_node: ASTNode,
        right_node: ASTNode,
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        if not (loop_var := self._extract_variable_name(left_node)):
            return

        if element_type := self._infer_iterable_element_type(
            right_node, local_var_types, module_qn
        ):
            local_var_types[loop_var] = element_type
            logger.debug(lg.PY_LOOP_VAR_INFERRED, var=loop_var, type=element_type)

    def _infer_iterable_element_type(
        self, iterable_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if iterable_node.type == cs.TS_PY_LIST:
            return self._infer_list_element_type(iterable_node)

        if iterable_node.type == cs.TS_PY_CALL:
            # `for w in load_widgets():` types through the callee's container
            # return annotation (`-> list[Widget]`); a scalar return is not an
            # element type, so only the container marker unwraps. A
            # `self.load_more()` iterable falls back to the enclosing class's
            # own method.
            call_text = safe_decode_text(iterable_node)
            if not call_text:
                return None
            return _container_element_type(
                self._infer_method_call_return_type(
                    call_text, module_qn, local_var_types
                )
                or self._self_method_element_type(iterable_node, module_qn)
            )

        if iterable_node.type == cs.TS_PY_ATTRIBUTE:
            # `for w in self.widgets:` reads the attribute's already-inferred
            # container type (keyed as `self.widgets` by the self-assignment
            # pass).
            attr_text = safe_decode_text(iterable_node)
            if not attr_text:
                return None
            return _container_element_type(local_var_types.get(attr_text))

        if (
            iterable_node.type != cs.TS_PY_IDENTIFIER
            or iterable_node.text is None
            or (var_name := safe_decode_text(iterable_node)) is None
        ):
            return None
        return self._infer_variable_element_type(var_name, local_var_types, module_qn)

    def _infer_list_element_type(self, list_node: ASTNode) -> str | None:
        for child in list_node.children:
            if child.type != cs.TS_PY_CALL:
                continue
            func_node = child.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func_node
                and func_node.type == cs.TS_PY_IDENTIFIER
                and func_node.text
                and (class_name := safe_decode_text(func_node))
                and class_name[0].isupper()
            ):
                return class_name
        return None

    def _infer_instance_variable_types_from_assignments(
        self,
        assignments: list[ASTNode],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        for assignment in assignments:
            self._process_self_assignment(assignment, local_var_types, module_qn)

    def _process_self_assignment(
        self, assignment: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
        if not (
            left_node
            and right_node
            and left_node.type == cs.TS_PY_ATTRIBUTE
            and (left_text := left_node.text)
            and (attr_name := left_text.decode(cs.ENCODING_UTF8)).startswith(
                cs.PY_SELF_PREFIX
            )
        ):
            return
        assigned_type = self._infer_type_from_expression(right_node, module_qn)
        if not assigned_type and right_node.type == cs.TS_PY_IDENTIFIER:
            # self.x = param: a bare identifier carries the type of the matching
            # (already-seeded) parameter or local, so flow it onto the attribute.
            ident = safe_decode_text(right_node)
            assigned_type = local_var_types.get(ident) if ident else None
        if not assigned_type:
            return
        local_var_types[attr_name] = assigned_type
        logger.debug(lg.PY_INSTANCE_VAR_INFERRED, attr=attr_name, type=assigned_type)

    def _analyze_self_assignments(
        self, node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        py_lang_queries = self.queries.get(cs.SupportedLanguage.PYTHON)
        py_lang_obj = py_lang_queries["language"] if py_lang_queries else None
        if py_lang_obj is not None:
            try:
                q = get_cached_query(py_lang_obj, cs.PY_ASSIGNMENT_QUERY)
                cursor = QueryCursor(q)
                captures = cursor.captures(node)
                for assign_node in captures.get("assignment", []):
                    self._process_self_assignment(
                        assign_node, local_var_types, module_qn
                    )
                return
            except Exception:
                pass
        stack: list[ASTNode] = [node]
        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_ASSIGNMENT:
                self._process_self_assignment(current, local_var_types, module_qn)
            stack.extend(reversed(current.children))

    def _self_method_element_type(
        self, call_node: ASTNode, module_qn: str
    ) -> str | None:
        """The return type of a ``self.m()`` call, resolved on the enclosing
        class.

        Deliberately scoped to iterable position: a general ``self`` receiver
        must stay override-aware (an abstract stub's concrete sibling wins),
        but an iterated call only needs the visible method's container
        annotation.
        """
        func_node = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func_node is None or func_node.type != cs.TS_PY_ATTRIBUTE:
            return None
        text = safe_decode_text(func_node)
        if not text or not text.startswith(cs.PY_SELF_PREFIX):
            return None
        parts = text.split(cs.SEPARATOR_DOT)
        if len(parts) != 2:
            return None
        if (class_node := self._enclosing_class_node(call_node)) is None:
            return None
        name_node = class_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None or not (class_name := safe_decode_text(name_node)):
            return None
        class_qn = self._find_class_in_scope(class_name, module_qn) or class_name
        if cs.SEPARATOR_DOT not in class_qn:
            # A same-module class resolves to its bare name; the method lookup
            # needs the full module.Class.method shape.
            class_qn = f"{module_qn}{cs.SEPARATOR_DOT}{class_qn}"
        return self._get_method_return_type_from_ast(
            f"{class_qn}{cs.SEPARATOR_DOT}{parts[1]}"
        )

    def _enclosing_class_node(self, node: ASTNode) -> ASTNode | None:
        current = node.parent
        while current is not None:
            if current.type == cs.TS_PY_CLASS_DEFINITION:
                return current
            current = current.parent
        return None

    def _find_init_method_node(self, class_node: ASTNode) -> ASTNode | None:
        body = class_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return None
        for child in body.children:
            if child.type == cs.TS_PY_DECORATED_DEFINITION:
                func = next(
                    (
                        c
                        for c in child.children
                        if c.type == cs.TS_PY_FUNCTION_DEFINITION
                    ),
                    None,
                )
            elif child.type == cs.TS_PY_FUNCTION_DEFINITION:
                func = child
            else:
                continue
            if func is None:
                continue
            name_node = func.child_by_field_name(cs.FIELD_NAME)
            if (
                name_node
                and (text := name_node.text)
                and text.decode(cs.ENCODING_UTF8) == cs.PY_METHOD_INIT
            ):
                return func
        return None

    def _infer_instance_attributes_from_init(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # Instance attributes are assigned in __init__ (self.x = T()), so a method
        # that only reads self.x has no local assignment to infer from. Scan the
        # enclosing class's __init__ and seed the attribute types, letting a
        # reassignment in the calling method win (setdefault).
        if (class_node := self._enclosing_class_node(caller_node)) is None:
            return
        init_node = self._find_init_method_node(class_node)
        if init_node is None or init_node is caller_node:
            return
        init_types: dict[str, str] = {}
        # Seed __init__ parameter types first so self.x = param flows the
        # annotation onto the attribute.
        self._infer_parameter_types(init_node, init_types, module_qn)
        self._analyze_self_assignments(init_node, init_types, module_qn)
        for attr, attr_type in init_types.items():
            if attr.startswith(cs.PY_SELF_PREFIX):
                local_var_types.setdefault(attr, attr_type)

    def _has_property_decorator(self, decorated_node: ASTNode) -> bool:
        for child in decorated_node.children:
            if child.type == cs.TS_PY_DECORATOR and (text := child.text):
                tail = (
                    text.decode(cs.ENCODING_UTF8)
                    .lstrip(cs.DECORATOR_AT)
                    .split(cs.SEPARATOR_DOT)[-1]
                )
                if tail in cs.PROPERTY_DECORATORS:
                    return True
        return False

    def _infer_property_return_types(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # self.prop where prop is an @property carries the property's declared
        # return type, so a chained self.prop.method() resolves against the
        # returned class rather than an ambiguous same-named method elsewhere.
        if (class_node := self._enclosing_class_node(caller_node)) is None:
            return
        self._collect_property_return_types(class_node, local_var_types)

    def _collect_property_return_types(
        self, class_node: ASTNode, out: dict[str, str]
    ) -> None:
        body = class_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        for child in body.children:
            if child.type != cs.TS_PY_DECORATED_DEFINITION:
                continue
            if not self._has_property_decorator(child):
                continue
            func = next(
                (c for c in child.children if c.type == cs.TS_PY_FUNCTION_DEFINITION),
                None,
            )
            if func is None:
                continue
            name_node = func.child_by_field_name(cs.FIELD_NAME)
            return_node = func.child_by_field_name(cs.FIELD_RETURN_TYPE)
            if not (
                name_node
                and (name_text := name_node.text)
                and return_node
                and (return_text := return_node.text)
            ):
                continue
            # The return_type field wraps a type node; only a bare class name (not
            # a union, subscripted generic, or string forward ref) seeds a type.
            return_type = return_text.decode(cs.ENCODING_UTF8)
            if return_type.isidentifier():
                out.setdefault(
                    f"{cs.PY_SELF_PREFIX}{name_text.decode(cs.ENCODING_UTF8)}",
                    return_type,
                )

    def _infer_class_annotation_types(
        self, caller_node: ASTNode, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        # A class-level annotation (_handler: LanguageHandler) declares an instance
        # attribute's type even when it is assigned from a factory call whose return
        # type cannot be inferred, so seed self.<name> from the annotation.
        if (class_node := self._enclosing_class_node(caller_node)) is None:
            return
        self._collect_class_annotation_types(class_node, local_var_types)

    def _collect_class_annotation_types(
        self, class_node: ASTNode, out: dict[str, str]
    ) -> None:
        body = class_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        for child in body.children:
            if child.type != cs.TS_PY_EXPRESSION_STATEMENT:
                continue
            assignment = child.children[0] if child.children else None
            if assignment is None or assignment.type != cs.TS_PY_ASSIGNMENT:
                continue
            left_node = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
            type_node = assignment.child_by_field_name(cs.TS_FIELD_TYPE)
            if not (
                left_node
                and left_node.type == cs.TS_PY_IDENTIFIER
                and type_node
                and (name := safe_decode_text(left_node))
                and (type_text := safe_decode_text(type_node))
                and type_text.isidentifier()
            ):
                continue
            out.setdefault(f"{cs.PY_SELF_PREFIX}{name}", type_text)

    def _expand_chained_attribute_types(
        self,
        local_var_types: dict[str, str],
        module_qn: str,
        aliases: dict[str, _Alias] | None = None,
        max_depth: int = 4,
    ) -> None:
        # A chained reference a.b.c needs the type of a.b (member b on a's class).
        # Each pass: (1) propagate local aliases (x = ref) from the referent's type,
        # (2) for every typed ref, seed ref.member -> member type (full QN), so
        # deeper chains and aliases resolve next pass until a fixpoint.
        aliases = aliases or {}
        for _ in range(max_depth):
            added = False
            for local, alias in aliases.items():
                if local not in local_var_types and (
                    alias_type := self._get_alias_type(
                        alias, local_var_types, module_qn
                    )
                ):
                    local_var_types[local] = alias_type
                    added = True
            for ref, type_name in list(local_var_types.items()):
                class_qn = self._class_qn_of_type(type_name, module_qn)
                if not class_qn:
                    continue
                for member, member_type in self._class_member_types_by_qn(
                    class_qn
                ).items():
                    key = f"{ref}{cs.SEPARATOR_DOT}{member}"
                    if key not in local_var_types:
                        local_var_types[key] = member_type
                        added = True
            if not added:
                break

    def _get_alias_type(
        self, alias: _Alias, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        """The type an alias gives the variable, after any operator.

        Candidates of different types -- `widget if flag else engine` -- become
        the union an annotation would spell (`Widget | Engine`), so the resolver
        applies the one policy it already has for a genuine multi-type union
        (`_strip_optional`: it "stays unresolved"). Typing only the first branch
        drops the other's call, and leaving the receiver untyped hands it to the
        bare-name fallback, which picks one of the two arbitrarily. Only `| None`
        is ignored when comparing: an Optional and its bare type are the same
        receiver.
        """
        left = self._get_candidate_types(alias.candidates, local_var_types, module_qn)
        if alias.dunder is None:
            return left
        right = self._get_candidate_types(alias.reflected, local_var_types, module_qn)
        return self._get_operator_result_type(left, alias.dunder, right, module_qn)

    def _get_candidate_types(
        self,
        candidates: tuple[ASTNode, ...],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> str | None:
        found: dict[frozenset[str], str] = {}
        for node in candidates:
            if node.type == cs.TS_PY_CALL:
                inferred = self._infer_type_from_expression(node, module_qn)
            else:
                inferred = local_var_types.get(safe_decode_text(node) or "")
            if inferred:
                found.setdefault(_non_none_members(inferred), inferred)
        if not found:
            return None
        return f" {cs.PY_UNION_SEPARATOR} ".join(found.values())

    def _get_operator_result_type(
        self, left: str | None, dunder: str, right: str | None, module_qn: str
    ) -> str | None:
        """What `left <op> right` evaluates to, following Python's dispatch.

        Each member of the left type is tried first: a project class answers
        with the return type of the operator method found anywhere up its
        bases (`Sub / cfg` where `Base.__truediv__ -> Product` is a Product);
        a type outside the project -- `pathlib.Path`, `str` -- keeps its own
        type, which is right for `Path / "sub"`. A project class that defines
        the method nowhere hands the operation to the RIGHT operand's
        reflected method, exactly as the interpreter would; if that answers
        nothing either the receiver stays untyped rather than guessing.
        """
        results: dict[str, None] = {}
        forward_missing = False
        left_members = _union_members(left) if left else []
        right_classes = [
            qn
            for member in (_union_members(right) if right else [])
            if (qn := self._get_project_class_qn(member, module_qn)) is not None
        ]
        for member in left_members:
            class_qn = self._get_project_class_qn(member, module_qn)
            if class_qn is None:
                results.setdefault(member, None)
                continue
            # A right operand whose class is a STRICT subclass of the left's
            # gets its reflected method called first, before the left's
            # forward one -- `base / derived` runs `Derived.__rtruediv__`
            # even though `Base.__truediv__` exists.
            subclass_result = self._get_subclass_reflected_type(
                class_qn, right_classes, dunder
            )
            if subclass_result is not None:
                found, returned = subclass_result
                if returned:
                    results.setdefault(returned, None)
                continue
            found, returned = self._get_dunder_return_type(class_qn, dunder)
            if not found:
                forward_missing = True
            elif returned:
                results.setdefault(returned, None)
        if forward_missing or not left_members:
            for member in _union_members(right) if right else []:
                class_qn = self._get_project_class_qn(member, module_qn)
                if class_qn is None:
                    continue
                found, returned = self._get_dunder_return_type(
                    class_qn, _reflected_dunder(dunder)
                )
                if found and returned:
                    results.setdefault(returned, None)
        if not results:
            return None
        return f" {cs.PY_UNION_SEPARATOR} ".join(results)

    def _get_project_class_qn(self, type_name: str, module_qn: str) -> str | None:
        """The registry qn of a class THIS project defines, else None.

        `_class_qn_of_type` also answers for an imported external class
        (`pathlib.Path`), spelled as a dotted name that no AST backs. Treating
        that as a project class whose operator is "missing" would hand
        `Path / "sub"` to reflected dispatch and then leave it untyped; the
        registry is what separates a class we can read from one we cannot.
        """
        class_qn = self._class_qn_of_type(type_name, module_qn)
        if not class_qn or self.function_registry.get(class_qn) != NodeType.CLASS:
            return None
        return class_qn

    def _get_subclass_reflected_type(
        self, left_qn: str, right_classes: list[str], dunder: str
    ) -> tuple[bool, str | None] | None:
        """The reflected method of a right operand that subclasses the left.

        Returns None when no right class is a strict subclass of `left_qn`
        that defines the reflected dunder, so the caller falls through to the
        left's forward method. Python applies this priority only when the
        subclass actually provides the reflected method.
        """
        for right_qn in right_classes:
            if right_qn == left_qn or left_qn not in self._get_mro(right_qn):
                continue
            found, returned = self._get_dunder_return_type(
                right_qn, _reflected_dunder(dunder)
            )
            if found:
                return found, returned
        return None

    def _get_dunder_return_type(
        self, class_qn: str, dunder: str
    ) -> tuple[bool, str | None]:
        """(defined anywhere up the bases?, its return type if known)."""
        for cls in self._get_mro(class_qn):
            method_qn = f"{cls}{cs.SEPARATOR_DOT}{dunder}"
            if self._find_method_ast_node(method_qn) is not None:
                return True, self._get_method_return_type_from_ast(method_qn)
        return False, None

    def _get_mro(self, class_qn: str) -> list[str]:
        """Python's method resolution order for a project class.

        C3 linearisation over `class_inheritance`, which keeps each class's
        bases in declaration order, so `Child(A, B)` with `A(X)` resolves
        `Child, A, X, B` -- the order the interpreter uses -- rather than the
        breadth-first `Child, A, B, X` that put a base's operator ahead of a
        grandparent's. A hierarchy C3 rejects (inconsistent bases, a cycle, or
        one it cannot see all of) falls back to breadth-first, which is what
        CallResolver._mro still uses; a wrong order there costs an operator's
        result type, never a crash.
        """
        return self._get_c3_mro(class_qn, ()) or self._get_bfs_mro(class_qn)

    def _get_c3_mro(self, class_qn: str, stack: tuple[str, ...]) -> list[str] | None:
        if class_qn in stack or len(stack) > _MAX_ALIAS_DEPTH:
            return None
        bases = list(self.class_inheritance.get(class_qn, []))
        sequences: list[list[str]] = []
        for base in bases:
            linear = self._get_c3_mro(base, (*stack, class_qn))
            if linear is None:
                return None
            sequences.append(linear)
        sequences.append(bases)
        order = [class_qn]
        while any(sequences):
            sequences = [seq for seq in sequences if seq]
            head = next(
                (
                    seq[0]
                    for seq in sequences
                    if not any(seq[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if head is None:
                return None
            order.append(head)
            for seq in sequences:
                if seq[0] == head:
                    seq.pop(0)
        return order

    def _get_bfs_mro(self, class_qn: str) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []
        queue = [class_qn]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            order.append(cur)
            queue.extend(self.class_inheritance.get(cur, []))
        return order

    def _get_assignment_alias(self, right: ASTNode) -> _Alias | None:
        """Reduce an assignment's rhs to an `_Alias`, or None if it types nothing.

        A bare call (`x = Engine()`) is left to `_traverse_single_pass`, which
        already types it. An operator whose dunder is not in the table makes
        no claim: its result type is unknown, and untyped is the safe answer.
        """
        if right.type == cs.TS_PY_CALL:
            return None
        if right.type != cs.TS_PY_BINARY_OPERATOR:
            candidates = self._get_value_leaves(right, 0)
            return _Alias(candidates, None, ()) if candidates else None
        operator = right.child_by_field_name(cs.FIELD_OPERATOR)
        token = safe_decode_text(operator) if operator is not None else None
        dunder = cs.PY_BINARY_OPERATOR_DUNDERS.get(token or "")
        if dunder is None:
            return None
        left = right.child_by_field_name(cs.TS_FIELD_LEFT)
        right_operand = right.child_by_field_name(cs.TS_FIELD_RIGHT)
        candidates = self._get_value_leaves(left, 1) if left is not None else ()
        reflected = (
            self._get_value_leaves(right_operand, 1)
            if right_operand is not None
            else ()
        )
        if not candidates and not reflected:
            return None
        return _Alias(candidates, dunder, reflected)

    def _get_value_leaves(self, node: ASTNode, depth: int) -> tuple[ASTNode, ...]:
        """The leaf expressions an rhs can evaluate to, in preference order.

        Without this a variable assigned from any expression got NO type, and a
        later `var.method()` fell back to matching the bare method name against
        every class that defines it (issue #1868): `resolved = target or
        Path("x")` followed by `resolved.resolve()` emitted a CALLS edge to every
        `resolve` method in the project.

        `depth` bounds the descent: a RecursionError here would not surface --
        this runs inside the broad `except Exception` in `type_inference.py`,
        which would discard the whole function's type map and revert every
        receiver in it to bare-name matching, reintroducing #1868 from an
        unrelated statement elsewhere in the same function.
        """
        if depth > _MAX_ALIAS_DEPTH:
            return ()
        if node.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE, cs.TS_PY_CALL):
            return (node,)
        if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
            picks = self._get_boolean_value_operands(node)
        elif node.type == cs.TS_PY_CONDITIONAL_EXPRESSION:
            # `a if cond else b`: the VALUE operands are the first and last
            # named children; the middle one is the condition, whose type is
            # irrelevant. None of the three carries a field name.
            kids = node.named_children
            picks = (kids[0], kids[-1]) if kids else ()
        elif node.type == cs.TS_PY_BINARY_OPERATOR:
            # Nested under another expression (`(a / b) or c`): the left
            # operand is the value's origin and the operator is not tracked
            # this deep -- an approximation that is right for path idioms.
            left = node.child_by_field_name(cs.TS_FIELD_LEFT)
            picks = (left,) if left is not None else ()
        elif node.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
            picks = tuple(node.named_children[:1])
        else:
            return ()
        return tuple(
            leaf for pick in picks for leaf in self._get_value_leaves(pick, depth + 1)
        )

    def _get_boolean_value_operands(self, node: ASTNode) -> tuple[ASTNode, ...]:
        """The operands `a or b` / `a and b` can evaluate to.

        `or` yields whichever operand is truthy first, so both are candidates,
        left preferred. `and` yields the RIGHT operand whenever the left is
        truthy, and a falsy left otherwise -- never something a method is then
        called on -- so only the right operand is a receiver candidate. Taking
        the left would type `flag and Engine()` as bool and drop the call.
        """
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        is_and = operator is not None and safe_decode_text(operator) == cs.PY_OP_AND
        fields = (
            (cs.TS_FIELD_RIGHT,) if is_and else (cs.TS_FIELD_LEFT, cs.TS_FIELD_RIGHT)
        )
        return tuple(
            operand
            for field in fields
            if (operand := node.child_by_field_name(field)) is not None
        )

    def _collect_local_aliases(self, caller_node: ASTNode) -> dict[str, _Alias]:
        # Record what each local variable was assigned (resolver = self._resolver,
        # resolved = target or Path("x")), so its type propagates from what it
        # can evaluate to. Skip nested scopes.
        aliases: dict[str, _Alias] = {}
        boundary = (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
        stack: list[ASTNode] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary:
                continue
            if node.type == cs.TS_PY_ASSIGNMENT:
                left = node.child_by_field_name(cs.TS_FIELD_LEFT)
                right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
                if (
                    left is not None
                    and left.type == cs.TS_PY_IDENTIFIER
                    and right is not None
                    and (local := safe_decode_text(left))
                    and local not in aliases
                    and (alias := self._get_assignment_alias(right)) is not None
                ):
                    aliases[local] = alias
            stack.extend(node.children)
        return aliases

    def _class_qn_of_type(self, type_name: str, module_qn: str) -> str | None:
        if cs.SEPARATOR_DOT in type_name:
            return type_name
        return resolve_class_name(
            type_name, module_qn, self.import_processor, self.function_registry
        )

    def _class_member_types_by_qn(self, class_qn: str) -> dict[str, str]:
        if class_qn in self._class_member_type_cache:
            return self._class_member_type_cache[class_qn]
        members: dict[str, str] = {}
        class_node = self._find_class_node(class_qn)
        if class_node is not None:
            class_module_qn = class_qn.rpartition(cs.SEPARATOR_DOT)[0]
            raw: dict[str, str] = {}
            self._collect_property_return_types(class_node, raw)
            self._collect_class_annotation_types(class_node, raw)
            if (init_node := self._find_init_method_node(class_node)) is not None:
                init_types: dict[str, str] = {}
                self._infer_parameter_types(init_node, init_types, class_module_qn)
                self._analyze_self_assignments(init_node, init_types, class_module_qn)
                for attr, attr_type in init_types.items():
                    raw.setdefault(attr, attr_type)
            for attr, attr_type in raw.items():
                if not attr.startswith(cs.PY_SELF_PREFIX):
                    continue
                member = attr[len(cs.PY_SELF_PREFIX) :]
                resolved = self._class_qn_of_type(attr_type, class_module_qn)
                members[member] = resolved or attr_type
        self._class_member_type_cache[class_qn] = members
        return members

    def _infer_variable_element_type(
        self, var_name: str, local_var_types: dict[str, str], module_qn: str
    ) -> str | None:
        if (
            var_name in local_var_types
            and (var_type := local_var_types[var_name])
            and var_type != cs.TYPE_INFERENCE_LIST
        ):
            # A container-marked variable (`widgets = load_widgets()` with a
            # `-> list[Widget]` annotation) iterates as its element type.
            return _container_element_type(var_type) or var_type
        return self._infer_method_return_element_type(var_name, module_qn)

    def _infer_method_return_element_type(
        self, var_name: str, module_qn: str
    ) -> str | None:
        if cs.PY_VAR_PATTERN_ALL in var_name or var_name.endswith(
            cs.PY_VAR_SUFFIX_PLURAL
        ):
            return self._analyze_repository_item_type(module_qn)
        return None

    def _analyze_repository_item_type(self, module_qn: str) -> str | None:
        repo_qn_patterns = [
            f"{module_qn.split(cs.SEPARATOR_DOT, maxsplit=1)[0]}{cs.PY_MODELS_BASE_PATH}{cs.PY_CLASS_REPOSITORY}",
            cs.PY_CLASS_REPOSITORY,
        ]

        for repo_qn in repo_qn_patterns:
            create_method = f"{repo_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_CREATE}"
            if create_method in self.function_registry:
                return cs.TYPE_INFERENCE_BASE_MODEL

        return None

    def _extract_variable_name(self, node: ASTNode) -> str | None:
        if node.type != cs.TS_PY_IDENTIFIER or node.text is None:
            return None
        return safe_decode_text(node) or None
