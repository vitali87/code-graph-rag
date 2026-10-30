"""Dart's share of the call pass: the getters a caller reads (a Dart getter
read is a bare identifier or a member selector, never an invocation), the
constructors a `: this(...)` or `: super(...)` clause runs, the class a
named constructor call constructs, and the Dart shapes of call arguments.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and hands Dart's part to the `DartCallResolution` it builds."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol

from tree_sitter import Node

from ... import constants as cs
from ...language_spec import LanguageSpec
from ...node_text import safe_decode_text
from ...types_defs import LanguageQueries
from ...utils import qn_markers
from ..call_host import VerdictHost
from ..edge_site import EdgeWriterHost, host_site_scoped
from ..language_calls import CallName, CallScan, LanguageCalls
from . import utils as dart_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class DartCallHost(EdgeWriterHost, VerdictHost, Protocol):
    """What Dart's call passes read and call on the call pass that builds
    them: the edge emitter, and the verdict, how surely the call node being
    scanned bound its callee."""


VALUE_WRAPPER_TYPES = frozenset(
    {cs.TS_DART_LIST_LITERAL, cs.TS_DART_SET_OR_MAP_LITERAL, cs.TS_DART_ARGUMENT}
)
# Scopes that bound a Dart local/parameter declaration for getter-read
# shadowing; a declaration hides a same-name getter only inside them. A loop
# variable scopes its for_statement, a catch parameter its try_statement.
_SHADOW_SCOPE_TYPES = frozenset(
    {
        cs.TS_DART_BLOCK,
        cs.TS_DART_FUNCTION_EXPRESSION,
        cs.TS_DART_LOCAL_FUNCTION_DECLARATION,
        cs.TS_DART_FOR_STATEMENT,
        cs.TS_DART_TRY_STATEMENT,
    }
)
_LOCAL_DECLARATION_TYPES = frozenset(
    {
        cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION,
        cs.TS_DART_INITIALIZED_IDENTIFIER,
    }
)
# Statement scopes whose binder is live only AFTER its declaration: the
# for-in iterable and the try body precede theirs and read the getter. A
# block-scoped local needs no such split: Dart scopes it to the whole block
# and rejects reads before the declaration at compile time.
_STATEMENT_SCOPE_TYPES = frozenset({cs.TS_DART_FOR_STATEMENT, cs.TS_DART_TRY_STATEMENT})
# Parents whose direct identifier is never a value read: member/cascade
# selectors carry the chain passes' members, a label names a parameter,
# catch parameters only BIND names, and a signature's identifier is the
# DECLARED name (the module pass walks class bodies for field initializers,
# so signatures are in view there).
_NON_READ_PARENT_TYPES = (
    frozenset(
        {
            cs.TS_DART_LABEL,
            cs.TS_DART_UNCONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CONDITIONAL_ASSIGNABLE_SELECTOR,
            cs.TS_DART_CASCADE_SELECTOR,
            cs.TS_DART_CATCH_PARAMETERS,
        }
    )
    | cs.DART_SIGNATURE_TYPES
)


def _scope_span(decl: Node, walk_root: Node) -> tuple[int, int]:
    # The byte span a declaration shadows: its nearest enclosing scope, with
    # statement scopes (for/try) starting at the declaration END because the
    # for-in iterable and the try body precede their binder. No scope
    # ancestor (a signature parameter) falls through to the whole body.
    anc = decl.parent
    while anc is not None and anc is not walk_root:
        if anc.type in _SHADOW_SCOPE_TYPES:
            if anc.type in _STATEMENT_SCOPE_TYPES:
                return (decl.end_byte, anc.end_byte)
            return (anc.start_byte, anc.end_byte)
        anc = anc.parent
    return (walk_root.start_byte, walk_root.end_byte)


def _is_owned_function_body(node: Node) -> bool:
    # A function_body belonging to a top-level function or class member has
    # its OWN caller pass; a closure's or local function's body does not (a
    # Dart lambda's reads attribute to the enclosing scope), so only bodies
    # NOT under a nested-scope node are skipped by the module walks.
    return (
        node.type == cs.TS_DART_FUNCTION_BODY
        and (parent := node.parent) is not None
        and parent.type not in cs.DART_NESTED_SCOPE_NODE_TYPES
    )


def _declared_names(node: Node) -> list[str]:
    # The name(s) a parameter, local declaration, loop variable, catch
    # parameter, or pattern binding binds; anything else declares nothing.
    node_type = node.type
    if node_type in (cs.TS_DART_FORMAL_PARAMETER, cs.TS_DART_CATCH_PARAMETERS):
        return [
            name
            for child in node.named_children
            if child.type == cs.TS_DART_IDENTIFIER and (name := safe_decode_text(child))
        ]
    if node_type in _LOCAL_DECLARATION_TYPES or node_type == cs.TS_DART_FOR_LOOP_PARTS:
        # `for (final total in xs)`: the FIRST identifier is the loop
        # variable, the second the iterable expression.
        first = next(
            (
                child
                for child in node.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        name = safe_decode_text(first) if first is not None else None
        return [name] if name else []
    if node_type == cs.TS_DART_PATTERN_VARIABLE_DECLARATION:
        return _pattern_bound_names(node)
    return []


def _pattern_bound_names(node: Node) -> list[str]:
    # `var (alpha, beta) = rhs` binds every identifier inside the *_pattern
    # subtree; the RHS expression binds nothing.
    names: list[str] = []
    stack = [
        child
        for child in node.named_children
        if child.type.endswith(cs.DART_PATTERN_NODE_SUFFIX)
    ]
    while stack:
        current = stack.pop()
        if current.type == cs.TS_DART_IDENTIFIER and (
            name := safe_decode_text(current)
        ):
            names.append(name)
        stack.extend(current.named_children)
    return names


def _is_bare_read(node: Node) -> bool:
    # A bare-identifier getter read, INCLUDING a receiver-position chain head:
    # `_wonders.length` reads the `_wonders` getter even though the member
    # pass only emits for the final member (issue #873). Only a head invoked
    # directly (`f(x)` = identifier + selector(argument_part)) belongs to the
    # call pass; a label's identifier names a parameter, and a selector's
    # own identifier is the member the chain pass already handled.
    following = node.next_named_sibling
    if following is not None and (
        following.type == cs.TS_DART_ARGUMENT_PART
        or (
            following.type == cs.TS_DART_SELECTOR
            and any(
                child.type == cs.TS_DART_ARGUMENT_PART
                for child in following.named_children
            )
        )
    ):
        return False
    parent = node.parent
    if parent is None or parent.type in _NON_READ_PARENT_TYPES:
        return False
    if parent.type == cs.TS_DART_FOR_LOOP_PARTS:
        # The FIRST identifier of a for-in is the loop BINDER, not a read;
        # the iterable position after it reads normally.
        first = next(
            (
                child
                for child in parent.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        if first is not None and first.start_byte == node.start_byte:
            return False
    return True


def collection_values(node: Node) -> list[Node]:
    # A Dart MAP stores each value inside a `pair` node's value field
    # (`{"tap": onTap}`); a set exposes its elements directly; a typed
    # literal's `type_arguments` child carries types, never values.
    children: list[Node] = []
    for child in node.named_children:
        if child.type == cs.TS_DART_TYPE_ARGUMENTS:
            continue
        if child.type == cs.TS_DART_PAIR:
            if (pair_value := child.child_by_field_name(cs.FIELD_VALUE)) is not None:
                children.append(pair_value)
            continue
        children.append(child)
    return children


def call_arguments_node(call_node: Node) -> Node | None:
    if (
        call_node.type in cs.DART_CONSTRUCTION_NODE_TYPES
        or call_node.type in cs.DART_CONSTRUCTOR_DELEGATION_TYPES
    ):
        # `new X(...)`, `const X(...)`, `X<T>.named(...)` and a constructor's
        # `: this(...)` / `: super(...)` clause hold their `arguments` node
        # directly.
        return next(
            (
                child
                for child in call_node.named_children
                if child.type == cs.TS_DART_ARGUMENTS
            ),
            None,
        )
    if call_node.type == cs.TS_DART_RELATIONAL_EXPRESSION:
        # `X<T>(arg)` mis-parsed as a chained comparison (issue #2010): the
        # one argument is the parenthesised operand after the outer `>`.
        return dart_utils.generic_call_argument(call_node)
    # Dart has no call-expression node: a selector/cascade_section wraps its
    # arguments in an argument_part holding the real `arguments` node one
    # level down.
    for part in call_node.named_children:
        if part.type == cs.TS_DART_ARGUMENT_PART:
            return next(
                (
                    grand
                    for grand in part.named_children
                    if grand.type == cs.TS_DART_ARGUMENTS
                ),
                None,
            )
    return None


def add_named_argument(child: Node, keyword: dict[str, Node]) -> None:
    # A named value is a `named_argument` whose label child names it and
    # whose last named child is the expression.
    label = next(
        (grand for grand in child.named_children if grand.type == cs.TS_DART_LABEL),
        None,
    )
    value_node = child.named_children[-1] if child.named_children else None
    name_node = (
        label.named_children[0] if label is not None and label.named_children else None
    )
    if (
        name_node is not None
        and value_node is not None
        and value_node is not label
        and (name := safe_decode_text(name_node)) is not None
    ):
        keyword[name] = value_node


def _read_name(
    node: Node,
    prop_names: set[str],
    shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
) -> str | None:
    # The dotted name this node READS, or None: member selectors and
    # cascades reassemble their receiver chain; a bare identifier counts
    # only when no in-scope local/parameter shadows it.
    node_type = node.type
    if node_type == cs.TS_DART_SELECTOR:
        if _is_shadowed_construction(node, shadow_spans):
            return None
        read_name = dart_utils.member_read_name(node)
    elif node_type == cs.TS_DART_CASCADE_SECTION:
        read_name = dart_utils.cascade_read_name(node)
    elif node_type == cs.TS_DART_IDENTIFIER and _is_bare_read(node):
        read_name = _unshadowed_name(node, prop_names, shadow_spans)
    else:
        return None
    if read_name and read_name.rsplit(cs.SEPARATOR_DOT, 1)[-1] in prop_names:
        return read_name
    return None


def _is_shadowed_construction(
    node: Node,
    shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
) -> bool:
    # `X<int>(1).m` is token-for-token identical to the chained
    # comparison `a < b > (1).m`, so reading the receiver as a
    # construction is a guess (issue #2015). When a local or parameter
    # of that name is in scope the operand is that variable, not a type,
    # so the site is a comparison and must not bind its member: without
    # this, a local named after a class emits a wrong REFERENCES edge.
    base = dart_utils.ambiguous_construction_base(node)
    if base is None:
        return False
    # An import-prefixed base is dotted (`p.Box`, issue #2033) while the
    # shadow spans are keyed by the bare binder, so check the LEADING
    # segment: a local named `p` shadows the prefix and makes
    # `p.Box < b > (1).x` a comparison, exactly as a local `Box` does for
    # the unprefixed form.
    binder = base.split(cs.SEPARATOR_DOT, 1)[0]
    pos = node.start_byte
    return any(lo <= pos < hi for lo, hi in shadow_spans().get(binder, ()))


def _unshadowed_name(
    node: Node,
    prop_names: set[str],
    shadow_spans: Callable[[], dict[str, list[tuple[int, int]]]],
) -> str | None:
    name = safe_decode_text(node)
    if not name or name not in prop_names:
        return None
    pos = node.start_byte
    if any(lo <= pos < hi for lo, hi in shadow_spans().get(name, ())):
        return None
    return name


def _shadow_spans(
    caller_node: Node,
    walk_root: Node,
    skip_owned_members: bool = False,
) -> dict[str, list[tuple[int, int]]]:
    # {declared name: [byte span of each declaring SCOPE]} for parameters
    # and locals. A declaration shadows a same-name getter only for bare
    # reads INSIDE its declaring scope: a closure's parameter must not
    # suppress the enclosing method's read (cf. csharp.calls._shadow_spans).
    # Method parameters live in the SIGNATURE, outside the walked body,
    # so their scope walk falls through to the whole body.
    spans: dict[str, list[tuple[int, int]]] = {}
    stack = list(caller_node.children)
    if walk_root is not caller_node:
        stack.extend(walk_root.children)
    while stack:
        node = stack.pop()
        # For the class-initializer pass, member signatures and owned
        # bodies are invisible to the READ walk, so their parameters and
        # locals must not join the shadow map either: a method parameter
        # scopes its own body, never a sibling field initializer.
        if skip_owned_members and (
            node.type in cs.DART_SIGNATURE_TYPES or _is_owned_function_body(node)
        ):
            continue
        for name in _declared_names(node):
            spans.setdefault(name, []).append(_scope_span(node, walk_root))
        stack.extend(node.children)
    return spans


class DartCallResolution(LanguageCalls):
    """Writes the getter reads and constructor runs Dart spells without a
    call node, and the class a named constructor call constructs.

    Built and owned by `CallProcessor`, whose resolver it reads by
    reference."""

    __slots__ = ("_host", "_resolver")

    # The argument-REFERENCE half without the callable-parameter flow,
    # which is untuned for Dart: a tear-off passed as an argument keeps
    # its target reachable (Flutter's `onPressed: _handleTap`).
    references_arguments = True
    # A getter body can read another getter (`=> _wonders.length`, issue
    # #873) in a file with no call expression.
    scans_callers_without_calls = True
    types_locals = True
    imports_bind_calls = True
    runs_declared_constructors = True
    # A getter in argument position is read, not torn off (issue #869).
    property_args_are_reads = True

    def __init__(self, host: DartCallHost, resolver: CallResolver) -> None:
        self._host = host
        self._resolver = resolver

    def call_name(self, call_node: Node) -> CallName:
        # A Dart call node is a selector/cascade_section holding the
        # argument_part; the target name lives in the PRECEDING sibling
        # chain, not inside the node.
        return CallName(True, dart_utils.call_name(call_node))

    def method_name(self, method_node: Node, module_qn: str) -> str | None:
        # A factory_constructor_signature has no `name` field at all, so
        # the field lookup skipped every factory and its body was never
        # scanned (issue #2482); the definition pass names it this way.
        return dart_utils.get_name(method_node)

    def ingest_module_references(
        self,
        root_node: Node,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list[Node]],
        sorted_func_nodes: list[Node] | None,
    ) -> None:
        if dart_prop_names := self._resolver.function_registry.property_names():
            self.ingest_module_initializer_reads(
                root_node,
                module_qn,
                queries[language][cs.QUERY_CONFIG],
                dart_prop_names,
            )

    def ingest_caller_references(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        caller_type: str,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        class_context: str | None,
        local_var_types: dict[str, str] | None,
    ) -> None:
        if class_context:
            self.ingest_constructor_delegations(
                caller_node, caller_spec, class_context, module_qn
            )

    def ingest_module_initializer_reads(
        self,
        root_node: Node,
        module_qn: str,
        dart_config: LanguageSpec,
        dart_prop_names: set[str],
    ) -> None:
        # Field initializers execute at construction time even in a
        # file holding nothing but classes (no module caller pass
        # runs there), so each class subtree is scanned at FILE
        # level, before the no-calls early return.
        module_spec = (cs.NodeLabel.MODULE, cs.KEY_QUALIFIED_NAME, module_qn)
        for child in root_node.named_children:
            if child.type in dart_config.class_node_types:
                self._ingest_class_initializer_reads(
                    child,
                    module_spec,
                    module_qn,
                    module_qn,
                    None,
                    dart_config,
                    dart_prop_names,
                )

    @host_site_scoped
    def ingest_property_reads(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
    ) -> None:
        # A getter access is a bare identifier or a member selector, never an
        # invocation (issue #869), so callers that only READ a getter emit
        # nothing and dead-code flags it (wonderous' _enableVideo/startYr
        # family).
        # Emit a REFERENCES edge (a read is not an invocation; the call graph
        # stays invocation-only) from the caller to each getter it reads:
        # bare identifiers resolve against the enclosing class (implicit
        # this), member selectors reassemble their receiver chain and resolve
        # through receiver typing. Registry-guarded: only resolutions landing
        # on a MARKED property emit, so unrelated same-name locals or
        # functions never fabricate an edge. Nested closures are walked (a
        # Dart lambda's reads attribute to the enclosing method, matching the
        # flat-attribute call pass), so their parameters join the shadow set.
        class_types = lang_config.class_node_types
        seen: set[str] = set()

        # The grammar splits a definition into a signature node and a SIBLING
        # function_body: the signature holds no reads, so walk the body.
        body = dart_utils.body_node(caller_node)
        walk_root = body if body is not None else caller_node
        # The bodiless caller is the MODULE pass: FIELD INITIALIZERS read
        # getters outside any method body (`late final body =
        # _createFont(contentFont, ..)`), so each class subtree is walked by
        # a per-class sub-pass carrying the OWNING class as resolution
        # context. Method callers keep skipping nested classes instead.
        is_module_scope = body is None
        shadow_cell: list[dict[str, list[tuple[int, int]]]] = []

        def shadow_spans() -> dict[str, list[tuple[int, int]]]:
            # Built lazily on the first bare read; most callers have none.
            if not shadow_cell:
                shadow_cell.append(_shadow_spans(caller_node, walk_root))
            return shadow_cell[0]

        stack = list(walk_root.children)
        while stack:
            node = self._host._site_node = stack.pop()
            if node.type in class_types:
                # Class subtrees (field initializers) are scanned once at
                # FILE level by _ingest_class_initializer_reads.
                continue
            if is_module_scope and _is_owned_function_body(node):
                continue
            read_name = _read_name(node, prop_names, shadow_spans)
            if read_name:
                self._emit_property_read(
                    read_name,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_context,
                    seen,
                    node.start_byte,
                )
            stack.extend(node.children)

    @host_site_scoped
    def _ingest_class_initializer_reads(
        self,
        class_node: Node,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        lang_config: LanguageSpec,
        prop_names: set[str],
        parent_qn: str | None = None,
    ) -> None:
        # Field initializers run in the class's OWN scope: bare reads resolve
        # against the owning class (two classes may share a getter name), and
        # each class gets its own dedup set. A closure inside an initializer
        # belongs to no method pass, so its body is walked here; only
        # method/constructor bodies (owned by a class-body signature) have
        # their own pass. Dart forbids nested class declarations, but the
        # defensive recursion still threads the enclosing context through.
        class_types = lang_config.class_node_types
        name_node = next(
            (
                child
                for child in class_node.named_children
                if child.type == cs.TS_DART_IDENTIFIER
            ),
            None,
        )
        class_name = safe_decode_text(name_node) if name_node is not None else None
        owner_qn = parent_qn if parent_qn is not None else module_qn
        class_ctx = f"{owner_qn}{cs.SEPARATOR_DOT}{class_name}" if class_name else None
        seen: set[str] = set()
        shadow_cell: list[dict[str, list[tuple[int, int]]]] = []

        def shadow_spans() -> dict[str, list[tuple[int, int]]]:
            # Initializer closures declare parameters; their spans confine
            # any shadowing to the closure itself. Member signatures and
            # owned bodies stay out: a method parameter scopes its own body,
            # which this walk never reads.
            if not shadow_cell:
                shadow_cell.append(
                    _shadow_spans(class_node, class_node, skip_owned_members=True)
                )
            return shadow_cell[0]

        stack = list(class_node.children)
        while stack:
            node = self._host._site_node = stack.pop()
            if node.type in class_types:
                self._ingest_class_initializer_reads(
                    node,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    lang_config,
                    prop_names,
                    parent_qn=class_ctx,
                )
                continue
            if _is_owned_function_body(node):
                continue
            read_name = _read_name(node, prop_names, shadow_spans)
            if read_name:
                self._emit_property_read(
                    read_name,
                    caller_spec,
                    caller_qn,
                    module_qn,
                    local_var_types,
                    class_ctx,
                    seen,
                    node.start_byte,
                )
            stack.extend(node.children)

    def _emit_property_read(
        self,
        read_name: str,
        caller_spec: tuple[str, str, str],
        caller_qn: str,
        module_qn: str,
        local_var_types: dict[str, str] | None,
        class_context: str | None,
        seen: set[str],
        call_point: int | None = None,
    ) -> None:
        if read_name in seen:
            return
        registry = self._resolver.function_registry
        res_qn: str | None = None
        # A bare read inside a class binds the OWNING class's member first:
        # the trie fallback is name-global and would pick a same-named getter
        # from whichever class sorts first (a module-caller's caller_qn
        # carries no class to guide it).
        if class_context is not None and cs.SEPARATOR_DOT not in read_name:
            candidate = f"{class_context}{cs.SEPARATOR_DOT}{read_name}"
            if registry.get(candidate) is not None:
                res_qn = candidate
        if res_qn is None:
            resolved = self._resolver.resolve_function_call(
                read_name,
                module_qn,
                local_var_types,
                class_context,
                caller_qn,
                language=cs.SupportedLanguage.DART,
                call_point=call_point,
            )
            if not resolved:
                return
            res_qn = resolved[1]
        if not registry.is_property(res_qn) or res_qn == caller_qn:
            return
        seen.add(read_name)
        self._host.emit_rel(
            caller_spec,
            cs.RelationshipType.REFERENCES,
            (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, res_qn),
        )

    @host_site_scoped
    def ingest_constructor_delegations(
        self,
        caller_node: Node,
        caller_spec: tuple[str, str, str],
        class_qn: str,
        module_qn: str,
    ) -> None:
        # `: this(...)` and `: super(...)` run another constructor without
        # any call node, so a constructor reached only through a redirect or
        # a subclass's super initializer reported dead (issue #2482). An
        # unnamed constructor is registered under its class's simple name.
        resolver = self._resolver
        registry = resolver.function_registry
        constructors = resolver.type_inference.facts.dart_constructor_qns
        for delegation in dart_utils.constructor_delegations(caller_node):
            owner_qn = class_qn
            if delegation.to_super:
                # `super` is the `extends` class ONLY, as written in this
                # file: a `with` mixin's same-named METHOD, an `implements`
                # interface's constructor, or anything at all for a class
                # with no `extends` is never what runs (PR #2804).
                written = dart_utils.superclass_type(caller_node)
                if written is None:
                    continue
                owner_qn = resolver.follow_reexports(
                    resolver.dart.resolve_dart_written_type(written, module_qn)
                )
            simple = qn_markers.strip_dup_marker(
                owner_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
            )
            target_qn = f"{owner_qn}{cs.SEPARATOR_DOT}{delegation.name or simple}"
            if (
                registry.get(target_qn) != cs.NodeLabel.METHOD
                or target_qn not in constructors
            ):
                continue
            self._host._site_node = delegation.site
            self._host._resolution = cs.EdgeResolution.EXACT
            self._host.emit_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.METHOD, cs.KEY_QUALIFIED_NAME, target_qn),
            )

    def bound_callee(
        self,
        ctx: CallScan,
        call_node: Node,
        call_name: str,
        callee_type: str,
        callee_qn: str,
    ) -> tuple[str, str]:
        if (
            callee_type != cs.NodeLabel.CLASS
            and callee_qn in self._resolver.type_inference.facts.dart_constructor_qns
        ):
            self.instantiate_named_ctor_owner(
                ctx.caller_spec, callee_qn, ctx.ensure_rel
            )
        return callee_type, callee_qn

    def instantiate_named_ctor_owner(
        self,
        caller_spec: tuple[str, str, str],
        callee_qn: str,
        ensure_rel: Callable[..., None],
    ) -> None:
        # A named constructor (`Box.of(1)`) resolves to its own METHOD,
        # never to the class, so the call pass's class branch never records
        # the construction: INSTANTIATES the owning class here and let
        # the method path keep the CALLS edge (issue #2012).
        owner_qn = callee_qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
        owner_variants = [
            variant
            for variant in self._resolver.function_registry.variants(owner_qn)
            if self._resolver.function_registry.get(variant) == cs.NodeLabel.CLASS
        ]
        # Twin classes take the same stamp the class branch gives a
        # two-candidate construction.
        self._host._resolution = (
            cs.EdgeResolution.OVERLOAD
            if len(owner_variants) > 1
            else self._host.verdict
        )
        for class_variant in owner_variants:
            ensure_rel(
                caller_spec,
                cs.RelationshipType.INSTANTIATES,
                (cs.NodeLabel.CLASS, cs.KEY_QUALIFIED_NAME, class_variant),
            )
