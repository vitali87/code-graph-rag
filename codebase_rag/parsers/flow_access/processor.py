from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..call_resolver import CallResolver
from ..import_processor import ImportProcessor
from ..io_access import (
    IO_SINKS,
    RESOURCE_QN_FORMAT,
    HandleBinding,
    IODirection,
    IOSink,
    ResourceKind,
    call_name,
    literal_target,
    normalise,
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

# (H) Nested defs/classes are separate callers processed on their own; pruning them
# (H) keeps a nested body's taint out of this caller's flow.
_SCOPE_BOUNDARIES = (
    cs.TS_PY_FUNCTION_DEFINITION,
    cs.TS_PY_CLASS_DEFINITION,
    cs.TS_PY_DECORATED_DEFINITION,
)

# (H) One collected assignment `lhs = <call>` whose callee is not a read source;
# (H) resolved later against _returns_taint for return-flow.
_CallAssign = tuple[str, Node, str]
# (H) One collected call site as (node, raw callee name).
_CallSite = tuple[Node, str]


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
        # (H) QNs whose body returns a tainted value; consulted for return-flow.
        # (H) ponytail: single-pass and source-order dependent (a callee defined
        # (H) after its caller is missed), exactly like CallProcessor._returned_callables;
        # (H) a finalize/fixpoint pass is the upgrade if cross-file recall matters.
        self._returns_taint: set[str] = set()

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
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        read_sinks = {s.callee: s for s in sinks if s.direction == IODirection.READ}
        write_sinks = {s.callee: s for s in sinks if s.direction == IODirection.WRITE}

        plain_assigns: list[tuple[str, str]] = []
        source_seeds: dict[str, HandleBinding] = {}
        call_assigns: list[_CallAssign] = []
        all_calls: list[_CallSite] = []
        returned_names: list[str] = []
        returned_source = False

        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            node_type = node.type
            if node_type in _SCOPE_BOUNDARIES:
                continue
            stack.extend(node.children)
            if node_type == cs.TS_PY_ASSIGNMENT:
                self._collect_assignment(
                    node,
                    import_map,
                    read_sinks,
                    plain_assigns,
                    source_seeds,
                    call_assigns,
                )
            elif node_type == cs.TS_PY_CALL:
                if (raw := call_name(node)) is not None:
                    all_calls.append((node, raw))
            elif node_type == cs.TS_PY_RETURN_STATEMENT:
                returned_source = (
                    self._collect_return(node, import_map, read_sinks, returned_names)
                    or returned_source
                )

        tainted: dict[str, HandleBinding | None] = dict(source_seeds)
        return_edges: list[tuple[str, str]] = []
        for lhs, _node, raw in call_assigns:
            callee = self._resolve(raw, module_qn, class_context, caller_qn, language)
            if callee is not None and callee[1] in self._returns_taint:
                tainted.setdefault(lhs, None)
                return_edges.append(callee)

        # (H) ponytail: order-insensitive worklist to a fixpoint; a var read before its
        # (H) assignment is over-tainted. Bodies are small; add source-order/CFG
        # (H) sensitivity only if false positives from that ever show up.
        changed = True
        while changed:
            changed = False
            for lhs, rhs in plain_assigns:
                if rhs in tainted and lhs not in tainted:
                    tainted[lhs] = tainted[rhs]
                    changed = True

        self._emit_resource_flows(all_calls, write_sinks, import_map, tainted)
        self._emit_arg_flows(
            all_calls,
            caller_spec,
            tainted,
            module_qn,
            class_context,
            caller_qn,
            language,
        )
        for callee_type, callee_qn in return_edges:
            self.ingestor.ensure_relationship_batch(
                (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                cs.RelationshipType.FLOWS_TO,
                caller_spec,
                properties={KEY_VIA: VIA_RETURN, KEY_KIND: FlowKind.RETURN.value},
            )

        if returned_source or any(name in tainted for name in returned_names):
            self._returns_taint.add(caller_qn)

    def _collect_assignment(
        self,
        node: Node,
        import_map: dict[str, str],
        read_sinks: dict[str, IOSink],
        plain_assigns: list[tuple[str, str]],
        source_seeds: dict[str, HandleBinding],
        call_assigns: list[_CallAssign],
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
            plain_assigns.append((lhs, right.text.decode(cs.ENCODING_UTF8)))
        elif right.type == cs.TS_PY_CALL and (raw := call_name(right)) is not None:
            if seed := self._source_binding(right, raw, import_map, read_sinks):
                source_seeds[lhs] = seed
            else:
                call_assigns.append((lhs, right, raw))

    @staticmethod
    def _source_binding(
        call_node: Node,
        raw_name: str,
        import_map: dict[str, str],
        read_sinks: dict[str, IOSink],
    ) -> HandleBinding | None:
        name = normalise(raw_name, import_map)
        sink = read_sinks.get(name) if name else None
        if sink is None:
            return None
        identity = literal_target(call_node, sink.target_arg, sink.target_kw)
        return HandleBinding(kind=sink.kind, identity=identity)

    def _collect_return(
        self,
        node: Node,
        import_map: dict[str, str],
        read_sinks: dict[str, IOSink],
        returned_names: list[str],
    ) -> bool:
        returned_source = False
        for child in node.named_children:
            if child.type == cs.TS_PY_IDENTIFIER and child.text is not None:
                returned_names.append(child.text.decode(cs.ENCODING_UTF8))
            elif child.type == cs.TS_PY_CALL and (raw := call_name(child)) is not None:
                if self._source_binding(child, raw, import_map, read_sinks):
                    returned_source = True
        return returned_source

    def _emit_resource_flows(
        self,
        all_calls: list[_CallSite],
        write_sinks: dict[str, IOSink],
        import_map: dict[str, str],
        tainted: dict[str, HandleBinding | None],
    ) -> None:
        for node, raw in all_calls:
            name = normalise(raw, import_map)
            sink = write_sinks.get(name) if name else None
            if sink is None:
                continue
            dst_identity = literal_target(node, sink.target_arg, sink.target_kw)
            for arg_name, _via in self._arg_names(node):
                if (source := tainted.get(arg_name)) is not None:
                    self._emit_resource_flow(source, sink.kind, dst_identity)

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

    def _emit_arg_flows(
        self,
        all_calls: list[_CallSite],
        caller_spec: tuple[str, str, str],
        tainted: dict[str, HandleBinding | None],
        module_qn: str,
        class_context: str | None,
        caller_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        for node, raw in all_calls:
            vias = [via for name, via in self._arg_names(node) if name in tainted]
            if not vias:
                continue
            callee = self._resolve(raw, module_qn, class_context, caller_qn, language)
            if callee is None:
                continue
            callee_type, callee_qn = callee
            for via in vias:
                self.ingestor.ensure_relationship_batch(
                    caller_spec,
                    cs.RelationshipType.FLOWS_TO,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                    properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
                )

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
