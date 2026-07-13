from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..import_processor import ImportProcessor
from .constants import (
    DYNAMIC_TARGET,
    KEY_KIND,
    PY_SCOPE_BOUNDARIES,
    RESOURCE_QN_FORMAT,
    SQL_READ_KEYWORDS,
    SQL_WRITE_KEYWORDS,
    IODirection,
    ResourceKind,
)
from .descriptor import LANGUAGE_DESCRIPTORS, LanguageDescriptor
from .extract import (
    call_name,
    definition_header_nodes,
    literal_target,
    registry_match,
    scope_seed_nodes,
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
        selection: CaptureSelection,
    ) -> None:
        self.ingestor = ingestor
        # (H) import_processor owns import_mapping[module_qn][local] = full_name,
        # (H) used to expand a callee head token to its imported module path.
        self._import_processor = import_processor
        # (H) When neither I/O edge is enabled, skip the body walk entirely.
        self._selection = selection
        self._enabled = selection.io_enabled

    def process_io_for_caller(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        module_qn: str,
        language: cs.SupportedLanguage,
    ) -> None:
        if not self._enabled:
            return
        sinks = IO_SINKS.get(language, ())
        constructors = IO_HANDLE_CONSTRUCTORS.get(language, ())
        if not sinks and not constructors:
            return
        import_map = self._import_processor.import_mapping.get(module_qn, {})
        sink_by_name = {s.callee: s for s in sinks}
        # (H) Non-Python languages take a lean direct-sink walk (issue #714): match
        # (H) call sinks and emit, without Python's handle/scope machinery (streams
        # (H) and data-flow are a follow-up). Python keeps the full handle-aware walk.
        if language != cs.SupportedLanguage.PYTHON:
            descriptor = LANGUAGE_DESCRIPTORS.get(language)
            if descriptor is not None:
                self._emit_direct_sinks(
                    caller_node, caller_spec, import_map, sink_by_name, descriptor
                )
            return
        ctor_by_name = {c.callee: c for c in constructors}

        # (H) Single forward pre-order DFS: bindings and accesses interleave in
        # (H) source order, so a handle method resolves only against bindings seen
        # (H) before it (no forward-reference edge) and a rebind resolves to the
        # (H) last prior assignment. `reversed` keeps children left-to-right.
        # (H) ponytail: source-order, not path-sensitive; add a CFG pass if
        # (H) branch-precise handle resolution ever matters.
        # (H) Seed from the caller's OWN scope (a def/class contributes only its
        # (H) body block; a module every child) and, on hitting a nested def,
        # (H) descend into its HEADER only -- default args, annotations, bases and
        # (H) decorators execute in THIS scope at definition time, while the
        # (H) nested body is its own caller. So a read/write is credited to the
        # (H) scope that actually runs it (matches flow_access and CALLS).
        # (H) Seed with handles bound in ENCLOSING scopes -- an instance attribute
        # (H) set in another method (`self.conn = sqlite3.connect(...)` in __init__)
        # (H) or a module/outer-function local -- so a handle method here resolves
        # (H) against the scope that constructed it, not only same-body bindings. A
        # (H) local rebind in this body shadows the inherited one (DFS overwrites).
        # (H) But Python makes a name local for the WHOLE function if it is assigned
        # (H) anywhere in the body, so an inherited plain-name handle is invisible
        # (H) even before that assignment (a use before it is UnboundLocalError).
        # (H) Drop inherited plain names rebound locally; the DFS re-adds them at the
        # (H) assignment, so uses after it still resolve. Attribute keys (self.x) are
        # (H) never locals, so they are unaffected.
        handles = self._inherited_handles(caller_node, import_map, ctor_by_name)
        for name in self._locally_assigned_names(caller_node):
            if cs.SEPARATOR_DOT not in name:
                handles.pop(name, None)
        stack = list(reversed(scope_seed_nodes(caller_node)))
        while stack:
            node = stack.pop()
            if node.type in PY_SCOPE_BOUNDARIES:
                stack.extend(reversed(definition_header_nodes(node)))
                continue
            bound = self._binding_from_node(node, import_map, ctor_by_name)
            if bound is not None:
                var, binding = bound
                handles[var] = binding
            elif node.type == cs.TS_PY_CALL:
                self._emit_call(node, caller_spec, import_map, sink_by_name, handles)
            stack.extend(reversed(node.children))

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
        # (H) `f = open(...)` binds a plain name; `self.f = open(...)` binds an
        # (H) attribute -- keep the full dotted text ("self.f") as the handle key so
        # (H) a later `self.f.write(...)` resolves against it.
        if (
            target is None
            or call is None
            or target.type not in (cs.TS_PY_IDENTIFIER, cs.TS_PY_ATTRIBUTE)
            or call.type != cs.TS_PY_CALL
            or target.text is None
        ):
            return None
        ctor = registry_match(ctor_by_name, call_name(call), import_map)
        if ctor is None:
            return None
        identity = literal_target(call, ctor.target_arg, ctor.target_kw)
        return target.text.decode(cs.ENCODING_UTF8), HandleBinding(
            kind=ctor.kind, identity=identity
        )

    def _inherited_handles(
        self,
        caller_node: Node,
        import_map: dict[str, str],
        ctor_by_name: dict[str, HandleConstructor],
    ) -> dict[str, HandleBinding]:
        # (H) Handle bindings visible from ENCLOSING scopes, walked innermost-first
        # (H) so a nearer scope shadows a farther one (setdefault keeps the first
        # (H) seen). An enclosing class contributes its `self.<attr>` handles (set in
        # (H) any method); an enclosing function/module contributes its top-level
        # (H) local handles. Nested scopes are pruned -- their locals are not visible.
        handles: dict[str, HandleBinding] = {}
        class_scanned = False
        node = caller_node.parent
        while node is not None:
            if node.type == cs.TS_PY_CLASS_DEFINITION:
                if not class_scanned:
                    class_scanned = True
                    self._collect_self_attr_handles(
                        node, import_map, ctor_by_name, handles
                    )
            elif node.type in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_MODULE):
                self._collect_scope_var_handles(node, import_map, ctor_by_name, handles)
            node = node.parent
        return handles

    def _locally_assigned_names(self, caller_node: Node) -> set[str]:
        # (H) Plain identifiers assigned anywhere in this scope's OWN body (nested
        # (H) defs/classes pruned): assignment / with-as / for targets. Any such name
        # (H) is local for the whole function, so an inherited handle of that name is
        # (H) shadowed. Dotted attribute targets (self.x) are not locals -- skipped.
        names: set[str] = set()
        stack = list(scope_seed_nodes(caller_node))
        while stack:
            node = stack.pop()
            if node.type in PY_SCOPE_BOUNDARIES:
                continue
            target: Node | None = None
            if node.type in (cs.TS_PY_ASSIGNMENT, cs.TS_PY_FOR_STATEMENT):
                target = node.child_by_field_name(cs.TS_FIELD_LEFT)
            elif node.type == cs.TS_PY_AS_PATTERN:
                alias = next(
                    (c for c in node.children if c.type == cs.TS_PY_AS_PATTERN_TARGET),
                    None,
                )
                target = alias.children[0] if alias and alias.children else None
            if (
                target is not None
                and target.type == cs.TS_PY_IDENTIFIER
                and target.text is not None
            ):
                names.add(target.text.decode(cs.ENCODING_UTF8))
            stack.extend(node.children)
        return names

    def _collect_scope_var_handles(
        self,
        scope_node: Node,
        import_map: dict[str, str],
        ctor_by_name: dict[str, HandleConstructor],
        handles: dict[str, HandleBinding],
    ) -> None:
        # (H) Top-level handle bindings of one scope's OWN body; nested defs/classes
        # (H) are pruned (their locals belong to their own scope, not this one).
        stack = list(scope_seed_nodes(scope_node))
        while stack:
            node = stack.pop()
            if node.type in PY_SCOPE_BOUNDARIES:
                continue
            bound = self._binding_from_node(node, import_map, ctor_by_name)
            if bound is not None:
                handles.setdefault(bound[0], bound[1])
            stack.extend(node.children)

    def _collect_self_attr_handles(
        self,
        class_node: Node,
        import_map: dict[str, str],
        ctor_by_name: dict[str, HandleConstructor],
        handles: dict[str, HandleBinding],
    ) -> None:
        # (H) `self.<attr> = <constructor>()` bindings anywhere in the class body
        # (H) (descending method bodies, since __init__ is the usual site); nested
        # (H) classes are skipped because their `self` is a different object.
        stack = list(scope_seed_nodes(class_node))
        while stack:
            node = stack.pop()
            if node.type == cs.TS_PY_CLASS_DEFINITION:
                continue
            bound = self._binding_from_node(node, import_map, ctor_by_name)
            if bound is not None and bound[0].startswith(cs.PY_SELF_PREFIX):
                handles.setdefault(bound[0], bound[1])
            stack.extend(node.children)

    def _emit_direct_sinks(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Lean non-Python walk (issue #714): DFS the caller body for call sinks,
        # (H) pruning nested definitions (their body is a separate caller) so I/O is
        # (H) credited to the scope that runs it. No handle/stream tracking yet.
        # (H) A function/method caller exposes its statements under the `body` field;
        # (H) the module root (top-level calls) has no body field, so seed from its
        # (H) own children instead. named_children skips anonymous punctuation, which
        # (H) JS/TS grammars produce in bulk.
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        seed = body if body is not None else caller_node
        stack = list(seed.named_children)
        while stack:
            node = stack.pop()
            # (H) Nested defs are their own caller; skip entirely. JS default args and
            # (H) decorators evaluate at call time in the nested scope (unlike Python's
            # (H) definition-time headers), so crediting them here would be a false
            # (H) positive -- full pruning is correct for these grammars.
            if node.type in descriptor.nested_scope_types:
                continue
            if node.type == descriptor.call_type:
                self._emit_direct_call(
                    node, caller_spec, import_map, sink_by_name, descriptor
                )
            stack.extend(node.named_children)

    def _emit_direct_call(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        descriptor: LanguageDescriptor,
    ) -> None:
        raw = call_name(node)
        if raw is None:
            return
        sink = registry_match(sink_by_name, raw, import_map)
        if sink is None:
            return
        identity = literal_target(
            node,
            sink.target_arg,
            sink.target_kw,
            string_type=descriptor.string_type,
            content_type=descriptor.string_content_type,
            keyword_arg_type=descriptor.keyword_arg_type,
        )
        self._emit(caller_spec, sink.direction, sink.kind, identity)

    def _emit_call(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        handles: dict[str, HandleBinding],
    ) -> None:
        raw = call_name(node)
        if raw is None:
            return
        if self._emit_handle_method(node, caller_spec, raw, handles):
            return
        sink = registry_match(sink_by_name, raw, import_map)
        if sink is None:
            return
        mode = (
            literal_target(node, sink.mode_arg, sink.mode_kw)
            if sink.mode_arg is not None or sink.mode_kw is not None
            else None
        )
        mode_literal = None if mode == DYNAMIC_TARGET else mode
        direction = sink.effective_direction(mode_literal)
        identity = literal_target(node, sink.target_arg, sink.target_kw)
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
        sql = literal_target(call_node, 0)
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
        # (H) Only enabled edges survive the filter; if none do, skip the Resource
        # (H) node too so selective capture never leaves an orphaned I/O node.
        rels = [r for r in self._rels(direction) if self._selection.rel_enabled(r)]
        if not rels:
            return
        resource_qn = RESOURCE_QN_FORMAT.format(kind=kind.value, identity=identity)
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.RESOURCE,
            {
                cs.KEY_QUALIFIED_NAME: resource_qn,
                cs.KEY_NAME: identity,
                KEY_KIND: kind.value,
            },
        )
        for rel in rels:
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
