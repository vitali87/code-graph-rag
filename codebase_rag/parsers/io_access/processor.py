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
    head_is_genuine_module,
    is_require_alias,
    literal_target,
    match_normalised,
    registry_match,
    scope_seed_nodes,
    string_literal,
)
from .models import HandleBinding, HandleConstructor, IOSink
from .registry import (
    IO_HANDLE_CONSTRUCTORS,
    IO_HANDLE_METHODS,
    IO_MEMBER_READS,
    IO_SINKS,
)

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
                    caller_node,
                    caller_spec,
                    import_map,
                    sink_by_name,
                    IO_MEMBER_READS.get(language, ()),
                    descriptor,
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
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Lean non-Python walk (issue #714): find call sinks in the caller body,
        # (H) crediting I/O to the scope that runs it. No handle/stream tracking yet.
        # (H) The walk is lexically scope-aware so a same-named local (`const fs`,
        # (H) `function fetch`, a parameter) shadows the builtin ONLY where it is in
        # (H) scope: block-scoped const/let shadow inside their own block and nested
        # (H) blocks, never a sibling/outer use. A function/method caller exposes its
        # (H) statements under the `body` field; the module root (top-level calls) has
        # (H) no body field, so seed from its own children.
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            # (H) Module root (top-level calls): its own children are the statements.
            statements = list(caller_node.named_children)
        elif body.type == descriptor.block_scope_type:
            statements = list(body.named_children)
        else:
            # (H) Expression-bodied arrow (`() => fetch(url)`): the body IS the
            # (H) statement/expression, so walk it directly.
            statements = [body]
        # (H) Parameters are visible in every block of the function body and always
        # (H) shadow a same-named builtin (a parameter is never an import alias).
        params = self._param_names(caller_node, descriptor)
        self._walk_scope(
            statements,
            frozenset(params),
            caller_spec,
            import_map,
            sink_by_name,
            member_reads,
            descriptor,
        )

    def _walk_scope(
        self,
        statements: list[Node],
        inherited: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Names in scope for calls in THESE statements: the enclosing scopes'
        # (H) names plus this block's own const/let/function declarations. A
        # (H) `const fs = require('fs')` declarator is an import alias (the genuine
        # (H) module, resolved by _resolve_sink), so _block_declarations skips it;
        # (H) but a local `const fs = {}` IS a shadow, even if `fs` is imported
        # (H) module-wide, so import names are NOT blanket-removed here.
        in_scope = inherited | self._block_declarations(statements, descriptor)
        stack = list(statements)
        while stack:
            node = stack.pop()
            # (H) Nested function/method: its own caller, walked separately.
            if node.type in descriptor.nested_scope_types:
                continue
            # (H) A nested { } is a child lexical scope: recurse with this block's
            # (H) names inherited, so its declarations shadow only inside it.
            if node.type == descriptor.block_scope_type:
                self._walk_scope(
                    list(node.named_children),
                    in_scope,
                    caller_spec,
                    import_map,
                    sink_by_name,
                    member_reads,
                    descriptor,
                )
                continue
            if node.type == descriptor.call_type:
                self._emit_direct_call(
                    node, caller_spec, import_map, sink_by_name, descriptor, in_scope
                )
            elif member_reads and node.type in (
                descriptor.member_expression_type,
                descriptor.subscript_type,
            ):
                self._emit_member_read(
                    node, caller_spec, member_reads, in_scope, import_map, descriptor
                )
            stack.extend(node.named_children)

    def _emit_member_read(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Env-style member reads: `process.env.X` (member) / `process.env['X']`
        # (H) (subscript) read env var X. Match on the object prefix; skip when the
        # (H) prefix head (`process`) is shadowed by a local binding or a non-global
        # (H) import, mirroring the call-sink shadow rules.
        obj = node.child_by_field_name(descriptor.object_field)
        if obj is None or obj.text is None:
            return
        obj_text = obj.text.decode(cs.ENCODING_UTF8)
        for prefix, kind in member_reads:
            if obj_text != prefix:
                continue
            head = prefix.partition(cs.SEPARATOR_DOT)[0]
            if head in in_scope or not head_is_genuine_module(
                import_map.get(head), head
            ):
                return
            identity = self._member_identity(node, descriptor)
            self._emit(caller_spec, IODirection.READ, kind, identity)
            return

    def _member_identity(self, node: Node, descriptor: LanguageDescriptor) -> str:
        # (H) The accessed key: a member's `property` (`process.env.SECRET` -> SECRET),
        # (H) or a subscript's string index (`process.env['T']` -> T); else <dynamic>.
        if node.type == descriptor.member_expression_type:
            prop = node.child_by_field_name(descriptor.property_field)
            if prop is not None and prop.text is not None:
                return prop.text.decode(cs.ENCODING_UTF8)
            return DYNAMIC_TARGET
        index = node.child_by_field_name(descriptor.subscript_index_field)
        if index is not None and index.type == descriptor.string_type:
            return string_literal(
                index, descriptor.string_type, descriptor.string_content_type
            )
        return DYNAMIC_TARGET

    def _block_declarations(
        self, statements: list[Node], descriptor: LanguageDescriptor
    ) -> set[str]:
        # (H) Names declared directly in these statements: const/let/var declarators
        # (H) and hoisted `function` declarations. Nested blocks and nested functions
        # (H) are their own scopes and are not descended into (their locals are theirs).
        # (H) ponytail: `var` is really function-scoped, but a var redefining a
        # (H) builtin name is rare enough to treat block-locally.
        names: set[str] = set()
        stack = list(statements)
        while stack:
            node = stack.pop()
            if node.type in descriptor.nested_scope_types:
                if (name := self._named_child_text(node, descriptor)) is not None:
                    names.add(name)
                continue
            if node.type == descriptor.block_scope_type:
                continue
            if node.type == descriptor.declarator_type and not is_require_alias(
                node, descriptor.call_type
            ):
                names |= self._declarator_names(node, descriptor)
            stack.extend(node.named_children)
        return names

    def _declarator_names(
        self, declarator: Node, descriptor: LanguageDescriptor
    ) -> set[str]:
        # (H) The local names a declarator binds: JS `const fs = ...` / destructuring
        # (H) uses the `name` field; Go `os := ...` uses the `left` field (an
        # (H) expression_list of identifiers). All are collected so they shadow a
        # (H) same-named builtin/package.
        target = declarator.child_by_field_name(
            cs.TS_FIELD_NAME
        ) or declarator.child_by_field_name(cs.FIELD_LEFT)
        names: set[str] = set()
        if target is not None:
            self._pattern_names(target, descriptor, names)
        return names

    def _pattern_names(
        self, node: Node, descriptor: LanguageDescriptor, out: set[str]
    ) -> None:
        node_type = node.type
        if node_type in (
            descriptor.identifier_type,
            cs.TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN,
        ):
            if node.text:
                out.add(node.text.decode(cs.ENCODING_UTF8))
        elif node_type == cs.TS_PAIR_PATTERN:
            # (H) `{ key: local }` binds the VALUE (local), not the property key.
            if (value := node.child_by_field_name(cs.FIELD_VALUE)) is not None:
                self._pattern_names(value, descriptor, out)
        elif node_type == cs.TS_GO_PARAMETER_DECLARATION:
            # (H) Go `func f(os Config)`: the `name` field(s) are the bound locals.
            for child in node.children_by_field_name(cs.TS_FIELD_NAME):
                self._pattern_names(child, descriptor, out)
        elif node_type in (
            cs.TS_OBJECT_PATTERN,
            cs.TS_ARRAY_PATTERN,
            cs.TS_REST_PATTERN,
            cs.TS_GO_EXPRESSION_LIST,
        ):
            for child in node.named_children:
                self._pattern_names(child, descriptor, out)

    def _param_names(
        self, caller_node: Node, descriptor: LanguageDescriptor
    ) -> set[str]:
        # (H) Parameter names bound in the whole function body. A param is a bare
        # (H) identifier, a destructuring pattern (`function f({ http }) {}`), a TS
        # (H) `required_parameter` wrapper whose `pattern` field holds either, or a Go
        # (H) `parameter_declaration` (`os Config`) -- all handled by _pattern_names,
        # (H) unwrapping the TS wrapper first.
        names: set[str] = set()
        params = caller_node.child_by_field_name(descriptor.params_field)
        if params is not None:
            for child in params.named_children:
                target = child.child_by_field_name(cs.TS_FIELD_PATTERN) or child
                self._pattern_names(target, descriptor, names)
        return names

    @staticmethod
    def _named_child_text(node: Node, descriptor: LanguageDescriptor) -> str | None:
        name = node.child_by_field_name(cs.TS_FIELD_NAME)
        if name is not None and name.type == descriptor.identifier_type and name.text:
            return name.text.decode(cs.ENCODING_UTF8)
        return None

    def _emit_direct_call(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        descriptor: LanguageDescriptor,
        local_names: frozenset[str],
    ) -> None:
        raw = call_name(node)
        if raw is None:
            return
        sink = self._resolve_sink(raw, import_map, sink_by_name, local_names)
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

    @staticmethod
    def _resolve_sink(
        raw: str,
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        local_names: frozenset[str],
    ) -> IOSink | None:
        # (H) Match a JS/TS call against the sink table, respecting shadowing:
        # (H)  - a name bound locally (a local `const fs`, `function fetch`, or a
        # (H)    parameter) is never the builtin -> no match.
        # (H)  - the import-normalised name is tried first, so an ALIASED builtin
        # (H)    (`const myfs = require('fs')` -> myfs.x resolves to fs.x) matches.
        # (H)  - the raw dotted name is a last resort, allowed only when the head
        # (H)    resolves to the genuine module (a builtin maps `fs` -> `fs` /
        # (H)    `fs.default` / `node:fs...`; a local `import fs from './x'` maps it
        # (H)    elsewhere, so its raw `fs.writeFileSync` must not fire).
        head, sep, _ = raw.partition(cs.SEPARATOR_DOT)
        if (head if sep else raw) in local_names:
            return None
        # (H) The import-normalised name matches first: a JS named import may resolve
        # (H) to `node:fs.writeFileSync` (node:-stripped too), and a Go call resolves
        # (H) through its package path (`http.Get` -> `net/http.Get`, aliases included),
        # (H) which the registry keys on -- so a third-party pkg named `http` misses.
        if (sink := match_normalised(raw, import_map, sink_by_name)) is not None:
            return sink
        if not sep:
            return None
        if not head_is_genuine_module(import_map.get(head), head):
            return None
        return sink_by_name.get(raw)

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
