from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from typing import NamedTuple

from tree_sitter import Node

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..call_resolver import CallResolver
from ..import_processor import ImportProcessor
from ..io_access import (
    DYNAMIC_TARGET,
    IO_MACRO_SINKS,
    IO_MEMBER_READS,
    IO_SINKS,
    IO_STREAM_SINKS,
    LANGUAGE_DESCRIPTORS,
    PY_SCOPE_BOUNDARIES,
    RESOURCE_QN_FORMAT,
    HandleBinding,
    IODirection,
    IOSink,
    LanguageDescriptor,
    ResourceKind,
    binding_targets_values,
    call_name,
    definition_header_nodes,
    first_token_arg_string,
    head_is_genuine_module,
    is_require_alias,
    iter_token_tree_calls,
    lean_binding_targets,
    literal_target,
    match_normalised,
    registry_match,
    scope_seed_nodes,
    string_literal,
    unwrap_argument,
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
    # What is tainting a variable: resolved resource origins plus the set of
    # callee QNs whose (possibly not-yet-processed) return value it carries. A
    # variable is present in the taint map iff it is tainted; a purely-pending
    # taint is only really tainted once finalize resolves a pending callee to a
    # tainted return -- how forward/cross-file return-taint is recovered (712).
    origins: frozenset[HandleBinding]
    pending: frozenset[str]


_EMPTY_TAINT = Taint(frozenset(), frozenset())


def _merge_taint(a: Taint, b: Taint) -> Taint:
    return Taint(a.origins | b.origins, a.pending | b.pending)


# Live taint state threaded through the walk: variable -> its Taint.
type _TaintMap = dict[str, Taint]

# Return wrappers whose taint lives in their elements: `return (x)`,
# `return a, b`, `return [x]`. Unwrapped so a tainted identifier/call inside
# them is still seen as a returned tainted value.
_RETURN_UNWRAP = (
    cs.TS_PY_PARENTHESIZED_EXPRESSION,
    cs.TS_PY_EXPRESSION_LIST,
    cs.TS_PY_TUPLE,
    cs.TS_PY_LIST,
)


def _return_value_nodes(return_node: Node) -> list[Node]:
    # Left-to-right leaf value nodes of a return statement, unwrapping the
    # parenthesis/tuple/list wrappers above.
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
    # Per-caller constants threaded through the source-ordered walk.
    caller_spec: tuple[str, str, str]
    caller_qn: str
    module_qn: str
    class_context: str | None
    language: cs.SupportedLanguage
    import_map: dict[str, str]
    read_sinks: dict[str, IOSink]
    write_sinks: dict[str, IOSink]
    # Write sinks reached NOT by a plain call: Rust `println!` (macro) and C++
    # `std::cout << x` (stream insertion). Empty for languages without them.
    macro_sinks: dict[str, IOSink]
    stream_sinks: dict[str, IOSink]
    # The caller's local receiver types (same map the CALLS pipeline used):
    # callee resolution for taint edges must honor typed receivers, or an
    # external-typed `cl.Get` unique-binds back to the enclosing method and
    # fabricates a FLOWS_TO self edge (viper's Get -> Get).
    local_var_types: dict[str, str] | None


# Languages whose local declarations hoist to the whole function scope (JS/TS
# const/let are in the temporal dead zone before their line, so using the name
# earlier is a ReferenceError; treating it as shadowed function-wide is safe).
# Go has no hoisting: a `:=`/`var` shadow only applies from its point forward, so
# its names are added to the live shadow set during the source-order walk instead.
_HOISTED_DECL_LANGS = frozenset(
    {cs.SupportedLanguage.JS, cs.SupportedLanguage.TS, cs.SupportedLanguage.TSX}
)

# Branch-and-merge node types for the non-hoisted flat walk (issue #714
# follow-up): each grammar spells its conditionals/loops/try with these type
# names, and the sets are unions across Go/Java/Rust/C++ (a type absent from
# one grammar simply never appears in its trees).
_FLAT_IF_TYPES = frozenset({cs.TS_JS_IF_STATEMENT, cs.TS_RS_IF_EXPRESSION})
_FLAT_LOOP_TYPES = frozenset(
    {
        cs.TS_JS_FOR_STATEMENT,
        cs.TS_JS_WHILE_STATEMENT,
        cs.TS_ENHANCED_FOR_STATEMENT,
        cs.TS_CPP_FOR_RANGE_LOOP,
        cs.TS_RS_FOR_EXPRESSION,
        cs.TS_RS_WHILE_EXPRESSION,
    }
)
# Loops whose body ALWAYS runs at least once (do-while, Rust `loop`): no
# zero-iteration skip path in the exit merge.
_FLAT_MANDATORY_LOOP_TYPES = frozenset({cs.TS_DO_STATEMENT, cs.TS_RS_LOOP_EXPRESSION})
_FLAT_TRY_TYPES = frozenset(
    {cs.TS_JS_TRY_STATEMENT, cs.TS_TRY_WITH_RESOURCES_STATEMENT}
)
# Switch-family statements routed to the shared branch-and-merge walker.
# C++'s "switch_statement" string is shared with JS, but the flat walk only
# ever sees C++ trees (JS routes through its own walk).
_FLAT_SWITCH_TYPES = frozenset(
    {
        cs.TS_GO_EXPRESSION_SWITCH_STATEMENT,
        cs.TS_GO_TYPE_SWITCH_STATEMENT,
        cs.TS_GO_SELECT_STATEMENT,
        cs.TS_JAVA_SWITCH_EXPRESSION,
        cs.TS_CPP_SWITCH_STATEMENT,
    }
)
# Every case-arm node type across the grammars; arms NOT in the
# fallthrough subset are exclusive (entered only from the switch header).
_SWITCH_ARM_TYPES = frozenset(
    {
        cs.TS_GO_EXPRESSION_CASE,
        cs.TS_GO_TYPE_CASE,
        cs.TS_GO_COMMUNICATION_CASE,
        cs.TS_GO_DEFAULT_CASE,
        cs.TS_JAVA_SWITCH_RULE,
        cs.TS_JAVA_SWITCH_BLOCK_STATEMENT_GROUP,
        cs.TS_CPP_CASE_STATEMENT,
        cs.TS_JS_SWITCH_CASE,
        cs.TS_JS_SWITCH_DEFAULT,
    }
)
# Arms a previous case can fall INTO (no break modeling: the entry unions
# the previous arm's exit). Go cases and Java arrow rules never fall
# through, so they enter only from the header state.
_FALLTHROUGH_ARM_TYPES = frozenset(
    {
        cs.TS_JAVA_SWITCH_BLOCK_STATEMENT_GROUP,
        cs.TS_CPP_CASE_STATEMENT,
        cs.TS_JS_SWITCH_CASE,
        cs.TS_JS_SWITCH_DEFAULT,
    }
)

# Arm node types spelled `default` explicitly (Go default_case, JS
# switch_default); the other grammars mark default structurally.
_EXPLICIT_DEFAULT_ARM_TYPES = frozenset(
    {cs.TS_GO_DEFAULT_CASE, cs.TS_JS_SWITCH_DEFAULT}
)


_JAVA_SWITCH_ARM_TYPES = frozenset(
    {cs.TS_JAVA_SWITCH_RULE, cs.TS_JAVA_SWITCH_BLOCK_STATEMENT_GROUP}
)


def _last_arm_statement(arm: Node) -> Node | None:
    # The arm's last top-level statement; Go nests arm statements inside a
    # statement_list, the C-family grammars keep them flat.
    last = arm.named_children[-1] if arm.named_children else None
    if last is not None and last.type == cs.TS_GO_STATEMENT_LIST:
        return last.named_children[-1] if last.named_children else None
    return last


def _arm_falls_into_next(arm: Node) -> bool:
    # Whether control can leave this arm by entering the NEXT one: a
    # C-family arm falls through unless its last statement is a plain
    # break (the break snapshot already carried that path out); a Go arm
    # falls only via the explicit trailing `fallthrough` keyword.
    last = _last_arm_statement(arm)
    if arm.type in _FALLTHROUGH_ARM_TYPES:
        return last is None or last.type != cs.TS_BREAK_STATEMENT
    return last is not None and last.type == cs.TS_GO_FALLTHROUGH_STATEMENT


def _py_case_always_matches(arm: Node) -> bool:
    # An UNGUARDED irrefutable pattern always matches; a guarded one can
    # fail its guard, so it never removes the no-match path.
    if arm.child_by_field_name(cs.TS_PY_FIELD_GUARD) is not None:
        return False
    pattern = next(
        (child for child in arm.named_children if child.type == cs.TS_PY_CASE_PATTERN),
        None,
    )
    return pattern is not None and _py_pattern_irrefutable(pattern)


def _py_pattern_irrefutable(pattern: Node) -> bool:
    # Irrefutable case patterns: `_` (empty case_pattern), a bare CAPTURE
    # name (dotted_name with exactly one identifier; multi-part dotted
    # names are value patterns that compare), and `<irrefutable> as x`.
    children = pattern.named_children
    if not children:
        return True
    if len(children) != 1:
        return False
    child = children[0]
    if child.type == cs.TS_PY_DOTTED_NAME:
        return child.named_child_count == 1
    if child.type == cs.TS_PY_AS_PATTERN:
        inner = next(
            (c for c in child.named_children if c.type == cs.TS_PY_CASE_PATTERN),
            None,
        )
        return inner is not None and _py_pattern_irrefutable(inner)
    if child.type == cs.TS_PY_UNION_PATTERN:
        # `1 | _` / `1 | other`: only the LAST alternative may legally be
        # irrefutable, and a bare `_` alternative is an ANONYMOUS node, so
        # inspect ALL children for the final non-separator one.
        last = next(
            (
                c
                for c in reversed(child.children)
                if c.is_named or c.type == cs.TS_PY_WILDCARD_NODE
            ),
            None,
        )
        if last is None:
            return False
        if last.type == cs.TS_PY_WILDCARD_NODE:
            return True
        return last.type == cs.TS_PY_DOTTED_NAME and last.named_child_count == 1
    return False


def _merge_optional_taints(left: Taint | None, right: Taint | None) -> Taint | None:
    if left is None:
        return right
    if right is None:
        return left
    return _merge_taint(left, right)


def _is_short_circuit(node: Node) -> bool:
    operator = node.child_by_field_name(cs.FIELD_OPERATOR)
    return (
        operator is not None
        and operator.text is not None
        and operator.text.decode(cs.ENCODING_UTF8) in cs.JS_SHORT_CIRCUIT_OPERATORS
    )


def _switch_arm_is_default(arm: Node) -> bool:
    # C++ default is a case_statement without a `value` field; a Java
    # default arm's switch_label has no named children (a case label
    # carries its pattern/expression there).
    if arm.type in _EXPLICIT_DEFAULT_ARM_TYPES:
        return True
    if arm.type == cs.TS_CPP_CASE_STATEMENT:
        return arm.child_by_field_name(cs.FIELD_VALUE) is None
    if arm.type in _JAVA_SWITCH_ARM_TYPES:
        # A group can stack labels (`case 1: default:`), so ANY empty
        # label marks the arm as the default target.
        return any(
            child.type == cs.TS_JAVA_SWITCH_LABEL and child.named_child_count == 0
            for child in arm.named_children
        )
    return False


class _JsCtx(NamedTuple):
    # Per-caller constants for the lean non-Python flow walk (issue #714).
    flow: _FlowCtx
    descriptor: LanguageDescriptor
    member_reads: tuple[tuple[str, ResourceKind], ...]
    # Names that shadow a same-named builtin source/sink (a local `const fetch`, a
    # parameter, a Go `os := ...`). MUTABLE: seeded with parameters (+ hoisted
    # declarations for JS/TS) and grown with each Go binding as the walk reaches
    # it, so a later Go shadow never suppresses an earlier valid source read.
    local_names: set[str]


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
        # Per-function return-taint SUMMARY collected during the walk: caller QN
        # -> the Taint it returns (resolved origins + pending callee QNs whose
        # return it forwards). Resolved to a fixpoint in finalize(), so a callee
        # defined/processed AFTER its caller (forward or cross-file) is still
        # accounted for (issue #712). Only plain data crosses the walk boundary,
        # never a tree-sitter Node (the AST cache evicts trees).
        self._summaries: dict[str, Taint] = {}
        # Deferred emission facts, drained once in finalize() when every summary
        # is in and the fixpoint is known. Each carries only serialized data:
        # return-edge candidates (callee_type, callee_qn, caller_spec) emit a
        # return edge iff the callee turns out to return taint; resource flows
        # (pending callee QNs, sink kind, sink identity) emit origin -> sink for
        # each pending callee's resolved origins; arg edges (pending QNs,
        # caller_spec, callee_type, callee_qn, via) emit iff a pending callee
        # resolves to a tainted return.
        self._return_edge_candidates: list[tuple[str, str, tuple[str, str, str]]] = []
        # Break-exit collectors: the top list captures the taint state AT
        # each `break` inside the switch arm being walked (a break exits
        # the switch with the state it saw THEN, not the arm's end state).
        # Loop walkers push None to shield the collector: a break in a
        # nested loop targets that loop, not the enclosing switch.
        self._break_exit_stack: list[list[_TaintMap] | None] = []
        self._deferred_resource_flows: list[
            tuple[frozenset[str], ResourceKind, str]
        ] = []
        self._deferred_arg_edges: list[
            tuple[frozenset[str], tuple[str, str, str], str, str, str]
        ] = []
        # Per-caller scratch for the return-taint summary, accumulated across
        # every branch of the structured walk (a return on ANY path contributes).
        # Reset at the top of each process_flow_for_caller; not reentrant (the
        # call processor drives one caller at a time).
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
        local_var_types: dict[str, str] | None = None,
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
            macro_sinks=IO_MACRO_SINKS.get(language, {}),
            stream_sinks=IO_STREAM_SINKS.get(language, {}),
            local_var_types=local_var_types,
        )
        # Non-Python languages take the lean flow walk (issue #714): taint
        # from a read source (process.env, fetch, fs.readFile) reaching a
        # write sink emits a resource->resource flow, a tainted value passed
        # to a callee emits an arg edge, and a returned tainted value feeds
        # the shared return-taint fixpoint. The walk is path-sensitive to
        # Python's level: if/loops/try branch-and-merge, plus each
        # language's own branching forms (Rust match, the switch family,
        # Go select, do-while). Python keeps its original walk below.
        if language != cs.SupportedLanguage.PYTHON:
            descriptor = LANGUAGE_DESCRIPTORS.get(language)
            if descriptor is not None:
                self._process_lean_flow(
                    caller_node, ctx, descriptor, IO_MEMBER_READS.get(language, ())
                )
            return

        # Path-sensitive MAY walk (issue #713): taint is threaded statement by
        # statement, but each if/elif/else, try/except and loop is evaluated
        # per branch against a COPY of the incoming state and unioned at the
        # merge point. So taint that survives on ANY path survives the join, and
        # a variable is killed only when reassigned to an untainted value on
        # EVERY path. Straight-line code still behaves exactly as the old flat
        # walk (single path, no branches to merge).
        self._acc_returns_taint = False
        self._acc_return_taint = _EMPTY_TAINT
        # Seed from the caller's own scope; on a nested def descend into its
        # header only (default args/decorators/bases run in THIS scope, its
        # body is a separate caller). Same scoping as io_access.
        tainted: _TaintMap = {}
        for node in scope_seed_nodes(caller_node):
            tainted = self._walk_stmt(node, tainted, ctx)

        if self._acc_returns_taint:
            self._summaries[caller_qn] = self._acc_return_taint

    # Lean non-Python (JS/TS) straight-line flow (issue #714) below.
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
            local_names=self._js_local_names(caller_node, descriptor, ctx.language),
        )
        body = caller_node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            statements = list(caller_node.named_children)
        elif body.type == descriptor.block_scope_type:
            statements = list(body.named_children)
        else:
            statements = [body]
        tainted: _TaintMap = {}
        if ctx.language in _HOISTED_DECL_LANGS:
            # Path-sensitive MAY walk (issue #714 follow-up): each JS/TS if/else,
            # loop, and try branch is evaluated against a COPY of the incoming
            # state and unioned at the merge, so taint surviving on ANY path
            # survives and a kill counts only when it happens on EVERY path.
            for node in statements:
                tainted = self._walk_js_stmt(node, tainted, jc)
        else:
            # Go/Java path-sensitive MAY walk (issue #714 follow-up): an
            # if_statement branches-and-merges like the JS walk (its condition/
            # consequence/alternative fields are shared across the grammars), and
            # the live shadow set is snapshotted per branch and restored at the
            # merge, so a block-scoped Go/Java declaration inside a branch does not
            # leak its shadow past the join. Loops and try are walked straight-line
            # (one source-order pass), matching the previous flat behaviour --
            # loop-carried and per-branch try taint stay a follow-up.
            for node in statements:
                tainted = self._walk_flat_stmt(node, tainted, jc)
        if self._acc_returns_taint:
            self._summaries[ctx.caller_qn] = self._acc_return_taint

    def _walk_flat_stmt(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # Structured walk for the non-hoisted flat languages (Go, Java, Rust,
        # C++): if/loop/try/match nodes branch-and-merge (issue #714 follow-up);
        # every other node applies its leaf effect and threads state through its
        # children in source order (so a nested call in an argument is seen).
        node_type = node.type
        if node_type in jc.descriptor.nested_scope_types:
            return state
        if node_type == cs.TS_BREAK_STATEMENT:
            self._record_break_exit(state)
            return state
        if node_type in _FLAT_IF_TYPES:
            return self._walk_flat_if(node, state, jc)
        if node_type in _FLAT_LOOP_TYPES:
            return self._walk_flat_loop(node, state, jc)
        if node_type in _FLAT_MANDATORY_LOOP_TYPES:
            return self._walk_flat_mandatory_loop(node, state, jc)
        if node_type in _FLAT_TRY_TYPES:
            return self._walk_flat_try(node, state, jc)
        if node_type in _FLAT_SWITCH_TYPES:
            return self._walk_switch(node, state, jc, self._walk_flat_stmt)
        if node_type == cs.TS_RS_MATCH_EXPRESSION:
            return self._walk_flat_match(node, state, jc)
        self._apply_js_leaf(node, state, jc)
        for child in node.named_children:
            state = self._walk_flat_stmt(child, state, jc)
        return state

    def _walk_flat_loop(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # Loop shield: a break inside the body targets THIS loop, never an
        # enclosing switch arm's collector.
        self._shield_breaks()
        try:
            return self._walk_flat_loop_inner(node, state, jc)
        finally:
            self._unshield_breaks()

    def _walk_flat_loop_inner(
        self, node: Node, state: _TaintMap, jc: _JsCtx
    ) -> _TaintMap:
        # The body runs zero or more times: union the skip path with one pass,
        # then re-walk once from that merge so taint carried from a later
        # iteration into an earlier statement is caught (same two-pass
        # approximation as the JS/Python loop walks; edges are
        # MERGE-idempotent so the re-walk never duplicates them). Header
        # declarations (a Go `for i := 0` / range var) stay shadowed for both
        # body passes; body-local shadows reset between passes and the whole
        # loop's shadows reset on exit. A Java enhanced-for is ITSELF the
        # binding node (extra_declarator_types), so its leaf effect (binding
        # the loop var to the iterable's taint) runs before the body.
        pre_loop_shadows = set(jc.local_names)
        if node.type in jc.descriptor.extra_declarator_types:
            self._apply_js_leaf(node, state, jc)
        body = node.child_by_field_name(cs.FIELD_BODY)
        state, update = self._walk_loop_header(node, state, jc, body)
        if body is None:
            self._restore_shadows(pre_loop_shadows, jc)
            return state
        header_shadows = set(jc.local_names)
        once = self._walk_flat_stmt(body, dict(state), jc)
        if update is not None:
            once = self._walk_flat_stmt(update, once, jc)
        self._restore_shadows(header_shadows, jc)
        merged = self._merge([state, once])
        twice = self._walk_flat_stmt(body, dict(merged), jc)
        if update is not None:
            twice = self._walk_flat_stmt(update, twice, jc)
        self._restore_shadows(pre_loop_shadows, jc)
        return self._merge([state, twice])

    def _walk_loop_header(
        self, node: Node, state: _TaintMap, jc: _JsCtx, body: Node | None
    ) -> tuple[_TaintMap, Node | None]:
        # Walk the pre-body header children and return the update clause
        # unwalked: a C-style for's update runs only AFTER a completed body
        # iteration, never on the zero-iteration path and never before the
        # first body pass, so the caller walks it after each body pass.
        # Java/C++ hold it in an `update` field on the loop node; Go nests
        # it inside the for_clause.
        update = node.child_by_field_name(cs.FIELD_UPDATE)
        for child in node.named_children:
            if body is not None and child.id == body.id:
                continue
            if update is not None and child.id == update.id:
                continue
            if child.type == cs.TS_GO_FOR_CLAUSE and update is None:
                update = child.child_by_field_name(cs.FIELD_UPDATE)
                state = self._walk_go_for_clause(child, update, state, jc)
                continue
            state = self._walk_flat_stmt(child, state, jc)
        return state, update

    def _walk_go_for_clause(
        self, clause: Node, update: Node | None, state: _TaintMap, jc: _JsCtx
    ) -> _TaintMap:
        # Go's init;cond;post for_clause: walk everything but the post
        # (update) statement, which the loop walk defers past the body.
        for part in clause.named_children:
            if update is None or part.id != update.id:
                state = self._walk_flat_stmt(part, state, jc)
        return state

    def _walk_flat_mandatory_loop(
        self, node: Node, state: _TaintMap, jc: _JsCtx
    ) -> _TaintMap:
        # A do-while / Rust `loop` body ALWAYS runs at least once, so the
        # pre-loop state is NOT part of the exit merge (a kill in the body
        # kills on every straight-line path), and a do-while condition runs
        # AFTER each body pass, so a sink there sees the body's taint
        # (Rust `loop` has no condition field). Exit = merge(one iteration,
        # two iterations); the second pass catches loop-carried taint
        # exactly like _walk_flat_loop. `break` is not modelled: a kill
        # below a conditional break still counts, the accepted flat-walk
        # approximation.
        return self._walk_mandatory_loop(node, state, jc, self._walk_flat_stmt)

    def _walk_mandatory_loop(
        self,
        node: Node,
        state: _TaintMap,
        jc: _JsCtx,
        walk: Callable[[Node, _TaintMap, _JsCtx], _TaintMap],
    ) -> _TaintMap:
        # Loop shield: a break inside the body targets THIS loop, never an
        # enclosing switch arm's collector.
        self._shield_breaks()
        try:
            return self._walk_mandatory_loop_inner(node, state, jc, walk)
        finally:
            self._unshield_breaks()

    def _walk_mandatory_loop_inner(
        self,
        node: Node,
        state: _TaintMap,
        jc: _JsCtx,
        walk: Callable[[Node, _TaintMap, _JsCtx], _TaintMap],
    ) -> _TaintMap:
        body = node.child_by_field_name(cs.FIELD_BODY)
        condition = node.child_by_field_name(cs.TS_FIELD_CONDITION)
        pre_shadows = set(jc.local_names)
        once = dict(state)
        if body is not None:
            once = walk(body, once, jc)
        if condition is not None:
            once = walk(condition, once, jc)
        self._restore_shadows(pre_shadows, jc)
        twice = dict(once)
        if body is not None:
            twice = walk(body, twice, jc)
        if condition is not None:
            twice = walk(condition, twice, jc)
        self._restore_shadows(pre_shadows, jc)
        return self._merge([once, twice])

    def _walk_flat_try(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # The try body may run fully (no-throw path) or partially before a
        # catch, so each handler is seeded with union(pre, body_exit): taint
        # killed inside the body still reaches the handler, since the throw
        # may precede the kill. A finally runs on the merged state.
        pre_shadows = set(jc.local_names)
        body = node.child_by_field_name(cs.FIELD_BODY)
        # try-with-resources: the resource declarations run before the body
        # on EVERY path (a throwing body still ran them), so the non-body,
        # non-handler children (the resource_specification) walk into the
        # pre-body state that also seeds each catch. The catch/finally node
        # type strings are shared verbatim by the JS, Java, and C++ grammars.
        for child in node.named_children:
            if child.type in (cs.TS_JS_CATCH_CLAUSE, cs.TS_JS_FINALLY_CLAUSE):
                continue
            if body is not None and child.id == body.id:
                continue
            state = self._walk_flat_stmt(child, state, jc)
        body_exit = (
            self._walk_flat_stmt(body, dict(state), jc)
            if body is not None
            else dict(state)
        )
        self._restore_shadows(pre_shadows, jc)
        branch_exits: list[_TaintMap] = [body_exit]
        finally_clause: Node | None = None
        for child in node.named_children:
            if child.type == cs.TS_JS_CATCH_CLAUSE:
                branch_exits.append(
                    self._walk_flat_stmt(child, self._merge([state, body_exit]), jc)
                )
                self._restore_shadows(pre_shadows, jc)
            elif child.type == cs.TS_JS_FINALLY_CLAUSE:
                finally_clause = child
        merged = self._merge(branch_exits)
        if finally_clause is not None:
            merged = self._walk_flat_stmt(finally_clause, merged, jc)
            self._restore_shadows(pre_shadows, jc)
        return merged

    def _walk_switch(
        self,
        node: Node,
        state: _TaintMap,
        jc: _JsCtx,
        walk: Callable[[Node, _TaintMap, _JsCtx], _TaintMap],
    ) -> _TaintMap:
        # Shared switch-family branch-and-merge (Go switch/type-switch/
        # select, Java switch, C++ switch, JS/TS switch). The header
        # (initializer, switched value, condition) runs on all paths; each
        # arm walks against a copy and the exits union (MAY join). An
        # exclusive arm (Go, Java arrow rule) enters from the header state
        # only; a fallthrough-capable arm also unions the previous arm's
        # fall-through state. Without a default arm the implicit no-match
        # path joins the exit merge; with one, some arm always runs, so a
        # kill on EVERY arm kills.
        body = node.child_by_field_name(cs.FIELD_BODY)
        arm_container = body if body is not None else node
        arms = [
            child
            for child in arm_container.named_children
            if child.type in _SWITCH_ARM_TYPES
        ]
        arm_ids = {arm.id for arm in arms}
        if body is not None:
            arm_ids.add(body.id)
        pre_switch_shadows = set(jc.local_names)
        for child in node.named_children:
            if child.id not in arm_ids:
                state = walk(child, state, jc)
        if not arms:
            self._restore_shadows(pre_switch_shadows, jc)
            return state
        exits, has_default = self._walk_switch_arms(arms, state, jc, walk)
        if not has_default:
            exits.append(dict(state))
        self._restore_shadows(pre_switch_shadows, jc)
        return self._merge(exits)

    def _walk_switch_arms(
        self,
        arms: list[Node],
        state: _TaintMap,
        jc: _JsCtx,
        walk: Callable[[Node, _TaintMap, _JsCtx], _TaintMap],
    ) -> tuple[list[_TaintMap], bool]:
        pre_arm_shadows = set(jc.local_names)
        exits: list[_TaintMap] = []
        fall_in: _TaintMap | None = None
        has_default = False
        for index, arm in enumerate(arms):
            has_default = has_default or _switch_arm_is_default(arm)
            entry = dict(state)
            # fall_in is non-None exactly when the PREVIOUS arm can fall
            # into this one (C-family arm without a trailing break, or a Go
            # arm ending in the explicit `fallthrough` keyword).
            if fall_in is not None:
                entry = self._merge([entry, fall_in])
            # Each break inside the arm exits the switch with the state it
            # saw THEN (a conditional break before a later kill carries the
            # live taint out), captured by the arm's own collector.
            self._break_exit_stack.append([])
            try:
                arm_exit = walk(arm, entry, jc)
            finally:
                break_exits = self._break_exit_stack.pop() or []
            self._restore_shadows(pre_arm_shadows, jc)
            exits.extend(break_exits)
            falls = index < len(arms) - 1 and _arm_falls_into_next(arm)
            # An arm's END state exits the switch directly only when it
            # does NOT continue into the next arm (a falling arm's state
            # reaches the merge THROUGH that arm instead; a stacked
            # `case 1: default:` label's empty group falls straight
            # through).
            if not falls:
                exits.append(arm_exit)
            fall_in = arm_exit if falls else None
        return exits, has_default

    def _walk_flat_match(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # Rust match: the scrutinee runs on all paths; each arm is walked
        # against a copy and unioned (MAY join). Arms are exhaustive, so the
        # merge is over the arms only (no implicit skip path).
        value = node.child_by_field_name(cs.FIELD_VALUE)
        if value is not None:
            state = self._walk_flat_stmt(value, state, jc)
        body = node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return state
        pre_shadows = set(jc.local_names)
        arm_exits: list[_TaintMap] = []
        for arm in body.named_children:
            if arm.type != cs.TS_RS_MATCH_ARM:
                continue
            arm_exits.append(self._walk_flat_stmt(arm, dict(state), jc))
            self._restore_shadows(pre_shadows, jc)
        return self._merge(arm_exits) if arm_exits else state

    def _walk_flat_if(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # The header (any initializer + condition) runs on all paths; each of the
        # then / else(-if) / implicit skip paths is walked against a copy and
        # unioned (MAY join). Two shadow scopes are restored: a branch grows the
        # live shadow set with its own block-scoped declarations (restored between
        # branches), and a Go `if` initializer (`if x := f(); cond {}`) is scoped
        # to the whole if statement (restored on exit), so neither shadows a
        # source/sink past its scope.
        pre_if_shadows = set(jc.local_names)
        consequence = node.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
        alternative = node.child_by_field_name(cs.FIELD_ALTERNATIVE)
        skip = {n.id for n in (consequence, alternative) if n is not None}
        for child in node.named_children:
            if child.id not in skip:
                state = self._walk_flat_stmt(child, state, jc)
        pre_shadows = set(jc.local_names)
        branch_exits: list[_TaintMap] = []
        if consequence is not None:
            branch_exits.append(self._walk_flat_stmt(consequence, dict(state), jc))
            self._restore_shadows(pre_shadows, jc)
        if alternative is not None:
            # else_clause holds either a block or a nested if (else-if chain); the
            # recursion handles both and merges within.
            branch_exits.append(self._walk_flat_stmt(alternative, dict(state), jc))
            self._restore_shadows(pre_shadows, jc)
        else:
            # No else: the skip path preserves the incoming state.
            branch_exits.append(dict(state))
        self._restore_shadows(pre_if_shadows, jc)
        return self._merge(branch_exits)

    def _record_break_exit(self, state: _TaintMap) -> None:
        # Snapshot the live state at a `break` for the enclosing switch
        # arm's collector. None on top = a loop shield (the break targets
        # that loop); empty stack = no enclosing switch at all. A labeled
        # break targeting a farther construct still records here: the
        # extra state only widens the MAY join, the sound direction.
        if self._break_exit_stack and self._break_exit_stack[-1] is not None:
            self._break_exit_stack[-1].append(dict(state))

    def _shield_breaks(self) -> None:
        self._break_exit_stack.append(None)

    def _unshield_breaks(self) -> None:
        self._break_exit_stack.pop()

    @staticmethod
    def _restore_shadows(pre_shadows: set[str], jc: _JsCtx) -> None:
        # Reset the mutable live shadow set to a pre-branch snapshot in place (jc is
        # a NamedTuple, so the set object is shared; mutate, do not rebind).
        jc.local_names.clear()
        jc.local_names.update(pre_shadows)

    def _walk_js_stmt(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # Path-sensitive walk for JS/TS: control-flow nodes branch-and-merge, every
        # other node applies its leaf effect and threads state through its children
        # in source order (so a nested call in an argument is still seen).
        node_type = node.type
        if node_type in jc.descriptor.nested_scope_types:
            return state
        if node_type == cs.TS_BREAK_STATEMENT:
            self._record_break_exit(state)
            return state
        if node_type == cs.TS_JS_IF_STATEMENT:
            return self._walk_js_if(node, state, jc)
        if node_type in (
            cs.TS_JS_WHILE_STATEMENT,
            cs.TS_JS_FOR_STATEMENT,
            cs.TS_JS_FOR_IN_STATEMENT,
        ):
            return self._walk_js_loop(node, state, jc)
        if node_type == cs.TS_JS_TRY_STATEMENT:
            return self._walk_js_try(node, state, jc)
        if node_type == cs.TS_JS_SWITCH_STATEMENT:
            return self._walk_switch(node, state, jc, self._walk_js_stmt)
        if node_type == cs.TS_DO_STATEMENT:
            # do-while: the body always runs once and the condition runs
            # AFTER it, so this is the mandatory-loop shape, not the
            # zero-or-more loop walk.
            return self._walk_mandatory_loop(node, state, jc, self._walk_js_stmt)
        self._apply_js_leaf(node, state, jc)
        for child in node.named_children:
            state = self._walk_js_stmt(child, state, jc)
        return state

    def _walk_js_if(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # The condition runs on all paths; each of the then / else(-if) / implicit
        # skip paths is walked against a copy and unioned (MAY join).
        cond = node.child_by_field_name(cs.TS_FIELD_CONDITION)
        if cond is not None:
            state = self._walk_js_stmt(cond, state, jc)
        branch_exits: list[_TaintMap] = []
        consequence = node.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
        if consequence is not None:
            branch_exits.append(self._walk_js_stmt(consequence, dict(state), jc))
        alternative = node.child_by_field_name(cs.FIELD_ALTERNATIVE)
        if alternative is not None:
            # else_clause holds either a block or a nested if (else-if chain); the
            # recursion handles both and merges within.
            branch_exits.append(self._walk_js_stmt(alternative, dict(state), jc))
        else:
            # No else: the skip path preserves the incoming state.
            branch_exits.append(dict(state))
        return self._merge(branch_exits)

    def _walk_js_loop(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # Loop shield: a break inside the body targets THIS loop, never an
        # enclosing switch arm's collector.
        self._shield_breaks()
        try:
            return self._walk_js_loop_inner(node, state, jc)
        finally:
            self._unshield_breaks()

    def _walk_js_loop_inner(
        self, node: Node, state: _TaintMap, jc: _JsCtx
    ) -> _TaintMap:
        # The initializer/condition/iterable runs before the body; the body runs
        # zero or more times, so union the skip path with one pass, then re-walk
        # once from that merge to catch taint carried from a later iteration into
        # an earlier statement. ponytail: two passes, not a full fixpoint; edges
        # are MERGE-idempotent so the re-walk never duplicates them.
        body = node.child_by_field_name(cs.FIELD_BODY)
        # A C-style for's `increment` runs AFTER the body each iteration, not in
        # the header, so skip it below and walk it after each body pass instead.
        increment = (
            node.child_by_field_name(cs.FIELD_INCREMENT)
            if node.type == cs.TS_JS_FOR_STATEMENT
            else None
        )
        skip_ids = {n.id for n in (body, increment) if n is not None}
        for child in node.named_children:
            if child.id in skip_ids:
                continue
            state = self._walk_js_stmt(child, state, jc)
        if body is not None:
            once = self._walk_js_stmt(body, dict(state), jc)
            if increment is not None:
                once = self._walk_js_stmt(increment, once, jc)
            merged = self._merge([state, once])
            twice = self._walk_js_stmt(body, dict(merged), jc)
            if increment is not None:
                twice = self._walk_js_stmt(increment, twice, jc)
            state = self._merge([state, twice])
        return state

    def _walk_js_try(self, node: Node, state: _TaintMap, jc: _JsCtx) -> _TaintMap:
        # The try body may run fully (no-throw path) or partially before a catch;
        # seed the handler with union(pre, body_exit) so taint introduced before a
        # throw still reaches it. A finally runs on the merged state of both.
        body = node.child_by_field_name(cs.FIELD_BODY)
        body_exit = (
            self._walk_js_stmt(body, dict(state), jc)
            if body is not None
            else dict(state)
        )
        branch_exits: list[_TaintMap] = [body_exit]
        handler = node.child_by_field_name(cs.FIELD_HANDLER)
        if handler is not None:
            branch_exits.append(
                self._walk_js_stmt(handler, self._merge([state, body_exit]), jc)
            )
        merged = self._merge(branch_exits)
        finalizer = node.child_by_field_name(cs.FIELD_FINALIZER)
        if finalizer is not None:
            merged = self._walk_js_stmt(finalizer, merged, jc)
        return merged

    def _apply_js_leaf(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        # The leaf effect of one node on the taint map: bind (JS/Go), Go range
        # kill, a call's sink/arg edges, or a return's contribution to the summary.
        node_type = node.type
        d = jc.descriptor
        if node_type == d.declarator_type or node_type in (
            cs.TS_ASSIGNMENT_EXPRESSION,
            cs.TS_GO_ASSIGNMENT_STATEMENT,
        ):
            self._lean_bind(node, tainted, jc)
        elif node_type in d.extra_declarator_types:
            if node_type == cs.TS_GO_RANGE_CLAUSE:
                self._lean_kill(node, tainted, jc)
            else:
                self._lean_bind(node, tainted, jc)
        elif node_type == d.call_type:
            self._js_call(node, tainted, jc)
        elif d.macro_type is not None and node_type == d.macro_type:
            self._flow_macro(node, tainted, jc)
        elif d.stream_sink_type is not None and node_type == d.stream_sink_type:
            self._flow_stream(node, tainted, jc)
        elif node_type == cs.TS_RETURN_STATEMENT:
            returned = self._js_return_taint(node, tainted, jc)
            if returned is not None:
                self._acc_returns_taint = True
                self._acc_return_taint = _merge_taint(self._acc_return_taint, returned)

    def _lean_bind(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        # Bind LHS name(s) to their RHS taint across the grammars: JS uses a single
        # `name`/`value` (declarator) or `left`/`right` (assignment); Go uses `left`/
        # `right` expression_lists (`:=`, `=`) or `name`/`value` (`var`/`const`).
        # Shared (LHS names, RHS values) extraction with the I/O handle walk.
        targets, values = binding_targets_values(node, jc.descriptor)
        # `resp, err := http.Get(u)`: one RHS call feeding several LHS taints them
        # all (a tuple return can't be split statically, over-approximating err).
        spread = len(values) == 1 and len(targets) > 1
        # Go assigns in parallel: every RHS is evaluated against the PRE-assignment
        # map before any LHS is updated, so `a, b = b, a` swaps correctly. Compute
        # all taints first, then apply, or an earlier LHS update would corrupt a
        # later RHS read of the same name.
        computed: list[tuple[str, Taint | None]] = []
        for index, name in enumerate(targets):
            if name is None:
                continue
            rhs = (
                values[0]
                if spread
                else (values[index] if index < len(values) else None)
            )
            computed.append((name, self._js_expr_taint(rhs, tainted, jc)))
        for name, taint in computed:
            if taint is not None:
                tainted[name] = taint
            else:
                tainted.pop(name, None)
        # Register the bound names AFTER reading the RHS (which still saw the
        # pre-declaration scope): a Go shadow applies only from here forward.
        self._register_shadows(targets, jc)

    def _lean_kill(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        names = lean_binding_targets(left, jc.descriptor)
        for name in names:
            if name is not None:
                tainted.pop(name, None)
        self._register_shadows(names, jc)

    @staticmethod
    def _register_shadows(names: list[str | None], jc: _JsCtx) -> None:
        # Non-hoisted (Go) declarations grow the live shadow set as the walk
        # reaches them; JS/TS names are already seeded function-wide (and a
        # require-alias must never be registered, so skip hoisted languages).
        if jc.flow.language in _HOISTED_DECL_LANGS:
            return
        for name in names:
            if name is not None:
                jc.local_names.add(name)

    def _js_expr_taint(
        self, node: Node | None, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # The Taint an expression carries: an identifier propagates the map; a
        # member/subscript may be an env source; a call may be a read source or a
        # function whose return is deferred (pending) to the fixpoint.
        if node is None:
            return None
        d = jc.descriptor
        node_type = node.type
        if node_type in (cs.TS_AWAIT_EXPRESSION, cs.TS_PARENTHESIZED_EXPRESSION):
            # Unwrap `await expr` and `(expr)` to the inner source expression.
            return self._js_expr_taint(self._js_first_expr(node), tainted, jc)
        if node_type == cs.TS_GO_TYPE_CONVERSION_EXPRESSION:
            # Go `[]byte(s)` / `string(b)`: value-preserving, taint carries.
            return self._js_expr_taint(
                node.child_by_field_name(cs.TS_GO_FIELD_OPERAND), tainted, jc
            )
        if node_type == cs.TS_RS_REFERENCE_EXPRESSION:
            # Rust `&s` / `&mut s`: a borrow of a tainted value is tainted.
            return self._js_expr_taint(
                node.child_by_field_name(cs.FIELD_VALUE), tainted, jc
            )
        if node_type in (cs.TS_JS_TERNARY_EXPRESSION, cs.TS_PY_CONDITIONAL_EXPRESSION):
            # `c ? a : b` (Java shares ternary_expression, C++ spells it
            # conditional_expression): the value MAY be either branch, so
            # their taints union; the condition never becomes the value.
            return _merge_optional_taints(
                self._js_expr_taint(
                    node.child_by_field_name(cs.TS_FIELD_CONSEQUENCE), tainted, jc
                ),
                self._js_expr_taint(
                    node.child_by_field_name(cs.FIELD_ALTERNATIVE), tainted, jc
                ),
            )
        if (
            node_type == cs.TS_BINARY_EXPRESSION
            and jc.flow.language in _HOISTED_DECL_LANGS
            and _is_short_circuit(node)
        ):
            # JS-ONLY: `env || 'fallback'` / `??` / `&&` yield one OPERAND
            # as the result, so the taints of both union (MAY). In
            # Go/Java/C++/Rust these operators produce a boolean, never an
            # operand, so the value stays clean there.
            return _merge_optional_taints(
                self._js_expr_taint(
                    node.child_by_field_name(cs.TS_FIELD_LEFT), tainted, jc
                ),
                self._js_expr_taint(
                    node.child_by_field_name(cs.TS_FIELD_RIGHT), tainted, jc
                ),
            )
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
                jc.flow.local_var_types,
            )
            if callee is not None:
                self._return_edge_candidates.append(
                    (callee[0], callee[1], jc.flow.caller_spec)
                )
                return Taint(frozenset(), frozenset({callee[1]}))
            # A method chain (`std::env::var("X").unwrap()`): the callee itself is
            # not a source, but its receiver call may be, so recurse the left spine.
            # Gated on the receiver being a call (not a bare identifier) so plain
            # variable taint is never propagated through an arbitrary method.
            func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if func is not None and func.type == d.member_expression_type:
                receiver = func.child_by_field_name(d.object_field)
                if receiver is not None and receiver.type == d.call_type:
                    return self._js_expr_taint(receiver, tainted, jc)
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
                wrapper_type=jc.descriptor.argument_wrapper_type,
                template_type=jc.descriptor.template_string_type,
                substitution_type=jc.descriptor.template_substitution_type,
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
            jc.flow.local_var_types,
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

    def _emit_taint_to_sink(
        self, taint: Taint, kind: ResourceKind, identity: str
    ) -> None:
        # A tainted value reaching a write sink: emit resolved origins now, defer
        # pending callee returns to the fixpoint (mirrors the _js_call write branch).
        for origin in taint.origins:
            self._emit_resource_flow(origin, kind, identity)
        if taint.pending:
            self._deferred_resource_flows.append((taint.pending, kind, identity))

    def _flow_macro(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        # A Rust macro sink (`println!(secret)`) writes STDOUT (identity <dynamic>,
        # the arg is a format template). tree-sitter flattens the macro body to raw
        # tokens, so taint reaches it two ways: a tainted local as a bare identifier
        # token, or a read source inlined as a scoped call in the token stream.
        macro = node.child_by_field_name(cs.TS_RS_FIELD_MACRO)
        if macro is None or macro.text is None:
            return
        sink = self._js_match_sink(
            macro.text.decode(cs.ENCODING_UTF8), jc.flow.macro_sinks, jc
        )
        if sink is None:
            return
        for child in node.named_children:
            if child.type == cs.TS_RS_TOKEN_TREE:
                for taint in self._macro_arg_taints(child, tainted, jc):
                    self._emit_taint_to_sink(taint, sink.kind, DYNAMIC_TARGET)

    def _macro_arg_taints(
        self, token_tree: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> list[Taint]:
        out: list[Taint] = []
        # A tainted local reaching the macro args by name: either a bare identifier
        # token (`println!("{}", secret)`) or an inline format capture inside the
        # template string (`println!("{secret}")`, Rust 2021+), which has no
        # separate identifier token.
        named = self._bare_arg_identifiers(
            token_tree, jc.descriptor.identifier_type, cs.TS_RS_TOKEN_SCOPE
        ) | self._macro_format_captures(token_tree, jc)
        for name in named:
            taint = tainted.get(name)
            if taint is not None:
                out.append(taint)
        # A read source inlined as a scoped call (`std::env::var("X")`).
        for raw, args in iter_token_tree_calls(
            token_tree,
            cs.TS_RS_TOKEN_SCOPE,
            jc.descriptor.identifier_type,
            cs.TS_RS_TOKEN_TREE,
        ):
            sink = self._js_match_sink(raw, jc.flow.read_sinks, jc)
            if sink is None:
                continue
            identity = DYNAMIC_TARGET
            if sink.target_arg == 0:
                identity = first_token_arg_string(
                    args, jc.descriptor.string_type, jc.descriptor.string_content_type
                )
            out.append(
                Taint(
                    frozenset({HandleBinding(kind=sink.kind, identity=identity)}),
                    frozenset(),
                )
            )
        return out

    def _macro_format_captures(self, token_tree: Node, jc: _JsCtx) -> set[str]:
        # Names captured inline by the format template (the FIRST string literal in
        # the macro args), e.g. `println!("{secret} {x:?}")`. Only the first string
        # is the template; later string args are values, not templates.
        for child in token_tree.children:
            if child.type == jc.descriptor.string_type:
                content = string_literal(
                    child, jc.descriptor.string_type, jc.descriptor.string_content_type
                )
                if content == DYNAMIC_TARGET:
                    return set()
                return self._format_capture_names(content)
        return set()

    @staticmethod
    def _format_capture_names(template: str) -> set[str]:
        # Identifier names in `{name}` / `{name:spec}` placeholders of a Rust format
        # template. `{{`/`}}` are escaped braces; positional (`{}`, `{0}`) captures
        # reference no local, so only alphanumeric-identifier names are returned.
        out: set[str] = set()
        i, n = 0, len(template)
        while i < n:
            char = template[i]
            if char == "{":
                if i + 1 < n and template[i + 1] == "{":
                    i += 2
                    continue
                close = template.find("}", i + 1)
                if close == -1:
                    break
                name = template[i + 1 : close].split(":", 1)[0].strip()
                if (
                    name
                    and (name[0].isalpha() or name[0] == "_")
                    and all(c.isalnum() or c == "_" for c in name)
                ):
                    out.add(name)
                i = close + 1
            elif char == "}" and i + 1 < n and template[i + 1] == "}":
                i += 2
            else:
                i += 1
        return out

    @staticmethod
    def _bare_arg_identifiers(
        token_tree: Node, identifier_type: str, scope_separator: str
    ) -> set[str]:
        # Identifiers in a flattened macro body that are NOT a segment of a scoped
        # path: a path segment (`std`/`env`/`var` in `std::env::var`) is adjacent to
        # a `::` token, so it is excluded and a tainted local that happens to share a
        # segment name (a local `env`) is not confused with the path (over-taint P1).
        out: set[str] = set()
        stack = [token_tree]
        while stack:
            current = stack.pop()
            kids = current.children
            for i, child in enumerate(kids):
                if child.type == identifier_type and child.text:
                    prev_sep = i > 0 and kids[i - 1].type == scope_separator
                    next_sep = i + 1 < len(kids) and kids[i + 1].type == scope_separator
                    if not prev_sep and not next_sep:
                        out.add(child.text.decode(cs.ENCODING_UTF8))
                elif child.type == cs.TS_RS_TOKEN_TREE:
                    stack.append(child)
        return out

    def _flow_stream(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        # A C++ stream sink (`std::cout << a << b`) nests left-associatively. Act
        # only at the TOP of the `<<` chain, walk the `left` spine to the base
        # operand; if it is a stream sink (cout/cerr), flow the taint of every
        # inserted operand to STDOUT. A non-stream base (arithmetic `x << 2`) misses.
        d = jc.descriptor
        if not self._is_stream_insertion(node, d):
            return
        parent = node.parent
        if parent is not None and self._is_stream_insertion(parent, d):
            return
        operands: list[Node] = []
        base = node
        while self._is_stream_insertion(base, d):
            right = base.child_by_field_name(cs.FIELD_RIGHT)
            if right is not None:
                operands.append(right)
            left = base.child_by_field_name(cs.FIELD_LEFT)
            if left is None:
                return
            base = left
        if base.text is None:
            return
        sink = self._js_match_sink(
            base.text.decode(cs.ENCODING_UTF8), jc.flow.stream_sinks, jc
        )
        if sink is None:
            return
        for operand in operands:
            taint = self._js_expr_taint(operand, tainted, jc)
            if taint is not None:
                self._emit_taint_to_sink(taint, sink.kind, DYNAMIC_TARGET)

    @staticmethod
    def _is_stream_insertion(node: Node, descriptor: LanguageDescriptor) -> bool:
        # A binary_expression whose `operator` field is the stream-insertion token.
        if (
            descriptor.stream_sink_type is None
            or node.type != descriptor.stream_sink_type
        ):
            return False
        operator = node.child_by_field_name(cs.FIELD_OPERATOR)
        return (
            operator is not None
            and operator.text is not None
            and operator.text.decode(cs.ENCODING_UTF8)
            == descriptor.stream_sink_operator
        )

    def _js_arg_taints(
        self, node: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> list[tuple[str, Taint | None]]:
        args = node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return []
        out: list[tuple[str, Taint | None]] = []
        # Comments are named children; exclude them so arg positions stay correct.
        # C# wraps each arg in an `argument` node, so unwrap to the real
        # expression before reading its taint (no-op for the bare-arg grammars).
        wrapper = jc.descriptor.argument_wrapper_type
        for index, child in enumerate(self._named_no_comments(args)):
            out.append(
                (
                    VIA_ARG_FORMAT.format(index=index),
                    self._js_expr_taint(unwrap_argument(child, wrapper), tainted, jc),
                )
            )
        return out

    def _js_return_taint(
        self, node: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # A return carries the taint of any returned value: JS `return expr` is a
        # direct child, Go `return expr` wraps it in an expression_list (and
        # `return a, b` several); union the taint over every returned value.
        result: Taint | None = None
        for expr in self._lean_return_values(node):
            taint = self._js_expr_taint(expr, tainted, jc)
            if taint is not None:
                result = taint if result is None else _merge_taint(result, taint)
        return result

    @staticmethod
    def _lean_return_values(node: Node) -> list[Node]:
        out: list[Node] = []
        for child in node.named_children:
            if child.type == cs.TS_COMMENT:
                continue
            if child.type == cs.TS_GO_EXPRESSION_LIST:
                out.extend(c for c in child.named_children if c.type != cs.TS_COMMENT)
            else:
                out.append(child)
        return out

    @staticmethod
    def _named_no_comments(node: Node) -> list[Node]:
        return [c for c in node.named_children if c.type != cs.TS_COMMENT]

    @staticmethod
    def _js_first_expr(node: Node) -> Node | None:
        # The first meaningful sub-expression, skipping comment named children.
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
            wrapper_type=jc.descriptor.argument_wrapper_type,
            template_type=jc.descriptor.template_string_type,
            substitution_type=jc.descriptor.template_substitution_type,
        )
        return HandleBinding(kind=sink.kind, identity=identity)

    @staticmethod
    def _js_match_sink(
        raw: str, sink_map: dict[str, IOSink], jc: _JsCtx
    ) -> IOSink | None:
        # Same shadow-aware matching as io_access: a locally-bound name is not
        # the builtin; the import-normalised name matches first (so an aliased
        # builtin resolves), then the raw dotted name only when its head resolves
        # to the genuine module (node:/require/ESM), never a shadowing local module.
        # Rust (scope_separator="::") keys sinks under the full `std::` form: expand
        # the head through the import map on `::` (`use std::env; env::var` ->
        # `std::env::var`; a fully-qualified `std::env::var` has an unimported `std`
        # head and stays as-is); a head bound to a local name is shadowed.
        scope_sep = jc.descriptor.scope_separator
        if scope_sep is not None:
            scoped_head, _, scoped_rest = raw.partition(scope_sep)
            if scoped_head in jc.local_names:
                return None
            base = jc.flow.import_map.get(scoped_head)
            if base is not None:
                raw = f"{base}{scope_sep}{scoped_rest}" if scoped_rest else base
            return sink_map.get(raw)
        head, sep, _ = raw.partition(cs.SEPARATOR_DOT)
        if (head if sep else raw) in jc.local_names:
            return None
        if (sink := match_normalised(raw, jc.flow.import_map, sink_map)) is not None:
            return sink
        if not sep or not head_is_genuine_module(jc.flow.import_map.get(head), head):
            return None
        return sink_map.get(raw)

    @staticmethod
    def _js_import_shadowed(head: str, jc: _JsCtx) -> bool:
        return not head_is_genuine_module(jc.flow.import_map.get(head), head)

    def _js_local_names(
        self,
        caller_node: Node,
        descriptor: LanguageDescriptor,
        language: cs.SupportedLanguage,
    ) -> set[str]:
        # The shadow set seed: always the caller's parameters (in scope for the
        # whole body in every grammar). For hoisted-declaration languages (JS/TS)
        # also every declarator/function name in the body up front, since those
        # shadow function-wide. Go declarations are NOT hoisted, so they are added
        # to the live set during the walk (see _lean_bind) rather than seeded here.
        # A `const fs = require('fs')` declarator is an import alias (the genuine
        # module), so it is NOT a shadow; a local `const fs = {}` IS one.
        names: set[str] = set()
        params = caller_node.child_by_field_name(descriptor.params_field)
        if params is not None:
            for child in params.named_children:
                self._js_binding_names(child, descriptor, names)
        if language not in _HOISTED_DECL_LANGS:
            return names
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
        return names

    def _js_binding_names(
        self, node: Node, descriptor: LanguageDescriptor, out: set[str]
    ) -> None:
        # The names a binding target introduces: a plain identifier, a TS
        # required/optional parameter wrapper (its `pattern`), a default
        # (assignment_pattern left), a destructuring pattern's leaves, or a Go
        # expression_list / `parameter_declaration` (`os Config`).
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
        elif node_type == cs.TS_GO_PARAMETER_DECLARATION:
            for child in node.children_by_field_name(cs.TS_FIELD_NAME):
                self._js_binding_names(child, descriptor, out)
        elif node_type in (
            cs.TS_OBJECT_PATTERN,
            cs.TS_ARRAY_PATTERN,
            cs.TS_REST_PATTERN,
            cs.TS_GO_EXPRESSION_LIST,
        ):
            for child in node.named_children:
                self._js_binding_names(child, descriptor, out)

    @staticmethod
    def _merge(states: list[_TaintMap]) -> _TaintMap:
        # MAY union across branches: a variable is tainted after the join if it
        # is tainted on ANY incoming path; its origins/pending are the union of
        # those from the paths where it is tainted. A variable absent from every
        # branch (killed/never tainted on all paths) stays absent.
        out: _TaintMap = {}
        for state in states:
            for var, taint in state.items():
                existing = out.get(var)
                out[var] = _merge_taint(existing, taint) if existing else taint
        return out

    def _walk_stmt(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        node_type = node.type
        if node_type in PY_SCOPE_BOUNDARIES:
            # Nested def/class: only its header executes in this scope.
            for header in definition_header_nodes(node):
                state = self._walk_stmt(header, state, ctx)
            return state
        if node_type == cs.TS_PY_IF_STATEMENT:
            return self._walk_if(node, state, ctx)
        if node_type in (cs.TS_PY_FOR_STATEMENT, cs.TS_PY_WHILE_STATEMENT):
            return self._walk_loop(node, state, ctx)
        if node_type == cs.TS_PY_TRY_STATEMENT:
            return self._walk_try(node, state, ctx)
        if node_type == cs.TS_PY_MATCH_STATEMENT:
            return self._walk_py_match(node, state, ctx)
        if node_type == cs.TS_PY_ASSIGNMENT:
            self._apply_assignment(node, state, ctx)
        elif node_type == cs.TS_PY_CALL:
            self._apply_call(node, state, ctx)
        elif node_type == cs.TS_PY_RETURN_STATEMENT:
            # Process every return: each `return callee()` emits its own
            # callee->caller edge, and the body's returned-source set is the
            # UNION over all returns (any branch), so branches returning
            # DIFFERENT tainted sources each carry to callers of this function.
            returned = self._return_taint_source(node, state, ctx)
            if returned is not None:
                self._acc_returns_taint = True
                self._acc_return_taint = _merge_taint(self._acc_return_taint, returned)
        # Descend into children in source order (an assignment's RHS call still
        # needs _apply_call for its arg edges; nested calls in args likewise).
        for child in node.children:
            state = self._walk_stmt(child, state, ctx)
        return state

    def _walk_py_match(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        # match arms are EXCLUSIVE: each case_clause walks against a copy
        # of the post-subject state and the exits union (MAY join), same
        # semantics as the lean walk's switch family. The implicit
        # no-match path joins unless an UNGUARDED `case _` (empty
        # case_pattern, no guard) always matches.
        subject = node.child_by_field_name(cs.FIELD_SUBJECT)
        if subject is not None:
            state = self._walk_stmt(subject, state, ctx)
        body = node.child_by_field_name(cs.FIELD_BODY)
        if body is None:
            return state
        arm_exits: list[_TaintMap] = []
        has_wildcard = False
        for arm in body.named_children:
            if arm.type != cs.TS_PY_CASE_CLAUSE:
                continue
            has_wildcard = has_wildcard or _py_case_always_matches(arm)
            arm_exits.append(self._walk_stmt(arm, dict(state), ctx))
        if not arm_exits:
            return state
        if not has_wildcard:
            arm_exits.append(dict(state))
        return self._merge(arm_exits)

    def _walk_if(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        # The if-condition runs on all paths; process it in the incoming state.
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
        # No else means the skip path preserves the incoming state.
        if not has_else:
            branch_exits.append(dict(state))
        return self._merge(branch_exits) if branch_exits else state

    def _walk_loop(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        # The while-condition / for-iterable runs before the body.
        for field in (cs.TS_FIELD_CONDITION, cs.TS_FIELD_RIGHT):
            part = node.child_by_field_name(field)
            if part is not None:
                state = self._walk_stmt(part, state, ctx)
        body = node.child_by_field_name(cs.FIELD_BODY)
        if body is not None:
            # The body runs zero or more times: union the skip path with one
            # pass, then re-run the body once from that merge so taint carried
            # from a later iteration into an earlier statement is caught.
            # ponytail: two passes, not a full fixpoint; iterate to stability
            # only if deeper loop-carried taint chains ever matter. Edges are
            # MERGE-idempotent, so the re-walk never duplicates graph edges.
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
                    # An except handler can run after the try body partially
                    # executed, so seed it with union(pre, body_exit); taint
                    # introduced before the raise must still reach the handler.
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
        # The try body completing normally (no else) is itself a path.
        if not has_else:
            branch_exits.append(body_exit)
        merged = self._merge(branch_exits) if branch_exits else body_exit
        for clause in node.children:
            if clause.type == cs.TS_PY_FINALLY_CLAUSE:
                block = next(
                    (c for c in clause.children if c.type == cs.TS_PY_BLOCK), None
                )
                if block is not None:
                    # finally runs on every path: apply it to the merged state.
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
        taint = self._py_value_taint(right, tainted, ctx)
        if taint is not None:
            tainted[lhs] = taint
        else:
            # Any other RHS (literal, expression, unresolved call) leaves
            # lhs clean.
            tainted.pop(lhs, None)

    def _py_value_taint(
        self, node: Node, tainted: _TaintMap, ctx: _FlowCtx
    ) -> Taint | None:
        # The Taint a Python value expression carries: identifiers
        # propagate the map, calls seed a source or defer on the callee's
        # return, and value-selection forms (`a if c else b`, `a or b`,
        # `a and b`) union their operands (the result MAY be either).
        if node.type == cs.TS_PY_IDENTIFIER and node.text is not None:
            return tainted.get(node.text.decode(cs.ENCODING_UTF8))
        if node.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
            inner = next(iter(node.named_children), None)
            return self._py_value_taint(inner, tainted, ctx) if inner else None
        if node.type == cs.TS_PY_CONDITIONAL_EXPRESSION:
            # Named children are [consequence, condition, alternative]; the
            # condition never becomes the value.
            values = node.named_children
            if len(values) != 3:
                return None
            return _merge_optional_taints(
                self._py_value_taint(values[0], tainted, ctx),
                self._py_value_taint(values[2], tainted, ctx),
            )
        if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
            return _merge_optional_taints(
                self._py_value_taint_field(node, cs.TS_FIELD_LEFT, tainted, ctx),
                self._py_value_taint_field(node, cs.TS_FIELD_RIGHT, tainted, ctx),
            )
        if node.type == cs.TS_PY_CALL and (raw := call_name(node)) is not None:
            if seed := self._source_binding(node, raw, ctx.import_map, ctx.read_sinks):
                return Taint(frozenset({seed}), frozenset())
            callee = self._resolve(
                raw,
                ctx.module_qn,
                ctx.class_context,
                ctx.caller_qn,
                ctx.language,
                ctx.local_var_types,
            )
            if callee is not None:
                # Defer: mark the value pending on the callee's return and
                # record a candidate return edge. finalize() decides
                # whether the callee really returns taint, so a callee
                # processed later still counts.
                self._return_edge_candidates.append(
                    (callee[0], callee[1], ctx.caller_spec)
                )
                return Taint(frozenset(), frozenset({callee[1]}))
        return None

    def _py_value_taint_field(
        self, node: Node, field: str, tainted: _TaintMap, ctx: _FlowCtx
    ) -> Taint | None:
        child = node.child_by_field_name(field)
        return self._py_value_taint(child, tainted, ctx) if child is not None else None

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
                # Resolved origins emit resource flows now; pending callees defer
                # to finalize, when the (possibly forward) callee's origins resolve.
                for source in taint.origins:
                    self._emit_resource_flow(source, sink.kind, dst_identity)
                if taint.pending:
                    self._deferred_resource_flows.append(
                        (taint.pending, sink.kind, dst_identity)
                    )
            return
        callee = self._resolve(
            raw,
            ctx.module_qn,
            ctx.class_context,
            ctx.caller_qn,
            ctx.language,
            ctx.local_var_types,
        )
        if callee is None:
            return
        callee_type, callee_qn = callee
        for arg_name, via in arg_names:
            taint = tainted.get(arg_name)
            if taint is None:
                continue
            if taint.origins:
                # Definitely tainted arg: emit the caller->callee arg edge now.
                self.ingestor.ensure_relationship_batch(
                    ctx.caller_spec,
                    cs.RelationshipType.FLOWS_TO,
                    (callee_type, cs.KEY_QUALIFIED_NAME, callee_qn),
                    properties={KEY_VIA: via, KEY_KIND: FlowKind.ARG.value},
                )
            elif taint.pending:
                # Tainted only via a pending callee: defer, so no arg edge is
                # emitted if that callee turns out to return nothing tainted.
                self._deferred_arg_edges.append(
                    (taint.pending, ctx.caller_spec, callee_type, callee_qn, via)
                )

    def _return_taint_source(
        self, node: Node, tainted: _TaintMap, ctx: _FlowCtx
    ) -> Taint | None:
        # The Taint this return yields, or None if it returns nothing tainted.
        # `return a, b` unwraps to several value nodes; their taints union. A
        # directly-returned callee is deferred (pending) exactly like an
        # assigned one, so finalize() resolves it after every summary is in.
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
                    raw,
                    ctx.module_qn,
                    ctx.class_context,
                    ctx.caller_qn,
                    ctx.language,
                    ctx.local_var_types,
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
        # Called once after every function has been walked (issue #712): resolve
        # the per-function return-taint summaries to a fixpoint, then drain the
        # deferred facts. A callee processed AFTER its caller is now known, so a
        # forward/cross-file return edge and the resource flow it carries appear.
        # All emitted edges are MERGE-idempotent, so re-emitting an edge the
        # inline walk already produced (backward case) is harmless.
        if not self._enabled:
            return
        resolved, is_tainted = self._resolve_summaries()
        for callee_type, callee_qn, caller_spec in self._return_edge_candidates:
            if is_tainted.get(callee_qn):
                self._emit_return_edge((callee_type, callee_qn), caller_spec)
        for pending, sink_kind, sink_identity in self._deferred_resource_flows:
            # Union origins across pending callees first so a shared origin is
            # emitted once, not per callee.
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
        # One-shot per run: clear so a reused processor never re-emits.
        self._return_edge_candidates.clear()
        self._deferred_resource_flows.clear()
        self._deferred_arg_edges.clear()

    def _resolve_summaries(
        self,
    ) -> tuple[dict[str, frozenset[HandleBinding]], dict[str, bool]]:
        # Worklist fixpoint over the serialized summaries: a function's resolved
        # origins are its own plus every pending callee's, and it is tainted if
        # it has an origin or any pending callee is tainted. Origins/taintedness
        # only grow, so this converges through recursion. Re-queue only a
        # callee's callers when it changes, keeping the whole pass O(V + E).
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
        local_var_types: dict[str, str] | None = None,
    ) -> tuple[str, str] | None:
        info = self._resolver.resolve_function_call(
            raw_name, module_qn, local_var_types, class_context, caller_qn, language
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
