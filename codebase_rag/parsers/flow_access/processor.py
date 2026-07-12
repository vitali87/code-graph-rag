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

        # (H) Source-ordered pre-order walk with a live taint map: push children
        # (H) reversed so the leftmost is visited first, giving straight-line
        # (H) statement order. Each assignment retaints or KILLS its target, so a
        # (H) later overwrite with an untainted value removes stale taint (no
        # (H) monotonic false positive). ponytail: not path-sensitive; a KILL on one
        # (H) branch of an if/else drops taint conservatively -- add a CFG pass only
        # (H) if branch precision ever matters.
        tainted: dict[str, frozenset[HandleBinding]] = {}
        body_returns_taint = False
        body_return_sources: set[HandleBinding] = set()
        # (H) Seed from the caller's own scope; on a nested def descend into its
        # (H) header only (default args/decorators/bases run in THIS scope, its
        # (H) body is a separate caller). Same scoping as io_access.
        stack = list(reversed(scope_seed_nodes(caller_node)))
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in PY_SCOPE_BOUNDARIES:
                stack.extend(reversed(definition_header_nodes(node)))
                continue
            if node_type == cs.TS_PY_ASSIGNMENT:
                self._apply_assignment(node, tainted, ctx)
            elif node_type == cs.TS_PY_CALL:
                self._apply_call(node, tainted, ctx)
            elif node_type == cs.TS_PY_RETURN_STATEMENT:
                # (H) Process every return: each `return callee()` emits its own
                # (H) callee->caller edge, and the body's returned-source set is the
                # (H) UNION over all returns, so branches returning DIFFERENT tainted
                # (H) sources each carry to callers of this function.
                sources = self._return_taint_source(node, tainted, ctx)
                if sources is not None:
                    body_returns_taint = True
                    body_return_sources |= sources
            stack.extend(reversed(node.children))

        if body_returns_taint:
            self._returns_taint[caller_qn] = frozenset(body_return_sources)

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
