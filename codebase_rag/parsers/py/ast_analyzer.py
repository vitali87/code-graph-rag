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

_PY_TRAVERSE_QUERY = (
    f"({cs.TS_PY_ASSIGNMENT}) @assignment "
    f"({cs.TS_PY_LIST_COMPREHENSION}) @comprehension "
    f"({cs.TS_PY_FOR_STATEMENT}) @for_stmt "
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


# Target shapes that bind plain names, and so are descended into for them:
# `a, (b, c)`, `[a, b]`, `a, *rest`, and a with statement's `as` target,
# which tree-sitter parses as a `tuple` / `list` EXPRESSION, not a pattern.
# An attribute or subscript target (`self.x = ...`, `p[0] = ...`) binds no
# local.
_PY_BINDING_PATTERN_TYPES = cs.PY_UNPACKING_TARGET_TYPES | {
    cs.TS_PY_LIST_SPLAT_PATTERN,
    cs.TS_PY_AS_PATTERN_TARGET,
    cs.TS_PY_TUPLE,
    cs.TS_PY_LIST,
}
# Statements whose `left` field is the target they bind.
_PY_LEFT_BINDERS = frozenset(
    {cs.TS_PY_ASSIGNMENT, cs.TS_PY_AUGMENTED_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT}
)
# Nodes whose `name` field is the one identifier they bind: a walrus, and a
# def or class, whose NAME is the enclosing body's while its body is not.
_PY_NAME_BINDERS = frozenset(
    {cs.TS_PY_NAMED_EXPRESSION, cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION}
)
_PY_IMPORT_TYPES = frozenset(
    {cs.TS_PY_IMPORT_STATEMENT, cs.TS_PY_IMPORT_FROM_STATEMENT}
)
# A body of its own: what is bound inside it is not the enclosing body's.
_PY_NESTED_SCOPE_TYPES = frozenset(
    {cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION, cs.TS_PY_LAMBDA}
)


def _identifiers_in(target: Node) -> Iterator[Node]:
    """The local names a binding target binds: itself, or every one inside a
    pattern; nothing for an attribute or subscript target."""
    if target.type == cs.TS_PY_IDENTIFIER:
        yield target
    elif target.type in _PY_BINDING_PATTERN_TYPES:
        for child in target.named_children:
            yield from _identifiers_in(child)


def _with_aliases(statement: Node) -> Iterator[Node]:
    """The `as` targets of a with statement's items."""
    for clause in statement.named_children:
        for item in clause.named_children:
            value = item.child_by_field_name(cs.FIELD_VALUE)
            if value is None or value.type != cs.TS_PY_AS_PATTERN:
                continue
            alias = value.child_by_field_name(cs.FIELD_ALIAS)
            if alias is not None:
                yield alias


def _except_aliases(clause: Node) -> Iterator[Node]:
    """The `as` target of an except clause."""
    for child in clause.named_children:
        if child.type == cs.TS_PY_AS_PATTERN:
            alias = child.child_by_field_name(cs.FIELD_ALIAS)
            if alias is not None:
                yield alias


def _import_targets(statement: Node) -> Iterator[Node]:
    """The identifiers an import binds: `a` for `import a.b`, `y` for
    `from m import y`, the alias for `... as z`; never a `from` module."""
    module = statement.child_by_field_name(cs.FIELD_MODULE_NAME)
    for child in statement.named_children:
        if module is not None and child.id == module.id:
            continue
        if child.type == cs.TS_ALIASED_IMPORT:
            alias = child.child_by_field_name(cs.FIELD_ALIAS)
            if alias is not None:
                yield alias
        elif child.type == cs.TS_DOTTED_NAME and child.named_children:
            yield child.named_children[0]


def _capture_targets(pattern: Node) -> Iterator[Node]:
    """The identifier a `case` pattern captures: a bare name, which is a
    one-segment `dotted_name`. `_` captures nothing; `Color.RED` matches a
    value."""
    for child in pattern.named_children:
        if (
            child.type == cs.TS_PY_DOTTED_NAME
            and len(child.named_children) == 1
            and safe_decode_text(child) != cs.TS_PY_WILDCARD_NODE
        ):
            yield child.named_children[0]


def _binding_targets(node: Node) -> Iterator[Node]:
    """The targets ONE statement or expression binds, without its body:
    assignment, augmented assignment, `for`, `with ... as`, walrus,
    `except ... as`, `import`, a `case` capture, a nested def or class.
    Not modelled: `global` / `nonlocal` declarations and `del`."""
    if node.type in _PY_LEFT_BINDERS:
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        if left is not None:
            yield left
    elif node.type in _PY_NAME_BINDERS:
        name = node.child_by_field_name(cs.FIELD_NAME)
        if name is not None:
            yield name
    elif node.type == cs.TS_PY_WITH_STATEMENT:
        yield from _with_aliases(node)
    elif node.type == cs.TS_PY_EXCEPT_CLAUSE:
        yield from _except_aliases(node)
    elif node.type in _PY_IMPORT_TYPES:
        yield from _import_targets(node)
    elif node.type == cs.TS_PY_CASE_PATTERN:
        yield from _capture_targets(node)


def _parameter_names(function: Node) -> Iterator[str]:
    """Every name a def's parameter list binds, annotated or not."""
    parameters = function.child_by_field_name(cs.TS_FIELD_PARAMETERS)
    for parameter in parameters.named_children if parameters is not None else ():
        # `x`, `x: T`, `x=1`, `x: T = 1`, `*args`, `**kw`: the identifier is
        # the node itself or its first named child; a bare `/` or `*` has none.
        candidates = (parameter, *parameter.named_children)
        if (
            first := next(
                (c for c in candidates if c.type == cs.TS_PY_IDENTIFIER), None
            )
        ) is not None and (name := safe_decode_text(first)):
            yield name


def _scope_of(node: Node) -> int | None:
    """Id of the def, class or module whose body a node sits in directly."""
    current = node.parent
    while current is not None and current.type not in _PY_SCOPE_TYPES:
        current = current.parent
    return current.id if current is not None else None


def _bindings_in(scope: Node) -> Iterator[tuple[Node, Node]]:
    """(binding node, identifier bound) for every name the statements
    directly in `scope` bind, without entering a nested def, class or
    lambda."""
    stack = list(scope.children)
    while stack:
        node = stack.pop()
        for target in _binding_targets(node):
            for identifier in _identifiers_in(target):
                yield node, identifier
        if node.type not in _PY_NESTED_SCOPE_TYPES:
            stack.extend(node.children)


def _import_bound_leaf(item: Node, name: str, from_import: bool) -> str | None:
    """The path an import item binds `name` to, or None when it binds another
    name: the target of `x as name`, `name` itself in `from m import name`,
    the package `a` in `import a.b`."""
    if item.type == cs.TS_ALIASED_IMPORT:
        alias = item.child_by_field_name(cs.FIELD_ALIAS)
        target = item.child_by_field_name(cs.FIELD_NAME)
        if alias is None or target is None or safe_decode_text(alias) != name:
            return None
        return safe_decode_text(target) or None
    if item.type != cs.TS_DOTTED_NAME:
        return None
    text = safe_decode_text(item) or ""
    bound = text if from_import else text.split(cs.SEPARATOR_DOT)[0]
    return bound if bound == name else None


def _import_binding_path(statement: Node, name: str) -> str | None:
    """The dotted path a body-level import binds `name` to, leading dots
    dropped: `helpers.make_pair` for `from .helpers import make_pair`,
    `helpers` for `from . import helpers`, `os` for `import os as helpers`."""
    module = statement.child_by_field_name(cs.FIELD_MODULE_NAME)
    prefix = (safe_decode_text(module) or "").lstrip(cs.SEPARATOR_DOT) if module else ""
    for item in statement.named_children:
        if module is not None and item.id == module.id:
            continue
        if leaf := _import_bound_leaf(item, name, module is not None):
            return cs.SEPARATOR_DOT.join(part for part in (prefix, leaf) if part)
    return None


def _reimports(binder: Node, name: str, import_map: dict[str, str]) -> bool:
    """Whether a body-level import binds `name` to what the import map says
    it is: then the name is a re-import the map resolves, not a shadow. The
    map keeps a module-level entry over a body import it could not place
    (`import os as helpers` under `from . import helpers`), so the two must
    agree on the path, or the name means something the project does not
    hold (CodeRabbit)."""
    if binder.type not in _PY_IMPORT_TYPES:
        return False
    path = _import_binding_path(binder, name)
    qn = import_map.get(name)
    return bool(path and qn) and (qn == path or qn.endswith(cs.SEPARATOR_DOT + path))


def _guards_an_optional_import(
    binder: Node, name: str, import_map: dict[str, str]
) -> bool:
    """Whether `binder` is the fallback of an optional-import idiom:

        try:
            from . import helpers
        except ImportError:
            helpers = None

    The handler's binding is real, but on the path where the name resolves
    to anything the graph can reach, the `try` body's import bound it. The
    union over both branches would otherwise put the name back into the
    shadow set and drop a call edge the module genuinely has (issue #1907
    review). Only a re-import the map already reflects counts, so a handler
    guarding an import of something else still shadows.
    """
    node: Node | None = binder
    while node is not None and node.type != cs.TS_PY_FUNCTION_DEFINITION:
        if node.type == cs.TS_PY_EXCEPT_CLAUSE:
            try_statement = node.parent
            if try_statement is None:
                return False
            body = try_statement.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                return False
            return any(
                _reimports(statement, name, import_map)
                for statement in body.named_children
            )
        node = node.parent
    return False


def _locally_bound_names(caller: Node, import_map: dict[str, str]) -> frozenset[str]:
    """Every name the caller's body reads as a local rather than as the
    module's: its parameters, whatever its own statements bind, and whatever
    each enclosing def binds (a closure's free names), typed or not. Python
    makes a name bound ANYWHERE in a body local for the whole body, so an
    imported module or function it shadows is unreachable there even before
    the binding runs (Greptile P1). An enclosing CLASS body is skipped, as
    Python skips it; a body-level import the import map reflects is a
    re-import, not a shadow."""
    names: set[str] = set()
    scope: Node | None = caller
    while scope is not None:
        if scope.id == caller.id or scope.type == cs.TS_PY_FUNCTION_DEFINITION:
            names.update(_parameter_names(scope))
            names.update(
                name
                for binder, identifier in _bindings_in(scope)
                if (name := safe_decode_text(identifier))
                and not _reimports(binder, name, import_map)
                and not _guards_an_optional_import(binder, name, import_map)
            )
        scope = scope.parent
    return frozenset(names)


def _call_bound_by(binder: Node) -> Node | None:
    """The call a plain `p = call()` binds to its WHOLE target, else None:
    `p, q = fw()`, `p += x`, `for p in xs`, `with cm as p`, `(p := x)` and
    an annotation alone all bind p to something other than a call."""
    if binder.type != cs.TS_PY_ASSIGNMENT:
        return None
    target = binder.child_by_field_name(cs.TS_FIELD_LEFT)
    value = binder.child_by_field_name(cs.TS_FIELD_RIGHT)
    if target is None or target.type != cs.TS_PY_IDENTIFIER:
        return None
    return value if value is not None and value.type == cs.TS_PY_CALL else None


def _binding_events(
    name: str, before: int, caller: Node
) -> list[tuple[int, Node | None]]:
    """(position, the call bound, or None for any other binding), for every
    binding of `name` in the caller's own scope before byte `before`."""
    return [
        (identifier.start_byte, _call_bound_by(binder))
        for binder, identifier in _bindings_in(caller)
        if identifier.end_byte <= before and safe_decode_text(identifier) == name
    ]


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
            self, caller_node: Node, module_qn: str, class_context: str | None = None
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

    def shadowed_import_names(self, caller: Node, module_qn: str) -> frozenset[str]:
        """The import-map names the caller's body binds as locals.

        `helpers = supplied` (or `def use(helpers)`, `for helpers in xs`, a
        `case [helpers]` capture, an enclosing def's local, ...) makes
        `helpers` local for the WHOLE body, so `helpers.make_pair()` can
        never reach the module the import map holds under that name
        (issue #1907). A body-level import the map reflects is a re-import,
        not a shadow, and stays out of the set.
        """
        import_map = self.import_processor.import_mapping.get(module_qn)
        if not import_map:
            return frozenset()
        return _locally_bound_names(caller, import_map) & frozenset(import_map)

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

                stack.extend(reversed(current.children))

        for assignment in assignments:
            self._process_assignment_simple(assignment, local_var_types, module_qn)

        for assignment in assignments:
            self._process_assignment_complex(assignment, local_var_types, module_qn)
        self._process_assignment_unpacking(
            node, assignments, local_var_types, module_qn
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
        caller: Node,
        assignments: list[Node],
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
        which is exactly what unpacking consumes position by position. The
        walk captures every assignment under the caller, including a nested
        def's, whose targets are that def's locals, not the caller's
        (CodeRabbit).
        """
        unpackings = [
            assignment
            for assignment in assignments
            if _scope_of(assignment) == caller.id
            and (left := assignment.child_by_field_name(cs.TS_FIELD_LEFT)) is not None
            and left.type in cs.PY_UNPACKING_TARGET_TYPES
        ]
        if not unpackings:
            return
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        bound = _locally_bound_names(caller, import_map)
        for assignment in unpackings:
            self._bind_unpacked_targets(
                assignment, caller, bound, local_var_types, module_qn
            )

    def _bind_unpacked_targets(
        self,
        assignment: Node,
        caller: Node,
        bound: frozenset[str],
        local_var_types: dict[str, str],
        module_qn: str,
    ) -> None:
        left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
        right = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
        if (
            left is None
            or right is None
            or (call := self._defining_call(right, caller)) is None
        ):
            return
        elements = self._tuple_return_elements(call, module_qn, local_var_types, bound)
        targets = [t for t in left.named_children if t.type != cs.TS_COMMENT]
        if len(elements) == 1 and elements[0][1]:
            elements = [elements[0]] * len(targets)  # `tuple[T, ...]`
        if len(elements) != len(targets):
            return  # a count mismatch, or no tuple annotation at all
        for target, (element, _homogeneous) in zip(targets, elements, strict=True):
            # A nested pattern or a starred target binds no one type.
            name = (
                safe_decode_text(target) if target.type == cs.TS_PY_IDENTIFIER else None
            )
            if name and name not in local_var_types:
                local_var_types[name] = element

    def _defining_call(self, right: Node, caller: Node) -> Node | None:
        """The call an unpacking's right-hand side comes from, or None.

        Either the call itself, or -- for `parsed = f(); a, b = parsed` -- the
        call that the nearest earlier binding of the name IN THE SAME SCOPE
        gave it. A nearer binding to anything but a call clears it: after
        `p = fw(); p = supplied` (or `p += x`, `for p in xs`, `with cm as p`),
        `p` is not fw's result. Captures are not guaranteed to come back in
        document order, so the nearest binding is the max position, not the
        last seen.
        """
        if right.type == cs.TS_PY_CALL:
            return right
        if right.type != cs.TS_PY_IDENTIFIER or not (name := safe_decode_text(right)):
            return None
        events = _binding_events(name, right.start_byte, caller)
        if not events:
            return None
        return max(events, key=lambda event: event[0])[1]

    def _tuple_return_elements(
        self,
        call: Node,
        module_qn: str,
        local_var_types: dict[str, str],
        bound: frozenset[str],
    ) -> list[tuple[str, bool]]:
        """(element type, is `...`-homogeneous) per position of the callee's
        `tuple[...]` return annotation, Optional stripped; empty if the callee
        cannot be found or does not return a tuple."""
        callee = self._callee_definition(call, module_qn, local_var_types, bound)
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
        self,
        call: Node,
        module_qn: str,
        local_var_types: dict[str, str],
        bound: frozenset[str],
    ) -> tuple[Node, str] | None:
        """(definition node, qualified name) of what a call invokes, or None.

        A name bound in the caller's own scope -- a parameter, an assignment,
        a loop or with target, typed or not -- is that local, never the
        imported module or function of the same name. An earlier guard asked
        the type map, which an untyped local never reaches (Greptile P1).
        """
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if func is None:
            return None
        import_map = self.import_processor.import_mapping.get(module_qn, {})
        if func.type == cs.TS_PY_IDENTIFIER:
            return self._free_function_definition(func, module_qn, import_map, bound)
        if func.type == cs.TS_PY_ATTRIBUTE:
            return self._method_definition(
                func, module_qn, import_map, local_var_types, bound
            )
        return None

    def _free_function_definition(
        self,
        func: Node,
        module_qn: str,
        import_map: dict[str, str],
        bound: frozenset[str],
    ) -> tuple[Node, str] | None:
        name = safe_decode_text(func)
        if not name or name in bound:
            return None
        for qn in (import_map.get(name), f"{module_qn}{cs.SEPARATOR_DOT}{name}"):
            if qn and (node := self._find_function_ast_node(qn)) is not None:
                return node, qn
        return None

    def _method_definition(
        self,
        func: Node,
        module_qn: str,
        import_map: dict[str, str],
        local_var_types: dict[str, str],
        bound: frozenset[str],
    ) -> tuple[Node, str] | None:
        text = self._extract_full_method_call(func)
        if not text:
            return None
        # `helpers.make_pair()`: the receiver is an imported MODULE, which the
        # method resolver (built for typed receivers) cannot name; the import
        # map can, and the qn it gives is a function's.
        receiver, _, leaf = text.rpartition(cs.SEPARATOR_DOT)
        if (
            cs.SEPARATOR_DOT not in receiver
            and receiver not in bound
            and (module := import_map.get(receiver))
        ):
            qn = f"{module}{cs.SEPARATOR_DOT}{leaf}"
            if (node := self._find_function_ast_node(qn)) is not None:
                return node, qn
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
            # The enclosing class, when there is one, so `w = self.parse();
            # return w` types the return the same way the call pass types
            # the body (issue #1901). The seed is inert for a free function.
            owner = method_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            class_context = (
                owner if self.function_registry.get(owner) == NodeType.CLASS else None
            )
            local_vars = self.build_local_variable_type_map(
                method_node, module_qn, class_context
            )
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
