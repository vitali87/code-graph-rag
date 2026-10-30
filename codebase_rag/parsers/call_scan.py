"""A caller's calls: the scan the call pass runs over one function, method
or module.

`CallProcessor._ingest_function_calls` hands every caller to the
`CallerCalls` it owns. The scan types the caller's locals, runs the side
passes that read a caller (I/O access, Go RPC exposure, data flow), records
the callable parameters it may invoke, writes the property reads and the
references its language finds in it, and then binds each of its call nodes
through the `CallBinding` under one `CallScan`. It calls back through
`CallScanHost` for a language's call strategy, the resolver call that notes
the verdict of the call node being bound, the edge writer, and the record
of a caller whose local types could not be built."""

from collections.abc import Mapping
from typing import Any, Protocol

from tree_sitter import Node, QueryCursor

from .. import constants as cs
from .. import logs as ls
from ..node_text import sorted_captures
from ..types_defs import LanguageQueries, PropertyDict
from .call_binding import CallBinding
from .call_references import flow_scope_boundaries
from .call_resolver import CallResolver, Resolved
from .callable_flow import CallableFlow
from .cpp import calls as cpp_calls
from .edge_site import EdgeSiteHost
from .flow_access import FlowProcessor
from .io_access import IOAccessProcessor
from .language_calls import CallScan, LanguageCalls
from .rpc_exposure import GoRpcExposureProcessor


class CallScanHost(EdgeSiteHost, Protocol):
    """What a caller's scan calls on the call pass that owns it: a
    language's call strategy, the resolver call that notes its verdict, the
    edge writer and the record of a failed caller."""

    def _calls_for(self, language: str | None) -> LanguageCalls: ...

    def _emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def _resolve_noting(self, *args: Any, **kwargs: Any) -> Resolved | None: ...

    def _record_caller_failure(
        self, message: str, caller: str, error: Exception
    ) -> None: ...


class CallerCalls:
    """Scans one caller: its locals, side passes, references and calls.

    Built and owned by `CallProcessor`."""

    __slots__ = (
        "_host",
        "_resolver",
        "_flow",
        "_binding",
        "_io_access",
        "_rpc_exposure",
        "_flow_access",
    )

    def __init__(
        self,
        host: CallScanHost,
        resolver: CallResolver,
        flow: CallableFlow,
        binding: CallBinding,
        *,
        io_access: IOAccessProcessor,
        rpc_exposure: GoRpcExposureProcessor,
        flow_access: FlowProcessor,
    ) -> None:
        self._host = host
        self._resolver = resolver
        self._flow = flow
        self._binding = binding
        self._io_access = io_access
        self._rpc_exposure = rpc_exposure
        self._flow_access = flow_access

    def ingest(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        call_nodes: list[Node] | None,
        call_name_cache: dict[int, str | None] | None,
    ) -> None:
        local_var_types, span_bindings, cpp_spans = self._caller_local_types(
            caller_node, caller_qn, module_qn, language, class_context
        )
        language_calls = self._host._calls_for(language)
        language_calls.record_local_names(
            caller_node, caller_qn, caller_type, module_qn
        )

        caller_spec = (caller_type, cs.KEY_QUALIFIED_NAME, caller_qn)

        self._run_side_passes(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            class_context,
            local_var_types,
        )

        caller_params = self._record_caller_flow_params(
            caller_node,
            caller_qn,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )
        self._ingest_caller_property_reads(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )
        language_calls.ingest_caller_references(
            caller_node,
            caller_spec,
            caller_qn,
            caller_type,
            module_qn,
            language,
            queries,
            class_context,
            local_var_types,
        )

        if call_nodes is None:
            call_nodes = self._queried_call_nodes(caller_node, language, queries)
        if not call_nodes:
            return

        ctx = self._call_scan_context(
            caller_node,
            caller_qn,
            caller_spec,
            module_qn,
            language,
            queries,
            class_context,
            call_name_cache,
            local_var_types,
            span_bindings,
            caller_params,
        )
        self._ingest_call_nodes(ctx, call_nodes, cpp_spans)
        # Edges the later passes emit (decorators, property reads, operator
        # dispatch) are exact bindings; they must not carry the verdict the
        # last call node of this loop left behind.
        self._host._resolution = cs.EdgeResolution.EXACT

    def _run_side_passes(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # The passes that read a caller for facts other than its calls: the
        # I/O it performs, the RPC methods it exposes and its data flow.
        self._io_access.process_io_for_caller(
            caller_node, caller_spec, module_qn, language
        )
        self._rpc_exposure.process_caller(caller_node, module_qn, language)
        self._flow_access.process_flow_for_caller(
            caller_node,
            caller_spec,
            caller_qn,
            module_qn,
            language,
            class_context,
            local_var_types,
        )

    @staticmethod
    def _queried_call_nodes(
        caller_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    ) -> list[Node]:
        # A caller handed no call nodes runs its language's call query over
        # its own span; a language without one has none.
        calls_query = queries[language].get(cs.QUERY_CALLS)
        if not calls_query:
            return []
        cursor = QueryCursor(calls_query)
        captures = sorted_captures(cursor, caller_node)
        return captures.get(cs.CAPTURE_CALL, [])

    def _caller_local_types(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None,
    ) -> tuple[
        dict[str, str] | None,
        list[tuple[int, int, str, str]],
        list[tuple[int, int, str, str]],
    ]:
        """The caller's local types, its span-scoped bindings and its C++
        declaration windows."""
        language_calls = self._host._calls_for(language)
        if not language_calls.types_locals:
            return None, [], []
        try:
            local_var_types = (
                self._resolver.type_inference.build_local_variable_type_map(
                    caller_node, module_qn, language, class_context
                )
            )
            cpp_spans = language_calls.local_type_spans(
                caller_node, module_qn, local_var_types
            )
            # Rust match arms and iterator-adaptor closures both reuse one
            # binding name for different types at different byte ranges (`cmd`
            # per arm; `|x|` per collection); the flat map keeps only one. These
            # span-scoped bindings let each call resolve against the binding
            # containing it.
            span_bindings = self._span_bindings(
                caller_node, module_qn, class_context, language, local_var_types
            )
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            # A walk that still fails on some shape costs this caller its local
            # types, not the file its calls: they resolve untyped (#3173).
            self._host._record_caller_failure(
                ls.CALLER_LOCAL_TYPES_FAILED, caller_qn, e
            )
            return {}, [], []
        return local_var_types, span_bindings, cpp_spans

    def _span_bindings(
        self,
        caller_node: Node,
        module_qn: str,
        class_context: str | None,
        language: cs.SupportedLanguage,
        local_var_types: dict[str, str] | None,
    ) -> list[tuple[int, int, str, str]]:
        if local_var_types is None:
            return []
        return self._host._calls_for(language).span_bindings(
            caller_node, module_qn, class_context, local_var_types
        )

    def _ingest_call_nodes(
        self,
        ctx: CallScan,
        call_nodes: list[Node],
        cpp_spans: list[tuple[int, int, str, str]],
    ) -> None:
        # A C++ name whose declarations disagree (a block local `B b` hiding
        # a `::B b` field) is typed per call by the declaration in force
        # there, for every use the call makes of it: a receiver, a functor
        # called (`b(1)`) or handed over (`keep(b)`), an operator operand.
        flat_types = ctx.local_var_types
        for call_node in call_nodes:
            if cpp_spans and flat_types is not None:
                ctx.local_var_types = cpp_calls.types_at(
                    flat_types, cpp_spans, call_node.start_byte
                )
            self._binding.ingest_call_node(ctx, call_node)

    def _record_caller_flow_params(
        self,
        caller_node: Node,
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> frozenset[str]:
        ordered_params = self._host._calls_for(language).parameter_names(caller_node)
        if ordered_params is None:
            return frozenset()
        self._flow.record_caller(
            caller_node,
            caller_qn,
            ordered_params,
            module_qn,
            language,
            local_var_types,
            class_context,
            flow_scope_boundaries(queries[language][cs.QUERY_CONFIG]),
        )
        return frozenset(ordered_params)

    def _ingest_caller_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        # Runs independently of call_nodes: a getter access is an attribute, not
        # a call, so callers that read a property but make no other call must
        # still reach this pass before the early return below.
        if prop_names := self._resolver.function_registry.property_names():
            self._host._calls_for(language).ingest_property_reads(
                caller_node,
                caller_spec,
                caller_qn,
                module_qn,
                local_var_types,
                class_context,
                queries[language][cs.QUERY_CONFIG],
                prop_names,
            )

    def _call_scan_context(
        self,
        caller_node: Node,
        caller_qn: str,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        call_name_cache: dict[int, str | None] | None,
        local_var_types: dict[str, str] | None,
        span_bindings: list[tuple[int, int, str, str]],
        caller_params: frozenset[str],
    ) -> CallScan:
        calls = self._host._calls_for(language)
        resolver = self._resolver
        resolve_func = self._host._resolve_noting
        builtins, operators = calls.binds_builtin_calls, calls.binds_operator_calls
        resolve_builtin = resolver.resolve_builtin_call if builtins else None
        resolve_cpp_op = resolver.cpp.resolve_cpp_operator_call if operators else None
        # A method-group pass is not an invocation: C# records it as
        # REFERENCES everywhere (CALLS here put 282 phantom edges into the
        # Polly call graph, retrieval precision 1.0 -> 0.92); flow languages
        # keep their historical CALLS form for external/builtin callees.
        arg_ref_rel = (
            cs.RelationshipType.CALLS
            if calls.flows_callables
            else cs.RelationshipType.REFERENCES
        )
        return CallScan(
            caller_node=caller_node,
            caller_qn=caller_qn,
            caller_spec=caller_spec,
            module_qn=module_qn,
            language=language,
            queries=queries,
            calls=calls,
            class_context=class_context,
            call_name_cache=call_name_cache,
            local_var_types=local_var_types,
            span_bindings=span_bindings,
            caller_params=caller_params,
            # The dispatch fan-out treats a receiver typed to a template
            # parameter like an untyped receiver rather than an external.
            cpp_template_params=calls.template_params(caller_node),
            resolve_func=resolve_func,
            resolve_builtin=resolve_builtin,
            resolve_cpp_op=resolve_cpp_op,
            ensure_rel=self._host._emit_rel,
            arg_ref_rel=arg_ref_rel,
        )
