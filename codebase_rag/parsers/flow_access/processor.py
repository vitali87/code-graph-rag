from __future__ import annotations

from collections import defaultdict, deque
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..call_resolver import CallResolver
from ..import_processor import ImportProcessor
from ..io_access import (
    DYNAMIC_TARGET,
    IO_MEMBER_READS,
    IO_SINKS,
    LANGUAGE_DESCRIPTORS,
    PY_SCOPE_BOUNDARIES,
    RESOURCE_QN_FORMAT,
    HandleBinding,
    IODirection,
    IOSink,
    LanguageDescriptor,
    ResourceKind,
    call_name,
    definition_header_nodes,
    is_require_alias,
    literal_target,
    match_normalised,
    registry_match,
    scope_seed_nodes,
    string_literal,
)
from .constants import (
    KEY_KIND,
    KEY_VIA,
    VIA_ARG_FORMAT,
    VIA_KW_FORMAT,
    VIA_RETURN,
    FlowKind,
)

_BUILTIN_QN_PREFIX = f"{cs.BUILTIN_PREFIX}{cs.SEPARATOR_DOT}"


class Taint(NamedTuple):
    # (H) What is tainting a variable: resolved resource origins plus the set of
    # (H) callee QNs whose (possibly not-yet-processed) return value it carries. A
    # (H) variable is present in the taint map iff it is tainted; a purely-pending
    # (H) taint is only really tainted once finalize resolves a pending callee to a
    # (H) tainted return -- how forward/cross-file return-taint is recovered (712).
    origins: frozenset[HandleBinding]
    pending: frozenset[str]


_EMPTY_TAINT = Taint(frozenset(), frozenset())


def _merge_taint(a: Taint, b: Taint) -> Taint:
    return Taint(a.origins | b.origins, a.pending | b.pending)


# (H) Live taint state threaded through the walk: variable -> its Taint.
type _TaintMap = dict[str, Taint]

# (H) Return wrappers whose taint lives in their elements: `return (x)`,
# (H) `return a, b`, `return [x]`. Unwrapped so a tainted identifier/call inside
# (H) them is still seen as a returned tainted value.
_RETURN_UNWRAP = (
    cs.TS_PY_PARENTHESIZED_EXPRESSION,
    cs.TS_PY_EXPRESSION_LIST,
    cs.TS_PY_TUPLE,
    cs.TS_PY_LIST,
)


def _return_value_nodes(return_node: Node) -> list[Node]:
    # (H) Left-to-right leaf value nodes of a return statement, unwrapping the
    # (H) parenthesis/tuple/list wrappers above.
    out: list[Node] = []
    queue = list(return_node.named_children)
    while queue:
        child = queue.pop(0)
        if child.type in _RETURN_UNWRAP:
            queue[:0] = list(child.named_children)
        else:
            out.append(child)
    return out


class _FlowCtx(NamedTuple):
    # (H) Per-caller constants threaded through the source-ordered walk.
    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    class_context: str | None
    language: cs.SupportedLanguage
    import_map: dict[str, str]
    read_sinks: dict[str, IOSink]
    write_sinks: dict[str, IOSink]


class _JsCtx(NamedTuple):
    # (H) Per-caller constants for the lean non-Python flow walk (issue #714).
    flow: _FlowCtx
    descriptor: LanguageDescriptor
    member_reads: tuple[tuple[str, ResourceKind], ...]
    # (H) Names bound in the caller scope, which shadow a same-named builtin
    # (H) source/sink (a local `const fetch`, `function process`, a parameter).
    local_names: frozenset[str]


class FlowProcessor:
    """Detects intra-procedural value flow in a function body and emits FLOWS_TO
    edges: resource->resource (a read source reaches a write sink), caller->callee
    (a tainted value is passed as an argument), and callee->caller (a callee whose
    return value is tainted)."""

    def __init__(
        self,
        ingestor: IngestorProtocol,
        import_processor: ImportProcessor,
        resolver: CallResolver,
        selection: CaptureSelection,
    ) -> None:
        self.ingestor = ingestor
        self._import_processor = import_processor
        self._resolver = resolver
        self._selection = selection
        self._enabled = selection.rel_enabled(cs.RelationshipType.FLOWS_TO)
        # (H) Per-function return-taint SUMMARY collected during the walk: caller QN
        # (H) -> the Taint it returns (resolved origins + pending callee QNs whose
        # (H) return it forwards). Resolved to a fixpoint in finalize(), so a callee
        # (H) defined/processed AFTER its caller (forward or cross-file) is still
        # (H) accounted for (issue #712). Only plain data crosses the walk boundary,
        # (H) never a tree-sitter Node (the AST cache evicts trees).
        self._summaries: dict[str, Taint] = {}
        # (H) Deferred emission facts, drained once in finalize() when every summary
        # (H) is in and the fixpoint is known. Each carries only serialized data:
        # (H) return-edge candidates (callee_type, callee_qn, caller_spec) emit a
        # (H) return edge iff the callee turns out to return taint; resource flows
        # (H) (pending callee QNs, sink kind, sink identity) emit origin -> sink for
        # (H) each pending callee's resolved origins; arg edges (pending QNs,
        # (H) caller_spec, callee_type, callee_qn, via) emit iff a pending callee
        # (H) resolves to a tainted return.
        self._return_edge_candidates: list[tuple[str, str, tuple[str, str, str]]] = []
        self._deferred_resource_flows: list[
            tuple[frozenset[str], ResourceKind, str]
        ] = []
        self._deferred_arg_edges: list[
            tuple[frozenset[str], tuple[str, str, str], str, str, str]
        ] = []
        # (H) Per-caller scratch for the return-taint summary, accumulated across
        # (H) every branch of the structured walk (a return on ANY path contributes).
        # (H) Reset at the top of each process_flow_for_caller; not reentrant (the
        # (H) call processor drives one caller at a time).
        self._acc_returns_taint = False
        self._acc_return_taint = _EMPTY_TAINT

    def process_flow_for_caller(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        class_context: str | None,
    ) -> None:
        if not self._enabled:
            return
        sinks = IO_SINKS.get(language, ())
        if not sinks:
            return
        ctx = _FlowCtx(
            caller_spec=caller_spec,
            caller_qn=caller_qn,
            module_qn=module_qn,
            class_context=class_context,
            language=language,
            import_map=self._import_processor.import_mapping.get(module_qn, {}),
            read_sinks={s.callee: s for s in sinks if s.direction == IODirection.READ},
            write_sinks={
                s.callee: s for s in sinks if s.direction == IODirection.WRITE
            },
        )
        # (H) Non-Python languages take a lean STRAIGHT-LINE flow walk (issue #714):
        # (H) taint from a read source (process.env, fetch, fs.readFile) reaching a
        # (H) write sink emits a resource->resource flow, a tainted value passed to a
        # (H) callee emits an arg edge, and a returned tainted value feeds the shared
        # (H) return-taint fixpoint. No path-sensitivity yet (that is a follow-up),
        # (H) and Python keeps its full path-sensitive walk below.
        if language != cs.SupportedLanguage.PYTHON:
            descriptor = LANGUAGE_DESCRIPTORS.get(language)
            if descriptor is not None:
                self._process_lean_flow(
                    caller_node, ctx, descriptor, IO_MEMBER_READS.get(language, ())
                )
            return

        # (H) Path-sensitive MAY walk (issue #713): taint is threaded statement by
        # (H) statement, but each if/elif/else, try/except and loop is evaluated
        # (H) per branch against a COPY of the incoming state and unioned at the
        # (H) merge point. So taint that survives on ANY path survives the join, and
        # (H) a variable is killed only when reassigned to an untainted value on
        # (H) EVERY path. Straight-line code still behaves exactly as the old flat
        # (H) walk (single path, no branches to merge).
        self._acc_returns_taint = False
        self._acc_return_taint = _EMPTY_TAINT
        # (H) Seed from the caller's own scope; on a nested def descend into its
        # (H) header only (default args/decorators/bases run in THIS scope, its
        # (H) body is a separate caller). Same scoping as io_access.
        tainted: _TaintMap = {}
        for node in scope_seed_nodes(caller_node):
            tainted = self._walk_stmt(node, tainted, ctx)

        if self._acc_returns_taint:
            self._summaries[caller_qn] = self._acc_return_taint

    # (H) Lean non-Python (JS/TS) straight-line flow (issue #714) below.
    def _process_lean_flow(
        self,
        caller_node: Node,
        ctx: _FlowCtx,
        descriptor: LanguageDescriptor,
        member_reads: tuple[tuple[str, ResourceKind], ...],
    ) -> None:
        self._acc_returns_taint = False
        self._acc_return_taint = _EMPTY_TAINT
        jc = _JsCtx(
            flow=ctx,
            descriptor=descriptor,
            member_reads=member_reads,
            local_names=self._js_local_names(caller_node, descriptor),
        )
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            statements = list(caller_node.named_children)
        elif body.type == descriptor.block_scope_type:
            statements = list(body.named_children)
        else:
            statements = [body]
        tainted: _TaintMap = {}
        # (H) ponytail: flat source-order walk, not path-sensitive (that is the JS
        # (H) follow-up); a single taint map, nested functions pruned as their own
        # (H) callers. Reuses the shared summary/deferred/finalize machinery (#712).
        stack = list(reversed(statements))
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in descriptor.nested_scope_types:
                continue
            if node_type == descriptor.declarator_type:
                self._js_bind(node, cs.TS_FIELD_NAME, cs.FIELD_VALUE, tainted, jc)
            elif node_type == cs.TS_ASSIGNMENT_EXPRESSION:
                self._js_bind(node, cs.FIELD_LEFT, cs.FIELD_RIGHT, tainted, jc)
            elif node_type == descriptor.call_type:
                self._js_call(node, tainted, jc)
            elif node_type == cs.TS_RETURN_STATEMENT:
                returned = self._js_return_taint(node, tainted, jc)
                if returned is not None:
                    self._acc_returns_taint = True
                    self._acc_return_taint = _merge_taint(
                        self._acc_return_taint, returned
                    )
            stack.extend(reversed(node.named_children))
        if self._acc_returns_taint:
            self._summaries[ctx.caller_qn] = self._acc_return_taint

    def _js_bind(
        self,
        node: Node,
        target_field: str,
        value_field: str,
        tainted: _TaintMap,
        jc: _JsCtx,
    ) -> None:
        target = node.child_by_field_name(target_field)
        if (
            target is None
            or target.type != jc.descriptor.identifier_type
            or target.text is None
        ):
            return
        lhs = target.text.decode(cs.ENCODING_UTF8)
        taint = self._js_expr_taint(node.child_by_field_name(value_field), tainted, jc)
        if taint is not None:
            tainted[lhs] = taint
        else:
            tainted.pop(lhs, None)

    def _js_expr_taint(
        self, node: Node | None, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # (H) The Taint an expression carries: an identifier propagates the map; a
        # (H) member/subscript may be an env source; a call may be a read source or a
        # (H) function whose return is deferred (pending) to the fixpoint.
        if node is None:
            return None
        d = jc.descriptor
        node_type = node.type
        if node_type in (cs.TS_AWAIT_EXPRESSION, cs.TS_PARENTHESIZED_EXPRESSION):
            # (H) Unwrap `await expr` and `(expr)` to the inner source expression.
            return self._js_expr_taint(self._js_first_expr(node), tainted, jc)
        if node_type == d.identifier_type:
            return (
                tainted.get(node.text.decode(cs.ENCODING_UTF8)) if node.text else None
            )
        if node_type in (d.member_expression_type, d.subscript_type):
            if (binding := self._js_member_source(node, jc)) is not None:
                return Taint(frozenset({binding}), frozenset())
            return None
        if node_type == d.call_type:
            raw = call_name(node)
            if raw is None:
                return None
            if (binding := self._js_read_source(node, raw, jc)) is not None:
                return Taint(frozenset({binding}), frozenset())
            callee = self._resolve(
                raw,
                jc.flow.module_qn,
                jc.flow.class_context,
                jc.flow.caller_qn,
                jc.flow.language,
            )
            if callee is not None:
                self._return_edge_candidates.append(
                    (callee[0], callee[1], jc.flow.caller_spec)
                )
                return Taint(frozenset(), frozenset({callee[1]}))
        return None

    def _js_call(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        raw = call_name(node)
        if raw is None:
            return
        args = self._js_arg_taints(node, tainted, jc)
        if not args:
            return
        if (sink := self._js_match_sink(raw, jc.flow.write_sinks, jc)) is not None:
            dst_identity = literal_target(
                node,
                sink.target_arg,
                sink.target_kw,
                string_type=jc.descriptor.string_type,
                content_type=jc.descriptor.string_content_type,
                keyword_arg_type=jc.descriptor.keyword_arg_type,
            )
            for _via, taint in args:
                if taint is None:
                    continue
                for origin in taint.origins:
                    self._emit_resource_flow(origin, sink.kind, dst_identity)
                if taint.pending:
                    self._deferred_resource_flows.append(
                        (taint.pending, sink.kind, dst_identity)
                    )
            return
        callee = self._resolve(
            raw,
            jc.flow.module_qn,
            jc.flow.class_context,
            jc.flow.caller_qn,
            jc.flow.language,
        )
        if callee is None:
            return
        callee_type, callee_qn = callee
        for via, taint in args:
            if taint is None:
                continue
            if taint.origins:
                self.ingestor.ensure_relationship_batch(
                    jc.flow.caller_spec,
                    cs.RelationshipType.FLOWS_TO,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                    properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
                )
            elif taint.pending:
                self._deferred_arg_edges.append(
                    (taint.pending, jc.flow.caller_spec, callee_type, callee_qn, via)
                )

    def _js_arg_taints(
        self, node: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> list[tuple[str, Taint | None]]:
        args = node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return []
        out: list[tuple[str, Taint | None]] = []
        # (H) Comments are named children; exclude them so arg positions stay correct.
        for index, child in enumerate(self._named_no_comments(args)):
            out.append(
                (
                    VIA_ARG_FORMAT.format(index=index),
                    self._js_expr_taint(child, tainted, jc),
                )
            )
        return out

    def _js_return_taint(
        self, node: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        return self._js_expr_taint(self._js_first_expr(node), tainted, jc)

    @staticmethod
    def _named_no_comments(node: Node) -> list[Node]:
        return [c for c in node.named_children if c.type != cs.TS_COMMENT]

    @staticmethod
    def _js_first_expr(node: Node) -> Node | None:
        # (H) The first meaningful sub-expression, skipping comment named children.
        for child in node.named_children:
            if child.type != cs.TS_COMMENT:
                return child
        return None

    def _js_member_source(self, node: Node, jc: _JsCtx) -> HandleBinding | None:
        obj = node.child_by_field_name(jc.descriptor.object_field)
        if obj is None or obj.text is None:
            return None
        obj_text = obj.text.decode(cs.ENCODING_UTF8)
        for prefix, kind in jc.member_reads:
            if obj_text != prefix:
                continue
            head = prefix.partition(cs.SEPARATOR_DOT)[0]
            if head in jc.local_names or self._js_import_shadowed(head, jc):
                return None
            return HandleBinding(kind=kind, identity=self._js_member_identity(node, jc))
        return None

    def _js_member_identity(self, node: Node, jc: _JsCtx) -> str:
        d = jc.descriptor
        if node.type == d.member_expression_type:
            prop = node.child_by_field_name(d.property_field)
            if prop is not None and prop.text is not None:
                return prop.text.decode(cs.ENCODING_UTF8)
            return DYNAMIC_TARGET
        index = node.child_by_field_name(d.subscript_index_field)
        if index is not None and index.type == d.string_type:
            return string_literal(index, d.string_type, d.string_content_type)
        return DYNAMIC_TARGET

    def _js_read_source(self, node: Node, raw: str, jc: _JsCtx) -> HandleBinding | None:
        sink = self._js_match_sink(raw, jc.flow.read_sinks, jc)
        if sink is None:
            return None
        identity = literal_target(
            node,
            sink.target_arg,
            sink.target_kw,
            string_type=jc.descriptor.string_type,
            content_type=jc.descriptor.string_content_type,
            keyword_arg_type=jc.descriptor.keyword_arg_type,
        )
        return HandleBinding(kind=sink.kind, identity=identity)

    @staticmethod
    def _js_match_sink(
        raw: str, sink_map: dict[str, IOSink], jc: _JsCtx
    ) -> IOSink | None:
        # (H) Same shadow-aware matching as io_access: a locally-bound name is not
        # (H) the builtin; the import-normalised name matches first (so an aliased
        # (H) builtin resolves), then the raw dotted name only when its head resolves
        # (H) to the genuine module (node:/require/ESM), never a shadowing local module.
        head, sep, _ = raw.partition(cs.SEPARATOR_DOT)
        if (head if sep else raw) in jc.local_names:
            return None
        if (sink := match_normalised(raw, jc.flow.import_map, sink_map)) is not None:
            return sink
        if not sep:
            return None
        base = jc.flow.import_map.get(head)
        ok = (
            base is None
            or base.split(cs.SEPARATOR_DOT)[0].removeprefix(cs.NODE_BUILTIN_PREFIX)
            == head
        )
        return sink_map.get(raw) if ok else None

    @staticmethod
    def _js_import_shadowed(head: str, jc: _JsCtx) -> bool:
        base = jc.flow.import_map.get(head)
        return base is not None and (
            base.split(cs.SEPARATOR_DOT)[0].removeprefix(cs.NODE_BUILTIN_PREFIX) != head
        )

    def _js_local_names(
        self, caller_node: Node, descriptor: LanguageDescriptor
    ) -> frozenset[str]:
        # (H) ponytail: flat collection of the caller's parameters + every declarator
        # (H) and hoisted function name in the body (nested functions pruned). Not
        # (H) block-scoped -- the io walk has the precise version; refine if JS flow
        # (H) precision on that ever matters. A `const fs = require('fs')` declarator
        # (H) is an import alias (the genuine module), so it is NOT a shadow; but a
        # (H) local `const fs = {}` IS one, even if `fs` is also imported module-wide.
        names: set[str] = set()
        params = caller_node.child_by_field_name(descriptor.params_field)
        if params is not None:
            for child in params.named_children:
                self._js_binding_names(child, descriptor, names)
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        stack = list((body or caller_node).named_children)
        while stack:
            node = stack.pop()
            if node.type in descriptor.nested_scope_types:
                name = node.child_by_field_name(cs.TS_FIELD_NAME)
                if name is not None and name.text:
                    names.add(name.text.decode(cs.ENCODING_UTF8))
                continue
            if (
                node.type == descriptor.declarator_type
                and not is_require_alias(node, descriptor.call_type)
                and (name := node.child_by_field_name(cs.TS_FIELD_NAME)) is not None
            ):
                self._js_binding_names(name, descriptor, names)
            stack.extend(node.named_children)
        return frozenset(names)

    def _js_binding_names(
        self, node: Node, descriptor: LanguageDescriptor, out: set[str]
    ) -> None:
        # (H) The names a binding target introduces: a plain identifier, a TS
        # (H) required/optional parameter wrapper (its `pattern`), a default
        # (H) (assignment_pattern left), or a destructuring pattern's leaves.
        node_type = node.type
        if node_type in (
            descriptor.identifier_type,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
        ):
            if node.text:
                out.add(node.text.decode(cs.ENCODING_UTF8))
        elif node_type == cs.TS_PAIR_PATTERN:
            if (value := node.child_by_field_name(cs.FIELD_VALUE)) is not None:
                self._js_binding_names(value, descriptor, out)
        elif node_type in (cs.TS_REQUIRED_PARAMETER, cs.TS_OPTIONAL_PARAMETER):
            if (pattern := node.child_by_field_name(cs.TS_FIELD_PATTERN)) is not None:
                self._js_binding_names(pattern, descriptor, out)
        elif node_type == cs.TS_ASSIGNMENT_PATTERN:
            left = node.child_by_field_name(cs.FIELD_LEFT) or self._js_first_expr(node)
            if left is not None:
                self._js_binding_names(left, descriptor, out)
        elif node_type in (
            cs.TS_OBJECT_PATTERN,
            cs.TS_ARRAY_PATTERN,
            cs.TS_REST_PATTERN,
        ):
            for child in node.named_children:
                self._js_binding_names(child, descriptor, out)

    @staticmethod
    def _merge(states: list[_TaintMap]) -> _TaintMap:
        # (H) MAY union across branches: a variable is tainted after the join if it
        # (H) is tainted on ANY incoming path; its origins/pending are the union of
        # (H) those from the paths where it is tainted. A variable absent from every
        # (H) branch (killed/never tainted on all paths) stays absent.
        out: _TaintMap = {}
        for state in states:
            for var, taint in state.items():
                existing = out.get(var)
                out[var] = _merge_taint(existing, taint) if existing else taint
        return out

    def _walk_stmt(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        node_type = node.type
        if node_type in PY_SCOPE_BOUNDARIES:
            # (H) Nested def/class: only its header executes in this scope.
            for header in definition_header_nodes(node):
                state = self._walk_stmt(header, state, ctx)
            return state
        if node_type == cs.TS_PY_IF_STATEMENT:
            return self._walk_if(node, state, ctx)
        if node_type in (cs.TS_PY_FOR_STATEMENT, cs.TS_PY_WHILE_STATEMENT):
            return self._walk_loop(node, state, ctx)
        if node_type == cs.TS_PY_TRY_STATEMENT:
            return self._walk_try(node, state, ctx)
        if node_type == cs.TS_PY_ASSIGNMENT:
            self._apply_assignment(node, state, ctx)
        elif node_type == cs.TS_PY_CALL:
            self._apply_call(node, state, ctx)
        elif node_type == cs.TS_PY_RETURN_STATEMENT:
            # (H) Process every return: each `return callee()` emits its own
            # (H) callee->caller edge, and the body's returned-source set is the
            # (H) UNION over all returns (any branch), so branches returning
            # (H) DIFFERENT tainted sources each carry to callers of this function.
            returned = self._return_taint_source(node, state, ctx)
            if returned is not None:
                self._acc_returns_taint = True
                self._acc_return_taint = _merge_taint(self._acc_return_taint, returned)
        # (H) Descend into children in source order (an assignment's RHS call still
        # (H) needs _apply_call for its arg edges; nested calls in args likewise).
        for child in node.children:
            state = self._walk_stmt(child, state, ctx)
        return state

    def _walk_if(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        # (H) The if-condition runs on all paths; process it in the incoming state.
        cond = node.child_by_field_name(cs.TS_FIELD_CONDITION)
        if cond is not None:
            state = self._walk_stmt(cond, state, ctx)
        branch_exits: list[_TaintMap] = []
        consequence = node.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
        if consequence is not None:
            branch_exits.append(self._walk_stmt(consequence, dict(state), ctx))
        has_else = False
        for clause in node.children:
            if clause.type == cs.TS_PY_ELIF_CLAUSE:
                elif_cond = clause.child_by_field_name(cs.TS_FIELD_CONDITION)
                if elif_cond is not None:
                    state = self._walk_stmt(elif_cond, state, ctx)
                elif_body = clause.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
                if elif_body is not None:
                    branch_exits.append(self._walk_stmt(elif_body, dict(state), ctx))
            elif clause.type == cs.TS_PY_ELSE_CLAUSE:
                has_else = True
                else_body = clause.child_by_field_name(cs.FIELD_BODY)
                if else_body is not None:
                    branch_exits.append(self._walk_stmt(else_body, dict(state), ctx))
        # (H) No else means the skip path preserves the incoming state.
        if not has_else:
            branch_exits.append(dict(state))
        return self._merge(branch_exits) if branch_exits else state

    def _walk_loop(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        # (H) The while-condition / for-iterable runs before the body.
        for field in (cs.TS_FIELD_CONDITION, cs.TS_FIELD_RIGHT):
            part = node.child_by_field_name(field)
            if part is not None:
                state = self._walk_stmt(part, state, ctx)
        body = node.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            # (H) The body runs zero or more times: union the skip path with one
            # (H) pass, then re-run the body once from that merge so taint carried
            # (H) from a later iteration into an earlier statement is caught.
            # (H) ponytail: two passes, not a full fixpoint; iterate to stability
            # (H) only if deeper loop-carried taint chains ever matter. Edges are
            # (H) MERGE-idempotent, so the re-walk never duplicates graph edges.
            once = self._walk_stmt(body, dict(state), ctx)
            merged = self._merge([state, once])
            twice = self._walk_stmt(body, dict(merged), ctx)
            state = self._merge([state, twice])
        for clause in node.children:
            if clause.type == cs.TS_PY_ELSE_CLAUSE:
                else_body = clause.child_by_field_name(cs.FIELD_BODY)
                if else_body is not None:
                    state = self._walk_stmt(else_body, state, ctx)
        return state

    def _walk_try(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        body = node.child_by_field_name(cs.FIELD_BODY)
        body_exit = (
            self._walk_stmt(body, dict(state), ctx) if body is not None else dict(state)
        )
        branch_exits: list[_TaintMap] = []
        has_else = False
        for clause in node.children:
            if clause.type == cs.TS_PY_EXCEPT_CLAUSE:
                block = next(
                    (c for c in clause.children if c.type == cs.TS_PY_BLOCK), None
                )
                if block is not None:
                    # (H) An except handler can run after the try body partially
                    # (H) executed, so seed it with union(pre, body_exit) -- taint
                    # (H) introduced before the raise must still reach the handler.
                    branch_exits.append(
                        self._walk_stmt(block, self._merge([state, body_exit]), ctx)
                    )
            elif clause.type == cs.TS_PY_ELSE_CLAUSE:
                has_else = True
                else_body = clause.child_by_field_name(cs.FIELD_BODY)
                if else_body is not None:
                    branch_exits.append(
                        self._walk_stmt(else_body, dict(body_exit), ctx)
                    )
        # (H) The try body completing normally (no else) is itself a path.
        if not has_else:
            branch_exits.append(body_exit)
        merged = self._merge(branch_exits) if branch_exits else body_exit
        for clause in node.children:
            if clause.type == cs.TS_PY_FINALLY_CLAUSE:
                block = next(
                    (c for c in clause.children if c.type == cs.TS_PY_BLOCK), None
                )
                if block is not None:
                    # (H) finally runs on every path: apply it to the merged state.
                    merged = self._walk_stmt(block, merged, ctx)
        return merged

    def _apply_assignment(self, node: Node, tainted: _TaintMap, ctx: _FlowCtx) -> None:
        left = node.child_by_field_name(cs.TS_FIELD_LEFT)
        right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        if (
            left is None
            or right is None
            or left.type != cs.TS_PY_IDENTIFIER
            or left.text is None
        ):
            return
        lhs = left.text.decode(cs.ENCODING_UTF8)
        if right.type == cs.TS_PY_IDENTIFIER and right.text is not None:
            rhs = right.text.decode(cs.ENCODING_UTF8)
            if rhs in tainted:
                tainted[lhs] = tainted[rhs]
            else:
                tainted.pop(lhs, None)
            return
        if right.type == cs.TS_PY_CALL and (raw := call_name(right)) is not None:
            if seed := self._source_binding(right, raw, ctx.import_map, ctx.read_sinks):
                tainted[lhs] = Taint(frozenset({seed}), frozenset())
                return
            callee = self._resolve(
                raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
            )
            if callee is not None:
                # (H) Defer: mark lhs pending on the callee's return and record a
                # (H) candidate return edge. finalize() decides whether the callee
                # (H) really returns taint, so a callee processed later still counts.
                self._return_edge_candidates.append(
                    (callee[0], callee[1], ctx.caller_spec)
                )
                tainted[lhs] = Taint(frozenset(), frozenset({callee[1]}))
                return
        # (H) Any other RHS (literal, expression, unresolved call) leaves lhs clean.
        tainted.pop(lhs, None)

    def _apply_call(self, node: Node, tainted: _TaintMap, ctx: _FlowCtx) -> None:
        raw = call_name(node)
        if raw is None:
            return
        arg_names = self._arg_names(node)
        if not arg_names:
            return
        sink = registry_match(ctx.write_sinks, raw, ctx.import_map)
        if sink is not None:
            dst_identity = literal_target(node, sink.target_arg, sink.target_kw)
            for arg_name, _via in arg_names:
                taint = tainted.get(arg_name)
                if taint is None:
                    continue
                # (H) Resolved origins emit resource flows now; pending callees defer
                # (H) to finalize, when the (possibly forward) callee's origins resolve.
                for source in taint.origins:
                    self._emit_resource_flow(source, sink.kind, dst_identity)
                if taint.pending:
                    self._deferred_resource_flows.append(
                        (taint.pending, sink.kind, dst_identity)
                    )
            return
        callee = self._resolve(
            raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
        )
        if callee is None:
            return
        callee_type, callee_qn = callee
        for arg_name, via in arg_names:
            taint = tainted.get(arg_name)
            if taint is None:
                continue
            if taint.origins:
                # (H) Definitely tainted arg: emit the caller->callee arg edge now.
                self.ingestor.ensure_relationship_batch(
                    ctx.caller_spec,
                    cs.RelationshipType.FLOWS_TO,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                    properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
                )
            elif taint.pending:
                # (H) Tainted only via a pending callee: defer, so no arg edge is
                # (H) emitted if that callee turns out to return nothing tainted.
                self._deferred_arg_edges.append(
                    (taint.pending, ctx.caller_spec, callee_type, callee_qn, via)
                )

    def _return_taint_source(
        self, node: Node, tainted: _TaintMap, ctx: _FlowCtx
    ) -> Taint | None:
        # (H) The Taint this return yields, or None if it returns nothing tainted.
        # (H) `return a, b` unwraps to several value nodes; their taints union. A
        # (H) directly-returned callee is deferred (pending) exactly like an
        # (H) assigned one, so finalize() resolves it after every summary is in.
        result = _EMPTY_TAINT
        tainted_here = False
        for child in _return_value_nodes(node):
            if child.type == cs.TS_PY_IDENTIFIER and child.text is not None:
                name = child.text.decode(cs.ENCODING_UTF8)
                if (taint := tainted.get(name)) is not None:
                    tainted_here = True
                    result = _merge_taint(result, taint)
            elif child.type == cs.TS_PY_CALL and (raw := call_name(child)) is not None:
                if seed := self._source_binding(
                    child, raw, ctx.import_map, ctx.read_sinks
                ):
                    tainted_here = True
                    result = _merge_taint(result, Taint(frozenset({seed}), frozenset()))
                    continue
                callee = self._resolve(
                    raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
                )
                if callee is not None:
                    self._return_edge_candidates.append(
                        (callee[0], callee[1], ctx.caller_spec)
                    )
                    tainted_here = True
                    result = _merge_taint(
                        result, Taint(frozenset(), frozenset({callee[1]}))
                    )
        return result if tainted_here else None

    def _emit_return_edge(
        self, callee: tuple[str, str], caller_spec: tuple[str, str, str]
    ) -> None:
        callee_type, callee_qn = callee
        self.ingestor.ensure_relationship_batch(
            (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
            cs.RelationshipType.FLOWS_TO,
            caller_spec,
            properties={KEY_VIA: VIA_RETURN, KEY_KIND: FlowKind.RETURN.value},
        )

    def finalize(self) -> None:
        # (H) Called once after every function has been walked (issue #712): resolve
        # (H) the per-function return-taint summaries to a fixpoint, then drain the
        # (H) deferred facts. A callee processed AFTER its caller is now known, so a
        # (H) forward/cross-file return edge and the resource flow it carries appear.
        # (H) All emitted edges are MERGE-idempotent, so re-emitting an edge the
        # (H) inline walk already produced (backward case) is harmless.
        if not self._enabled:
            return
        resolved, is_tainted = self._resolve_summaries()
        for callee_type, callee_qn, caller_spec in self._return_edge_candidates:
            if is_tainted.get(callee_qn):
                self._emit_return_edge((callee_type, callee_qn), caller_spec)
        for pending, sink_kind, sink_identity in self._deferred_resource_flows:
            # (H) Union origins across pending callees first so a shared origin is
            # (H) emitted once, not per callee.
            origins: set[HandleBinding] = set()
            for callee_qn in pending:
                origins |= resolved.get(callee_qn, frozenset())
            for origin in origins:
                self._emit_resource_flow(origin, sink_kind, sink_identity)
        for (
            pending,
            caller_spec,
            callee_type,
            callee_qn,
            via,
        ) in self._deferred_arg_edges:
            if any(is_tainted.get(p) for p in pending):
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.FLOWS_TO,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                    properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
                )
        # (H) One-shot per run: clear so a reused processor never re-emits.
        self._return_edge_candidates.clear()
        self._deferred_resource_flows.clear()
        self._deferred_arg_edges.clear()

    def _resolve_summaries(
        self,
    ) -> tuple[dict[str, frozenset[HandleBinding]], dict[str, bool]]:
        # (H) Worklist fixpoint over the serialized summaries: a function's resolved
        # (H) origins are its own plus every pending callee's, and it is tainted if
        # (H) it has an origin or any pending callee is tainted. Origins/taintedness
        # (H) only grow, so this converges through recursion. Re-queue only a
        # (H) callee's callers when it changes, keeping the whole pass O(V + E).
        resolved = {qn: summary.origins for qn, summary in self._summaries.items()}
        is_tainted = {
            qn: bool(summary.origins) for qn, summary in self._summaries.items()
        }
        callers_of: dict[str, set[str]] = defaultdict(set)
        for qn, summary in self._summaries.items():
            for callee_qn in summary.pending:
                callers_of[callee_qn].add(qn)
        worklist = deque(qn for qn, s in self._summaries.items() if s.pending)
        queued = set(worklist)
        while worklist:
            qn = worklist.popleft()
            queued.discard(qn)
            summary = self._summaries[qn]
            new_origins = summary.origins
            new_tainted = bool(summary.origins)
            for callee_qn in summary.pending:
                new_origins = new_origins | resolved.get(callee_qn, frozenset())
                new_tainted = new_tainted or is_tainted.get(callee_qn, False)
            if new_origins != resolved[qn] or new_tainted != is_tainted[qn]:
                resolved[qn] = new_origins
                is_tainted[qn] = new_tainted
                for caller in callers_of[qn]:
                    if caller not in queued:
                        worklist.append(caller)
                        queued.add(caller)
        return resolved, is_tainted

    @staticmethod
    def _source_binding(
        call_node: Node,
        raw_name: str,
        import_map: dict[str, str],
        read_sinks: dict[str, IOSink],
    ) -> HandleBinding | None:
        sink = registry_match(read_sinks, raw_name, import_map)
        if sink is None:
            return None
        identity = literal_target(call_node, sink.target_arg, sink.target_kw)
        return HandleBinding(kind=sink.kind, identity=identity)

    def _emit_resource_flow(
        self, source: HandleBinding, dst_kind: ResourceKind, dst_identity: str
    ) -> None:
        src_qn = self._ensure_resource(source.kind, source.identity)
        dst_qn = self._ensure_resource(dst_kind, dst_identity)
        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, src_qn),
            cs.RelationshipType.FLOWS_TO,
            (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, dst_qn),
            properties={KEY_KIND: FlowKind.RESOURCE.value},
        )

    def _ensure_resource(self, kind: ResourceKind, identity: str) -> str:
        qn = RESOURCE_QN_FORMAT.format(kind=kind.value, identity=identity)
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.RESOURCE,
            {
                cs.KEY_QUALIFIED_NAME: qn,
                cs.KEY_NAME: identity,
                KEY_KIND: kind.value,
            },
        )
        return qn

    def _resolve(
        self,
        raw_name: str,
        module_qn: str,
        class_context: str | None,
        caller_qn: str,
        language: cs.SupportedLanguage,
    ) -> tuple[str, str] | None:
        info = self._resolver.resolve_function_call(
            raw_name, module_qn, None, class_context, caller_qn, language
        )
        if info is None or info[1].startswith(_BUILTIN_QN_PREFIX):
            return None
        return info

    @staticmethod
    def _arg_names(call_node: Node) -> list[tuple[str, str]]:
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return []
        names: list[tuple[str, str]] = []
        index = 0
        for child in args.named_children:
            if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                key = child.child_by_field_name(cs.TS_FIELD_NAME)
                value = child.child_by_field_name(cs.FIELD_VALUE)
                if (
                    key is not None
                    and key.text is not None
                    and value is not None
                    and value.type == cs.TS_PY_IDENTIFIER
                    and value.text is not None
                ):
                    names.append(
                        (
                            value.text.decode(cs.ENCODING_UTF8),
                            VIA_KW_FORMAT.format(
                                name=key.text.decode(cs.ENCODING_UTF8)
                            ),
                        )
                    )
                continue
            if child.type == cs.TS_PY_IDENTIFIER and child.text is not None:
                names.append(
                    (
                        child.text.decode(cs.ENCODING_UTF8),
                        VIA_ARG_FORMAT.format(index=index),
                    )
                )
            index += 1
        return names
