from __future__ import annotations

from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..call_resolver import CallResolver
from ..import_processor import ImportProcessor
from ..io_access import (
    IO_SINKS,
    PY_SCOPE_BOUNDARIES,
    RESOURCE_QN_FORMAT,
    HandleBinding,
    IODirection,
    IOSink,
    ResourceKind,
    call_name,
    definition_header_nodes,
    literal_target,
    registry_match,
    scope_seed_nodes,
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

# (H) Live taint state threaded through the walk: variable -> the set of resource
# (H) origins currently tainting it (empty set = tainted, origin unknown).
type _TaintMap = dict[str, frozenset[HandleBinding]]

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
        # (H) QN -> the SET of source bindings its body can return (empty set = it
        # (H) returns a tainted value of unknown origin, e.g. a chained return;
        # (H) absent = it returns nothing tainted). A set, not one binding, so a
        # (H) callee returning DIFFERENT sources on different branches carries every
        # (H) origin across the call boundary. Consulted for return-flow so a
        # (H) returned value keeps its origin resource(s) through the call.
        # (H) ponytail: single-pass and source-order dependent (a callee defined
        # (H) after its caller is missed), exactly like CallProcessor._returned_callables;
        # (H) a finalize/fixpoint pass is the upgrade if cross-file recall matters.
        self._returns_taint: dict[str, frozenset[HandleBinding]] = {}
        # (H) Per-caller scratch for the return-taint summary, accumulated across
        # (H) every branch of the structured walk (a return on ANY path contributes).
        # (H) Reset at the top of each process_flow_for_caller; not reentrant (the
        # (H) call processor drives one caller at a time).
        self._acc_returns_taint = False
        self._acc_return_sources: set[HandleBinding] = set()

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
        # (H) ponytail: Python-only in phase 1, mirroring io_access; other languages
        # (H) need their own source/sink tables and node types first.
        if language != cs.SupportedLanguage.PYTHON:
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

        # (H) Path-sensitive MAY walk (issue #713): taint is threaded statement by
        # (H) statement, but each if/elif/else, try/except and loop is evaluated
        # (H) per branch against a COPY of the incoming state and unioned at the
        # (H) merge point. So taint that survives on ANY path survives the join, and
        # (H) a variable is killed only when reassigned to an untainted value on
        # (H) EVERY path. Straight-line code still behaves exactly as the old flat
        # (H) walk (single path, no branches to merge).
        self._acc_returns_taint = False
        self._acc_return_sources = set()
        # (H) Seed from the caller's own scope; on a nested def descend into its
        # (H) header only (default args/decorators/bases run in THIS scope, its
        # (H) body is a separate caller). Same scoping as io_access.
        tainted: dict[str, frozenset[HandleBinding]] = {}
        for node in scope_seed_nodes(caller_node):
            tainted = self._walk_stmt(node, tainted, ctx)

        if self._acc_returns_taint:
            self._returns_taint[caller_qn] = frozenset(self._acc_return_sources)

    @staticmethod
    def _merge(states: list[_TaintMap]) -> _TaintMap:
        # (H) MAY union across branches: a variable is tainted after the join if it
        # (H) is tainted on ANY incoming path; its origin set is the union of the
        # (H) origins from the paths where it is tainted. A variable absent from
        # (H) every branch (killed/never tainted on all paths) stays absent.
        out: _TaintMap = {}
        for state in states:
            for var, origins in state.items():
                out[var] = out.get(var, frozenset()) | origins
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
            sources = self._return_taint_source(node, state, ctx)
            if sources is not None:
                self._acc_returns_taint = True
                self._acc_return_sources |= sources
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

    def _apply_assignment(
        self, node: Node, tainted: dict[str, frozenset[HandleBinding]], ctx: _FlowCtx
    ) -> None:
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
                tainted[lhs] = frozenset({seed})
                return
            callee = self._resolve(
                raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
            )
            if callee is not None and callee[1] in self._returns_taint:
                tainted[lhs] = self._returns_taint[callee[1]]
                self._emit_return_edge(callee, ctx.caller_spec)
                return
        # (H) Any other RHS (literal, expression, untainted call) leaves lhs clean.
        tainted.pop(lhs, None)

    def _apply_call(
        self, node: Node, tainted: dict[str, frozenset[HandleBinding]], ctx: _FlowCtx
    ) -> None:
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
                # (H) One resource->resource edge per known origin of the tainted
                # (H) arg; an empty set (tainted, unknown origin) emits nothing.
                for source in tainted.get(arg_name, frozenset()):
                    self._emit_resource_flow(source, sink.kind, dst_identity)
            return
        vias = [via for arg_name, via in arg_names if arg_name in tainted]
        if not vias:
            return
        callee = self._resolve(
            raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
        )
        if callee is None:
            return
        callee_type, callee_qn = callee
        for via in vias:
            self.ingestor.ensure_relationship_batch(
                ctx.caller_spec,
                cs.RelationshipType.FLOWS_TO,
                (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
            )

    def _return_taint_source(
        self, node: Node, tainted: dict[str, frozenset[HandleBinding]], ctx: _FlowCtx
    ) -> frozenset[HandleBinding] | None:
        # (H) The origin set this return yields, or None if it returns nothing
        # (H) tainted. An empty set means tainted-but-unknown-origin. `return a, b`
        # (H) unwraps to several value nodes; their origins union.
        sources: set[HandleBinding] = set()
        tainted_here = False
        for child in _return_value_nodes(node):
            if child.type == cs.TS_PY_IDENTIFIER and child.text is not None:
                name = child.text.decode(cs.ENCODING_UTF8)
                if name in tainted:
                    tainted_here = True
                    sources |= tainted[name]
            elif child.type == cs.TS_PY_CALL and (raw := call_name(child)) is not None:
                if seed := self._source_binding(
                    child, raw, ctx.import_map, ctx.read_sinks
                ):
                    tainted_here = True
                    sources.add(seed)
                    continue
                callee = self._resolve(
                    raw, ctx.module_qn, ctx.class_context, ctx.caller_qn, ctx.language
                )
                if callee is not None and callee[1] in self._returns_taint:
                    # (H) A directly-returned tainted callee flows into this caller
                    # (H) exactly like an assigned one, so emit its return edge.
                    self._emit_return_edge(callee, ctx.caller_spec)
                    tainted_here = True
                    sources |= self._returns_taint[callee[1]]
        return frozenset(sources) if tainted_here else None

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
