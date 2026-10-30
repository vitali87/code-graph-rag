"""Interprocedural callable-parameter flow: the function a callable
parameter runs, traced from the call sites that bind it.

A callback passed to a first-party function whose parameter is invoked
(directly, through a pass-through parameter of another function, or by a
closure the function returns) runs wherever that parameter is invoked, a
call the receiver's own body cannot name. The `CallableFlow` the call pass
builds records, per file, what each call site binds to a callee's
parameters, what each function may return and where it invokes its
parameters, and once every file is walked resolves the bindings to a
fixpoint and writes the CALLS edges they imply. It calls back through
`FlowHost` for the edge writer, the edge site and the verdict a resolved
argument notes."""

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple, Protocol

from tree_sitter import Node

from .. import constants as cs
from ..node_text import safe_decode_text
from ..types_defs import PropertyDict
from .call_callbacks import FLOW_ARG_REF_TYPES, CallbackEdges
from .call_nodes import parse_call_arguments
from .call_resolver import CallResolver
from .callable_params import callable_parameter_invocations
from .edge_site import EdgeSiteHost, host_site_scoped
from .go import calls as go_calls
from .js_ts import calls as js_calls
from .utils import node_site_properties

_CALLABLE_NODE_LABELS = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)


class _CallableFlowArg(NamedTuple):
    # One call-site argument that may carry a callable: bound to a concrete
    # function (source_concrete) or to a caller parameter (source_caller +
    # source_param), keyed to the callee parameter by position or keyword.
    callee_qn: str
    position: int
    keyword: str
    source_concrete: str
    source_caller: str
    source_param: str


class _FactoryCall(NamedTuple):
    # A call `x(args)` where x was bound by `x = factory(...)`. Each returned
    # closure of factory receives args, so a callback argument flows into that
    # closure's callable parameter. Resolved in finalize once every function's
    # returned callables are known.
    scope_qn: str
    factory_qn: str
    positional: tuple[str, ...]
    keyword: tuple[tuple[str, str], ...]


class _PassedCallback(NamedTuple):
    # A callback one call site hands to a callable parameter, resolved there.
    # Its edge is written in finalize at the receiver's own invocations of the
    # parameter, whose file the walk may not have reached yet (issue #2459).
    source_spec: tuple[str, str, str]
    param_name: str
    rel_type: str
    target_spec: tuple[str, str, str]
    resolution: str


class _FileCallableFlow:
    # One file's callable-flow records. Kept per file because the processor
    # outlives a pass: a re-walk must replace only that file's records, while
    # a file the pass leaves alone still seeds slots finalize resolves.
    __slots__ = ("args", "returned_callables", "factory_calls", "invocations", "passed")

    def __init__(self) -> None:
        self.args: list[_CallableFlowArg] = []
        self.returned_callables: dict[str, set[str]] = {}
        self.factory_calls: list[_FactoryCall] = []
        # (function, parameter) -> the edge site of each place this file's
        # function invokes that parameter: where a callback it receives runs.
        self.invocations: dict[tuple[str, str], tuple[PropertyDict, ...]] = {}
        self.passed: list[_PassedCallback] = []


class FlowHost(EdgeSiteHost, Protocol):
    """What callable-parameter flow calls on the call pass that builds it:
    the edge writer, and the verdict a resolved argument notes for the call
    node being scanned. Beside the edge site, whose resolution a passed
    callback records for its deferred edge."""

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _noted[R: tuple[str, str] | None](self, resolved: R) -> R: ...


class CallableFlow:
    """Records what each call site binds to a callee's callable parameters
    and writes the edges the bindings imply once every file is walked.

    Built and owned by `CallProcessor`, which outlives a pass: the records
    are kept per file, so a re-walk replaces only that file's."""

    __slots__ = (
        "_host",
        "_callbacks",
        "_resolver",
        "_param_names",
        "_files",
        "_bucket",
    )

    def __init__(
        self, host: FlowHost, callbacks: CallbackEdges, resolver: CallResolver
    ) -> None:
        self._host = host
        self._callbacks = callbacks
        self._resolver = resolver
        # Ordered params per function and the per-call-site argument
        # bindings, resolved to a fixpoint in finalize.
        self._param_names: dict[str, list[str]] = {}
        # Return-value / factory tracing rides along: functions each function
        # may return (nested closures), and call sites `x = factory(...); x(cb)`
        # where cb flows into the returned closure's callable parameter.
        # Resolved to a fixpoint in finalize, so factory and call site may be
        # in any order. Keyed by the file whose walk recorded them, and the
        # file being walked writes into `_bucket`.
        self._files: dict[Path, _FileCallableFlow] = {}
        self._bucket = _FileCallableFlow()

    def begin_file(self, file_path: Path) -> None:
        """Start a file's walk with empty records."""
        # A re-walk describes the file's current source; appending to the old
        # records kept a removed call site's callback edge alive forever.
        self._bucket = self._files[file_path] = _FileCallableFlow()

    def forget_file(self, file_path: Path) -> None:
        """Drop a deleted file's records."""
        self._files.pop(file_path, None)

    def reset(self) -> None:
        """Drop every file's records."""
        self._files.clear()

    def record_caller(
        self,
        caller_node: Node,
        caller_qn: str,
        params: list[str],
        module_qn: str,
        language: cs.SupportedLanguage,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        """Record a caller's parameters, where it invokes them, and the
        closures it may return."""
        # Every flow-traced language records its callable params and the
        # closures it returns, so a later `x = factory(); x(cb)` alias call can
        # flow cb into the returned closure regardless of source language.
        self._param_names[caller_qn] = params
        self._record_param_invocations(caller_node, caller_qn, language)
        self._collect_returned_callables(
            caller_node,
            caller_qn,
            module_qn,
            local_var_types,
            class_context,
            boundary_types,
        )

    def _collect_returned_callables(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
    ) -> None:
        # Record which functions/closures this function may return, so a call site
        # that binds and invokes the returned value (x = factory(); x(cb)) can flow
        # cb into the returned closure. Only this scope's own return statements
        # count; a nested function's returns belong to it.
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            if node.type == cs.TS_PY_RETURN_STATEMENT:
                for name in self._returned_names(node):
                    self._record_returned_callable(
                        name, caller_qn, module_qn, local_var_types, class_context
                    )
            stack.extend(node.children)

    @staticmethod
    def _returned_names(return_node: Node) -> list[str]:
        # The identifier / attribute names a return statement hands back.
        names: list[str] = []
        for returned in return_node.named_children:
            child = js_calls.unwrap_ts_cast(returned)
            if child.type in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE) and (
                name := safe_decode_text(child)
            ):
                names.append(name)
        return names

    def _record_returned_callable(
        self,
        name: str,
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        registry = self._resolver.function_registry
        nested_qn = f"{caller_qn}{cs.SEPARATOR_DOT}{name}"
        # A duplicated name (if/else twin definitions) returns whichever
        # branch ran, so record EVERY twin; recording one leaves the others
        # unreachable and falsely dead.
        if nested_qn in registry:
            target_qn = nested_qn
        else:
            resolved = self._resolver.resolve_function_call(
                name, module_qn, local_var_types, class_context, caller_qn
            )
            if resolved is None or resolved[0] not in _CALLABLE_NODE_LABELS:
                return
            target_qn = resolved[1]
        self._bucket.returned_callables.setdefault(caller_qn, set()).update(
            registry.variants(target_qn)
        )

    def build_factory_alias_map(
        self,
        caller_node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        boundary_types: frozenset[str],
        caller_qn: str | None = None,
    ) -> dict[str, str]:
        """Each local a caller binds to a factory's result, to the factory."""
        # Map a local `x` to the function `factory` in `x = factory(...)`, so a
        # later `x(cb)` can be traced through factory's returned closure. Handles
        # each flow language's binding form (Python assignment, JS/TS
        # variable_declarator, Go short_var_declaration, C++ init_declarator).
        resolve_func = self._resolver.resolve_function_call
        aliases: dict[str, str] = {}
        stack: list[Node] = list(caller_node.children)
        while stack:
            node = stack.pop()
            if node.type in boundary_types:
                continue
            for var, fn_name in self._factory_bindings(node):
                if (
                    resolved := resolve_func(
                        fn_name, module_qn, local_var_types, class_context, caller_qn
                    )
                ) is not None:
                    aliases.setdefault(var, resolved[1])
            stack.extend(node.children)
        return aliases

    def _factory_bindings(self, node: Node) -> list[tuple[str, str]]:
        # Yield (local_name, called_function_name) for a `x = f(...)` binding node.
        match node.type:
            case cs.TS_PY_ASSIGNMENT:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_LEFT, cs.TS_FIELD_RIGHT, cs.TS_PY_CALL
                )
            case cs.TS_VARIABLE_DECLARATOR:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_NAME, cs.FIELD_VALUE, cs.TS_CALL_EXPRESSION
                )
            case cs.CppNodeType.INIT_DECLARATOR:
                return self._simple_factory_binding(
                    node, cs.TS_FIELD_DECLARATOR, cs.FIELD_VALUE, cs.TS_CALL_EXPRESSION
                )
            case cs.TS_GO_SHORT_VAR_DECLARATION:
                return go_calls.factory_bindings(node)
            case _:
                return []

    @staticmethod
    def _simple_factory_binding(
        node: Node, name_field: str, value_field: str, call_type: str
    ) -> list[tuple[str, str]]:
        left = node.child_by_field_name(name_field)
        right = node.child_by_field_name(value_field)
        # `const x = factory(...) as T` wraps the call in a cast; unwrap so the
        # factory binding is still recognised.
        if right is not None:
            right = js_calls.unwrap_ts_cast(right)
        if (
            left is not None
            and left.type == cs.TS_PY_IDENTIFIER
            and right is not None
            and right.type == call_type
            and (var := safe_decode_text(left))
            and (fn := right.child_by_field_name(cs.TS_FIELD_FUNCTION)) is not None
            and (fn_name := safe_decode_text(fn))
        ):
            return [(var, fn_name)]
        return []

    def _resolve_callback_qn(
        self,
        node: Node,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        caller_qn: str | None = None,
    ) -> str | None:
        if node.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE):
            return None
        if not (text := safe_decode_text(node)):
            return None
        resolved = self._host._noted(
            self._resolver.resolve_function_call(
                text, module_qn, local_var_types, class_context, caller_qn
            )
        )
        if resolved is None or resolved[0] not in _CALLABLE_NODE_LABELS:
            return None
        return resolved[1]

    def record_factory_call(
        self,
        call_node: Node,
        scope_qn: str,
        factory_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        """Record the callbacks a call through a factory's result passes, for
        the closures the factory returns."""
        positional, keyword = parse_call_arguments(call_node)
        pos_qns = tuple(
            self._resolve_callback_qn(
                n, module_qn, local_var_types, class_context, scope_qn
            )
            or ""
            for n in positional
        )
        kw_qns = tuple(
            (name, qn)
            for name, value in keyword.items()
            if (
                qn := self._resolve_callback_qn(
                    value, module_qn, local_var_types, class_context, scope_qn
                )
            )
        )
        if any(pos_qns) or kw_qns:
            self._bucket.factory_calls.append(
                _FactoryCall(scope_qn, factory_qn, pos_qns, kw_qns)
            )

    def ingest_callable_param_calls(
        self,
        call_node: Node,
        callee_type: str,
        callee_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        resolve_func: Callable[..., tuple[str, str] | None],
        caller_qn: str | None = None,
    ) -> None:
        """Record the callbacks a call hands to the callable parameters its
        callee invokes."""
        if not (params := self._resolver.function_registry.callable_params(callee_qn)):
            return
        positional, keyword = parse_call_arguments(call_node)
        source_spec = (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn)
        for param_name, index in params.items():
            arg_node = keyword.get(param_name)
            if arg_node is None and index < len(positional):
                arg_node = positional[index]
            if arg_node is not None:
                self._callbacks.emit_callback_edge(
                    source_spec,
                    arg_node,
                    module_qn,
                    local_var_types,
                    class_context,
                    resolve_func,
                    self._passed_callback_recorder(param_name),
                    caller_qn,
                )

    def _passed_callback_recorder(self, param_name: str) -> Callable[..., None]:
        # Stands in for ensure_rel: the edge's source is the callee, which runs
        # the callback where it invokes `param_name`, so the edge waits for
        # finalize and that site instead of taking the argument's, a position
        # in this file rather than the callee's (issue #2459). The verdict is
        # the one the callback resolution set for this edge (#1543).
        passed = self._bucket.passed
        host = self._host

        def record(
            source_spec: tuple[str, str, str],
            rel_type: str,
            target_spec: tuple[str, str, str],
        ) -> None:
            passed.append(
                _PassedCallback(
                    source_spec, param_name, rel_type, target_spec, host._resolution
                )
            )

        return record

    def _record_param_invocations(
        self, caller_node: Node, caller_qn: str, language: cs.SupportedLanguage
    ) -> None:
        # Only a function the registry marked as invoking a parameter is ever
        # the source of a callable-parameter edge, so no other body is scanned
        # a second time.
        if not self._resolver.function_registry.callable_params(caller_qn):
            return
        invocations = callable_parameter_invocations(caller_node, language)
        for param_name, nodes in invocations.items():
            self._bucket.invocations[(caller_qn, param_name)] = tuple(
                {**node_site_properties(node), cs.KEY_VIA_PARAM: param_name}
                for node in nodes
            )

    def collect_callable_flow(
        self,
        call_node: Node,
        callee_qn: str,
        caller_qn: str,
        caller_params: frozenset[str],
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
    ) -> None:
        """Record what each argument of a call to a first-party callee binds
        to that callee's parameters."""
        # Record, for each call-site argument that names a callable, whether it is a
        # concrete function or a parameter of the caller (a pass-through). The
        # fixpoint in finalize propagates concretes through pass-through params to
        # the functions that actually invoke them.
        positional, keyword = parse_call_arguments(call_node)
        items: list[tuple[int, str, Node]] = [
            (index, "", node) for index, node in enumerate(positional)
        ]
        items.extend((-1, name, node) for name, node in keyword.items())
        callable_labels = (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
            cs.NodeLabel.CLASS,
        )
        for position, keyword_name, raw_arg in items:
            # `f(callback as any)` casts the argument; unwrap so a cast callable
            # argument still flows.
            # `outer(handler.bind(this))` forwards the bound handler through
            # a pass-through param; peel .bind like the direct-argument path
            # or the propagated flow edge is lost.
            arg_node = js_calls.peel_bound_callable(raw_arg)
            if arg_node.type not in FLOW_ARG_REF_TYPES:
                continue
            arg_text = safe_decode_text(arg_node)
            if not arg_text:
                continue
            if arg_node.type == cs.TS_PY_IDENTIFIER and arg_text in caller_params:
                self._bucket.args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, "", caller_qn, arg_text
                    )
                )
                continue
            resolved = self._resolver.resolve_function_call(
                arg_text, module_qn, local_var_types, class_context, caller_qn
            )
            if resolved is not None and resolved[0] in callable_labels:
                self._bucket.args.append(
                    _CallableFlowArg(
                        callee_qn, position, keyword_name, resolved[1], "", ""
                    )
                )

    @host_site_scoped
    def finalize(self) -> None:
        """Resolve the recorded bindings to a fixpoint and write the edges
        they imply."""
        # Resolve the recorded call-site argument bindings to a fixpoint and emit a
        # CALLS edge from every function that invokes a callable parameter to each
        # concrete function that can reach it (directly or via pass-through params).
        # No walk is running, so an edge with no recorded site carries none rather
        # than the last node a walk left behind (Go's 1:0-7, issue #2459).
        self._host._site_node = None
        returned_callables = self._merged_returned_callables()
        seeds, edges = self._callable_flow_slots()
        self._emit_returned_closure_calls(returned_callables)
        self._emit_factory_closure_calls(returned_callables, seeds)
        bindings = self._propagate_flow_bindings(seeds, edges)
        invocations = self._merged_param_invocations()
        self._emit_callable_param_calls(bindings, invocations)
        self._emit_passed_callbacks(invocations)

    def _merged_param_invocations(
        self,
    ) -> dict[tuple[str, str], tuple[PropertyDict, ...]]:
        merged: dict[tuple[str, str], tuple[PropertyDict, ...]] = {}
        for flow in self._files.values():
            merged.update(flow.invocations)
        return merged

    @staticmethod
    def _param_invocation_sites(
        invocations: dict[tuple[str, str], tuple[PropertyDict, ...]],
        func_qn: str,
        param_name: str,
    ) -> tuple[PropertyDict, ...]:
        # A receiver whose walk recorded no invocation still invokes the
        # parameter (the registry says so): keep the edge, without a site and
        # so without `via_param`, since a site-less row merges on its endpoints
        # alone and would stamp it on a direct call joining the same pair.
        return invocations.get((func_qn, param_name)) or ({},)

    def _emit_passed_callbacks(
        self, invocations: dict[tuple[str, str], tuple[PropertyDict, ...]]
    ) -> None:
        # After the propagated bindings, so where both reach one site the
        # direct argument's own verdict is the one that stays (#1543).
        registry = self._resolver.function_registry
        file_flows = self._files.values()
        for passed in (p for flow in file_flows for p in flow.passed):
            source_qn = passed.source_spec[2]
            # A record outlives the walk that made it, and either end may have
            # been deleted by a later pass that did not re-walk this file.
            invoked = registry.callable_params(source_qn)
            if (
                not invoked
                or passed.param_name not in invoked
                or passed.target_spec[2] not in registry
            ):
                continue
            self._host._resolution = passed.resolution
            for site in self._param_invocation_sites(
                invocations, source_qn, passed.param_name
            ):
                self._host._emit_rel(
                    passed.source_spec, passed.rel_type, passed.target_spec, site
                )

    def _merged_returned_callables(self) -> dict[str, set[str]]:
        returned_callables: dict[str, set[str]] = defaultdict(set)
        for flow in self._files.values():
            for producer_qn, returned in flow.returned_callables.items():
                returned_callables[producer_qn] |= returned
        return returned_callables

    def _callable_flow_slots(
        self,
    ) -> tuple[
        defaultdict[tuple[str, str], set[str]],
        defaultdict[tuple[str, str], set[tuple[str, str]]],
    ]:
        # Each recorded argument keyed to its callee parameter slot: a concrete
        # function seeds the slot, a pass-through param links it to the caller's.
        seeds: defaultdict[tuple[str, str], set[str]] = defaultdict(set)
        edges: defaultdict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        file_flows = self._files.values()
        for arg in (arg for flow in file_flows for arg in flow.args):
            if arg.keyword:
                param_name = arg.keyword
            else:
                callee_params = self._param_names.get(arg.callee_qn)
                if callee_params is None or not (
                    0 <= arg.position < len(callee_params)
                ):
                    continue
                param_name = callee_params[arg.position]
            slot = (arg.callee_qn, param_name)
            if arg.source_concrete:
                seeds[slot].add(arg.source_concrete)
            else:
                edges[slot].add((arg.source_caller, arg.source_param))
        return seeds, edges

    def _emit_returned_closure_calls(
        self, returned_callables: dict[str, set[str]]
    ) -> None:
        registry = self._resolver.function_registry
        ensure_rel = self._host._emit_rel
        # A nested closure a function returns is reachable whenever that function
        # is reached (created and handed back as the return value). Nested
        # functions are no longer roots, so this producer edge keeps a genuinely
        # used closure (a returned decorator/formatter) live without reviving the
        # closures of an unreachable outer function.
        for producer_qn, returned in returned_callables.items():
            producer_type = registry.get(producer_qn)
            if producer_type is None:
                continue
            prefix = f"{producer_qn}{cs.SEPARATOR_DOT}"
            producer_spec = (producer_type, cs.KEY_QUALIFIED_NAME, producer_qn)
            for closure_qn in returned:
                if not closure_qn.startswith(prefix):
                    continue
                closure_type = registry.get(closure_qn)
                if closure_type is None:
                    continue
                ensure_rel(
                    producer_spec,
                    cs.RelationshipType.CALLS,
                    (closure_type, cs.KEY_QUALIFIED_NAME, closure_qn),
                )

    def _emit_factory_closure_calls(
        self,
        returned_callables: dict[str, set[str]],
        seeds: defaultdict[tuple[str, str], set[str]],
    ) -> None:
        file_flows = self._files.values()
        for fc in (fc for flow in file_flows for fc in flow.factory_calls):
            for closure_qn in returned_callables.get(fc.factory_qn, ()):
                self._emit_factory_closure_call(fc, closure_qn, seeds)

    def _emit_factory_closure_call(
        self,
        fc: _FactoryCall,
        closure_qn: str,
        seeds: defaultdict[tuple[str, str], set[str]],
    ) -> None:
        registry = self._resolver.function_registry
        # The returned closure runs when the alias is called, so it is
        # reachable from the enclosing scope.
        closure_type = registry.get(closure_qn)
        if closure_type is None:
            return
        scope_type = registry.get(fc.scope_qn) or cs.NodeLabel.MODULE
        self._host._emit_rel(
            (scope_type, cs.KEY_QUALIFIED_NAME, fc.scope_qn),
            cs.RelationshipType.CALLS,
            (closure_type, cs.KEY_QUALIFIED_NAME, closure_qn),
        )
        # Each argument the closure receives seeds its callable parameter,
        # so the callback is reached wherever the closure invokes it.
        closure_params = self._param_names.get(closure_qn)
        for index, callback_qn in enumerate(fc.positional):
            if (
                callback_qn
                and closure_params is not None
                and index < len(closure_params)
            ):
                seeds[(closure_qn, closure_params[index])].add(callback_qn)
        for keyword_name, callback_qn in fc.keyword:
            seeds[(closure_qn, keyword_name)].add(callback_qn)

    @staticmethod
    def _propagate_flow_bindings(
        seeds: defaultdict[tuple[str, str], set[str]],
        edges: defaultdict[tuple[str, str], set[tuple[str, str]]],
    ) -> dict[tuple[str, str], set[str]]:
        bindings: dict[tuple[str, str], set[str]] = {
            k: set(v) for k, v in seeds.items()
        }
        for slot in edges:
            bindings.setdefault(slot, set())
        changed = True
        while changed:
            changed = False
            for slot, sources in edges.items():
                for source in sources:
                    if (reachable := bindings.get(source)) and not reachable.issubset(
                        bindings[slot]
                    ):
                        bindings[slot] |= reachable
                        changed = True
        return bindings

    def _emit_callable_param_calls(
        self,
        bindings: dict[tuple[str, str], set[str]],
        invocations: dict[tuple[str, str], tuple[PropertyDict, ...]],
    ) -> None:
        registry = self._resolver.function_registry
        for func_qn, invoked in (
            (qn, registry.callable_params(qn)) for qn in self._param_names
        ):
            if not invoked or (func_type := registry.get(func_qn)) is None:
                continue
            source_spec = (func_type, cs.KEY_QUALIFIED_NAME, func_qn)
            for param_name in invoked:
                self._emit_bound_target_calls(
                    source_spec,
                    bindings.get((func_qn, param_name), ()),
                    self._param_invocation_sites(invocations, func_qn, param_name),
                )

    def _emit_bound_target_calls(
        self,
        source_spec: tuple[str, str, str],
        target_qns: set[str] | tuple[()],
        sites: tuple[PropertyDict, ...],
    ) -> None:
        registry = self._resolver.function_registry
        for target_qn in target_qns:
            target_type = registry.get(target_qn)
            if target_type is None:
                continue
            for variant in registry.variants(target_qn):
                for site in sites:
                    self._host._emit_rel(
                        source_spec,
                        cs.RelationshipType.CALLS,
                        (target_type, cs.KEY_QUALIFIED_NAME, variant),
                        site,
                    )
