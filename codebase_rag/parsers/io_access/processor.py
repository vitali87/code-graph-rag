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
    IO_MACRO_SINKS,
    IO_MEMBER_READS,
    IO_SINKS,
    IO_STREAM_SINKS,
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
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
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
            macro_sinks,
            stream_sinks,
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
        macro_sinks: dict[str, IOSink],
        stream_sinks: dict[str, IOSink],
        member_reads: tuple[tuple[str, ResourceKind], ...],
        descriptor: LanguageDescriptor,
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
    ) -> None:
        # (H) Emit the direct sinks / member reads within one statement subtree, under
        # (H) the given in-scope shadow set. A nested { } is a child lexical scope:
        # (H) recurse via _walk_scope so its declarations shadow only inside it (and,
        # (H) for declare-at-point langs, in its own source order). body_extra seeds a
        # (H) loop var into the body scope (the loop's own block) without exposing it to
        # (H) the header expressions walked in this flat pass.
        stack = [stmt]
        while stack:
            node = stack.pop()
            # (H) Nested function/method: its own caller, walked separately.
            if node.type in descriptor.nested_scope_types:
                continue
            if node.type == descriptor.block_scope_type:
                self._walk_scope(
                    list(node.named_children),
                    in_scope | body_extra,
                    caller_spec,
                    import_map,
                    sink_by_name,
                    macro_sinks,
                    stream_sinks,
                    member_reads,
                    descriptor,
                )
                continue
            if node.type == descriptor.call_type:
                self._emit_direct_call(
                    node, caller_spec, import_map, sink_by_name, descriptor, in_scope
                )
            elif (
                descriptor.macro_type is not None and node.type == descriptor.macro_type
            ):
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
                continue
            elif (
                stream_sinks
                and descriptor.stream_sink_type is not None
                and node.type == descriptor.stream_sink_type
            ):
                # (H) A stream-insertion sink (`std::cout << x`); descend still so a call
                # (H) sink in an inserted operand (`std::cout << getenv("X")`) is caught.
                self._emit_stream_sink(node, caller_spec, stream_sinks, descriptor)
            elif member_reads and node.type in (
                descriptor.member_expression_type,
                descriptor.subscript_type,
            ):
                self._emit_member_read(
                    node, caller_spec, member_reads, in_scope, import_map, descriptor
                )
            stack.extend(node.named_children)

    def _emit_stream_sink(
        self,
        node: Node,
        caller_spec: tuple[str, str, str],
        stream_sinks: dict[str, IOSink],
        descriptor: LanguageDescriptor,
    ) -> None:
        # (H) A `<<` chain like `std::cout << a << b` nests left-associatively:
        # (H) (((cout << a) << b)). Act only at the TOP of the chain (parent is not
        # (H) itself a `<<` insertion) and walk the `left` spine to the base operand; if
        # (H) the base is a stream sink (cout/cerr), emit ONE STDOUT write. A non-stream
        # (H) base (arithmetic `x << 2`) resolves to a non-sink and emits nothing.
        if not self._is_stream_insertion(node, descriptor):
            return
        parent = node.parent
        if parent is not None and self._is_stream_insertion(parent, descriptor):
            return
        base = node
        while self._is_stream_insertion(base, descriptor):
            left = base.child_by_field_name(cs.FIELD_LEFT)
            if left is None:
                return
            base = left
        if base.text is None:
            return
        sink = stream_sinks.get(base.text.decode(cs.ENCODING_UTF8))
        if sink is not None:
            self._emit(caller_spec, sink.direction, sink.kind, DYNAMIC_TARGET)

    @staticmethod
    def _is_stream_insertion(node: Node, descriptor: LanguageDescriptor) -> bool:
        # (H) A binary_expression whose `operator` field is the stream-insertion token.
        if node.type != descriptor.stream_sink_type:
            return False
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        return (
            operator is not None
            and operator.text is not None
            and operator.text.decode(cs.ENCODING_UTF8)
            == descriptor.stream_sink_operator
        )

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
        # (H) tree-sitter flattens a Rust macro body to raw tokens, so an inlined call
        # (H) (`std::env::var("X")`) is a run of `identifier` joined by `::` tokens
        # (H) followed by its args `token_tree` -- no call_expression node. Rebuild the
        # (H) scoped name from that run, resolve it against the sink table (respecting
        # (H) shadowing), and take arg0's string literal as the resource identity.
        path: list[str] = []
        expect_sep = False
        for child in token_tree.children:
            if child.type == cs.TS_IDENTIFIER and not expect_sep and child.text:
                path.append(child.text.decode(cs.ENCODING_UTF8))
                expect_sep = True
            elif child.type == cs.TS_RS_TOKEN_SCOPE and expect_sep:
                expect_sep = False
            elif child.type == cs.TS_RS_TOKEN_TREE:
                if path:
                    self._emit_token_tree_call(
                        cs.TS_RS_TOKEN_SCOPE.join(path),
                        child,
                        caller_spec,
                        import_map,
                        sink_by_name,
                        in_scope,
                        descriptor,
                    )
                path, expect_sep = [], False
                # (H) Recurse for a nested macro / grouped call in the arguments.
                self._scan_token_tree_calls(
                    child, caller_spec, import_map, sink_by_name, in_scope, descriptor
                )
            else:
                path, expect_sep = [], False

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
            identity = self._first_token_arg_string(args)
        self._emit(caller_spec, sink.direction, sink.kind, identity)

    def _first_token_arg_string(self, args: Node) -> str:
        # (H) arg0 of a flattened call's token_tree: the tokens before the first
        # (H) top-level comma. It is the resource path only when it is a lone string
        # (H) literal (`write(path, "x")` has a variable arg0 -> <dynamic>, not "x").
        arg0: list[Node] = []
        for child in args.children:
            if child.type in (cs.CHAR_PAREN_OPEN, cs.CHAR_PAREN_CLOSE):
                continue
            if child.type == cs.CHAR_COMMA:
                break
            arg0.append(child)
        if len(arg0) == 1 and arg0[0].type == cs.TS_RS_STRING_LITERAL:
            return string_literal(
                arg0[0], cs.TS_RS_STRING_LITERAL, cs.TS_RS_STRING_CONTENT
            )
        return DYNAMIC_TARGET

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
    ) -> None:
        raw = call_name(node)
        if raw is None:
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

    @staticmethod
    def _resolve_sink(
        raw: str,
        import_map: dict[str, str],
        sink_by_name: dict[str, IOSink],
        local_names: frozenset[str],
        sinks_require_import: bool,
        scope_separator: str | None = None,
    ) -> IOSink | None:
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
