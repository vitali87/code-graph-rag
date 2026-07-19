from __future__ import annotations

from collections.abc import Iterator
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..import_processor import ImportProcessor
from ..utils import cpp_declarator_name
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
    binding_targets_values,
    call_name,
    definition_header_nodes,
    first_token_arg_string,
    head_is_genuine_module,
    is_require_alias,
    iter_token_tree_calls,
    literal_target,
    match_normalised,
    registry_match,
    scope_seed_nodes,
    string_literal,
)
from .models import HandleBinding, HandleConstructor, IOSink
from .registry import (
    IO_CALL_HANDLE_WRAPPERS,
    IO_HANDLE_CONSTRUCTORS,
    IO_HANDLE_DERIVES,
    IO_HANDLE_METHODS,
    IO_IDENTITY_UNWRAP_CALLS,
    IO_IDENTITY_UNWRAP_NEW_TYPES,
    IO_LEAN_HANDLE_CONSTRUCTORS,
    IO_LEAN_HANDLE_METHODS,
    IO_MACRO_SINKS,
    IO_MEMBER_READS,
    IO_NEW_HANDLE_CONSTRUCTORS,
    IO_NEW_HANDLE_WRAPPERS,
    IO_SINKS,
    IO_STREAM_SINKS,
    IO_TYPE_HANDLE_CONSTRUCTORS,
)

_DIRECTION_REL = {
    IODirection.READ: cs.RelationshipType.READS_FROM,
    IODirection.WRITE: cs.RelationshipType.WRITES_TO,
}

# (H) Binding assignment nodes shared across the lean grammars: `x = expr` is an
# (H) assignment_expression in JS/Java/Rust/C++ and an assignment_statement in Go.
_LEAN_ASSIGNMENT_TYPES = frozenset(
    {cs.TS_ASSIGNMENT_EXPRESSION, cs.TS_GO_ASSIGNMENT_STATEMENT}
)


class _LeanHandles(NamedTuple):
    # (H) Per-caller handle state for the lean non-Python walk (issue #714): the
    # (H) per-language constructor/wrapper/method tables plus the mutable
    # (H) variable -> HandleBinding map, threaded as one unit.
    ctors: dict[str, HandleConstructor]
    new_ctors: dict[str, HandleConstructor]
    new_wrappers: frozenset[str]
    call_wrappers: dict[str, str]
    type_ctors: dict[str, ResourceKind]
    methods: dict[ResourceKind, dict[str, IODirection]]
    identity_calls: frozenset[str]
    identity_new_types: dict[str, ResourceKind]
    bindings: dict[str, HandleBinding]


def _lean_handles_for(language: cs.SupportedLanguage) -> _LeanHandles | None:
    ctors = {c.callee: c for c in IO_LEAN_HANDLE_CONSTRUCTORS.get(language, ())}
    new_ctors = IO_NEW_HANDLE_CONSTRUCTORS.get(language, {})
    type_ctors = IO_TYPE_HANDLE_CONSTRUCTORS.get(language, {})
    if not ctors and not new_ctors and not type_ctors:
        return None
    return _LeanHandles(
        ctors=ctors,
        new_ctors=new_ctors,
        new_wrappers=IO_NEW_HANDLE_WRAPPERS.get(language, frozenset()),
        call_wrappers=IO_CALL_HANDLE_WRAPPERS.get(language, {}),
        type_ctors=type_ctors,
        methods=IO_LEAN_HANDLE_METHODS.get(language, {}),
        identity_calls=IO_IDENTITY_UNWRAP_CALLS.get(language, frozenset()),
        identity_new_types=IO_IDENTITY_UNWRAP_NEW_TYPES.get(language, {}),
        bindings={},
    )


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
        # (H) Per-language macro sink table (Rust print macros), threaded through the
        # (H) walk to _emit_macro as a parameter (not instance state) so the processor
        # (H) stays stateless, mirroring sink_by_name.
        macro_sinks = IO_MACRO_SINKS.get(language, {})
        # (H) Per-language stream-insertion sink table (C++ std::cout/cerr `<<`), threaded
        # (H) the same way; empty for languages without operator I/O.
        stream_sinks = IO_STREAM_SINKS.get(language, {})
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
                    macro_sinks,
                    stream_sinks,
                    IO_MEMBER_READS.get(language, ()),
                    descriptor,
                    _lean_handles_for(language),
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
            bound = self._binding_from_node(node, import_map, ctor_by_name, handles)
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
        handles: dict[str, HandleBinding],
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
        raw = call_name(call)
        target_name = target.text.decode(cs.ENCODING_UTF8)
        ctor = registry_match(ctor_by_name, raw, import_map)
        if ctor is None:
            # (H) Derive (`cur = conn.cursor()`, issue #714): a method on a bound
            # (H) handle that yields a same-resource sub-handle binds the target
            # (H) to the parent's resource.
            derived = self._derived_python_binding(raw, handles)
            return None if derived is None else (target_name, derived)
        identity = literal_target(call, ctor.target_arg, ctor.target_kw)
        return target_name, HandleBinding(kind=ctor.kind, identity=identity)

    @staticmethod
    def _derived_python_binding(
        raw: str | None, handles: dict[str, HandleBinding]
    ) -> HandleBinding | None:
        if raw is None:
            return None
        receiver, sep, method = raw.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return None
        parent = handles.get(receiver)
        if parent is None or method not in IO_HANDLE_DERIVES.get(
            parent.kind, frozenset()
        ):
            return None
        return parent

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
            bound = self._binding_from_node(node, import_map, ctor_by_name, handles)
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
            bound = self._binding_from_node(node, import_map, ctor_by_name, handles)
            if bound is not None and bound[0].startswith(cs.PY_SELF_PREFIX):
                handles.setdefault(bound[0], bound[1])
            stack.extend(node.children)

    def _emit_direct_sinks(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
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
            macro_sinks,
            stream_sinks,
            member_reads,
            descriptor,
            lean_handles,
        )

    def _walk_scope(
        self,
        statements: list[Node],
        inherited: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> None:
        # (H) Go wraps a block's statements in a single `statement_list`; unwrap it so
        # (H) the source-order walk iterates the real statements, not one container.
        if descriptor.statement_container_type is not None:
            statements = [
                child
                for stmt in statements
                for child in (
                    stmt.named_children
                    if stmt.type == descriptor.statement_container_type
                    else (stmt,)
                )
            ]
        # (H) Names in scope for calls in these statements: the enclosing scopes'
        # (H) names plus this block's own declarations. A `const fs = require('fs')`
        # (H) declarator is an import alias (the genuine module, resolved by
        # (H) _resolve_sink), so _block_declarations skips it; but a local
        # (H) `const fs = {}` IS a shadow, even if `fs` is imported module-wide, so
        # (H) import names are NOT blanket-removed here.
        if descriptor.hoisted_declarations:
            # (H) JS/TS: declarations hoist / are lexically in scope block-wide (a use
            # (H) before a const/let is a TDZ error, not the outer name), so every
            # (H) declaration shadows the whole block at once.
            in_scope = inherited | self._block_declarations(statements, descriptor)
            for stmt in statements:
                self._walk_stmt_sinks(
                    stmt,
                    in_scope,
                    frozenset(),
                    caller_spec,
                    import_map,
                    sink_by_name,
                    macro_sinks,
                    stream_sinks,
                    member_reads,
                    descriptor,
                    lean_handles,
                )
            return
        # (H) Declare-at-point languages (Go, Java): a local is in scope only from its
        # (H) own declaration onward, so grow the shadow set in SOURCE ORDER. A call
        # (H) BEFORE a later same-named local is the real global and still emits; the
        # (H) local shadows only the statements from its own on. Whether the declaring
        # (H) statement's OWN initializer sees the name is language-specific
        # (H) (decl_in_own_initializer): Java adds it BEFORE walking (JLS 6.3:
        # (H) `T System = System.getenv()` and later comma-declarators resolve the
        # (H) local); Go adds it AFTER (scope starts after the ShortVarDecl, so
        # (H) `os := os.Getenv()` still reads the package). Loop-clause vars are always
        # (H) the exception: in scope in the BODY only (not the iterable header,
        # (H) evaluated before the var binds, nor sibling statements), so they are
        # (H) seeded via body_extra and never added to `live` here.
        live = set(inherited)
        for stmt in statements:
            loop_vars = self._loop_declarations(stmt, descriptor)
            plain = self._block_declarations([stmt], descriptor) - loop_vars
            if (
                descriptor.declaration_statement_type is not None
                and stmt.type == descriptor.declaration_statement_type
            ):
                # (H) Java multi-declarator: each declarator's initializer sees only the
                # (H) declarators up to and including itself, so walk them in source order.
                self._walk_declaration_ordered(
                    stmt,
                    frozenset(live),
                    caller_spec,
                    import_map,
                    sink_by_name,
                    macro_sinks,
                    stream_sinks,
                    member_reads,
                    descriptor,
                    lean_handles,
                )
            else:
                # (H) decl_in_own_initializer (Java): the name is in scope in its own
                # (H) initializer, so seed BEFORE walking; Go (=False): the initializer
                # (H) still reads the global, so the name is added only AFTER (below).
                pre = live | plain if descriptor.decl_in_own_initializer else live
                self._walk_stmt_sinks(
                    stmt,
                    frozenset(pre),
                    loop_vars,
                    caller_spec,
                    import_map,
                    sink_by_name,
                    macro_sinks,
                    stream_sinks,
                    member_reads,
                    descriptor,
                    lean_handles,
                )
            live |= plain

    def _walk_declaration_ordered(
        self,
        stmt: Node,
        base_scope: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> None:
        # (H) Walk a declaration statement's declarators in SOURCE ORDER (Java
        # (H) `local_variable_declaration`): a declarator's name enters scope for its own
        # (H) initializer (JLS 6.3) and the following declarators, so an EARLIER
        # (H) initializer's sink is not shadowed by a LATER declarator's name.
        cur = set(base_scope)
        for child in stmt.named_children:
            if child.type != descriptor.declarator_type:
                continue
            cur |= self._declarator_names(child, descriptor)
            self._walk_stmt_sinks(
                child,
                frozenset(cur),
                frozenset(),
                caller_spec,
                import_map,
                sink_by_name,
                macro_sinks,
                stream_sinks,
                member_reads,
                descriptor,
                lean_handles,
            )

    def _loop_declarations(
        self, stmt: Node, descriptor: LanguageDescriptor
    ) -> frozenset[str]:
        # (H) Names bound by a loop clause (Java for-each var, Go `range` var) within
        # (H) this statement: in scope in the loop body only. Stops at nested scopes /
        # (H) blocks so an inner loop's var is not hoisted to the outer statement.
        names: set[str] = set()
        stack = [stmt]
        while stack:
            node = stack.pop()
            if (
                node.type in descriptor.nested_scope_types
                or node.type == descriptor.block_scope_type
            ):
                continue
            if node.type in descriptor.loop_declarator_types:
                names |= self._declarator_names(node, descriptor)
            stack.extend(node.named_children)
        return frozenset(names)

    def _walk_stmt_sinks(
        self,
        stmt: Node,
        in_scope: frozenset[str],
        body_extra: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> None:
        # (H) Emit the direct sinks / member reads within one statement subtree, under
        # (H) the given in-scope shadow set. A nested { } is a child lexical scope:
        # (H) recurse via _walk_scope so its declarations shadow only inside it (and,
        # (H) for declare-at-point langs, in its own source order). body_extra seeds a
        # (H) loop var into the body scope (the loop's own block) without exposing it to
        # (H) the header expressions walked in this flat pass. The LIFO stack pushes
        # (H) children reversed so siblings pop in SOURCE order: a try-with-resources
        # (H) binds its resource handles before its body block reads them.
        stack = [stmt]
        while stack:
            node = stack.pop()
            # (H) Nested function/method: its own caller, walked separately.
            if node.type in descriptor.nested_scope_types:
                continue
            if node.type == descriptor.block_scope_type:
                self._walk_nested_block(
                    node,
                    in_scope | body_extra,
                    caller_spec,
                    import_map,
                    sink_by_name,
                    macro_sinks,
                    stream_sinks,
                    member_reads,
                    descriptor,
                    lean_handles,
                )
                continue
            if lean_handles is not None:
                self._maybe_bind_lean_handle(
                    node, in_scope, import_map, descriptor, lean_handles
                )
            if self._emit_node_sinks(
                node,
                in_scope,
                caller_spec,
                import_map,
                sink_by_name,
                macro_sinks,
                stream_sinks,
                member_reads,
                descriptor,
                lean_handles,
            ):
                stack.extend(reversed(node.named_children))

    def _walk_nested_block(
        self,
        node: Node,
        in_scope: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> None:
        # (H) A nested block is a child lexical scope for handles too: a
        # (H) handle declared inside it is out of scope after the block,
        # (H) so its binding must not leak to later statements (mirrors
        # (H) the in_scope shadow set, which is passed by value).
        snapshot = dict(lean_handles.bindings) if lean_handles is not None else None
        self._walk_scope(
            list(node.named_children),
            in_scope,
            caller_spec,
            import_map,
            sink_by_name,
            macro_sinks,
            stream_sinks,
            member_reads,
            descriptor,
            lean_handles,
        )
        if lean_handles is not None and snapshot is not None:
            lean_handles.bindings.clear()
            lean_handles.bindings.update(snapshot)

    def _emit_node_sinks(
        self,
        node: Node,
        in_scope: frozenset[str],
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> bool:
        # (H) Emit whatever sink `node` itself is; returns whether the walk should
        # (H) descend into its children (False only for macros, whose token-stream
        # (H) body _emit_macro consumes whole).
        if node.type == descriptor.call_type:
            self._emit_direct_call(
                node,
                caller_spec,
                import_map,
                sink_by_name,
                descriptor,
                in_scope,
                lean_handles,
            )
        elif descriptor.macro_type is not None and node.type == descriptor.macro_type:
            # (H) A macro sink (`println!`) writes STDOUT AND may inline a real call
            # (H) sink in its args (`println!("{}", env::var("X"))`); tree-sitter
            # (H) flattens the macro body to raw tokens (no call_expression node), so
            # (H) _emit_macro reconstructs scoped calls from the token stream itself.
            self._emit_macro(
                node,
                caller_spec,
                import_map,
                sink_by_name,
                macro_sinks,
                in_scope,
                descriptor,
            )
            return False
        elif (
            (stream_sinks or lean_handles is not None)
            and descriptor.stream_sink_type is not None
            and node.type == descriptor.stream_sink_type
        ):
            # (H) A stream-insertion sink (`std::cout << x`) or a stream operator on
            # (H) a bound handle (`out << x`, `in >> word`); descend still so a call
            # (H) sink in an inserted operand (`std::cout << getenv("X")`) is caught.
            self._emit_stream_sink(
                node, caller_spec, stream_sinks, descriptor, lean_handles
            )
        elif member_reads and node.type in (
            descriptor.member_expression_type,
            descriptor.subscript_type,
        ):
            self._emit_member_read(
                node, caller_spec, member_reads, in_scope, import_map, descriptor
            )
        return True

    def _emit_stream_sink(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        stream_sinks: dict[str, IOSink],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles | None,
    ) -> None:
        # (H) A `<<` chain like `std::cout << a << b` nests left-associatively:
        # (H) (((cout << a) << b)). Act only at the TOP of the chain (parent is not
        # (H) itself a stream operation) and walk the `left` spine to the base operand.
        # (H) A stream-sink base (cout/cerr) emits ONE STDOUT write; a bound handle
        # (H) base emits a WRITE (`out << x`) or READ (`in >> word`) of its resource
        # (H) (issue #714). A non-stream base (arithmetic `x << 2`) emits nothing.
        op = self._stream_operator(node, descriptor)
        if op is None:
            return
        parent = node.parent
        if parent is not None and self._stream_operator(parent, descriptor) is not None:
            return
        base = node
        while self._stream_operator(base, descriptor) is not None:
            left = base.child_by_field_name(cs.FIELD_LEFT)
            if left is None:
                return
            base = left
        if base.text is None:
            return
        base_text = base.text.decode(cs.ENCODING_UTF8)
        if op == descriptor.stream_sink_operator:
            sink = stream_sinks.get(base_text)
            if sink is not None:
                self._emit(caller_spec, sink.direction, sink.kind, DYNAMIC_TARGET)
                return
        if lean_handles is None:
            return
        binding = lean_handles.bindings.get(base_text)
        if binding is None:
            return
        direction = (
            IODirection.WRITE
            if op == descriptor.stream_sink_operator
            else IODirection.READ
        )
        self._emit(caller_spec, direction, binding.kind, binding.identity)

    @staticmethod
    def _stream_operator(node: Node, descriptor: LanguageDescriptor) -> str | None:
        # (H) The stream operator (`<<` insertion / `>>` extraction) of a
        # (H) binary_expression, or None when the node is not a stream operation.
        if node.type != descriptor.stream_sink_type:
            return None
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        if operator is None or operator.text is None:
            return None
        text = operator.text.decode(cs.ENCODING_UTF8)
        if text == descriptor.stream_sink_operator or (
            descriptor.stream_extract_operator is not None
            and text == descriptor.stream_extract_operator
        ):
            return text
        return None

    def _emit_macro(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        macro_sinks: dict[str, IOSink],
        in_scope: frozenset[str],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Match a macro invocation's name (`macro` field) against the per-language
        # (H) macro sink table (Rust `println!`/`eprintln!` -> STDOUT); the target is a
        # (H) format template, so the STDOUT identity is always <dynamic>. Then scan the
        # (H) macro's token_tree for an inlined call sink (`println!("{}", env::var("X"))`).
        # (H) ponytail: a macro name is matched by text alone; a locally redefined macro
        # (H) (`macro_rules! println`) would false-match. Tracking macro shadowing is the
        # (H) upgrade path if that pathological case ever matters.
        macro = node.child_by_field_name(cs.TS_RS_FIELD_MACRO)
        if macro is None or macro.text is None:
            return
        sink = macro_sinks.get(macro.text.decode(cs.ENCODING_UTF8))
        if sink is not None:
            self._emit(caller_spec, sink.direction, sink.kind, DYNAMIC_TARGET)
        for child in node.named_children:
            if child.type == cs.TS_RS_TOKEN_TREE:
                self._scan_token_tree_calls(
                    child, caller_spec, import_map, sink_by_name, in_scope, descriptor
                )

    def _scan_token_tree_calls(
        self,
        token_tree: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        in_scope: frozenset[str],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) Rebuild each inlined scoped call from the flat token stream (shared with
        # (H) the flow walk), resolve it against the sink table (respecting shadowing),
        # (H) and take arg0's string literal as the resource identity.
        for raw, args in iter_token_tree_calls(
            token_tree, cs.TS_RS_TOKEN_SCOPE, cs.TS_IDENTIFIER, cs.TS_RS_TOKEN_TREE
        ):
            self._emit_token_tree_call(
                raw, args, caller_spec, import_map, sink_by_name, in_scope, descriptor
            )

    def _emit_token_tree_call(
        self,
        raw: str,
        args: Node,
        caller_spec: tuple[str, str, str],
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        in_scope: frozenset[str],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) A reconstructed scoped call (name `raw`, `args` = its token_tree),
        # (H) resolved with the same import-expansion / shadow rules as a real call.
        sink = self._resolve_sink(
            raw,
            import_map,
            sink_by_name,
            in_scope,
            descriptor.sinks_require_import,
            descriptor.scope_separator,
        )
        if sink is None:
            return
        # (H) ponytail: only arg0 (or a kwless <dynamic>) is resolved from the flat
        # (H) token stream -- every Rust I/O sink targets arg 0 or nothing. A sink at a
        # (H) higher arg index would need positional token counting (upgrade path).
        identity = DYNAMIC_TARGET
        if sink.target_arg == 0:
            identity = first_token_arg_string(
                args, cs.TS_RS_STRING_LITERAL, cs.TS_RS_STRING_CONTENT
            )
        self._emit(caller_spec, sink.direction, sink.kind, identity)

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
            for direction in self._member_directions(node, descriptor):
                self._emit(caller_spec, direction, kind, identity)
            return

    @staticmethod
    def _member_directions(
        node: Node, descriptor: LanguageDescriptor
    ) -> list[IODirection]:
        # (H) A member access on an assignment's LHS mutates the resource:
        # (H) `process.env.KEY = v` is a WRITE (mislabeling it a read hid
        # (H) dotenv's core behavior); `+=` reads the old value AND writes.
        # (H) Any other position (including the assignment RHS) stays a read.
        # (H) Parent type gates first: most member accesses are plain reads,
        # (H) so the field lookup runs only for assignment parents. Node
        # (H) equality is by .id (the bindings hand out fresh Node objects).
        parent = node.parent
        if parent is None or parent.type not in (
            descriptor.assignment_type,
            descriptor.augmented_assignment_type,
            descriptor.update_expression_type,
        ):
            return [IODirection.READ]
        # (H) `++`/`--` wrap the mutated operand in the `argument` field.
        target_field = (
            cs.TS_JS_FIELD_ARGUMENT
            if parent.type == descriptor.update_expression_type
            else cs.FIELD_LEFT
        )
        target = parent.child_by_field_name(target_field)
        if target is None or target.id != node.id:
            return [IODirection.READ]
        if parent.type == descriptor.assignment_type:
            return [IODirection.WRITE]
        return [IODirection.READ, IODirection.WRITE]

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
            if (
                node.type == descriptor.declarator_type
                and not is_require_alias(node, descriptor.call_type)
            ) or node.type in descriptor.extra_declarator_types:
                names |= self._declarator_names(node, descriptor)
            stack.extend(node.named_children)
        return names

    def _declarator_names(
        self, declarator: Node, descriptor: LanguageDescriptor
    ) -> set[str]:
        # (H) The local names a declaration binds: JS `const fs = ...` / destructuring
        # (H) uses the `name` field; Go `var/const os = ...` also has `name` field(s),
        # (H) while `os := ...` / `range` use the `left` field (an expression_list of
        # (H) identifiers); Rust `let s = ...` uses the `pattern` field. All are
        # (H) collected so they shadow a same-named builtin.
        names: set[str] = set()
        for name in declarator.children_by_field_name(cs.TS_FIELD_NAME):
            self._pattern_names(name, descriptor, names)
        if (left := declarator.child_by_field_name(cs.FIELD_LEFT)) is not None:
            self._pattern_names(left, descriptor, names)
        for pattern in declarator.children_by_field_name(cs.TS_FIELD_PATTERN):
            self._pattern_names(pattern, descriptor, names)
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
        elif node_type in (cs.TS_GO_PARAMETER_DECLARATION, cs.TS_FORMAL_PARAMETER):
            # (H) Go `func f(os Config)` / Java `void f(Object System)`: the `name`
            # (H) field(s) are the bound locals.
            for child in node.children_by_field_name(cs.TS_FIELD_NAME):
                self._pattern_names(child, descriptor, out)
        elif node_type == cs.TS_SPREAD_PARAMETER:
            # (H) Java varargs `void f(Object... System)`: the type is a sibling and the
            # (H) bound name lives in a `variable_declarator` child (its `name` field).
            for child in node.named_children:
                if child.type == descriptor.declarator_type:
                    for name in child.children_by_field_name(cs.TS_FIELD_NAME):
                        self._pattern_names(name, descriptor, out)
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
        lean_handles: _LeanHandles | None,
    ) -> None:
        raw = call_name(node)
        if raw is None:
            return
        if lean_handles is not None and self._emit_lean_handle_method(
            node, caller_spec, raw, descriptor, lean_handles
        ):
            return
        sink = self._resolve_sink(
            raw,
            import_map,
            sink_by_name,
            local_names,
            descriptor.sinks_require_import,
            descriptor.scope_separator,
        )
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

    def _emit_lean_handle_method(
        self,
        call_node: Node,
        caller_spec: tuple[str, str, str],
        raw_name: str,
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> bool:
        # (H) `f.WriteString(s)` / `br.readLine()` / `out.write(..)`: a method call
        # (H) whose receiver is a bound handle variable is I/O on that handle's
        # (H) resource. Every lean grammar spells the receiver `recv.method` in the
        # (H) callee text (Rust field_expression methods included), so one dotted
        # (H) split covers them all.
        receiver, sep, method = raw_name.rpartition(cs.SEPARATOR_DOT)
        if not sep:
            return False
        binding = lean_handles.bindings.get(receiver)
        if binding is None:
            return False
        direction = lean_handles.methods.get(binding.kind, {}).get(method)
        if direction is None:
            return False
        if (
            direction == IODirection.READ_WRITE
            and binding.kind == ResourceKind.DATABASE
        ):
            # (H) java.sql `execute(sql)`: refine by the SQL first keyword.
            direction = self._sql_direction(call_node, direction, descriptor)
        self._emit(caller_spec, direction, binding.kind, binding.identity)
        return True

    def _maybe_bind_lean_handle(
        self,
        node: Node,
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> None:
        # (H) Track handle bindings as the source-ordered walk passes each binding
        # (H) node. Flat like Python's handle walk: last binding wins, no
        # (H) path-sensitivity (branch-precise handles are an upgrade path).
        if lean_handles.type_ctors and node.type == cs.TS_CPP_DECLARATION:
            self._bind_type_decl_handle(node, descriptor, lean_handles)
            return
        if (
            node.type != descriptor.declarator_type
            and node.type not in _LEAN_ASSIGNMENT_TYPES
            and node.type not in descriptor.extra_declarator_types
        ):
            return
        targets, values = binding_targets_values(node, descriptor)
        for name, value in self._paired_binding_values(targets, values):
            self._apply_lean_binding(
                name, value, in_scope, import_map, descriptor, lean_handles
            )

    @staticmethod
    def _paired_binding_values(
        targets: list[str | None], values: list[Node]
    ) -> Iterator[tuple[str, Node | None]]:
        # (H) Pair each named LHS target with its RHS value. One multi-value call
        # (H) feeding several LHS (Go `f, err := os.Open(..)`): the handle is the
        # (H) FIRST return value; the later targets (err) are not handles.
        spread = len(values) == 1 and len(targets) > 1
        for index, name in enumerate(targets):
            if name is None:
                continue
            if spread:
                value = values[0] if index == 0 else None
            else:
                value = values[index] if index < len(values) else None
            yield name, value

    def _apply_lean_binding(
        self,
        name: str,
        value: Node | None,
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> None:
        # (H) A C++ init_declarator whose value is an argument_list is the
        # (H) declaration-constructor form (`std::ifstream in("x")`), owned by
        # (H) _bind_type_decl_handle -- neither bind nor kill here.
        if value is not None and value.type == cs.TS_ARGUMENT_LIST:
            return
        binding = self._resolve_handle_value(
            value, in_scope, import_map, descriptor, lean_handles
        )
        if binding is not None:
            lean_handles.bindings[name] = binding
        else:
            # (H) Rebinding to a non-handle kills the handle.
            lean_handles.bindings.pop(name, None)

    def _resolve_handle_value(
        self,
        value: Node | None,
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> HandleBinding | None:
        # (H) The HandleBinding an RHS expression yields: a constructor call, a
        # (H) wrapper around a nested constructor or bound variable, a derive call
        # (H) on a bound handle, a `new` handle expression, or a handle alias.
        if value is None:
            return None
        node = self._unwrap_result(value)
        if node.type == descriptor.call_type:
            return self._handle_from_call(
                node, in_scope, import_map, descriptor, lean_handles
            )
        if (
            descriptor.new_expression_type is not None
            and node.type == descriptor.new_expression_type
        ):
            return self._handle_from_new(
                node, in_scope, import_map, descriptor, lean_handles
            )
        if node.type == descriptor.identifier_type and node.text is not None:
            # (H) `g := f` aliases the handle.
            return lean_handles.bindings.get(node.text.decode(cs.ENCODING_UTF8))
        return None

    @staticmethod
    def _unwrap_result(node: Node) -> Node:
        # (H) Rust Result unwrapping: `File::open(p)?` (try_expression) and
        # (H) `File::create(p).unwrap()` / `.expect(..)` all yield the inner
        # (H) handle. The node shapes are Rust-specific, so this is inert
        # (H) elsewhere.
        while True:
            if node.type == cs.TS_RS_TRY_EXPRESSION:
                inner = next(
                    (c for c in node.named_children if c.type != cs.TS_COMMENT), None
                )
                if inner is None:
                    return node
                node = inner
                continue
            fn = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if fn is not None and fn.type == cs.TS_RS_FIELD_EXPRESSION:
                field = fn.child_by_field_name(cs.RS_FIELD_FIELD)
                receiver = fn.child_by_field_name(cs.FIELD_VALUE)
                if (
                    field is not None
                    and field.text is not None
                    and field.text.decode(cs.ENCODING_UTF8)
                    in cs.RS_RESULT_UNWRAP_METHODS
                    and receiver is not None
                    and receiver.type == cs.TS_RS_CALL_EXPRESSION
                ):
                    node = receiver
                    continue
            return node

    def _handle_from_call(
        self,
        node: Node,
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> HandleBinding | None:
        raw = call_name(node)
        if raw is None:
            return None
        # (H) Wrapper first (`BufReader::new(f)`, `bufio.NewReader(f)`): the
        # (H) resource is arg0's.
        if (
            lean_handles.call_wrappers
            and self._resolve_sink(
                raw,
                import_map,
                lean_handles.call_wrappers,
                in_scope,
                descriptor.sinks_require_import,
                descriptor.scope_separator,
            )
            is not None
        ):
            return self._resolve_handle_value(
                self._first_positional_arg(node),
                in_scope,
                import_map,
                descriptor,
                lean_handles,
            )
        # (H) Derive (`conn.createStatement()`): a same-resource sub-handle.
        receiver, sep, method = raw.rpartition(cs.SEPARATOR_DOT)
        if sep:
            parent = lean_handles.bindings.get(receiver)
            if parent is not None and method in IO_HANDLE_DERIVES.get(
                parent.kind, frozenset()
            ):
                return parent
        ctor = self._resolve_sink(
            raw,
            import_map,
            lean_handles.ctors,
            in_scope,
            descriptor.sinks_require_import,
            descriptor.scope_separator,
        )
        if ctor is None:
            return None
        return HandleBinding(
            kind=ctor.kind,
            identity=self._ctor_identity(node, ctor, descriptor, lean_handles),
        )

    def _handle_from_new(
        self,
        node: Node,
        in_scope: frozenset[str],
        import_map: dict[str, str],
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> HandleBinding | None:
        # (H) Java `new`-shaped handles. A wrapper type delegates to arg0 (a nested
        # (H) constructor or a bound variable); PrintWriter falls through to its
        # (H) filename overload when arg0 is not a handle.
        type_node = node.child_by_field_name(cs.TS_FIELD_TYPE)
        if type_node is None or type_node.text is None:
            return None
        type_name = type_node.text.decode(cs.ENCODING_UTF8)
        if type_name in lean_handles.new_wrappers:
            inner = self._resolve_handle_value(
                self._first_positional_arg(node),
                in_scope,
                import_map,
                descriptor,
                lean_handles,
            )
            if inner is not None:
                return inner
        ctor = lean_handles.new_ctors.get(type_name)
        if ctor is None:
            # (H) An identity-carrier reached through a wrapper
            # (H) (`new Scanner(new File("x"))`): `new File` is not a handle, but
            # (H) it designates the resource, so it resolves to one here.
            kind = lean_handles.identity_new_types.get(type_name)
            if kind is None:
                return None
            return HandleBinding(
                kind=kind, identity=self._literal_arg0(node, descriptor)
            )
        return HandleBinding(
            kind=ctor.kind,
            identity=self._ctor_identity(node, ctor, descriptor, lean_handles),
        )

    def _bind_type_decl_handle(
        self,
        node: Node,
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> None:
        # (H) C++ declaration-constructor handles: `std::ifstream in("x.txt")`
        # (H) (init_declarator with an argument_list value) and the most vexing
        # (H) parse `std::ifstream dyn(path)` (a function_declarator), which still
        # (H) binds a FILE handle with a <dynamic> identity. A bare default-
        # (H) constructed stream (`std::ifstream f;` + later `f.open(..)`) is an
        # (H) upgrade path.
        type_node = node.child_by_field_name(cs.TS_FIELD_TYPE)
        if type_node is None or type_node.text is None:
            return
        kind = lean_handles.type_ctors.get(type_node.text.decode(cs.ENCODING_UTF8))
        if kind is None:
            return
        for child in node.named_children:
            if child.type == cs.TS_CPP_INIT_DECLARATOR:
                name = cpp_declarator_name(
                    child.child_by_field_name(cs.FIELD_DECLARATOR)
                )
                value = child.child_by_field_name(cs.FIELD_VALUE)
                if name is not None:
                    lean_handles.bindings[name] = HandleBinding(
                        kind=kind,
                        identity=self._first_string_arg(value, descriptor),
                    )
            elif child.type == cs.TS_CPP_FUNCTION_DECLARATOR:
                name = cpp_declarator_name(
                    child.child_by_field_name(cs.FIELD_DECLARATOR)
                )
                if name is not None:
                    lean_handles.bindings[name] = HandleBinding(
                        kind=kind, identity=DYNAMIC_TARGET
                    )

    @staticmethod
    def _first_string_arg(args: Node | None, descriptor: LanguageDescriptor) -> str:
        # (H) The first string-literal argument of a constructor's argument_list
        # (H) (`std::ifstream in("x.txt", std::ios::binary)` -> x.txt).
        if args is None:
            return DYNAMIC_TARGET
        for child in args.named_children:
            if child.type == descriptor.string_type:
                return string_literal(
                    child, descriptor.string_type, descriptor.string_content_type
                )
        return DYNAMIC_TARGET

    @staticmethod
    def _first_positional_arg(call_node: Node) -> Node | None:
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return None
        return next((c for c in args.named_children if c.type != cs.TS_COMMENT), None)

    def _ctor_identity(
        self,
        call_node: Node,
        ctor: HandleConstructor,
        descriptor: LanguageDescriptor,
        lean_handles: _LeanHandles,
    ) -> str:
        identity = literal_target(
            call_node,
            ctor.target_arg,
            ctor.target_kw,
            string_type=descriptor.string_type,
            content_type=descriptor.string_content_type,
            keyword_arg_type=descriptor.keyword_arg_type,
        )
        if identity != DYNAMIC_TARGET or ctor.target_arg != 0:
            return identity
        # (H) Identity one level down: `Files.newBufferedReader(Path.of("cfg"))`
        # (H) and `new Scanner(new File("x"))` carry the literal in the factory
        # (H) call / File constructor at the target position.
        arg = self._first_positional_arg(call_node)
        if arg is None:
            return identity
        if lean_handles.identity_calls and arg.type == descriptor.call_type:
            raw = call_name(arg)
            if raw is not None and raw in lean_handles.identity_calls:
                return self._literal_arg0(arg, descriptor)
        if (
            lean_handles.identity_new_types
            and descriptor.new_expression_type is not None
            and arg.type == descriptor.new_expression_type
        ):
            inner_type = arg.child_by_field_name(cs.TS_FIELD_TYPE)
            if (
                inner_type is not None
                and inner_type.text is not None
                and inner_type.text.decode(cs.ENCODING_UTF8)
                in lean_handles.identity_new_types
            ):
                return self._literal_arg0(arg, descriptor)
        return identity

    @staticmethod
    def _literal_arg0(node: Node, descriptor: LanguageDescriptor) -> str:
        return literal_target(
            node,
            0,
            None,
            string_type=descriptor.string_type,
            content_type=descriptor.string_content_type,
            keyword_arg_type=descriptor.keyword_arg_type,
        )

    @staticmethod
    def _resolve_sink[T](
        raw: str,
        import_map: dict[str, str],
        sink_by_name: dict[str, T],
        local_names: frozenset[str],
        sinks_require_import: bool,
        scope_separator: str | None = None,
    ) -> T | None:
        # (H) Rust (scope_separator="::"): sinks are keyed only under the full `std::`
        # (H) form. Expand the head segment through the import map on `::` (`use std::fs;
        # (H) fs::write` -> `std::fs::write`; a fully-qualified `std::fs::write` has an
        # (H) unimported `std` head and stays as-is). A bare `fs::write` with no import
        # (H) does not expand and misses -> no false match on a local `mod fs`. A head
        # (H) bound to a local name is shadowed.
        if scope_separator is not None:
            head, _, rest = raw.partition(scope_separator)
            if head in local_names:
                return None
            base = import_map.get(head)
            if base is not None:
                raw = f"{base}{scope_separator}{rest}" if rest else base
            return sink_by_name.get(raw)
        # (H) Match a JS/TS/Go call against the sink table, respecting shadowing:
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
        # (H) Go stdlib is always imported, so a dotted sink whose package head is
        # (H) NOT imported (e.g. a package-scope `var os`) is not the stdlib package.
        if sinks_require_import and sep and head not in import_map:
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

    def _sql_direction(
        self,
        call_node: Node,
        fallback: IODirection,
        descriptor: LanguageDescriptor | None = None,
    ) -> IODirection:
        # (H) ponytail: first-keyword heuristic only; a full SQL parse is the
        # (H) upgrade path if execute() direction precision ever matters.
        if descriptor is not None:
            sql = literal_target(
                call_node,
                0,
                string_type=descriptor.string_type,
                content_type=descriptor.string_content_type,
                keyword_arg_type=descriptor.keyword_arg_type,
            )
        else:
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
