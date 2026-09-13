from __future__ import annotations

import re
from abc import abstractmethod
from collections.abc import Iterator
from typing import TYPE_CHECKING, Protocol

from loguru import logger
from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ... import logs as lg
from ...types_defs import FunctionRegistryTrieProtocol, LanguageQueries, NodeType
from ..js_ts.utils import find_method_in_ast as find_js_method_in_ast
from ..utils import get_cached_query, safe_decode_text, sorted_captures

_PY_SCOPE_TYPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_MODULE}
)

# Statements that rebind a name WITHOUT an `assignment` node: `p += x`,
# `for p in xs`, `with cm as p`. Each ends the provenance an earlier
# `p = call()` gave the name (local review P2).
_PY_REBINDING_TYPES = frozenset(
    {cs.TS_PY_AUGMENTED_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT, cs.TS_PY_WITH_STATEMENT}
)

_PY_TRAVERSE_QUERY = (
    f"({cs.TS_PY_ASSIGNMENT}) @assignment "
    f"({cs.TS_PY_LIST_COMPREHENSION}) @comprehension "
    f"({cs.TS_PY_FOR_STATEMENT}) @for_stmt "
    f"({cs.TS_PY_AUGMENTED_ASSIGNMENT}) @augmented "
    f"({cs.TS_PY_WITH_STATEMENT}) @with_stmt "
    f"({cs.TS_PY_RETURN_STATEMENT}) @return_stmt"
)


def _split_top_level(inner: str, separator: str = cs.CHAR_COMMA) -> list[str]:
    """Split `A, dict[str, B], C` on the separators outside any brackets --
    the commas between positions, or the `|` between union members, which a
    plain `str.split` would also find INSIDE `tuple[Widget | None, Banner]`."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in inner:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _identifiers_in(target: Node) -> Iterator[Node]:
    """The identifiers a binding target names: itself, or every one inside a
    `a, (b, c)` pattern."""
    if target.type == cs.TS_PY_IDENTIFIER:
        yield target
        return
    for child in target.named_children:
        yield from _identifiers_in(child)


def _rebound_identifiers(node: Node) -> Iterator[Node]:
    """Identifiers a for / with / augmented assignment binds, without
    descending into the statement's body."""
    if node.type == cs.TS_PY_WITH_STATEMENT:
        for clause in node.named_children:
            for item in clause.named_children:
                value = item.child_by_field_name(cs.FIELD_VALUE)
                if value is not None and value.type == cs.TS_PY_AS_PATTERN:
                    alias = value.child_by_field_name(cs.FIELD_ALIAS)
                    if alias is not None:
                        yield from _identifiers_in(alias)
        return
    left = node.child_by_field_name(cs.TS_FIELD_LEFT)
    if left is not None:
        yield from _identifiers_in(left)


def _scope_of(node: Node) -> int | None:
    """Id of the def, class or module whose body a node sits in directly."""
    current = node.parent
    while current is not None and current.type not in _PY_SCOPE_TYPES:
        current = current.parent
    return current.id if current is not None else None


def _homogeneous_element(name: str, inner: str) -> str | None:
    """The single element type a container annotation guarantees, else ``None``.

    ``tuple[Widget, Banner]`` guarantees nothing about a given element, so
    only ``tuple[Widget]`` and the homogeneous ``tuple[Widget, ...]`` pass;
    generators yield their first argument; every other container takes
    exactly one. A nested-generic first argument survives to the caller's
    trust check, which rejects it.
    """
    parts = [part.strip() for part in inner.split(cs.CHAR_COMMA)]
    if name in cs.PY_TUPLE_CONTAINERS:
        if len(parts) == 1 or (len(parts) == 2 and parts[1] == cs.PY_ELLIPSIS):
            return parts[0]
        return None
    if limit := cs.PY_GENERATOR_ARG_LIMITS.get(name):
        return parts[0] if len(parts) <= limit else None
    return parts[0] if len(parts) == 1 else None


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from ..factory import ASTCacheProtocol
    from ..import_processor import ImportProcessor
    from ..js_ts.type_inference import JsTypeInferenceEngine

    class _AstAnalyzerDeps(Protocol):
        import_processor: ImportProcessor

        def build_local_variable_type_map(
            self, caller_node: Node, module_qn: str
        ) -> dict[str, str]: ...

        def _extract_full_method_call(self, node: Node) -> str | None: ...

        def _resolve_method_qualified_name(
            self,
            method_call: str,
            module_qn: str,
            local_var_types: dict[str, str] | None = None,
        ) -> str | None: ...

        def _analyze_comprehension(
            self, node: Node, local_var_types: dict[str, str], module_qn: str
        ) -> None: ...

        def _analyze_for_loop(
            self, node: Node, local_var_types: dict[str, str], module_qn: str
        ) -> None: ...

        def _infer_instance_variable_types_from_assignments(
            self,
            assignments: list[Node],
            local_var_types: dict[str, str],
            module_qn: str,
        ) -> None: ...

    _AstBase: type = _AstAnalyzerDeps
else:
    _AstBase = object


class PythonAstAnalyzerMixin(_AstBase):
    __slots__ = ()
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    module_qn_to_file_path: dict[str, Path]
    ast_cache: ASTCacheProtocol
    function_registry: FunctionRegistryTrieProtocol

    _js_type_inference_getter: Callable[[], JsTypeInferenceEngine]

    @abstractmethod
    def _infer_type_from_expression(self, node: Node, module_qn: str) -> str | None: ...

    @abstractmethod
    def _infer_type_from_expression_simple(
        self, node: Node, module_qn: str
    ) -> str | None: ...

    @abstractmethod
    def _infer_type_from_expression_complex(
        self, node: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> str | None: ...

    @abstractmethod
    def _infer_method_call_return_type(
        self, method_qn: str, module_qn: str, local_var_types: dict[str, str] | None
    ) -> str | None: ...

    @abstractmethod
    def _find_class_in_scope(self, class_name: str, module_qn: str) -> str | None: ...

    @abstractmethod
    def _infer_free_function_return_type(
        self, callee: str, module_qn: str
    ) -> str | None: ...

    _return_stmt_cache: dict[Node, list[Node]]

    def _traverse_single_pass(
        self, node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> tuple[list[Node], list[Node]]:
        """Types locals in one traversal; returns (comprehensions, for
        statements) so the coordinator can re-run loop inference after the
        attribute passes populate ``self.x`` types."""
        assignments: list[Node] = []
        comprehensions: list[Node] = []
        for_statements: list[Node] = []
        rebinds: list[Node] = []

        py_lang_queries = self.queries.get(cs.SupportedLanguage.PYTHON)
        py_lang_obj = py_lang_queries["language"] if py_lang_queries else None
        if py_lang_obj is not None:
            try:
                q = get_cached_query(py_lang_obj, _PY_TRAVERSE_QUERY)
                cursor = QueryCursor(q)
                captures = cursor.captures(node)
                assignments = captures.get("assignment", [])
                comprehensions = captures.get("comprehension", [])
                for_statements = captures.get("for_stmt", [])
                rebinds = [
                    *for_statements,
                    *captures.get("augmented", []),
                    *captures.get("with_stmt", []),
                ]
                if return_stmts := captures.get("return_stmt"):
                    self._return_stmt_cache[node] = return_stmts
            except Exception:
                py_lang_obj = None

        if py_lang_obj is None:
            stack: list[Node] = [node]
            while stack:
                current = stack.pop()
                node_type = current.type

                if node_type == cs.TS_PY_ASSIGNMENT:
                    assignments.append(current)
                elif node_type == cs.TS_PY_LIST_COMPREHENSION:
                    comprehensions.append(current)
                elif node_type == cs.TS_PY_FOR_STATEMENT:
                    for_statements.append(current)
                if node_type in _PY_REBINDING_TYPES:
                    rebinds.append(current)

                stack.extend(reversed(current.children))

        for assignment in assignments:
            self._process_assignment_simple(assignment, local_var_types, module_qn)

        for assignment in assignments:
            self._process_assignment_complex(assignment, local_var_types, module_qn)
        self._process_assignment_unpacking(
            assignments, rebinds, local_var_types, module_qn
        )

        for comp in comprehensions:
            self._analyze_comprehension(comp, local_var_types, module_qn)

        for for_stmt in for_statements:
            self._analyze_for_loop(for_stmt, local_var_types, module_qn)

        self._infer_instance_variable_types_from_assignments(
            assignments, local_var_types, module_qn
        )
        return comprehensions, for_statements

    def _process_assignment_simple(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment_node.child_by_field_name(cs.TS_FIELD_RIGHT)

        if not left_node or not right_node:
            return

        var_name = self._extract_assignment_variable_name(left_node)
        if not var_name:
            return

        if inferred_type := self._infer_type_from_expression_simple(
            right_node, module_qn
        ):
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_SIMPLE, var=var_name, type=inferred_type)

    def _process_assignment_complex(
        self, assignment_node: Node, local_var_types: dict[str, str], module_qn: str
    ) -> None:
        left_node = assignment_node.child_by_field_name(cs.TS_FIELD_LEFT)
        right_node = assignment_node.child_by_field_name(cs.TS_FIELD_RIGHT)

        if not left_node or not right_node:
            return

        var_name = self._extract_assignment_variable_name(left_node)
        if not var_name:
            return

        if var_name in local_var_types:
            return

        if inferred_type := self._infer_type_from_expression_complex(
            right_node, module_qn, local_var_types
        ):
            local_var_types[var_name] = inferred_type
            logger.debug(lg.PY_TYPE_COMPLEX, var=var_name, type=inferred_type)

    def _process_assignment_unpacking(
        self,
        assignments: list[Node],
        rebinds: list[Node],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        """`_ol, _oc, inner = parsed`: bind each target to its element type.

        The single-name processors above ignore a `pattern_list` /
        `tuple_pattern` target, so an unpacked local had no type and a call on
        it fell to the bare-name fallback (issue #1896, the real shape of
        trace/sourcemap.py:225). The right-hand side is a call annotated
        `tuple[A, B, C]`, or a local that was assigned such a call earlier in
        the same body; the annotation is read RAW here because
        `_annotated_return_type` deliberately refuses a heterogeneous tuple,
        which is exactly what unpacking consumes position by position.
        """
        for assignment in assignments:
            left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
            right = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
            if left is None or right is None:
                continue
            if left.type not in cs.PY_UNPACKING_TARGET_TYPES:
                continue
            call = self._defining_call(right, assignments, rebinds)
            if call is None:
                continue
            elements = self._tuple_return_elements(call, module_qn, local_var_types)
            if not elements:
                continue
            targets = [t for t in left.named_children if t.type != cs.TS_COMMENT]
            if len(elements) == 1 and elements[0][1]:
                elements = [elements[0]] * len(targets)  # `tuple[T, ...]`
            if len(elements) != len(targets):
                continue
            for target, (element, _homogeneous) in zip(targets, elements, strict=True):
                if target.type != cs.TS_PY_IDENTIFIER:
                    continue  # a nested pattern or a starred target binds no one type
                name = safe_decode_text(target)
                if name and name not in local_var_types:
                    local_var_types[name] = element

    def _defining_call(
        self, right: Node, assignments: list[Node], rebinds: list[Node]
    ) -> Node | None:
        """The call an unpacking's right-hand side comes from, or None.

        Either the call itself, or -- for `parsed = f(); a, b = parsed` -- the
        call that the nearest earlier binding of the name IN THE SAME SCOPE
        gave it. A nearer binding to anything but a call clears it: after
        `p = fw(); p = supplied` (or `p += x`, `for p in xs`, `with cm as p`),
        `p` is not fw's result. The walk captures every statement under the
        function, including a nested def's, whose `p` is a different variable.
        """
        if right.type == cs.TS_PY_CALL:
            return right
        if right.type != cs.TS_PY_IDENTIFIER:
            return None
        name = safe_decode_text(right)
        scope = _scope_of(right)
        # (position, the call bound, or None for any other binding), for every
        # same-name same-scope binding before the use. The NEAREST one decides:
        # `p = fw(); p = fb(); _n, w = p` unpacks fb's result. Captures are not
        # guaranteed to come back in document order, so take the max position.
        events: list[tuple[int, Node | None]] = []
        for earlier in assignments:
            target = earlier.child_by_field_name(cs.TS_FIELD_LEFT)
            value = earlier.child_by_field_name(cs.TS_FIELD_RIGHT)
            if (
                earlier.end_byte <= right.start_byte
                and target is not None
                and value is not None
                and target.type == cs.TS_PY_IDENTIFIER
                and safe_decode_text(target) == name
                and _scope_of(earlier) == scope
            ):
                events.append(
                    (earlier.start_byte, value if value.type == cs.TS_PY_CALL else None)
                )
        for statement in rebinds:
            for target in _rebound_identifiers(statement):
                if (
                    target.end_byte <= right.start_byte
                    and safe_decode_text(target) == name
                    and _scope_of(target) == scope
                ):
                    events.append((target.start_byte, None))
        if not events:
            return None
        return max(events, key=lambda event: event[0])[1]

    def _tuple_return_elements(
        self, call: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> list[tuple[str, bool]]:
        """(element type, is `...`-homogeneous) per position of the callee's
        `tuple[...]` return annotation, Optional stripped; empty if the callee
        cannot be found or does not return a tuple."""
        callee = self._callee_definition(call, module_qn, local_var_types)
        if callee is None:
            return []
        callee_node, callee_qn = callee
        type_node = callee_node.child_by_field_name(cs.FIELD_RETURN_TYPE)
        if type_node is None:
            return []
        text = (safe_decode_text(type_node) or "").strip().strip("\"'")
        members = [
            member
            for part in _split_top_level(text, cs.PY_UNION_SEPARATOR)
            if (member := part.strip()) and member != cs.PY_NONE
        ]
        if len(members) != 1:
            return []
        candidate = members[0]
        if optional := re.match(cs.PY_OPTIONAL_PATTERN, candidate):
            candidate = optional.group("inner").strip()
        container = re.match(cs.PY_GENERIC_CONTAINER_PATTERN, candidate)
        if container is None or container.group("name") not in cs.PY_TUPLE_CONTAINERS:
            return []
        parts = _split_top_level(container.group("inner"))
        if len(parts) == 2 and parts[1] == cs.PY_ELLIPSIS:
            return [(self._element_type(parts[0], callee_qn, module_qn), True)]
        return [
            (self._element_type(part, callee_qn, module_qn), False) for part in parts
        ]

    def _element_type(self, element: str, callee_qn: str, module_qn: str) -> str:
        # A name the scope resolves to a project class becomes that class;
        # anything else (`int`, `list[str]`) keeps its annotation text, which
        # is what a typed parameter stores too.
        element = element.strip().strip("\"'")
        return self._trusted_annotation_name(element, callee_qn, module_qn) or element

    def _callee_definition(
        self, call: Node, module_qn: str, local_var_types: dict[str, str]
    ) -> tuple[Node, str] | None:
        """(definition node, qualified name) of what a call invokes, or None."""
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        if func.type == cs.TS_PY_IDENTIFIER and (name := safe_decode_text(func)):
            import_map = self.import_processor.import_mapping.get(module_qn, {})
            for qn in (import_map.get(name), f"{module_qn}{cs.SEPARATOR_DOT}{name}"):
                if qn and (node := self._find_function_ast_node(qn)) is not None:
                    return node, qn
            return None
        if func.type == cs.TS_PY_ATTRIBUTE and (
            text := self._extract_full_method_call(func)
        ):
            # `helpers.make_pair()`: the receiver is an imported MODULE, which
            # the method resolver (built for typed receivers) cannot name; the
            # import map can, and the qn it gives is a function's.
            receiver, _, leaf = text.rpartition(cs.SEPARATOR_DOT)
            import_map = self.import_processor.import_mapping.get(module_qn, {})
            if (
                cs.SEPARATOR_DOT not in receiver
                and receiver not in local_var_types
                and (module := import_map.get(receiver))
                and (node := self._find_function_ast_node(f"{module}.{leaf}"))
                is not None
            ):
                return node, f"{module}.{leaf}"
            qn = self._resolve_method_qualified_name(text, module_qn, local_var_types)
            if qn and (node := self._find_method_ast_node(qn)) is not None:
                return node, qn
        return None

    def _extract_assignment_variable_name(self, node: Node) -> str | None:
        if node.type != cs.TS_PY_IDENTIFIER or node.text is None:
            return None
        return safe_decode_text(node) or None

    def _find_method_ast_node(self, method_qn: str) -> Node | None:
        qn_parts = method_qn.split(cs.SEPARATOR_DOT)
        if len(qn_parts) < 3:
            return None

        class_name = qn_parts[-2]
        method_name = qn_parts[-1]

        expected_module = cs.SEPARATOR_DOT.join(qn_parts[:-2])
        file_path = self.module_qn_to_file_path.get(expected_module)
        if not file_path or not (entry := self.ast_cache.load(file_path)):
            return None

        root_node, language = entry
        return self._find_method_in_ast(root_node, class_name, method_name, language)

    def _find_method_in_ast(
        self,
        root_node: Node,
        class_name: str,
        method_name: str,
        language: cs.SupportedLanguage,
    ) -> Node | None:
        match language:
            case cs.SupportedLanguage.PYTHON:
                return self._find_python_method_in_ast(
                    root_node, class_name, method_name
                )
            case (
                cs.SupportedLanguage.JS
                | cs.SupportedLanguage.TS
                | cs.SupportedLanguage.TSX
            ):
                return find_js_method_in_ast(root_node, class_name, method_name)
            case _:
                return None

    def _find_class_node(self, class_qn: str) -> Node | None:
        # Locate a class definition node from its qualified name so cross-class
        # attribute/property types resolve when handling chained calls.
        module_qn, _, class_name = class_qn.rpartition(cs.SEPARATOR_DOT)
        if not module_qn:
            return None
        file_path = self.module_qn_to_file_path.get(module_qn)
        if not file_path or not (entry := self.ast_cache.load(file_path)):
            return None
        root_node, language = entry
        if language != cs.SupportedLanguage.PYTHON:
            return None
        lang_queries = self.queries[cs.SupportedLanguage.PYTHON]
        class_query = lang_queries[cs.QUERY_KEY_CLASSES]
        if not class_query:
            return None
        cursor = QueryCursor(class_query)
        captures = sorted_captures(cursor, root_node)
        for class_node in captures.get(cs.QUERY_CAPTURE_CLASS, []):
            if not isinstance(class_node, Node):
                continue
            name_node = class_node.child_by_field_name(cs.TS_FIELD_NAME)
            if name_node and safe_decode_text(name_node) == class_name:
                return class_node
        return None

    def _find_callable_ast_node(self, qn: str) -> Node | None:
        # Dispatch on the registry label: a FUNCTION qn is module.func (module is
        # everything before the LAST dot); anything else keeps the method-shaped
        # module.Class.method lookup.
        if self.function_registry.get(qn) == NodeType.FUNCTION:
            return self._find_function_ast_node(qn)
        return self._find_method_ast_node(qn)

    def _find_function_ast_node(self, fn_qn: str) -> Node | None:
        # Top-level function definition for `mod.func`. Nested and method defs
        # carry longer qns, so anything under a class or function ancestor is
        # skipped.
        module_qn, _, fn_name = fn_qn.rpartition(cs.SEPARATOR_DOT)
        file_path = self.module_qn_to_file_path.get(module_qn)
        if not file_path or not (entry := self.ast_cache.load(file_path)):
            return None
        root_node, language = entry
        if language != cs.SupportedLanguage.PYTHON:
            return None
        fn_query = self.queries[cs.SupportedLanguage.PYTHON][cs.QUERY_KEY_FUNCTIONS]
        if not fn_query:
            return None
        cursor = QueryCursor(fn_query)
        captures = sorted_captures(cursor, root_node)
        for fn_node in captures.get(cs.QUERY_CAPTURE_FUNCTION, []):
            if not isinstance(fn_node, Node):
                continue
            name_node = fn_node.child_by_field_name(cs.TS_FIELD_NAME)
            if (
                name_node is not None
                and safe_decode_text(name_node) == fn_name
                and self._is_top_level_definition(fn_node)
            ):
                return fn_node
        return None

    def _is_top_level_definition(self, node: Node) -> bool:
        parent = node.parent
        while parent is not None:
            if parent.type in (
                cs.TS_PY_CLASS_DEFINITION,
                cs.TS_PY_FUNCTION_DEFINITION,
            ):
                return False
            parent = parent.parent
        return True

    def _find_python_method_in_ast(
        self, root_node: Node, class_name: str, method_name: str
    ) -> Node | None:
        lang_queries = self.queries[cs.SupportedLanguage.PYTHON]
        class_query = lang_queries[cs.QUERY_KEY_CLASSES]
        if not class_query:
            return None
        cursor = QueryCursor(class_query)
        captures = sorted_captures(cursor, root_node)

        method_query = lang_queries[cs.QUERY_KEY_FUNCTIONS]
        if not method_query:
            return None

        for class_node in captures.get(cs.QUERY_CAPTURE_CLASS, []):
            if not isinstance(class_node, Node):
                continue

            name_node = class_node.child_by_field_name(cs.TS_FIELD_NAME)
            if not name_node or name_node.text is None:
                continue

            if safe_decode_text(name_node) != class_name:
                continue

            body_node = class_node.child_by_field_name(cs.TS_FIELD_BODY)
            if not body_node:
                continue

            method_cursor = QueryCursor(method_query)
            method_captures = sorted_captures(method_cursor, body_node)

            for method_node in method_captures.get(cs.QUERY_CAPTURE_FUNCTION, []):
                if not isinstance(method_node, Node):
                    continue

                method_name_node = method_node.child_by_field_name(cs.TS_FIELD_NAME)
                if not method_name_node or method_name_node.text is None:
                    continue

                if safe_decode_text(method_name_node) == method_name:
                    return method_node

        return None

    def _analyze_method_return_statements(
        self, method_node: Node, method_qn: str, module_qn: str | None = None
    ) -> str | None:
        # module_qn defaults to the method-shaped derivation (mod.Class.meth -> mod);
        # free-function callers pass their own (mod.func -> mod), which the [-2]
        # split cannot express.
        if module_qn is None:
            module_qn = cs.SEPARATOR_DOT.join(method_qn.split(cs.SEPARATOR_DOT)[:-2])
        if annotated := self._annotated_return_type(method_node, method_qn, module_qn):
            return annotated

        return_nodes: list[Node] = []
        self._find_return_statements(method_node, return_nodes)

        for return_node in return_nodes:
            return_value = next(
                (
                    child
                    for child in return_node.children
                    if child.type not in (cs.TS_PY_RETURN, cs.TS_PY_KEYWORD)
                ),
                None,
            )
            if return_value and (
                inferred_type := self._analyze_return_expression(
                    return_value, method_qn, module_qn
                )
            ):
                return inferred_type

        return None

    def _annotated_return_type(
        self, method_node: Node, method_qn: str, module_qn: str
    ) -> str | None:
        # A declared `-> Widget` (or `-> Widget | None`, the quoted forward-ref
        # form, `-> Self`, or a homogeneous container like `-> list[Widget]`)
        # is the cheapest, most reliable return-type source; anything else
        # falls through to body inference. A container annotation produces the
        # canonical `list[<element>]` marker so loop-variable inference can
        # recover the element (issue #1304).
        type_node = method_node.child_by_field_name(cs.FIELD_RETURN_TYPE)
        if type_node is None or type_node.text is None:
            return None
        text = (safe_decode_text(type_node) or "").strip().strip("\"'")
        non_none = [
            member
            for part in text.split(cs.PY_UNION_SEPARATOR)
            if (member := part.strip()) and member != cs.PY_NONE
        ]
        if len(non_none) != 1:
            return None
        candidate = non_none[0]
        if optional := re.match(cs.PY_OPTIONAL_PATTERN, candidate):
            candidate = optional.group("inner").strip()
        if container := re.match(cs.PY_GENERIC_CONTAINER_PATTERN, candidate):
            element_text = _homogeneous_element(
                container.group("name"), container.group("inner")
            )
            if element_text is None:
                return None
            element = self._trusted_annotation_name(
                element_text.strip("\"'"), method_qn, module_qn
            )
            return cs.PY_LIST_TYPE_FORMAT.format(element=element) if element else None
        return self._trusted_annotation_name(candidate, method_qn, module_qn)

    def _trusted_annotation_name(
        self, candidate: str, method_qn: str, module_qn: str
    ) -> str | None:
        """A simple or dotted annotation name resolved in scope, else ``None``."""
        if not all(part.isidentifier() for part in candidate.split(cs.SEPARATOR_DOT)):
            return None
        if candidate == cs.PY_ANNOTATION_SELF:
            # `-> Self` names the enclosing class (the method qn minus its leaf,
            # kept fully qualified so cross-module receivers resolve); a free
            # function has none.
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            if len(qn_parts) < 3:
                return None
            return cs.SEPARATOR_DOT.join(qn_parts[:-1])
        if cs.SEPARATOR_DOT in candidate:
            return candidate
        return self._find_class_in_scope(candidate, module_qn) or candidate

    def _find_return_statements(self, node: Node, return_nodes: list[Node]) -> None:
        cached = self._return_stmt_cache.get(node)
        if cached is not None:
            return_nodes.extend(cached)
            return
        py_lang_queries = self.queries.get(cs.SupportedLanguage.PYTHON)
        py_lang_obj = py_lang_queries["language"] if py_lang_queries else None
        if py_lang_obj is not None:
            try:
                q = get_cached_query(py_lang_obj, cs.PY_RETURN_QUERY)
                cursor = QueryCursor(q)
                captures = cursor.captures(node)
                return_nodes.extend(captures.get("return_stmt", []))
                return
            except Exception:
                pass
        stack: list[Node] = [node]
        while stack:
            current = stack.pop()
            if current.type == cs.TS_PY_RETURN_STATEMENT:
                return_nodes.append(current)
            stack.extend(reversed(current.children))

    def _analyze_return_expression(
        self, expr_node: Node, method_qn: str, module_qn: str
    ) -> str | None:
        match expr_node.type:
            case cs.TS_PY_CALL:
                return self._analyze_call_return(expr_node, method_qn, module_qn)
            case cs.TS_PY_IDENTIFIER:
                return self._analyze_identifier_return(expr_node, method_qn, module_qn)
            case cs.TS_PY_ATTRIBUTE:
                return self._analyze_attribute_return(expr_node, method_qn)
            case _:
                return None

    def _analyze_call_return(
        self, expr_node: Node, method_qn: str, module_qn: str
    ) -> str | None:
        func_node = expr_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if not func_node:
            return None

        if (
            func_node.type == cs.TS_PY_IDENTIFIER
            and func_node.text is not None
            and (class_name := safe_decode_text(func_node))
        ):
            if resolved := self._resolve_call_class_name(
                class_name, method_qn, module_qn
            ):
                return resolved
            # A lowercase callee is a factory-calls-factory hop (django's
            # get_resolver delegating to _get_cached_resolver): follow the
            # inner factory's own return type transitively.
            return self._infer_free_function_return_type(class_name, module_qn)

        if func_node.type == cs.TS_PY_ATTRIBUTE:
            if method_call_text := self._extract_method_call_from_attr(func_node):
                return self._infer_method_call_return_type(
                    method_call_text, module_qn, None
                )

        return None

    def _resolve_call_class_name(
        self, class_name: str, method_qn: str, module_qn: str
    ) -> str | None:
        if class_name == cs.PY_KEYWORD_CLS:
            # `return cls(...)` names the enclosing class, fully qualified so
            # cross-module receivers resolve without a scope lookup.
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            return cs.SEPARATOR_DOT.join(qn_parts[:-1]) if len(qn_parts) >= 3 else None

        if class_name[0].isupper():
            resolved_class = self._find_class_in_scope(class_name, module_qn)
            return resolved_class or class_name

        return None

    def _analyze_identifier_return(
        self, expr_node: Node, method_qn: str, module_qn: str
    ) -> str | None:
        if expr_node.text is None:
            return None

        identifier = safe_decode_text(expr_node)
        if not identifier:
            return None

        if identifier in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            return qn_parts[-2] if len(qn_parts) >= 2 else None

        if method_node := self._find_callable_ast_node(method_qn):
            local_vars = self.build_local_variable_type_map(method_node, module_qn)
            if identifier in local_vars:
                logger.debug(
                    lg.PY_VAR_FROM_CONTEXT, var=identifier, type=local_vars[identifier]
                )
                return local_vars[identifier]

        logger.debug(lg.PY_VAR_CANNOT_INFER, var=identifier)
        return None

    def _analyze_attribute_return(self, expr_node: Node, method_qn: str) -> str | None:
        object_node = expr_node.child_by_field_name(cs.TS_FIELD_OBJECT)
        if (
            object_node
            and object_node.type == cs.TS_PY_IDENTIFIER
            and object_node.text is not None
            and (object_name := safe_decode_text(object_node))
            and object_name in (cs.PY_KEYWORD_CLS, cs.PY_KEYWORD_SELF)
        ):
            qn_parts = method_qn.split(cs.SEPARATOR_DOT)
            return qn_parts[-2] if len(qn_parts) >= 2 else None

        return None

    def _extract_method_call_from_attr(self, attr_node: Node) -> str | None:
        return safe_decode_text(attr_node) or None if attr_node.text else None
