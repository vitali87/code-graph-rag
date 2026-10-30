"""Python's share of the call pass: the callee a Python call binds through the
Jedi frontend, the edges Python's rules add to it (`self.m()` dispatch, the
conformers of a Protocol stub, a conditional import's local twin, a class
and its same-named def), the calls Python makes without a call node (a bare
decorator, an operator's dunder, an instance's `__call__`, a property read),
and the facts the pass records for a caller (the names it binds, the local
aliases it calls through, the functions a class's callable fields hold).

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver
and the reference walkers) and hands Python's part to the
`PythonCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import safe_decode_text
from ...types_defs import FunctionLocation, LanguageQueries, PropertyDict
from ..call_resolver import PY_EXTERNAL_TARGET
from ..edge_site import EdgeSiteHost, host_site_scoped
from ..js_ts import calls as js_calls
from ..language_calls import LanguageCalls
from .class_aliases import scan_class_body_aliases
from .receiver_rebinding import ReceiverRebinding, receiver_rebinding

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class PythonCallHost(EdgeSiteHost, Protocol):
    """What Python call resolution calls on the call pass that builds it: the
    edge emitter, the name-based resolve that notes its verdict for the call
    node being scanned, a function node's recorded location, a call's callee
    name and arguments, the callback emitter a passed function's edge goes
    through like any other callback's, and the module-scope reference scans
    Python shares with JS/TS. Beside the edge site, `_verdict`, how surely
    the call node being scanned binds its callee, which a single Protocol
    conformer's edge carries."""

    _verdict: str

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None: ...

    def _resolve_noting(
        self,
        call_name: str,
        module_qn: str,
        local_var_types: dict[str, str] | None = None,
        class_context: str | None = None,
        caller_qn: str | None = None,
        language: cs.SupportedLanguage | None = None,
        call_point: int | None = None,
    ) -> tuple[str, str] | None: ...

    def _get_call_target_name(
        self, call_node: Node, language: cs.SupportedLanguage | None = None
    ) -> str | None: ...

    def _parse_call_arguments(
        self, call_node: Node
    ) -> tuple[list[Node], dict[str, Node]]: ...

    def _emit_callback_edge(
        self,
        source_spec: tuple[str, str, str],
        arg_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
        rel_type: cs.RelationshipType = cs.RelationshipType.CALLS,
        language: cs.SupportedLanguage | None = None,
    ) -> None: ...

    def _ingest_py_js_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_field_assignments: bool = False,
    ) -> None: ...


class PythonCallScan(Protocol):
    """The caller a Python call node is scanned in."""

    module_qn: str
    caller_qn: str
    language: cs.SupportedLanguage
    class_context: str | None
    local_var_types: dict[str, str] | None
    is_python: bool


# Python callers whose own bindings shadow module and class names for the
# whole body (issue #2666); a class body or module is excluded on purpose.
_LOCAL_SCOPE_CALLERS = frozenset({cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_LAMBDA})

# A callee written as a name or attribute may be a typed instance; one written
# as a call (`Dispatcher()(1)`) is typed by what that call returns.
_INSTANCE_CALLEE_TYPES = frozenset(
    {cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE, cs.TS_PY_CALL}
)

# Python nested-scope boundaries used when scanning a scope for dispatch
# tables of function references.
SCOPE_BOUNDARY_TYPES = frozenset(
    {
        cs.TS_PY_FUNCTION_DEFINITION,
        cs.TS_PY_CLASS_DEFINITION,
        cs.TS_PY_DECORATED_DEFINITION,
    }
)


def _param_name(child: Node) -> str | None:
    match child.type:
        case cs.TS_PY_IDENTIFIER:
            return safe_decode_text(child)
        case cs.TS_PY_DEFAULT_PARAMETER | cs.TS_PY_TYPED_DEFAULT_PARAMETER:
            name_node = child.child_by_field_name(cs.FIELD_NAME)
            return safe_decode_text(name_node) if name_node else None
        case cs.TS_PY_TYPED_PARAMETER:
            inner = child.named_children[0] if child.named_children else None
            return (
                safe_decode_text(inner)
                if inner is not None and inner.type == cs.TS_PY_IDENTIFIER
                else None
            )
        case _:
            # *args/**kwargs/keyword_separator never bind fields.
            return None


def _operator_dunder_target(node: Node) -> tuple[Node | None, str] | None:
    """(receiver, dunder) that a Python operator node dispatches to, if any.

    `x[k]` is `__getitem__` (or `__setitem__` as an assignment target),
    `k in x` is `x.__contains__`, and `len(x)` is `x.__len__`.
    """
    match node.type:
        case cs.TS_PY_SUBSCRIPT:
            parent = node.parent
            left = (
                parent.child_by_field_name(cs.TS_FIELD_LEFT)
                if parent is not None and parent.type == cs.TS_PY_ASSIGNMENT
                else None
            )
            is_write = left is not None and left.id == node.id
            return (
                node.child_by_field_name(cs.FIELD_VALUE),
                cs.PY_DUNDER_SETITEM if is_write else cs.PY_DUNDER_GETITEM,
            )
        case cs.TS_PY_COMPARISON_OPERATOR:
            operators = node.child_by_field_name(cs.TS_FIELD_OPERATORS)
            if (
                operators is not None
                and (op_text := safe_decode_text(operators))
                and cs.PY_OP_IN in op_text.split()
                and node.named_children
            ):
                return node.named_children[-1], cs.PY_DUNDER_CONTAINS
        case cs.TS_PY_CALL:
            func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            args = node.child_by_field_name(cs.FIELD_ARGUMENTS)
            if (
                func is not None
                and safe_decode_text(func) == cs.PY_BUILTIN_LEN
                and args is not None
                and len(args.named_children) == 1
            ):
                return args.named_children[0], cs.PY_DUNDER_LEN
    return None


def _truthiness_operands(node: Node) -> list[Node | None]:
    """The operands a Python node tests for truthiness (`__bool__`)."""
    match node.type:
        case cs.TS_PY_BOOLEAN_OPERATOR:
            return [
                node.child_by_field_name(cs.TS_FIELD_LEFT),
                node.child_by_field_name(cs.TS_FIELD_RIGHT),
            ]
        case cs.TS_PY_NOT_OPERATOR:
            return [node.child_by_field_name(cs.TS_FIELD_ARGUMENT)]
        case (
            cs.TS_PY_IF_STATEMENT
            | cs.TS_PY_WHILE_STATEMENT
            | cs.TS_PY_ELIF_CLAUSE
            | cs.TS_PY_CONDITIONAL_EXPRESSION
        ):
            # A bare object as a condition is tested for truthiness; nested
            # boolean/not operators are handled when the walk reaches them.
            return [node.child_by_field_name(cs.TS_FIELD_CONDITION)]
    return []


def _bare_decorator_name(decorator_node: Node) -> str | None:
    # A bare decorator `@task` / `@pkg.deco` (no call parens) is not a
    # `call` node, so the normal call pass misses it even though applying
    # it runs `task(func)` at module load. A call decorator `@deco(...)`
    # IS already captured, so skip it here.
    named = decorator_node.named_children
    if not named:
        return None
    expr = named[0]
    if expr.type in (cs.TS_IDENTIFIER, cs.TS_ATTRIBUTE) and expr.text is not None:
        return expr.text.decode(cs.ENCODING_UTF8)
    return None


def _runs_at_module_load(node: Node) -> bool:
    # A definition runs at module load only at module or class-body scope;
    # nested inside a function body it runs at that function's call time,
    # so its decorator is not a module-load call.
    ancestor = node.parent
    while ancestor is not None:
        if ancestor.type == cs.TS_PY_FUNCTION_DEFINITION:
            return False
        ancestor = ancestor.parent
    return True


def _find_init_method(class_body: Node) -> Node | None:
    for child in class_body.children:
        candidate = child
        if candidate.type == cs.TS_PY_DECORATED_DEFINITION:
            candidate = candidate.child_by_field_name(cs.FIELD_DEFINITION) or candidate
        if candidate.type != cs.TS_PY_FUNCTION_DEFINITION:
            continue
        name_node = candidate.child_by_field_name(cs.FIELD_NAME)
        if name_node is not None and safe_decode_text(name_node) == (cs.PY_METHOD_INIT):
            return candidate
    return None


def _ordered_param_names(init_node: Node) -> tuple[str, ...]:
    params_node = init_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return ()
    names: list[str] = []
    for child in params_node.named_children:
        name = _param_name(child)
        if name and name != cs.PY_KEYWORD_SELF:
            names.append(name)
    return tuple(names)


def _annotated_field_names(class_body: Node) -> tuple[str, ...]:
    # NamedTuple/dataclass field order = annotated class-body assignments
    # (`fetch_name: Callable`, `other: int = 3`) in declaration order.
    names: list[str] = []
    for child in class_body.children:
        if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.named_children:
            continue
        stmt = child.named_children[0]
        if stmt.type != cs.TS_PY_ASSIGNMENT:
            continue
        left = stmt.child_by_field_name(cs.TS_FIELD_LEFT)
        has_type = stmt.child_by_field_name(cs.FIELD_TYPE) is not None
        if (
            left is not None
            and left.type == cs.TS_PY_IDENTIFIER
            and has_type
            and (name := safe_decode_text(left))
        ):
            names.append(name)
    return tuple(names)


def receiver_rebinding_at(
    ctx: PythonCallScan, call_node: Node, call_name: str
) -> ReceiverRebinding | None:
    # Only a `self.M()` / `cls.M()` in a method whose type map holds the
    # receiver can have been rebound; every other call skips the scan.
    receiver, _, member = call_name.partition(cs.SEPARATOR_DOT)
    if (
        not ctx.is_python
        or not member
        or receiver not in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS)
        or not ctx.local_var_types
        or receiver not in ctx.local_var_types
    ):
        return None
    return receiver_rebinding(call_node, receiver)


class PythonCallResolution(LanguageCalls):
    """Binds Python calls through the Jedi frontend, writes the edges Python's
    own rules add to them and the calls Python makes without a call node,
    and records the per-caller and per-class facts the resolver reads.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: the definition pass fills its registry as it registers
    each file."""

    __slots__ = ("_host", "_resolver")

    flows_callables = True
    references_arguments = True
    binds_callable_fields = True

    def __init__(self, host: PythonCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        self._host._ingest_py_js_module_references(
            root_node, module_qn, language, queries
        )
        self.ingest_module_decorator_calls(
            root_node,
            module_qn,
            language,
            queries,
            combined_captures,
            sorted_func_nodes,
        )
        self.ingest_class_alias_references(
            module_qn, combined_captures.get(cs.CAPTURE_CLASS) or []
        )

    def ingest_module_decorator_calls(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        decorator_targets = list(sorted_func_nodes or [])
        if combined_captures and (
            class_nodes := combined_captures.get(cs.CAPTURE_CLASS)
        ):
            decorator_targets.extend(class_nodes)
        if decorator_targets:
            self._ingest_decorator_calls(
                decorator_targets,
                module_qn,
                root_node,
                queries[language][cs.QUERY_CONFIG],
            )

    @host_site_scoped
    def _ingest_decorator_calls(
        self,
        nodes: list[Node],
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
    ) -> None:
        # Emit `(Module)->decorator` CALLS for bare decorators on functions,
        # methods, AND classes: the decoration executes at module-load time,
        # so the module is the caller. Only first-party callables get an edge.
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        alias_map: dict[str, str] | None = None
        for node in nodes:
            parent = node.parent
            if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
                continue
            if not _runs_at_module_load(parent):
                continue
            alias_map = self._emit_decorator_calls_on(
                parent, module_spec, module_qn, root_node, lang_config, alias_map
            )

    def _emit_decorator_calls_on(
        self,
        parent: Node,
        module_spec: tuple[str, str, str],
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
        alias_map: dict[str, str] | None,
    ) -> dict[str, str] | None:
        # One decorated definition's bare decorators, each a module-load CALLS
        # edge; returns the (lazily built) local-alias map for the next one.
        decorator_labels = (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
            cs.NodeLabel.CLASS,
        )
        for child in parent.children:
            if child.type != cs.TS_PY_DECORATOR:
                continue
            self._host._site_node = child
            name = _bare_decorator_name(child)
            if not name:
                continue
            callee, alias_map = self._resolve_decorator_callee(
                name, module_qn, root_node, lang_config, alias_map
            )
            if callee and callee[0] in decorator_labels:
                self._emit_bare_decorator_targets(module_spec, *callee)
        return alias_map

    def _emit_bare_decorator_targets(
        self, module_spec: tuple[str, str, str], callee_type: str, callee_qn: str
    ) -> None:
        # `@deco` runs `deco(func)`, so it binds as that call does: each
        # same-named definition is a candidate, and a class is constructed.
        # networkx's `@nx._dispatchable` is a class with a same-named docs
        # shim; skipping classes left its decorator sites on the shim alone
        # (issue #2621).
        registry = self._resolver.function_registry
        variants = registry.variants(callee_qn)
        prev_resolution = self._host._resolution
        if len(variants) > 1:
            self._host._resolution = cs.EdgeResolution.OVERLOAD
        try:
            for variant in variants:
                targets: list[tuple[str, str]] = []
                match registry.get(variant):
                    case cs.NodeLabel.FUNCTION | cs.NodeLabel.METHOD as kind:
                        targets.append((kind, variant))
                    case cs.NodeLabel.CLASS:
                        # `_emit_rel` writes a CALLS to a Class as INSTANTIATES.
                        targets.append((cs.NodeLabel.CLASS, variant))
                        init_qn = f"{variant}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
                        if init_qn in registry:
                            targets.append((cs.NodeLabel.METHOD, init_qn))
                    case None if callee_type != cs.NodeLabel.CLASS:
                        targets.append((callee_type, variant))
                for label, target_qn in targets:
                    self._host._emit_rel(
                        module_spec,
                        cs.RelationshipType.CALLS,
                        (label, cs.KEY_QUALIFIED_NAME, target_qn),
                    )
        finally:
            self._host._resolution = prev_resolution

    def _resolve_decorator_callee(
        self,
        name: str,
        module_qn: str,
        root_node: Node,
        lang_config: LanguageSpec,
        alias_map: dict[str, str] | None,
    ) -> tuple[tuple[str, str] | None, dict[str, str] | None]:
        resolver = self._resolver
        callee = resolver.resolve_function_call(name, module_qn)
        if not callee and cs.SEPARATOR_DOT not in name:
            # `@alias` where `alias = task` still calls task at load;
            # reuse the local-alias fallback the call pass uses.
            if alias_map is None:
                alias_map = self.local_alias_map(root_node, lang_config, module_qn)
            if (rhs := alias_map.get(name)) is not None:
                callee = resolver.resolve_function_call(rhs, module_qn)
        return callee, alias_map

    @host_site_scoped
    def ingest_class_alias_references(
        self, module_qn: str, class_nodes: list[Node]
    ) -> None:
        # `run = _plain` in a class body uses the method when the body runs
        # (issue #2620): at import for a top-level class, so from the module,
        # the way a call written there (`prop = property(_get, _set)`) is
        # already attributed; for a class inside a function, from that
        # function, so a dead factory keeps its class's implementation dead.
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        self._host._resolution = cs.EdgeResolution.EXACT
        for class_node in class_nodes:
            body = class_node.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                continue
            references = scan_class_body_aliases(body).references
            if not references:
                continue
            scope = self._class_body_scope(class_node, module_qn, module_spec)
            if scope is None:
                continue
            for site, method in references:
                if (target := self._host._recorded_caller(method, module_qn)) is None:
                    continue
                self._host._site_node = site
                self._host._emit_rel(
                    scope,
                    cs.RelationshipType.REFERENCES,
                    (target.label, cs.KEY_QUALIFIED_NAME, target.qualified_name),
                )

    def _class_body_scope(
        self,
        class_node: Node,
        module_qn: str,
        module_spec: tuple[str, str, str],
    ) -> tuple[str, str, str] | None:
        ancestor = class_node.parent
        while ancestor is not None:
            if ancestor.type == cs.TS_PY_FUNCTION_DEFINITION:
                owner = self._host._recorded_caller(ancestor, module_qn)
                if owner is None:
                    return None
                return (owner.label, cs.KEY_QUALIFIED_NAME, owner.qualified_name)
            ancestor = ancestor.parent
        return module_spec

    def collect_callable_field_bindings(
        self, root_node: Node, module_qn: str, call_nodes: list[Node]
    ) -> None:
        self.collect_ctor_field_metadata(root_node, module_qn)
        for call_node in call_nodes:
            self.record_call_field_bindings(call_node, module_qn)

    def record_call_field_bindings(self, call_node: Node, module_qn: str) -> None:
        # A class construction whose arguments name functions binds each
        # to the matching (keyword or positional) ctor field.
        resolver = self._resolver
        positional, keyword = self._host._parse_call_arguments(call_node)
        if not positional and not keyword:
            return
        name = self._host._get_call_target_name(call_node)
        if not name:
            return
        callee = resolver.resolve_function_call(name, module_qn)
        if not callee or callee[0] != cs.NodeLabel.CLASS:
            return
        registry = resolver.function_registry
        callable_labels = (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
        arg_entries: list[tuple[int | str, Node]] = list(enumerate(positional))
        arg_entries.extend(keyword.items())
        for key, value_node in arg_entries:
            if not (value_text := safe_decode_text(value_node)):
                continue
            bound = resolver.resolve_function_call(value_text, module_qn)
            if bound and bound[0] in callable_labels and bound[1] in registry:
                resolver.record_pending_field_binding(callee[1], key, bound[1])

    def collect_ctor_field_metadata(self, root_node: Node, module_qn: str) -> None:
        # For every class in this file, record the ordered ctor param names
        # (__init__ params, or annotated class-body fields for
        # NamedTuple/dataclass classes without __init__) and the
        # param -> attribute renames from `self.attr = param` statements.
        # The stack carries each node's ENCLOSING class qn so a nested class
        # (Inner inside Outer) resolves to module.Outer.Inner: a bare
        # resolve_function_call("Inner", module_qn) would miss it. A class
        # inside a FUNCTION is skipped (qn is caller-scoped), so
        # nested-in-method classes never reach here.
        stack: list[tuple[Node, str | None]] = [
            (child, None) for child in root_node.children
        ]
        while stack:
            node, enclosing_qn = stack.pop()
            if node.type == cs.TS_PY_FUNCTION_DEFINITION:
                continue
            if node.type == cs.TS_PY_CLASS_DEFINITION:
                self._visit_ctor_class(node, enclosing_qn, module_qn, stack)
            else:
                stack.extend((c, enclosing_qn) for c in node.children)

    def _visit_ctor_class(
        self,
        node: Node,
        enclosing_qn: str | None,
        module_qn: str,
        stack: list[tuple[Node, str | None]],
    ) -> None:
        name_node = node.child_by_field_name(cs.FIELD_NAME)
        class_name = safe_decode_text(name_node) if name_node else None
        body = node.child_by_field_name(cs.FIELD_BODY)
        if not class_name or body is None:
            return
        class_qn = self._ctor_class_qn(class_name, enclosing_qn, module_qn)
        # Descend into the body with THIS class as the enclosing scope so
        # its nested classes resolve; skip metadata when unresolved.
        stack.extend((c, class_qn) for c in body.children)
        if class_qn is not None:
            self._record_class_ctor_metadata(body, class_qn)

    def _ctor_class_qn(
        self, class_name: str, enclosing_qn: str | None, module_qn: str
    ) -> str | None:
        resolver = self._resolver
        if enclosing_qn is not None:
            candidate_qn = f"{enclosing_qn}{cs.SEPARATOR_DOT}{class_name}"
            if resolver.function_registry.get(candidate_qn) == cs.NodeLabel.CLASS:
                return candidate_qn
            return None
        resolved = resolver.resolve_function_call(class_name, module_qn)
        return resolved[1] if resolved and resolved[0] == cs.NodeLabel.CLASS else None

    def _record_class_ctor_metadata(self, body: Node, class_qn: str) -> None:
        resolver = self._resolver
        if init_node := _find_init_method(body):
            params = _ordered_param_names(init_node)
            resolver.record_ctor_params(class_qn, params)
            self._record_param_attr_renames(init_node, class_qn, set(params))
        else:
            resolver.record_ctor_params(class_qn, _annotated_field_names(body))

    def _record_param_attr_renames(
        self, init_node: Node, class_qn: str, params: set[str]
    ) -> None:
        # `self.ctx_factory = create_context` stores the param under a
        # DIFFERENT name; the field invocation goes through the attribute.
        body = init_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return
        self_prefix = f"{cs.PY_KEYWORD_SELF}{cs.SEPARATOR_DOT}"
        stack: list[Node] = list(body.children)
        while stack:
            node = stack.pop()
            # A `self.x = param` inside a nested helper (def later():
            # self.cb = handler) is that helper's store, not a constructor
            # rename; descending would let it override the real __init__
            # store, so stop at nested scopes.
            if node.type in SCOPE_BOUNDARY_TYPES:
                continue
            if node.type == cs.TS_PY_ASSIGNMENT:
                left = node.child_by_field_name(cs.TS_FIELD_LEFT)
                right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
                if (
                    left is not None
                    and right is not None
                    and left.type == cs.TS_PY_ATTRIBUTE
                    and right.type == cs.TS_PY_IDENTIFIER
                    and (left_text := safe_decode_text(left))
                    and left_text.startswith(self_prefix)
                    and (param := safe_decode_text(right)) in params
                ):
                    attr = left_text[len(self_prefix) :]
                    if attr != param:
                        self._resolver.record_ctor_param_attr(class_qn, param, attr)
            stack.extend(node.children)

    def record_local_names(
        self, caller_node: Node, caller_qn: str, module_qn: str
    ) -> None:
        # Names this caller binds that also name an import: the resolver
        # refuses to read the import map for them (issue #1907).
        python = self._resolver.type_inference.python_type_inference
        shadowed = python.ast_analyzer.shadowed_import_names(caller_node, module_qn)
        if shadowed:
            self._resolver.python_shadowed_imports[caller_qn] = shadowed
        else:
            self._resolver.python_shadowed_imports.pop(caller_qn, None)
        # Every name a function binds itself: a bare use of one never resolves
        # to the module function or method it shadows (issue #2666). Only a
        # function or lambda body: in a class body `alias = run` does read the
        # class's own `run`, and a module's bindings are its definitions.
        local = (
            python.ast_analyzer.locally_bound_names(caller_node, module_qn)
            if caller_node.type in _LOCAL_SCOPE_CALLERS
            else frozenset()
        )
        if local:
            self._resolver.python_local_names[caller_qn] = local
        else:
            self._resolver.python_local_names.pop(caller_qn, None)
        self._record_name_typed_params(caller_node, caller_qn, module_qn)

    def _record_name_typed_params(
        self, caller_node: Node, caller_qn: str, module_qn: str
    ) -> None:
        # Parameters whose class only their name spells: a call through one
        # is labelled heuristic (issue #2608).
        python = self._resolver.type_inference.python_type_inference
        guessed = python.variable_analyzer.name_typed_parameters(caller_node, module_qn)
        if guessed:
            self._resolver.python_name_typed_params[caller_qn] = frozenset(guessed)
        else:
            self._resolver.python_name_typed_params.pop(caller_qn, None)

    def resolve_callee(
        self,
        ctx: PythonCallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        # The Jedi frontend (issue #1183): a HIT is the exact callee
        # through re-exports/decorators the trie approximates; the
        # external sentinel proves the call leaves the repo and
        # suppresses trie fabrication. Any miss (incl. the frontend
        # off, so both maps are empty) falls back to the Python
        # heuristics with call_point preserved.
        callee_info = self._resolver.python.resolve_python_call_site(
            call_node, ctx.module_qn
        )
        if callee_info == PY_EXTERNAL_TARGET:
            callee_info = None
        elif callee_info is None:
            callee_info = self._host._resolve_noting(
                call_name,
                ctx.module_qn,
                call_var_types,
                ctx.class_context,
                ctx.caller_qn,
                ctx.language,
                call_point=call_node.start_byte,
            )
        return callee_info

    def emit_protocol_conformer_edges(
        self,
        caller_spec: tuple[str, str, str],
        dispatch_targets: set[tuple[str, str]],
        ensure_rel: Callable[..., None],
    ) -> None:
        edges = [
            (conformer_type, target_qn)
            for conformer_type, conformer_qn in dispatch_targets
            for target_qn in self._resolver.function_registry.variants(conformer_qn)
        ]
        # The receiver is one of the conformers, so with several the call is
        # a fan-out like any other same-named candidate set (issue #1526):
        # renaming one conformer must not rewrite the shared call site.
        self._host._resolution = (
            cs.EdgeResolution.OVERLOAD if len(edges) > 1 else self._host._verdict
        )
        for conformer_type, target_qn in edges:
            ensure_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (conformer_type, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def emit_self_dispatch(
        self,
        caller_spec: tuple[str, str, str],
        class_context: str,
        call_name: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        _, _, self_method = call_name.partition(cs.SEPARATOR_DOT)
        if cs.SEPARATOR_DOT not in self_method:
            for target_type, target_qn in self._resolver.self_dispatch_targets(
                class_context, self_method
            ):
                for variant in self._resolver.function_registry.variants(target_qn):
                    ensure_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    def emit_binding_twin(
        self,
        caller_spec: tuple[str, str, str],
        twin_qn: str,
        twin_type: cs.NodeLabel,
        ensure_rel: Callable[..., None],
    ) -> None:
        # A Python name holds one binding at a time, so a class and a
        # same-named def (a docs shim, an ImportError fallback) are both what
        # `Tool()` may run. Skipping the twin of the other kind bound every
        # construction to the shim alone (issue #2621).
        if twin_type in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            ensure_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (twin_type, cs.KEY_QUALIFIED_NAME, twin_qn),
            )
            return
        if twin_type != cs.NodeLabel.CLASS:
            return
        ensure_rel(
            caller_spec,
            cs.RelationshipType.INSTANTIATES,
            (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, twin_qn),
        )
        init_qn = f"{twin_qn}{cs.SEPARATOR_DOT}{cs.PY_METHOD_INIT}"
        if init_qn in self._resolver.function_registry:
            ensure_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, init_qn),
            )

    def emit_conditional_import_twin(
        self,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        call_name: str,
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # A platform-conditional import with a local fallback def (click's
        # `if WIN: from ._winconsole import X ... else: def X(...)`) is
        # statically undecidable; the call resolves to the import, so the
        # mutually-exclusive local def looks dead. When the name was bound
        # by a CONDITIONAL import and the CURRENT module also defines it,
        # fan the call out to the local twin too, mirroring the Go
        # build-variant fan-out. An UNCONDITIONAL import shadowing a local
        # def is plain shadowing: the local stays dead, so no edge.
        local_qn = f"{module_qn}{cs.SEPARATOR_DOT}{call_name}"
        if (
            local_qn != callee_qn
            and call_name
            in self._resolver.import_processor.python.conditional_imports.get(
                module_qn, ()
            )
            and self._resolver.function_registry.get(local_qn) == cs.NodeLabel.FUNCTION
        ):
            for variant in self._resolver.function_registry.variants(local_qn):
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, variant),
                )

    @host_site_scoped
    def ingest_operator_dispatch_calls(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        boundary = (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            if node.type in boundary:
                continue
            if (dunder := _operator_dunder_target(node)) is not None:
                self._emit_operator_dunder(
                    dunder[0], dunder[1], caller_spec, module_qn, local_var_types
                )
            elif node.type == cs.TS_PY_CALL:
                self._emit_instance_call(node, caller_spec, module_qn, local_var_types)
            for operand in _truthiness_operands(node):
                self._emit_truthiness(operand, caller_spec, module_qn, local_var_types)
            stack.extend(node.children)

    @host_site_scoped
    def _emit_truthiness(
        self,
        operand: Node | None,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # Truthiness of an object calls __bool__ if defined, else __len__. Only a
        # bare name/attribute operand names an object (a comparison/call is already
        # a bool and is handled elsewhere); try __bool__ first, then __len__.
        if operand is not None:
            self._host._site_node = operand
        if operand is None or operand.type not in (
            cs.TS_PY_IDENTIFIER,
            cs.TS_PY_ATTRIBUTE,
        ):
            return
        for dunder in (cs.PY_DUNDER_BOOL, cs.PY_DUNDER_LEN):
            if self._emit_operator_dunder(
                operand, dunder, caller_spec, module_qn, local_var_types
            ):
                return

    @host_site_scoped
    def _emit_operator_dunder(
        self,
        operand: Node | None,
        dunder: str,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> bool:
        # Resolve the implied <operand>.__dunder__ call; resolution only succeeds
        # for a first-party class that defines the dunder, so builtin containers
        # (dict/list) yield no edge. Restrict to simple attribute/name operands.
        # Returns whether an edge was emitted.
        if operand is None or not (operand_text := safe_decode_text(operand)):
            return False
        self._host._site_node = operand
        if any(ch in operand_text for ch in cs.PY_OPERAND_REJECT_CHARS):
            return False
        targets = self._resolver.python.operator_dunder_targets(
            operand_text, dunder, module_qn, local_var_types
        )
        if not targets:
            return False
        for callee_type, callee_qn in targets:
            for target_qn in self._resolver.function_registry.variants(callee_qn):
                self._host._emit_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (callee_type, cs.KEY_QUALIFIED_NAME, target_qn),
                )
        return True

    @host_site_scoped
    def _emit_instance_call(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # `obj(...)` on a first-party instance runs type(obj).__call__, which
        # the class body may bind to another method (issue #2620).
        callee = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if callee is None or callee.type not in _INSTANCE_CALLEE_TYPES:
            return
        is_call_result = callee.type == cs.TS_PY_CALL
        if not is_call_result and not local_var_types:
            return
        if not (callee_text := safe_decode_text(callee)):
            return
        targets = self._resolver.python.instance_call_targets(
            callee_text, is_call_result, module_qn, local_var_types
        )
        self._host._site_node = call_node
        for callee_type, callee_qn in sorted(targets):
            methods = self._resolver.member_alias_fanout(callee_qn, cs.PY_DUNDER_CALL)
            self._host._resolution = (
                cs.EdgeResolution.OVERLOAD
                if len(methods) > 1
                else cs.EdgeResolution.EXACT
            )
            for method in methods:
                for variant in self._resolver.function_registry.variants(method):
                    self._host._emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (callee_type, cs.KEY_QUALIFIED_NAME, variant),
                    )

    def ingest_callable_field_calls(
        self,
        call_name: str,
        caller_spec: tuple[str, str, str],
        local_var_types: dict[str, str] | None,
        ensure_rel: Callable[..., None],
    ) -> None:
        recv, sep, field = call_name.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return
        recv_type = local_var_types.get(recv) if local_var_types else None
        targets = self._resolver.callable_field_targets(field, recv_type)
        if not targets:
            return
        registry = self._resolver.function_registry
        for target_qn in targets:
            if target_qn in registry:
                ensure_rel(
                    caller_spec,
                    cs.RelationshipType.CALLS,
                    (registry[target_qn], cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def ingest_higher_order_builtin_calls(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        ensure_rel: Callable[..., None],
        caller_qn: str | None = None,
    ) -> None:
        positional, keyword = self._host._parse_call_arguments(call_node)
        for arg_node in (*positional, *keyword.values()):
            self._host._emit_callback_edge(
                caller_spec,
                arg_node,
                module_qn,
                local_var_types,
                class_context,
                resolve_func,
                ensure_rel,
                caller_qn,
            )

    def local_alias_map(
        self, caller_node: Node, lang_config: LanguageSpec, module_qn: str
    ) -> dict[str, str]:
        identifier = cs.TS_PY_IDENTIFIER
        attribute = cs.TS_PY_ATTRIBUTE
        assignment = cs.TS_PY_ASSIGNMENT
        left_field = cs.TS_FIELD_LEFT
        right_field = cs.TS_FIELD_RIGHT
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        aliases: dict[str, str] = {}
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == assignment:
                left = node.child_by_field_name(left_field)
                right = node.child_by_field_name(right_field)
                if (
                    left is not None
                    and left.type == identifier
                    and (left_text := left.text) is not None
                    and right is not None
                    and (
                        target := self._alias_reference_text(
                            right, identifier, attribute, module_qn
                        )
                    )
                    is not None
                ):
                    aliases.setdefault(left_text.decode(cs.ENCODING_UTF8), target)
            stack.extend(node.children)
        return aliases

    def _alias_reference_text(
        self, right: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        # An alias rhs is a plain name/attribute, a conditional that picks one
        # (resolve_builtin_call if is_js_ts else None), or getattr(recv, name)
        # dynamic dispatch. Take the name/attribute branch (consequence or
        # alternative, never the condition) or build recv.<name> for getattr.
        # A TS cast (`y as T`) is transparent; unwrap so `x = y as T` still aliases.
        right = js_calls.unwrap_ts_cast(right)
        if right.type in (identifier, attribute):
            return right.text.decode(cs.ENCODING_UTF8) if right.text else None
        if right.type == cs.TS_PY_CONDITIONAL_EXPRESSION and right.named_children:
            for branch in (right.named_children[0], right.named_children[-1]):
                if branch.type in (identifier, attribute) and branch.text:
                    return branch.text.decode(cs.ENCODING_UTF8)
        if right.type == cs.TS_PY_CALL:
            return self._getattr_reference_text(right, identifier, attribute, module_qn)
        return None

    def _getattr_reference_text(
        self, call: Node, identifier: str, attribute: str, module_qn: str
    ) -> str | None:
        func = call.child_by_field_name(cs.TS_FIELD_FUNCTION)
        args = call.child_by_field_name(cs.FIELD_ARGUMENTS)
        if (
            func is None
            or safe_decode_text(func) != cs.PY_BUILTIN_GETATTR
            or args is None
            or len(args.named_children) < 2
        ):
            return None
        receiver, name_node = args.named_children[0], args.named_children[1]
        if receiver.type not in (identifier, attribute):
            return None
        if (name := self._resolve_str_const(name_node, module_qn)) is None:
            return None
        return f"{safe_decode_text(receiver)}{cs.SEPARATOR_DOT}{name}"

    def _resolve_str_const(self, node: Node, module_qn: str) -> str | None:
        # Resolve a getattr name argument to its string value: a string literal
        # directly, or a module-level constant (cs.METHOD_X / METHOD_X) read from
        # the defining module's AST.
        if node.type == cs.TS_PY_STRING:
            content = next(
                (c for c in node.children if c.type == cs.TS_PY_STRING_CONTENT), None
            )
            return safe_decode_text(content) if content is not None else None
        if node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
            return None
        name_text = safe_decode_text(node)
        if not name_text:
            return None
        import_map = self._resolver.import_processor.import_mapping.get(module_qn, {})
        prefix, _, const_name = name_text.rpartition(cs.SEPARATOR_DOT)
        if not prefix:
            mapped = import_map.get(const_name)
            const_module_qn = (
                mapped.rsplit(cs.SEPARATOR_DOT, 1)[0] if mapped else module_qn
            )
        elif (mapped_module := import_map.get(prefix)) is not None:
            const_module_qn = mapped_module
        else:
            const_module_qn = prefix
        return self._module_string_constant(const_module_qn, const_name)

    def _module_string_constant(self, module_qn: str, const_name: str) -> str | None:
        type_inference = self._resolver.type_inference
        file_path = type_inference.module_qn_to_file_path.get(module_qn)
        if file_path is None or not (entry := type_inference.ast_cache.load(file_path)):
            return None
        root_node, _ = entry
        for child in root_node.children:
            if child.type != cs.TS_PY_EXPRESSION_STATEMENT or not child.children:
                continue
            assignment = child.children[0]
            if assignment.type != cs.TS_PY_ASSIGNMENT:
                continue
            left = assignment.child_by_field_name(cs.TS_FIELD_LEFT)
            right = assignment.child_by_field_name(cs.TS_FIELD_RIGHT)
            if (
                left is not None
                and left.type == cs.TS_PY_IDENTIFIER
                and safe_decode_text(left) == const_name
                and right is not None
                and right.type == cs.TS_PY_STRING
            ):
                return self._resolve_str_const(right, module_qn)
        return None

    @host_site_scoped
    def ingest_property_accesses(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # Accessing an @property getter invokes the getter method at runtime, but
        # tree-sitter sees a plain attribute, not a call. Resolve attribute
        # accesses whose tail names a known property and emit a CALLS edge to the
        # getter (skipping the attribute that is itself a call's function, already
        # resolved by the call path above).
        attr_type = cs.TS_PY_ATTRIBUTE
        function_types = lang_config.function_node_types
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        stack = list(caller_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            node_type = node.type
            if node_type in function_types or node_type in class_types:
                continue
            if node_type == attr_type and (text := node.text) is not None:
                attr_text = text.decode(cs.ENCODING_UTF8)
                if attr_text.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
                    self._emit_property_access(
                        node,
                        attr_text,
                        caller_spec,
                        caller_qn,
                        module_qn,
                        local_var_types,
                        class_context,
                        seen,
                    )
            stack.extend(node.children)

    def _emit_property_access(
        self,
        node: Node,
        attr_text: str,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        seen: set[str],
    ) -> None:
        # One property-named attribute access: a CALLS edge to the getter it
        # invokes, unless the attribute is itself a call's function.
        registry = self._resolver.function_registry
        parent = node.parent
        is_call_target = (
            parent is not None
            and parent.type == cs.TS_PY_CALL
            # `==` not `is`: py-tree-sitter builds a fresh
            # wrapper per lookup, so the node returned here is
            # never the same object as the one taken from
            # .children, and identity would leave this guard
            # permanently False.
            and parent.child_by_field_name(cs.TS_FIELD_FUNCTION) == node
        )
        if not is_call_target and (
            callee_info := self._resolver.resolve_function_call(
                attr_text,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
            )
        ):
            callee_qn = callee_info[1]
            if (
                registry.is_property(callee_qn)
                and callee_qn != caller_qn
                and callee_qn not in seen
            ):
                seen.add(callee_qn)
                for target_qn in registry.variants(callee_qn):
                    self._host._emit_rel(
                        caller_spec,
                        cs.RelationshipType.CALLS,
                        (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
                    )
