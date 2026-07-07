from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...services import IngestorProtocol
from ..import_processor import ImportProcessor
from .constants import (
    DYNAMIC_TARGET,
    KEY_KIND,
    RESOURCE_QN_FORMAT,
    SQL_READ_KEYWORDS,
    SQL_WRITE_KEYWORDS,
    IODirection,
    ResourceKind,
)
from .models import HandleBinding, HandleConstructor, IOSink
from .registry import IO_HANDLE_CONSTRUCTORS, IO_HANDLE_METHODS, IO_SINKS

_DIRECTION_REL = {
    IODirection.READ: cs.RelationshipType.READS_FROM,
    IODirection.WRITE: cs.RelationshipType.WRITES_TO,
}


class IOAccessProcessor:
    """Detects I/O reads/writes in a function body and emits READS_FROM /
    WRITES_TO edges to synthetic Resource nodes."""

    def __init__(
        self,
        ingestor: IngestorProtocol,
        import_processor: ImportProcessor,
        enabled: bool = True,
    ) -> None:
        self.ingestor = ingestor
        # (H) import_processor owns import_mapping[module_qn][local] = full_name,
        # (H) used to expand a callee head token to its imported module path.
        self._import_processor = import_processor
        # (H) When the io capture group is disabled, skip the body walk entirely
        # (H) (the filtering ingestor would drop the edges anyway, but this also
        # (H) saves the work).
        self._enabled = enabled

    def process_io_for_caller(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if not self._enabled:
            return
        # (H) ponytail: Python-only in phase 1; other languages need their own
        # (H) node types, add when their IO_SINKS tables land.
        if language != cs.SupportedLanguage.PYTHON:
            return
        sinks = IO_SINKS.get(language, ())
        constructors = IO_HANDLE_CONSTRUCTORS.get(language, ())
        if not sinks and not constructors:
            return
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        sink_by_name = {s.callee: s for s in sinks}
        ctor_by_name = {c.callee: c for c in constructors}

        handles = self._collect_handle_bindings(caller_node, import_map, ctor_by_name)
        self._emit_access_edges(
            caller_node, caller_spec, import_map, sink_by_name, handles
        )

    def _collect_handle_bindings(
        self,
        caller_node: Node,
        import_map: dict[str, str],
        ctor_by_name: dict[str, HandleConstructor],
    ) -> dict[str, HandleBinding]:
        handles: dict[str, HandleBinding] = {}
        # (H) Forward pre-order DFS so a rebind resolves to the last binding in
        # (H) source order; `reversed` on the pushed children keeps left-to-right.
        # (H) ponytail: source-order-last wins; control-flow branches are not
        # (H) path-sensitive, add a CFG pass if branch-precise handles ever matter.
        stack = [caller_node]
        while stack:
            node = stack.pop()
            stack.extend(reversed(node.children))
            bound = self._binding_from_node(node, import_map, ctor_by_name)
            if bound is not None:
                var, binding = bound
                handles[var] = binding
        return handles

    def _binding_from_node(
        self,
        node: Node,
        import_map: dict[str, str],
        ctor_by_name: dict[str, HandleConstructor],
    ) -> tuple[str, HandleBinding] | None:
        # (H) Both `f = open(...)` (assignment) and `with open(...) as f:`
        # (H) (as_pattern) bind a handle var to a constructor call.
        if node.type == cs.TS_PY_ASSIGNMENT:
            target = node.child_by_field_name(cs.TS_FIELD_LEFT)
            call = node.child_by_field_name(cs.TS_FIELD_RIGHT)
        elif node.type == cs.TS_PY_AS_PATTERN:
            call = next((c for c in node.children if c.type == cs.TS_PY_CALL), None)
            alias = next(
                (c for c in node.children if c.type == cs.TS_PY_AS_PATTERN_TARGET),
                None,
            )
            target = alias.children[0] if alias and alias.children else None
        else:
            return None
        if (
            target is None
            or call is None
            or target.type != cs.TS_PY_IDENTIFIER
            or call.type != cs.TS_PY_CALL
            or target.text is None
        ):
            return None
        name = self._normalise(self._call_name(call), import_map)
        ctor = ctor_by_name.get(name) if name else None
        if ctor is None:
            return None
        identity = self._literal_target(call, ctor.target_arg, ctor.target_kw)
        return target.text.decode(cs.ENCODING_UTF8), HandleBinding(
            kind=ctor.kind, identity=identity
        )

    def _emit_access_edges(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        handles: dict[str, HandleBinding],
    ) -> None:
        stack = list(caller_node.children)
        while stack:
            node = stack.pop()
            stack.extend(node.children)
            if node.type != cs.TS_PY_CALL:
                continue
            raw = self._call_name(node)
            if raw is None:
                continue
            if self._emit_handle_method(node, caller_spec, raw, handles):
                continue
            normalised = self._normalise(raw, import_map)
            sink = sink_by_name.get(normalised) if normalised else None
            if sink is None:
                continue
            mode = (
                self._literal_target(node, sink.mode_arg, sink.mode_kw)
                if sink.mode_arg is not None or sink.mode_kw is not None
                else None
            )
            mode_literal = None if mode == DYNAMIC_TARGET else mode
            direction = sink.effective_direction(mode_literal)
            identity = self._literal_target(node, sink.target_arg, sink.target_kw)
            self._emit(caller_spec, direction, sink.kind, identity)

    def _emit_handle_method(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        raw_name: str,
        handles: dict[str, HandleBinding],
    ) -> bool:
        receiver, sep, method = raw_name.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return False
        binding = handles.get(receiver)
        if binding is None:
            return False
        methods = IO_HANDLE_METHODS.get(binding.kind, {})
        direction = methods.get(method)
        if direction is None:
            return False
        if binding.kind == ResourceKind.DATABASE and method.startswith("execute"):
            direction = self._sql_direction(call_node, direction)
        self._emit(caller_spec, direction, binding.kind, binding.identity)
        return True

    def _sql_direction(self, call_node: Node, fallback: IODirection) -> IODirection:
        # (H) ponytail: first-keyword heuristic only; a full SQL parse is the
        # (H) upgrade path if execute() direction precision ever matters.
        sql = self._literal_target(call_node, 0)
        if sql == DYNAMIC_TARGET:
            return fallback
        head = sql.strip().split(maxsplit=1)[0].upper() if sql.strip() else ""
        if head in SQL_READ_KEYWORDS:
            return IODirection.READ
        if head in SQL_WRITE_KEYWORDS:
            return IODirection.WRITE
        return fallback

    def _emit(
        self,
        caller_spec: tuple[str, str, str],
        direction: IODirection,
        kind: ResourceKind,
        identity: str,
    ) -> None:
        resource_qn = RESOURCE_QN_FORMAT.format(kind=kind.value, identity=identity)
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.RESOURCE,
            {
                cs.KEY_QUALIFIED_NAME: resource_qn,
                cs.KEY_NAME: identity,
                KEY_KIND: kind.value,
            },
        )
        for rel in self._rels(direction):
            self.ingestor.ensure_relationship_batch(
                caller_spec,
                rel,
                (cs.NodeLabel.RESOURCE, cs.KEY_QUALIFIED_NAME, resource_qn),
            )

    @staticmethod
    def _rels(direction: IODirection) -> tuple[cs.RelationshipType, ...]:
        if direction == IODirection.READ_WRITE:
            return (
                cs.RelationshipType.READS_FROM,
                cs.RelationshipType.WRITES_TO,
            )
        return (_DIRECTION_REL[direction],)

    @staticmethod
    def _call_name(call_node: Node) -> str | None:
        fn = call_node.child_by_field_name(cs.TS_FIELD_FUNCTION)
        if fn is None or fn.text is None:
            return None
        return fn.text.decode(cs.ENCODING_UTF8)

    @staticmethod
    def _normalise(name: str | None, import_map: dict[str, str]) -> str | None:
        if name is None:
            return None
        head, sep, rest = name.partition(cs.SEPARATOR_DOT)
        base = import_map.get(head)
        if base is None:
            return name
        return f"{base}{cs.SEPARATOR_DOT}{rest}" if rest else base

    @staticmethod
    def _literal_target(
        call_node: Node, arg_index: int | None, arg_keyword: str | None = None
    ) -> str:
        if arg_index is None and arg_keyword is None:
            return DYNAMIC_TARGET
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return DYNAMIC_TARGET
        positional = [
            c for c in args.named_children if c.type != cs.TS_PY_KEYWORD_ARGUMENT
        ]
        if arg_index is not None and arg_index < len(positional):
            return IOAccessProcessor._string_literal(positional[arg_index])
        if arg_keyword is not None:
            return IOAccessProcessor._string_literal(
                IOAccessProcessor._keyword_value(args, arg_keyword)
            )
        return DYNAMIC_TARGET

    @staticmethod
    def _keyword_value(args: Node, keyword: str) -> Node | None:
        for child in args.named_children:
            if child.type != cs.TS_PY_KEYWORD_ARGUMENT:
                continue
            name = child.child_by_field_name(cs.TS_FIELD_NAME)
            if name is not None and name.text is not None:
                if name.text.decode(cs.ENCODING_UTF8) == keyword:
                    return child.child_by_field_name(cs.FIELD_VALUE)
        return None

    @staticmethod
    def _string_literal(arg: Node | None) -> str:
        if arg is None or arg.type != cs.TS_PY_STRING:
            return DYNAMIC_TARGET
        for child in arg.named_children:
            if child.type == cs.TS_PY_STRING_CONTENT and child.text is not None:
                return child.text.decode(cs.ENCODING_UTF8)
        return DYNAMIC_TARGET
