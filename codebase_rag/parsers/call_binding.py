"""The steps one call node goes through, from its name to its edges.

`CallProcessor` hands every call node of a caller's scan, with the `CallScan`
that caller built, to the `CallBinding` it owns. A node is first offered to
its language's own shapes, then named; a named call binds its callee through
the language's strategy, the name-based resolver and the fallbacks after
them (the other namespace's twin, a builtin, an operator overload, a local
alias, a factory's returned closure). An unbound call keeps its name for the
file that may define it and references the functions it passes; a bound one
writes its CALLS edge to each target the callee's variants and the caller's
builds admit, the construction a class callee runs, and the fan-outs its
language dispatches. The binding calls back through `BindingHost` for the
edge site, the verdict a callee binds with, and the record of the names a
module could not resolve."""

from typing import Protocol

from tree_sitter import Node

from .. import constants as cs
from .call_callbacks import CallbackEdges
from .call_construction import CallConstruction
from .call_host import VerdictHost
from .call_nodes import call_target_name
from .call_resolver import CallResolver
from .callable_flow import CallableFlow
from .definition_facts import DefinitionFacts
from .edge_site import EdgeSiteHost
from .import_host import UnresolvedNameHost
from .language_calls import CallScan
from .scope_boundaries import flow_scope_boundaries
from .string_call import StringCallSpec, string_call_target
from .utils import written_simple_name

# Nodes walked down a call's left spine before giving up on counting calls:
# a long operator chain (`(a + b + ... ).m()`) is not a receiver chain.
_CHAIN_SPINE_WALK_LIMIT = cs.MAX_RECEIVER_CHAIN_HOPS * 8
# A Java/C# object creation: its call name is the constructed TYPE, so the
# resolver must not offer a same-named method or function (issue #1629).
_OBJECT_CREATION_NODE_TYPES = frozenset(
    {
        cs.TS_OBJECT_CREATION_EXPRESSION,
        cs.TS_CSHARP_IMPLICIT_OBJECT_CREATION_EXPRESSION,
        cs.TS_SCALA_INSTANCE_EXPRESSION,
    }
)
# Qualified-name prefix marking a resolved callee as a builtin rather than a
# first-party function whose body the call chain can be followed into.
_BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"
_MACRO_INVOCATION_TYPES = (
    cs.TS_RS_MACRO_INVOCATION,
    cs.TS_JULIA_MACROCALL_EXPRESSION,
)


def _exceeds_receiver_chain_cap(call_node: Node) -> bool:
    """Whether `call_node` ends a receiver chain of too many calls (#2262).

    Every call in an n-hop chain (`a.m().m()...`) is resolved on its own
    text, which holds the whole receiver, so each pass over it made one long
    chain O(n^2). Walking the leftmost spine and counting nodes of the
    call's own type measures the chain in O(cap) without reading its text.
    """
    call_type = call_node.type
    calls = 0
    node = call_node
    for _ in range(_CHAIN_SPINE_WALK_LIMIT):
        # `child(0)` raises on a leaf rather than returning None.
        if node.child_count == 0:
            return False
        node = node.child(0)
        if node is None:
            return False
        if node.type == call_type:
            calls += 1
            if calls >= cs.MAX_RECEIVER_CHAIN_HOPS:
                return True
    return False


class BindingHost(EdgeSiteHost, VerdictHost, UnresolvedNameHost, Protocol):
    """What a call node's binding calls on the call pass that builds it: the
    record of a name a module could not resolve. Beside the edge site, which
    the binding sets to the node it scans, and the verdict, reset per node
    and read when its edges are emitted."""


class CallBinding:
    """Binds a scanned call node's callee and writes its edges.

    Built and owned by `CallProcessor`, with the collaborators a bound call
    hands its arguments, its construction and its callable-parameter flow
    to."""

    __slots__ = (
        "_host",
        "_resolver",
        "_facts",
        "_callbacks",
        "_flow",
        "_construction",
        "_string_call_specs",
    )

    def __init__(
        self,
        host: BindingHost,
        resolver: CallResolver,
        facts: DefinitionFacts,
        callbacks: CallbackEdges,
        flow: CallableFlow,
        construction: CallConstruction,
        string_call_specs: tuple[StringCallSpec, ...],
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._facts = facts
        self._callbacks = callbacks
        self._flow = flow
        self._construction = construction
        # Dispatchers that name their callee in a string argument, declared in
        # the repository's .cgr.toml. Empty unless the project opts in.
        self._string_call_specs = string_call_specs

    def ingest_call_node(self, ctx: CallScan, call_node: Node) -> None:
        """Bind one call node of a caller's scan and write its edges."""
        if _exceeds_receiver_chain_cap(call_node):
            # Left unresolved: resolving it would re-read the whole chain.
            return
        host = self._host
        host._site_node = call_node
        host._resolution = cs.EdgeResolution.EXACT
        # A callee bound by a language frontend (Jedi, the Java and C#
        # engines, the Go frontend, a typed C++ operator) never passes
        # through `noted`, which is where a resolver answer sets the
        # verdict; without this reset such an edge would inherit the
        # label the previous call node left behind (issue #1526).
        host.verdict = cs.EdgeResolution.EXACT
        if ctx.calls.ingest_call_shape(ctx, call_node):
            return
        call_name = self._scan_call_name(ctx, call_node)
        # A declared dispatcher (`callSp('usp_x')`) names its real callee in
        # a string, which no parser resolves as a call: without this the
        # edge stops at the dispatcher and the routine looks unreachable.
        # The dispatcher's own edge still emits below; this only ADDS the
        # one the syntax hides.
        if self._string_call_specs and call_name:
            self._emit_string_call_edges(ctx, call_node, call_name)
        if ctx.calls.ingest_functor_call(ctx, call_node):
            return
        # What the call site hands over whether or not its callee binds goes
        # BEFORE the no-name bail (a cast-wrapped callee yields no call name).
        call_name = ctx.calls.ingest_call_site(ctx, call_node, call_name)
        if not call_name:
            # A callee that is itself a call (`wraps(view_func)(_view_wrapper)`)
            # or otherwise yields no name still consumes its arguments through
            # whatever callable it produced; reference first-party functions
            # passed to it or dead-code flags every django-style view
            # decorator wrapper. REFERENCES (not arg_ref_rel) is
            # deliberate and predates the C# split: with no callee name
            # there is no invocation to assert, for ANY language.
            if ctx.calls.references_arguments:
                self._ingest_argument_references(
                    ctx, call_node, cs.RelationshipType.REFERENCES
                )
            return
        self._ingest_named_call(ctx, call_node, call_name)

    def _ingest_argument_references(
        self, ctx: CallScan, call_node: Node, rel_type: cs.RelationshipType
    ) -> None:
        # The functions a call passes as arguments, referenced from the
        # caller in its own scope.
        self._callbacks.ingest_argument_function_references(
            call_node,
            ctx.caller_spec,
            ctx.module_qn,
            ctx.local_var_types,
            ctx.class_context,
            self._resolver.resolve_function_call,
            ctx.ensure_rel,
            ctx.caller_qn,
            rel_type,
            ctx.language,
        )

    def _scan_call_name(self, ctx: CallScan, call_node: Node) -> str | None:
        node_id = id(call_node)
        if ctx.call_name_cache is not None and node_id in ctx.call_name_cache:
            call_name = ctx.call_name_cache[node_id]
        else:
            call_name = call_target_name(call_node, ctx.calls)
            if call_name is None:
                call_name = ctx.calls.fallback_call_name(ctx, call_node)
            if ctx.call_name_cache is not None:
                ctx.call_name_cache[node_id] = call_name
        return call_name

    def _emit_string_call_edges(
        self, ctx: CallScan, call_node: Node, call_name: str
    ) -> None:
        target_name = string_call_target(call_node, call_name, self._string_call_specs)
        if target_name:
            registry = self._resolver.function_registry
            # A qualified target (`app.usp_x`) matches through the
            # dotted-suffix scan (#513) and so reaches ONLY routines
            # carrying that schema in their FQN; an unqualified one
            # goes through the last-segment index and legitimately
            # fans onto every schema's candidate, mirroring what a
            # runtime search_path could pick.
            for target_qn in registry.find_ending_with(target_name):
                # find_ending_with indexes classes too; only a
                # callable takes the edge, under its OWN label — a
                # mislabeled edge is a phantom the database drops.
                target_type = registry.get(target_qn)
                if target_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
                    continue
                target_label = (
                    cs.NodeLabel.METHOD
                    if target_type == cs.NodeLabel.METHOD
                    else cs.NodeLabel.FUNCTION
                )
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (target_label, cs.KEY_QUALIFIED_NAME, target_qn),
                )

    def _ingest_named_call(
        self, ctx: CallScan, call_node: Node, call_name: str
    ) -> None:
        call_var_types = ctx.local_var_types
        if ctx.span_bindings:
            call_var_types = self._overlay_span_binding(
                call_name, call_node, ctx.local_var_types, ctx.span_bindings
            )
        language_calls = ctx.calls
        call_var_types = language_calls.receiver_types(
            ctx, call_node, call_name, call_var_types
        )
        if language_calls.ingest_own_call(ctx, call_node, call_name, call_var_types):
            return
        callee_info = self._resolve_call_callee(
            ctx, call_node, call_name, call_var_types
        )
        callee_info = self._fallback_callee(ctx, call_node, call_name, callee_info)
        language_calls.ingest_indirect_calls(ctx, call_node, call_name, callee_info)

        if not callee_info:
            self._ingest_unresolved_call(ctx, call_node, call_name)
            return
        self.ingest_resolved_call(ctx, call_node, call_name, callee_info)

    @staticmethod
    def _overlay_span_binding(
        call_name: str,
        call_node: Node,
        local_var_types: dict[str, str] | None,
        span_bindings: list[tuple[int, int, str, str]],
    ) -> dict[str, str] | None:
        # If the call's receiver base is a span-scoped binding (match-arm
        # variant, adaptor-closure parameter) whose range contains this call,
        # overlay that binding's type, shadowing the flat map's value. The
        # innermost (smallest) containing span wins for nested scopes.
        if local_var_types is None or cs.SEPARATOR_DOT not in call_name:
            return local_var_types
        base = call_name.split(cs.SEPARATOR_DOT, 1)[0]
        pos = call_node.start_byte
        best: tuple[int, str] | None = None
        for start, end, name, bound_type in span_bindings:
            if name == base and start <= pos < end:
                span = end - start
                if best is None or span < best[0]:
                    best = (span, bound_type)
        if best is None or local_var_types.get(base) == best[1]:
            return local_var_types
        if best[1] == cs.GO_UNTYPED_BINDING:
            # A Go local nothing can type: the name is untyped here, not the
            # type an outer declaration of it has (issue #2483).
            return {
                name: type_name
                for name, type_name in local_var_types.items()
                if name != base
            }
        return {**local_var_types, base: best[1]}

    def _resolve_call_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        call_var_types: dict[str, str] | None,
    ) -> tuple[str, str] | None:
        if (
            own := ctx.calls.resolve_callee(ctx, call_node, call_name, call_var_types)
        ).decided:
            return own.callee
        callee_info = ctx.resolve_func(
            call_name,
            ctx.module_qn,
            call_var_types,
            ctx.class_context,
            ctx.caller_qn,
            ctx.language,
            call_point=call_node.start_byte,
            # A Java/C#/Scala `new X(...)` names a type, never a method.
            constructing=ctx.calls.new_constructs_type
            and call_node.type in _OBJECT_CREATION_NODE_TYPES,
        )
        return ctx.calls.judge_callee(ctx, call_node, call_name, callee_info)

    def _fallback_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        if callee_info and ctx.calls.separate_macro_namespace:
            # Where macros and functions live in SEPARATE namespaces (Rust,
            # Julia), a macro invocation must not bind a same-named fn and
            # vice versa.
            is_macro_target = callee_info[1] in self._facts.macro_qns
            is_macro_invocation = call_node.type in _MACRO_INVOCATION_TYPES
            if is_macro_target != is_macro_invocation:
                # Swap to the other namespace's twin when the bucket
                # holds exactly one candidate there (issue #1882
                # review).
                sibling = self._sibling_qn_in_macro_namespace(
                    callee_info[1], is_macro_invocation
                )
                if sibling is None:
                    callee_info = None
                else:
                    callee_info = (callee_info[0], sibling)
        if not callee_info and ctx.resolve_builtin is not None:
            callee_info = ctx.resolve_builtin(call_name)
        if not callee_info and ctx.resolve_cpp_op is not None:
            callee_info = ctx.resolve_cpp_op(call_name, ctx.module_qn)
        if not callee_info:
            callee_info = ctx.calls.local_alias_callee(ctx, call_node, call_name)
        if not callee_info and cs.SEPARATOR_DOT not in call_name:
            callee_info = self._bare_name_callee(ctx, call_node, call_name, callee_info)
        return callee_info

    def _sibling_qn_in_macro_namespace(self, qn: str, want_macro: bool) -> str | None:
        # A same-named function/macro pair collides on one natural name;
        # the needed twin is the other bucket member.
        natural = qn.rsplit(cs.DUP_QN_MARKER, 1)[0] if cs.DUP_QN_MARKER in qn else qn
        registry = self._resolver.function_registry
        bucket = registry.variants(natural)
        if len(bucket) < 2:
            return None
        matches = [
            candidate
            for candidate in bucket
            if registry.get(candidate) is not None
            and (candidate in self._facts.macro_qns) == want_macro
        ]
        return matches[0] if len(matches) == 1 else None

    def _bare_name_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str] | None,
    ) -> tuple[str, str] | None:
        if callee_info is None and ctx.calls.flows_callables:
            # `x = factory(...); x(cb)`: x holds a closure returned by a
            # first-party factory (e.g. a retry/cache decorator applied
            # imperatively). Record the call so cb flows into that closure's
            # callable parameter once factory returns are known (finalize).
            if ctx.factory_aliases is None:
                ctx.factory_aliases = self._flow.build_factory_alias_map(
                    ctx.caller_node,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                    flow_scope_boundaries(ctx.queries[ctx.language][cs.QUERY_CONFIG]),
                    ctx.caller_qn,
                )
            if (factory_qn := ctx.factory_aliases.get(call_name)) is not None:
                self._flow.record_factory_call(
                    call_node,
                    ctx.caller_qn,
                    factory_qn,
                    ctx.module_qn,
                    ctx.local_var_types,
                    ctx.class_context,
                )
        return callee_info

    def _ingest_unresolved_call(
        self, ctx: CallScan, call_node: Node, call_name: str
    ) -> None:
        if call_name and not ctx.calls.imports_bind_calls:
            # The callee may be defined by a file added later; keep
            # its simple name so that file's arrival re-parses this
            # one (issue #1568). Not where an import would be needed
            # anyway: the importer lookup finds those waiters.
            self._host.note_unresolved(ctx.module_qn, written_simple_name(call_name))
        ctx.calls.ingest_unresolved_call(ctx, call_node, call_name)
        if ctx.calls.references_arguments:
            # The callee is not first-party (a framework/stdlib call such as
            # grpclib Handler(self.__rpc_x), JS setTimeout(target), or a
            # runtime dispatcher), so the call chain cannot be followed into
            # it. A first-party function handed to it as an argument is still
            # invoked, so record it as referenced from this scope to keep it
            # reachable, across every flow-traced language.
            self._ingest_argument_references(ctx, call_node, ctx.arg_ref_rel)

    def ingest_resolved_call(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_info: tuple[str, str],
    ) -> None:
        """Write the edges of a call whose callee bound."""
        callee_type, callee_qn = callee_info
        ctx.calls.record_cross_module_use(ctx.module_qn, callee_qn)
        callee_type, callee_qn = ctx.calls.bound_callee(
            ctx, call_node, call_name, callee_type, callee_qn
        )

        # A callee that resolved to a builtin (e.g. JS setTimeout(target))
        # has no first-party body to follow into, so pass-through flow is
        # pointless; but a first-party callback handed to it is still invoked,
        # so record a reference edge from this scope to keep it reachable.
        # The synthetic builtin.* qn never has a node, so emitting a CALLS
        # edge to it would only mint a phantom the database drops (issue
        # #652: 485 across the fixture suite) -- mirror the C++ builtin
        # operator rule and emit no edge at all.
        callee_is_builtin = callee_qn.startswith(_BUILTIN_QN_PREFIX)
        if callee_is_builtin:
            if ctx.calls.references_arguments:
                self._ingest_argument_references(ctx, call_node, ctx.arg_ref_rel)
            return

        if ctx.calls.flows_callables:
            self._flow.collect_callable_flow(
                call_node,
                callee_qn,
                ctx.caller_qn,
                ctx.caller_params,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
            )
        if ctx.calls.references_arguments:
            # Functions are first-class values: a first-party callee may STORE
            # a passed callback for later dynamic dispatch (config objects,
            # codecs, registries), which callable-param flow cannot trace, or
            # the callee may be a same-named misbind of an external method.
            # Record the pass itself as a REFERENCES edge from the passing
            # scope so the callback is never reported dead.
            self._ingest_argument_references(
                ctx, call_node, cs.RelationshipType.REFERENCES
            )
        self._ingest_first_party_call(ctx, call_node, call_name, callee_type, callee_qn)

    def _ingest_first_party_call(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> None:
        if (
            ctx.calls.dispatches_protocol_stubs
            # A stub the name-only fallback merely guessed (a dict literal's
            # `.get(...)`) says nothing about the receiver: fanning it out
            # turned one guess into exact edges to every conformer, test
            # doubles included (issue #2931). It stays one heuristic edge.
            and self._host.verdict != cs.EdgeResolution.HEURISTIC
            and (
                dispatch_targets := self._resolver.python.protocol_dispatch_targets(
                    callee_qn
                )
            )
        ):
            # The call resolved to a Protocol stub; the stub never runs, so emit
            # edges to the method on every conformer instead of the stub.
            ctx.calls.emit_protocol_conformer_edges(
                ctx.caller_spec, dispatch_targets, ctx.ensure_rel
            )
            return

        ctx.calls.ingest_receiver_dispatch(ctx, call_node, call_name, callee_qn)

        if ctx.calls.flows_callables:
            # f(...) invoked through a parameter: the edge runs from the
            # callee to whatever each call site binds to that parameter.
            self._flow.ingest_callable_param_calls(
                call_node,
                callee_type,
                callee_qn,
                ctx.module_qn,
                ctx.local_var_types,
                ctx.class_context,
                self._resolver.resolve_function_call,
                ctx.caller_qn,
            )

        if (
            ctx.calls.new_constructs_type
            and call_node.type in _OBJECT_CREATION_NODE_TYPES
            and callee_type != cs.NodeLabel.CLASS
        ):
            # `new X(...)` where X resolves to a non-class: a Java interface
            # or annotation implemented by an anonymous class (`new
            # Comparator<T>(){ ... }`), or a C# type the resolver could not bind
            # to a first-party class. There is no first-party constructor to
            # call, and a bare CALLS edge to a non-callable node (Interface) is
            # not valid, so drop the edge rather than emit it.
            return

        if callee_type == cs.NodeLabel.CLASS:
            # Record construction as INSTANTIATES -> the class node (keeps
            # CALLS function/method-only). When the class defines __init__,
            # ALSO redirect a CALLS edge to it (the constructor runs); when
            # it does not (dataclass/NamedTuple/pydantic), INSTANTIATES is
            # the only edge.
            redirected = self._construction.redirect_construction(ctx, callee_qn)
            if redirected is None:
                return
            callee_type, callee_qn = redirected

        if not self._emit_resolved_callee_targets(
            ctx, call_node, call_name, callee_type, callee_qn
        ):
            return
        self._emit_callee_fanouts(ctx, call_name, callee_type, callee_qn)

    def _emit_resolved_callee_targets(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> bool:
        resolver = self._resolver
        host = self._host
        callees = (
            resolver.member_alias_fanout(
                callee_qn, call_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            )
            if ctx.calls.fans_out_member_aliases and cs.SEPARATOR_DOT in call_name
            else [callee_qn]
        )
        targets = resolver.lexical_call_targets(
            call_name,
            ctx.module_qn,
            [
                variant
                for callee in callees
                for variant in resolver.function_registry.variants(callee)
            ],
            ctx.caller_qn,
        )
        if ctx.calls.separate_macro_namespace:
            # Filter the fan-out by the macro registry: an invocation
            # binds only macros, a plain call only non-macros.
            is_macro_invocation = call_node.type in _MACRO_INVOCATION_TYPES
            targets = [
                t
                for t in targets
                if (t in self._facts.macro_qns) == is_macro_invocation
            ]
            if not targets:
                return False
        if len(
            targets
        ) > 1 and not resolver.import_processor.rust.rust_block_item_qns.isdisjoint(
            targets
        ):
            # The dedup bucket holds a Rust block-local item beside an
            # item of another scope. Those are different functions
            # sharing a natural name, not spellings of one callee, and
            # the span-gated probe already chose between them, so the
            # hedge that fans an ambiguous callee onto its twins would
            # cross the block boundary here (issue #1061).
            targets = [callee_qn]
        # The resolver's verdict on this callee, then one edge per
        # same-named candidate when the callee fans out (issue #1526).
        host._resolution = host.verdict
        targets, exact_in_every_build = ctx.calls.build_targets(
            ctx.module_qn, callee_type, callee_qn, targets
        )
        if len(targets) > 1:
            host._resolution = cs.EdgeResolution.OVERLOAD
        call_resolution = host._resolution
        for target_qn in targets:
            host._resolution = (
                call_resolution
                if exact_in_every_build.get(target_qn, True)
                else cs.EdgeResolution.HEURISTIC
            )
            self._emit_callee_target(ctx, callee_type, target_qn)
        host._resolution = call_resolution
        return True

    def _emit_callee_target(
        self, ctx: CallScan, callee_type: str, target_qn: str
    ) -> None:
        # A duplicate-suffixed variant may be a DIFFERENT kind of node (a TS
        # namespace merged onto a function registers as a class); only
        # callable variants take a CALLS edge, and emitting the primary's
        # label onto a differently-typed node is a phantom the database drops
        # (issue #652). An unregistered target keeps its edge
        # (resolver-derived callees like an unwrapped fn.call base may not
        # register).
        target_type = self._resolver.function_registry.get(target_qn)
        if target_type is not None and target_type not in (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
        ):
            ctx.calls.emit_binding_twin(
                ctx.caller_spec, target_qn, target_type, ctx.ensure_rel
            )
            return
        # A callable variant takes its OWN label: a Scala method's local def
        # or anonymous-object member registers as a Function beside the
        # class's same-named Method, and the primary's label on the other is
        # the same phantom (issue #2848).
        label = callee_type if target_type is None else target_type
        ctx.ensure_rel(
            ctx.caller_spec,
            cs.RelationshipType.CALLS,
            (label, cs.KEY_QUALIFIED_NAME, target_qn),
        )

    def _emit_callee_fanouts(
        self,
        ctx: CallScan,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> None:
        if (
            not ctx.calls.emit_dispatch_fanouts(ctx, callee_type, callee_qn)
            and callee_type == cs.NodeLabel.METHOD
        ):
            self._emit_interface_sole_impls(ctx, callee_qn)
        ctx.calls.emit_callee_twins(ctx, call_name, callee_type, callee_qn)

    def _emit_interface_sole_impls(self, ctx: CallScan, callee_qn: str) -> None:
        # A call bound to an interface/trait method (the static callee;
        # removing its declaration breaks the call) with exactly ONE
        # implementer also runs the concrete method, so edge both. The
        # old REPLACING redirect orphaned the interface stub (gson's
        # FieldNamingStrategy.translateName reported dead); sorted():
        # the target label is a hash-randomized StrEnum.
        for impl_type, impl_qn in sorted(
            self._resolver.interface_sole_impl_targets(callee_qn)
        ):
            for variant in self._resolver.function_registry.variants(impl_qn):
                ctx.ensure_rel(
                    ctx.caller_spec,
                    cs.RelationshipType.CALLS,
                    (impl_type, cs.KEY_QUALIFIED_NAME, variant),
                )
