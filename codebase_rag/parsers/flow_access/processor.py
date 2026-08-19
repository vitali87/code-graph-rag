from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node

if TYPE_CHECKING:
    from ...types_defs import FunctionLocation, FunctionSpanKey

from ... import constants as cs
from ...capture import CaptureSelection
from ...services import IngestorProtocol
from ..call_resolver import CallResolver
from ..dart.utils import dart_body_node, dart_call_name, dart_member_read_name
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
    ArgHandleSink,
    HandleBinding,
    HandleConstructor,
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
    lean_definition_header_nodes,
    literal_target,
    match_normalised,
    positional_arg_node,
    registry_match,
    rust_unwrap_result,
    scope_seed_nodes,
    string_literal,
    unwrap_argument,
)
from ..io_access.registry import (
    IO_ARG_HANDLE_SINKS,
    IO_IDENTITY_UNWRAP_CALLS,
    IO_IDENTITY_UNWRAP_NEW_TYPES,
    IO_LEAN_HANDLE_CONSTRUCTORS,
    IO_LEAN_HANDLE_METHODS,
    IO_NEW_HANDLE_CONSTRUCTORS,
    IO_NEW_HANDLE_WRAPPERS,
    IO_TYPE_HANDLE_CONSTRUCTORS,
    LIBC_STD_STREAMS,
)
from ..utils import (
    c_positional_parameter_slots,
    cpp_declarator_name,
    cpp_positional_parameter_slots,
    csharp_positional_parameter_slots,
    dart_positional_parameter_slots,
    function_span_key,
    go_positional_parameter_slots,
    java_positional_parameter_slots,
    js_ts_positional_parameter_slots,
    php_positional_parameter_slots,
    python_free_variable_names,
    python_parameter_names,
    rust_positional_parameter_slots,
    safe_decode_text,
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
# The Dart `=` assignment token node type (no dedicated constant); the flow walk
# splits a Dart binding on it (issue #1173).
_DART_ASSIGN_OP = "="


# The `kind` half of an argument `via` tag (VIA_ARG_FORMAT / VIA_KW_FORMAT),
# used to map a call-site argument back to the callee's parameter (issue #1142).
_VIA_SEPARATOR = ":"
_VIA_ARG_KIND = VIA_ARG_FORMAT.split(_VIA_SEPARATOR, 1)[0]
_VIA_KW_KIND = VIA_KW_FORMAT.split(_VIA_SEPARATOR, 1)[0]

# A per-call-site pending key for the pass-through taint a call's RESULT carries
# (issue #1168). Folding an argument's taint into this token, rather than into
# the shared callee summary, keeps the composition specific to THIS call so a
# sibling call of the same helper with a clean argument is never contaminated.
# The NUL bytes keep it distinct from any real dotted qualified name.
_PASSTHROUGH_TOKEN_PREFIX = "\x00passthrough\x00"


def _passthrough_result_token(caller_qn: str, call_node: Node) -> str:
    return f"{_PASSTHROUGH_TOKEN_PREFIX}{caller_qn}\x00{call_node.start_byte}"


# Python parameter node types a positional argument can bind to, in order.
_PY_POSITIONAL_PARAM_TYPES = frozenset(
    {
        cs.TS_PY_IDENTIFIER,
        cs.TS_PY_TYPED_PARAMETER,
        cs.TS_PY_DEFAULT_PARAMETER,
        cs.TS_PY_TYPED_DEFAULT_PARAMETER,
    }
)


def _py_positional_param_name(node: Node) -> str | None:
    if node.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(node)
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node is not None and name_node.type == cs.TS_PY_IDENTIFIER:
        return safe_decode_text(name_node)
    if node.type == cs.TS_PY_TYPED_PARAMETER:
        for child in node.children:
            if child.type == cs.TS_PY_IDENTIFIER:
                return safe_decode_text(child)
    return None


def _py_positional_param_names(func_node: Node) -> list[str | None]:
    # Parameter names a positional argument can bind to, in order, STOPPING at
    # the first `*args`/`**kwargs`/bare-`*` boundary: positional arguments past
    # that point are absorbed by the variadic or are keyword-only, so mapping
    # arg:<index> into the full (variadic-dropping) name list would bind to the
    # wrong parameter (issue #1142, Greptile review on PR #1167). A leading
    # self/cls is dropped so positions line up with call-site arguments.
    params_node = func_node.child_by_field_name(cs.FIELD_PARAMETERS)
    if params_node is None:
        return []
    names: list[str | None] = []
    for child in params_node.named_children:
        # A comment or the `/` positional-only marker sits between parameters
        # without consuming a position; skip them and keep collecting.
        if child.type in (cs.TS_COMMENT, cs.TS_PY_POSITIONAL_SEPARATOR):
            continue
        if child.type not in _PY_POSITIONAL_PARAM_TYPES:
            # `*`, `*args`, `**kwargs` (or anything unexpected): everything after
            # is variadic-absorbed or keyword-only, so positional mapping stops.
            break
        name = _py_positional_param_name(child)
        if name is None:
            break
        names.append(name)
    if names and names[0] in (cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS):
        names = names[1:]
    return names


def _lean_parameter_slots(
    func_node: Node, language: str
) -> tuple[list[str | None], int | None]:
    # Position-aligned parameter slots for a lean-walk function, used to seed
    # parameter-taint summaries (issue #1169). Returns one entry per formal
    # positional slot (None where the slot binds no simple name) plus the index
    # of a variadic/rest slot, so arg:<index> maps to the right parameter even
    # when some parameters are unnamed, destructured, or variadic -- the shift
    # a compacting extractor would cause is what produces false source-to-sink
    # edges. Lean languages have no keyword-call syntax, so this table is the
    # only mapping needed (mirrors the Python _py_positional_param_names role).
    if language == cs.SupportedLanguage.GO:
        return go_positional_parameter_slots(func_node)
    if language in cs.JS_TS_LANGUAGES:
        return js_ts_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.CPP:
        return cpp_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.JAVA:
        return java_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.CSHARP:
        return csharp_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.RUST:
        return rust_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.C:
        return c_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.PHP:
        return php_positional_parameter_slots(func_node)
    if language == cs.SupportedLanguage.DART:
        return dart_positional_parameter_slots(func_node)
    return [], None


class Taint(NamedTuple):
    # What is tainting a variable: resolved resource origins plus the set of
    # callee QNs whose (possibly not-yet-processed) return value it carries. A
    # variable is present in the taint map iff it is tainted; a purely-pending
    # taint is only really tainted once finalize resolves a pending callee to a
    # tainted return -- how forward/cross-file return-taint is recovered (712).
    origins: frozenset[HandleBinding]
    pending: frozenset[str]
    # Names of the enclosing function's own parameters this value carries, so a
    # tainted argument passed at a call site connects to a sink reached inside
    # the callee body (forward parameter taint, issue #1142). Seeded only in the
    # Python walk today; empty everywhere else, leaving other walks unchanged.
    params: frozenset[str] = frozenset()


_EMPTY_TAINT = Taint(frozenset(), frozenset())


def _merge_taint(a: Taint, b: Taint) -> Taint:
    return Taint(a.origins | b.origins, a.pending | b.pending, a.params | b.params)


# Live taint state threaded through the walk: variable -> its Taint.
type _TaintMap = dict[str, Taint]
# Live handle bindings threaded through the lean walk: variable -> the resource
# handles it may hold. Set-valued so a name bound to different handles on
# different branches writes to ALL of them at a later method call (issue #1204).
type _HandleMap = dict[str, frozenset[HandleBinding]]


class _LeanState(NamedTuple):
    # Path-sensitive state threaded through the lean (non-Python) walk: the taint
    # map plus the resource-handle map, copied at branch entry and MAY-unioned at
    # the join exactly as the taint map alone was (issue #1204). Bundling the two
    # keeps every `state = self._walk_x(...)` call site unchanged.
    taint: _TaintMap
    handles: _HandleMap

    def copy(self) -> _LeanState:
        # frozensets are immutable, so a shallow copy of each dict is a full copy.
        return _LeanState(dict(self.taint), dict(self.handles))


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
        cs.TS_PHP_FOREACH_STATEMENT,
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
        cs.TS_PHP_DEFAULT_STATEMENT,
        cs.TS_DART_SWITCH_STATEMENT_CASE,
        cs.TS_DART_SWITCH_STATEMENT_DEFAULT,
    }
)
# Arms a previous case can fall INTO (no break modeling: the entry unions
# the previous arm's exit). Go cases and Java arrow rules never fall
# through, so they enter only from the header state. PHP `case`/`default`
# and Dart `switch_statement_case`/`_default` fall through C-style.
_FALLTHROUGH_ARM_TYPES = frozenset(
    {
        cs.TS_JAVA_SWITCH_BLOCK_STATEMENT_GROUP,
        cs.TS_CPP_CASE_STATEMENT,
        cs.TS_JS_SWITCH_CASE,
        cs.TS_JS_SWITCH_DEFAULT,
        cs.TS_PHP_DEFAULT_STATEMENT,
        cs.TS_DART_SWITCH_STATEMENT_CASE,
        cs.TS_DART_SWITCH_STATEMENT_DEFAULT,
    }
)

# Arm node types spelled `default` explicitly (Go default_case, JS
# switch_default, PHP default_statement, Dart switch_statement_default); the
# other grammars mark default structurally.
_EXPLICIT_DEFAULT_ARM_TYPES = frozenset(
    {
        cs.TS_GO_DEFAULT_CASE,
        cs.TS_JS_SWITCH_DEFAULT,
        cs.TS_PHP_DEFAULT_STATEMENT,
        cs.TS_DART_SWITCH_STATEMENT_DEFAULT,
    }
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
    # Handle-based writes (issue #1204): a local bound to a resource handle
    # (`f := os.Create("out")`) so a tainted write through it (`f.Write(secret)`)
    # emits a flow edge to the handle's resource. The live bindings ride the
    # path-sensitive _LeanState (branch-merged like taint); these are the constant
    # per-language registry views. Empty tables (no handle support) => inert.
    handle_ctors: dict[str, HandleConstructor]
    handle_methods: dict[ResourceKind, dict[str, IODirection]]
    # `new`-shaped handle constructors (Java `new FileWriter("x")`, C#
    # `new StreamWriter("x")`) plus the wrapper types that delegate their resource
    # to arg0 (`new BufferedWriter(new FileWriter(..))`) and the identity-carrier
    # types/calls reached at a target position (`new PrintWriter(new File("x"))`,
    # `Files.newBufferedWriter(Path.of("cfg"))`). Empty for languages whose handles
    # are only call-shaped (issue #1204).
    new_handle_ctors: dict[str, HandleConstructor]
    new_wrappers: frozenset[str]
    identity_calls: frozenset[str]
    identity_new_types: dict[str, ResourceKind]
    # Arg-shaped handle sinks (C/C++ libc `fwrite(data, 1, n, f)`,
    # `fprintf(f, fmt, x)`): the handle rides an ARGUMENT, not a receiver, so a
    # tainted non-handle arg flows to the handle's resource (issue #1204). Empty for
    # languages without the libc FILE* API.
    arg_handle_sinks: dict[str, ArgHandleSink]
    # Type-declaration handle constructors (C++ `std::ofstream out("x")`): a
    # declaration whose written type is one of these binds a FILE handle on its
    # declarator, so a later `out << x` / `out.write(..)` routes to that file
    # (issue #1220). Empty for languages without type-declaration stream handles.
    type_ctors: dict[str, ResourceKind]


@dataclass
class _PendingCapture:
    """A nested def whose captured-cell composition is deferred until the walk
    learns HOW the closure is used (issue #1211)."""

    nested_qn: str
    def_node: Node
    caller_qn: str
    free_vars: tuple[str, ...]
    snapshot: dict[str, Taint]
    called: bool = False
    escaped: bool = False


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
        function_locations: dict[FunctionSpanKey, FunctionLocation] | None = None,
    ) -> None:
        self.ingestor = ingestor
        self._import_processor = import_processor
        self._resolver = resolver
        self._selection = selection
        # Span-key -> registered FunctionLocation, so a nested def's capture record
        # uses the SAME (suffix-aware) qn the definition pass assigned, matching the
        # qn its own walk is keyed under; without it a duplicate-named nested def
        # would record under a reconstructed unsuffixed qn that never composes.
        self._function_locations = function_locations or {}
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
        self._break_exit_stack: list[list[_LeanState] | None] = []
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
        # Forward parameter-taint (issue #1142). A function's parameter is seeded
        # as a pseudo-origin during the walk; where it reaches a write sink or is
        # handed to another callee we record it here and compose at finalize, so
        # `secret = getenv(); log_it(secret)` with `log_it(m): logger.info(m)`
        # connects ENV -> STDOUT even though the source and the sink live in
        # different bodies. Only resolved callees participate.
        # (function_qn, parameter_name) -> the write sinks that parameter reaches.
        self._param_sinks: dict[tuple[str, str], set[tuple[ResourceKind, str]]] = (
            defaultdict(set)
        )
        # Transitive hand-off: function F's parameter P is passed as argument
        # `via` into resolved callee C, so C's reached sinks flow back to (F, P)
        # while the closure is computed (wrapper-of-a-wrapper).
        self._param_flow_edges: list[tuple[str, str, str, str]] = []
        # Return-direction hand-off (issue #1168): function F's parameter P is
        # passed as `via` into callee C AND F returns C's return value
        # (`return redact(x)`), so if C's matched parameter reaches C's return,
        # P reaches F's return too. The return analog of _param_flow_edges.
        self._return_param_edges: list[tuple[str, str, str, str]] = []
        # Concrete call sites: an argument carrying resolved origins or pending
        # callee returns enters resolved callee C as argument `via`; the fourth
        # element is the per-call-site pass-through token (issue #1168). Composed
        # against the parameter-sink closure in finalize to emit origin -> sink.
        self._param_call_sites: list[tuple[Taint, str, str, str]] = []
        self._pending_captures: dict[str, _PendingCapture] = {}
        # Per-function positional parameter slots so a call site's arg:<index>
        # resolves to the right callee parameter. The Python path truncates at
        # the first variadic/keyword-only boundary (self/cls dropped) and has no
        # None entries; the lean path (issue #1169) keeps one entry per formal
        # slot, using None where a slot binds no simple name, so unnamed or
        # destructured parameters do not shift later indices.
        self._positional_params: dict[str, list[str | None]] = {}
        # Index of a lean callee's variadic/rest slot, if any: every argument at
        # or after it maps to that parameter (issue #1169). The Python path
        # never records this because it truncates positional names at the first
        # variadic boundary.
        self._variadic_params: dict[str, int] = {}

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
            # A READ_WRITE sink (PHP `curl_exec`/`fsockopen`, verb-agnostic like a DB
            # execute) is BOTH a read source and a write sink, so it enters both maps
            # -- otherwise it would fall out of both and emit nothing (CodeRabbit, #1174).
            read_sinks={
                s.callee: s
                for s in sinks
                if s.direction in (IODirection.READ, IODirection.READ_WRITE)
            },
            write_sinks={
                s.callee: s
                for s in sinks
                if s.direction in (IODirection.WRITE, IODirection.READ_WRITE)
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
        # Seed each parameter as a pseudo-origin so a sink or callee hand-off it
        # reaches becomes a parameter-taint summary composed at finalize (#1142).
        # Every parameter (including keyword-only) is seeded so it can be matched
        # by keyword; positional matching uses the truncated list recorded below.
        for pname in python_parameter_names(caller_node):
            tainted[pname] = Taint(frozenset(), frozenset(), frozenset({pname}))
        positional = _py_positional_param_names(caller_node)
        if positional:
            self._positional_params[caller_qn] = positional
        # Seed each free variable as a capture pseudo-origin so a sink it reaches
        # becomes a capture summary composed at finalize against the enclosing
        # definition site (issue #1197). A free var matches only kw:<name>, so it
        # records no positional slot; a free var that is never a tainted capture
        # records a summary no call site composes, so it stays inert.
        for fv in python_free_variable_names(caller_node):
            if fv not in tainted:
                tainted[fv] = Taint(frozenset(), frozenset(), frozenset({fv}))
        self._pending_captures.clear()
        for node in scope_seed_nodes(caller_node):
            tainted = self._walk_stmt(node, tainted, ctx)
        self._flush_pending_captures()

        if self._acc_returns_taint:
            self._summaries[caller_qn] = self._acc_return_taint

    # Lean non-Python path-sensitive flow (issue #714) below.
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
            handle_ctors={
                c.callee: c for c in IO_LEAN_HANDLE_CONSTRUCTORS.get(ctx.language, ())
            },
            handle_methods=IO_LEAN_HANDLE_METHODS.get(ctx.language, {}),
            new_handle_ctors=IO_NEW_HANDLE_CONSTRUCTORS.get(ctx.language, {}),
            new_wrappers=IO_NEW_HANDLE_WRAPPERS.get(ctx.language, frozenset()),
            identity_calls=IO_IDENTITY_UNWRAP_CALLS.get(ctx.language, frozenset()),
            identity_new_types=IO_IDENTITY_UNWRAP_NEW_TYPES.get(ctx.language, {}),
            arg_handle_sinks=IO_ARG_HANDLE_SINKS.get(ctx.language, {}),
            type_ctors=IO_TYPE_HANDLE_CONSTRUCTORS.get(ctx.language, {}),
        )
        if ctx.language == cs.SupportedLanguage.DART:
            # Dart's body is a SIBLING `function_body` of the captured signature,
            # not a `body` field (issue #1173).
            statements = self._dart_body_statements(caller_node)
        else:
            body = caller_node.child_by_field_name(cs.FIELD_BODY)
            if body is None:
                statements = list(caller_node.named_children)
            elif body.type == descriptor.block_scope_type:
                statements = list(body.named_children)
            else:
                statements = [body]
        state = _LeanState(taint={}, handles={})
        # Seed each parameter as a pseudo-origin so a sink or callee hand-off it
        # reaches becomes a parameter-taint summary composed at finalize, exactly
        # as the Python walk does (issue #1169 extends #1142/#1168 to the lean
        # walk). The composition machinery in finalize is language-agnostic; only
        # languages with a parameter-name extractor (Go/JS/TS/C++/Java/C#/Rust/C)
        # get names, so the rest are seeded with nothing and are unaffected.
        lean_names, lean_variadic = _lean_parameter_slots(caller_node, ctx.language)
        for pname in lean_names:
            if pname is not None:
                state.taint[pname] = Taint(frozenset(), frozenset(), frozenset({pname}))
        if lean_names:
            self._positional_params[ctx.caller_qn] = lean_names
            if lean_variadic is not None:
                self._variadic_params[ctx.caller_qn] = lean_variadic
        if ctx.language in _HOISTED_DECL_LANGS:
            # Path-sensitive MAY walk (issue #714 follow-up): each JS/TS if/else,
            # loop, and try branch is evaluated against a COPY of the incoming
            # state and unioned at the merge, so taint surviving on ANY path
            # survives and a kill counts only when it happens on EVERY path.
            for node in statements:
                state = self._walk_js_stmt(node, state, jc)
        else:
            # Flat-language path-sensitive MAY walk (issue #714 follow-up):
            # if/loop/try/switch/match nodes branch-and-merge like the JS walk
            # (zero-or-more loops union the skip path and re-walk once for
            # loop-carried taint -- do-while/Rust `loop` bodies always run, so
            # their pre-state stays out of the merge -- and try seeds each
            # handler with union(pre, body_exit)), and
            # the live shadow set is snapshotted per branch and restored at the
            # merge, so a block-scoped Go/Java declaration inside a branch does
            # not leak its shadow past the join.
            for node in statements:
                state = self._walk_flat_stmt(node, state, jc)
        if self._acc_returns_taint:
            self._summaries[ctx.caller_qn] = self._acc_return_taint

    def _walk_flat_stmt(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # Structured walk for the non-hoisted flat languages (Go, Java, Rust,
        # C++): if/loop/try/match nodes branch-and-merge (issue #714 follow-up);
        # every other node applies its leaf effect and threads state through its
        # children in source order (so a nested call in an argument is seen).
        node_type = node.type
        if node_type in jc.descriptor.nested_scope_types:
            # Definition-time header expressions (TS parameter decorators) run in
            # THIS scope; walk them, then leave the body to its own caller pass.
            for header in lean_definition_header_nodes(node, jc.descriptor):
                state = self._walk_flat_stmt(header, state, jc)
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
        self._apply_js_leaf(node, state.taint, state.handles, jc)
        for child in node.named_children:
            state = self._walk_flat_stmt(child, state, jc)
        return state

    def _walk_flat_loop(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # Loop shield: a break inside the body targets THIS loop, never an
        # enclosing switch arm's collector.
        self._shield_breaks()
        try:
            return self._walk_flat_loop_inner(node, state, jc)
        finally:
            self._unshield_breaks()

    def _walk_flat_loop_inner(
        self, node: Node, state: _LeanState, jc: _JsCtx
    ) -> _LeanState:
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
            self._apply_js_leaf(node, state.taint, state.handles, jc)
        body = node.child_by_field_name(cs.FIELD_BODY)
        state, update = self._walk_loop_header(node, state, jc, body)
        if body is None:
            self._restore_shadows(pre_loop_shadows, jc)
            return state
        header_shadows = set(jc.local_names)
        once = self._walk_flat_stmt(body, state.copy(), jc)
        if update is not None:
            once = self._walk_flat_stmt(update, once, jc)
        self._restore_shadows(header_shadows, jc)
        merged = self._merge_lean([state, once])
        twice = self._walk_flat_stmt(body, merged.copy(), jc)
        if update is not None:
            twice = self._walk_flat_stmt(update, twice, jc)
        self._restore_shadows(pre_loop_shadows, jc)
        return self._merge_lean([state, twice])

    def _walk_loop_header(
        self, node: Node, state: _LeanState, jc: _JsCtx, body: Node | None
    ) -> tuple[_LeanState, Node | None]:
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
        self, clause: Node, update: Node | None, state: _LeanState, jc: _JsCtx
    ) -> _LeanState:
        # Go's init;cond;post for_clause: walk everything but the post
        # (update) statement, which the loop walk defers past the body.
        for part in clause.named_children:
            if update is None or part.id != update.id:
                state = self._walk_flat_stmt(part, state, jc)
        return state

    def _walk_flat_mandatory_loop(
        self, node: Node, state: _LeanState, jc: _JsCtx
    ) -> _LeanState:
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
        state: _LeanState,
        jc: _JsCtx,
        walk: Callable[[Node, _LeanState, _JsCtx], _LeanState],
    ) -> _LeanState:
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
        state: _LeanState,
        jc: _JsCtx,
        walk: Callable[[Node, _LeanState, _JsCtx], _LeanState],
    ) -> _LeanState:
        body = node.child_by_field_name(cs.FIELD_BODY)
        condition = node.child_by_field_name(cs.TS_FIELD_CONDITION)
        pre_shadows = set(jc.local_names)
        once = state.copy()
        if body is not None:
            once = walk(body, once, jc)
        if condition is not None:
            once = walk(condition, once, jc)
        self._restore_shadows(pre_shadows, jc)
        twice = once.copy()
        if body is not None:
            twice = walk(body, twice, jc)
        if condition is not None:
            twice = walk(condition, twice, jc)
        self._restore_shadows(pre_shadows, jc)
        return self._merge_lean([once, twice])

    def _walk_flat_try(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
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
            self._walk_flat_stmt(body, state.copy(), jc)
            if body is not None
            else state.copy()
        )
        self._restore_shadows(pre_shadows, jc)
        branch_exits: list[_LeanState] = [body_exit]
        finally_clause: Node | None = None
        for child in node.named_children:
            if child.type == cs.TS_JS_CATCH_CLAUSE:
                branch_exits.append(
                    self._walk_flat_stmt(
                        child, self._merge_lean([state, body_exit]), jc
                    )
                )
                self._restore_shadows(pre_shadows, jc)
            elif child.type == cs.TS_JS_FINALLY_CLAUSE:
                finally_clause = child
        merged = self._merge_lean(branch_exits)
        if finally_clause is not None:
            merged = self._walk_flat_stmt(finally_clause, merged, jc)
            self._restore_shadows(pre_shadows, jc)
        return merged

    def _walk_switch(
        self,
        node: Node,
        state: _LeanState,
        jc: _JsCtx,
        walk: Callable[[Node, _LeanState, _JsCtx], _LeanState],
    ) -> _LeanState:
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
            exits.append(state.copy())
        self._restore_shadows(pre_switch_shadows, jc)
        return self._merge_lean(exits)

    def _walk_switch_arms(
        self,
        arms: list[Node],
        state: _LeanState,
        jc: _JsCtx,
        walk: Callable[[Node, _LeanState, _JsCtx], _LeanState],
    ) -> tuple[list[_LeanState], bool]:
        pre_arm_shadows = set(jc.local_names)
        exits: list[_LeanState] = []
        fall_in: _LeanState | None = None
        has_default = False
        for index, arm in enumerate(arms):
            has_default = has_default or _switch_arm_is_default(arm)
            entry = state.copy()
            # fall_in is non-None exactly when the PREVIOUS arm can fall
            # into this one (C-family arm without a trailing break, or a Go
            # arm ending in the explicit `fallthrough` keyword).
            if fall_in is not None:
                entry = self._merge_lean([entry, fall_in])
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

    def _walk_flat_match(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
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
        arm_exits: list[_LeanState] = []
        for arm in body.named_children:
            if arm.type != cs.TS_RS_MATCH_ARM:
                continue
            arm_exits.append(self._walk_flat_stmt(arm, state.copy(), jc))
            self._restore_shadows(pre_shadows, jc)
        return self._merge_lean(arm_exits) if arm_exits else state

    def _walk_flat_if(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # The header (any initializer + condition) runs on all paths; each of the
        # then / else(-if) / implicit skip paths is walked against a copy and
        # unioned (MAY join). Two shadow scopes are restored: a branch grows the
        # live shadow set with its own block-scoped declarations (restored between
        # branches), and a Go `if` initializer (`if x := f(); cond {}`) is scoped
        # to the whole if statement (restored on exit), so neither shadows a
        # source/sink past its scope.
        pre_if_shadows = set(jc.local_names)
        consequence = node.child_by_field_name(jc.descriptor.if_consequence_field)
        # Most grammars carry a single `alternative` (else block or nested else-if);
        # PHP emits the whole chain as several sibling `alternative` children
        # (`else_if_clause`* then an optional `else_clause`), so gather them all
        # (issue #1174). For Go/Java/C++/Rust this list is 0 or 1 element -> unchanged.
        alternatives = node.children_by_field_name(cs.FIELD_ALTERNATIVE)
        skip = {n.id for n in (consequence, *alternatives) if n is not None}
        for child in node.named_children:
            if child.id not in skip:
                state = self._walk_flat_stmt(child, state, jc)
        pre_shadows = set(jc.local_names)
        branch_exits: list[_LeanState] = []
        if consequence is not None:
            branch_exits.append(self._walk_flat_stmt(consequence, state.copy(), jc))
            self._restore_shadows(pre_shadows, jc)
        for alternative in alternatives:
            # A block/nested-if (Go/Java/…) or a PHP else-if/else clause; the
            # recursion walks each in its own branch copy and merges (a MAY
            # over-approximation for the mutually-exclusive elseif arms).
            branch_exits.append(self._walk_flat_stmt(alternative, state.copy(), jc))
            self._restore_shadows(pre_shadows, jc)
        # An implicit skip path (condition false, nothing runs) survives unless the
        # chain ends in a TERMINAL else: one plain alternative (else block / nested
        # if) is terminal; a PHP chain ending in `else_if_clause` is not.
        if not alternatives or alternatives[-1].type == cs.TS_PHP_ELSE_IF_CLAUSE:
            branch_exits.append(state.copy())
        self._restore_shadows(pre_if_shadows, jc)
        return self._merge_lean(branch_exits)

    def _record_break_exit(self, state: _LeanState) -> None:
        # Snapshot the live state at a `break` for the enclosing switch
        # arm's collector. None on top = a loop shield (the break targets
        # that loop); empty stack = no enclosing switch at all. A labeled
        # break targeting a farther construct still records here: the
        # extra state only widens the MAY join, the sound direction.
        if self._break_exit_stack and self._break_exit_stack[-1] is not None:
            self._break_exit_stack[-1].append(state.copy())

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

    def _walk_js_stmt(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # Path-sensitive walk for JS/TS: control-flow nodes branch-and-merge, every
        # other node applies its leaf effect and threads state through its children
        # in source order (so a nested call in an argument is still seen).
        node_type = node.type
        if node_type in jc.descriptor.nested_scope_types:
            # Definition-time header expressions (TS parameter decorators) run in
            # THIS scope; walk them, then leave the body to its own caller pass.
            for header in lean_definition_header_nodes(node, jc.descriptor):
                state = self._walk_js_stmt(header, state, jc)
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
        self._apply_js_leaf(node, state.taint, state.handles, jc)
        for child in node.named_children:
            state = self._walk_js_stmt(child, state, jc)
        return state

    def _walk_js_if(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # The condition runs on all paths; each of the then / else(-if) / implicit
        # skip paths is walked against a copy and unioned (MAY join).
        cond = node.child_by_field_name(cs.TS_FIELD_CONDITION)
        if cond is not None:
            state = self._walk_js_stmt(cond, state, jc)
        branch_exits: list[_LeanState] = []
        consequence = node.child_by_field_name(cs.TS_FIELD_CONSEQUENCE)
        if consequence is not None:
            branch_exits.append(self._walk_js_stmt(consequence, state.copy(), jc))
        alternative = node.child_by_field_name(cs.FIELD_ALTERNATIVE)
        if alternative is not None:
            # else_clause holds either a block or a nested if (else-if chain); the
            # recursion handles both and merges within.
            branch_exits.append(self._walk_js_stmt(alternative, state.copy(), jc))
        else:
            # No else: the skip path preserves the incoming state.
            branch_exits.append(state.copy())
        return self._merge_lean(branch_exits)

    def _walk_js_loop(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # Loop shield: a break inside the body targets THIS loop, never an
        # enclosing switch arm's collector.
        self._shield_breaks()
        try:
            return self._walk_js_loop_inner(node, state, jc)
        finally:
            self._unshield_breaks()

    def _walk_js_loop_inner(
        self, node: Node, state: _LeanState, jc: _JsCtx
    ) -> _LeanState:
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
            once = self._walk_js_stmt(body, state.copy(), jc)
            if increment is not None:
                once = self._walk_js_stmt(increment, once, jc)
            merged = self._merge_lean([state, once])
            twice = self._walk_js_stmt(body, merged.copy(), jc)
            if increment is not None:
                twice = self._walk_js_stmt(increment, twice, jc)
            state = self._merge_lean([state, twice])
        return state

    def _walk_js_try(self, node: Node, state: _LeanState, jc: _JsCtx) -> _LeanState:
        # The try body may run fully (no-throw path) or partially before a catch;
        # seed the handler with union(pre, body_exit) so taint introduced before a
        # throw still reaches it. A finally runs on the merged state of both.
        body = node.child_by_field_name(cs.FIELD_BODY)
        body_exit = (
            self._walk_js_stmt(body, state.copy(), jc)
            if body is not None
            else state.copy()
        )
        branch_exits: list[_LeanState] = [body_exit]
        handler = node.child_by_field_name(cs.FIELD_HANDLER)
        if handler is not None:
            branch_exits.append(
                self._walk_js_stmt(handler, self._merge_lean([state, body_exit]), jc)
            )
        merged = self._merge_lean(branch_exits)
        finalizer = node.child_by_field_name(cs.FIELD_FINALIZER)
        if finalizer is not None:
            merged = self._walk_js_stmt(finalizer, merged, jc)
        return merged

    def _apply_js_leaf(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        # The leaf effect of one node on the taint map: bind (JS/Go), Go range
        # kill, a call's sink/arg edges, or a return's contribution to the summary.
        if jc.flow.language == cs.SupportedLanguage.DART:
            # Dart's sibling-chain grammar takes a dedicated leaf path (issue #1173).
            self._apply_dart_leaf(node, tainted, handles, jc)
            return
        node_type = node.type
        d = jc.descriptor
        if jc.type_ctors and node_type == cs.TS_CPP_DECLARATION:
            # C++ `std::ofstream out("x")`: bind the declarator as a FILE handle. The
            # walk still recurses into the child init_declarator, which the guard
            # below skips so it is not re-bound to no handle (issue #1220).
            self._bind_lean_type_decl(node, handles, jc)
            return
        if node_type == d.declarator_type or node_type in (
            cs.TS_ASSIGNMENT_EXPRESSION,
            cs.TS_GO_ASSIGNMENT_STATEMENT,
        ):
            if jc.type_ctors and self._decl_type_is_handle(node.parent, jc):
                return
            self._lean_bind(node, tainted, handles, jc)
        elif node_type in d.extra_declarator_types:
            if node_type == cs.TS_GO_RANGE_CLAUSE:
                self._lean_kill(node, tainted, handles, jc)
            else:
                self._lean_bind(node, tainted, handles, jc)
        elif node_type == d.call_type:
            self._js_call(node, tainted, handles, jc)
        elif d.macro_type is not None and node_type == d.macro_type:
            self._flow_macro(node, tainted, jc)
        elif d.stream_sink_type is not None and node_type == d.stream_sink_type:
            self._flow_stream(node, tainted, handles, jc)
        elif d.keyword_stdout_write_types and node_type in d.keyword_stdout_write_types:
            self._flow_keyword_write(node, tainted, jc)
        elif node_type == cs.TS_RETURN_STATEMENT:
            returned = self._js_return_taint(node, tainted, jc)
            if returned is not None:
                self._acc_returns_taint = True
                self._acc_return_taint = _merge_taint(self._acc_return_taint, returned)

    def _lean_bind(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
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
        computed: list[tuple[str, Taint | None, Node | None]] = []
        for index, name in enumerate(targets):
            if name is None:
                continue
            rhs = (
                values[0]
                if spread
                else (values[index] if index < len(values) else None)
            )
            computed.append((name, self._js_expr_taint(rhs, tainted, jc), rhs))
        for name, taint, rhs in computed:
            if taint is not None:
                tainted[name] = taint
            else:
                tainted.pop(name, None)
            # Track (or kill) the resource handle bound to this name, so a later
            # tainted write through it emits a flow edge (issue #1204). A binding is
            # a STRONG update (replaces the set), so straight-line reassignment
            # redirects the handle; only a branch join widens a name to multiple.
            if jc.handle_ctors or jc.new_handle_ctors:
                bindings = self._lean_handle_binding(rhs, handles, jc)
                if bindings:
                    handles[name] = bindings
                else:
                    handles.pop(name, None)
        # Register the bound names AFTER reading the RHS (which still saw the
        # pre-declaration scope): a Go shadow applies only from here forward.
        self._register_shadows(targets, jc)

    def _lean_kill(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        left = node.child_by_field_name(cs.FIELD_LEFT)
        if left is None:
            return
        names = lean_binding_targets(left, jc.descriptor)
        for name in names:
            if name is not None:
                tainted.pop(name, None)
                handles.pop(name, None)
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
            # A method chain: recurse the receiver ONLY through a taint-transparent
            # method -- Rust Result unwrapping (`std::env::var("X").unwrap()`) or a
            # value-preserving conversion (`s.as_bytes()`). A terminal method that
            # returns an unrelated value (`s.as_bytes().len()`, `.count()`) must not
            # propagate the receiver's taint (issue #1204). Languages with no such
            # methods (empty set) never recurse a chain here.
            func = node.child_by_field_name(cs.TS_FIELD_FUNCTION)
            if (
                func is not None
                and func.type == d.member_expression_type
                and d.taint_transparent_methods
            ):
                method = func.child_by_field_name(d.property_field)
                receiver = func.child_by_field_name(d.object_field)
                if (
                    receiver is not None
                    and method is not None
                    and method.text is not None
                    and method.text.decode(cs.ENCODING_UTF8)
                    in d.taint_transparent_methods
                ):
                    return self._js_expr_taint(receiver, tainted, jc)
        return None

    def _js_call(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
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
                # A parameter reaching this sink is a parameter-to-sink summary
                # (issue #1169), composed at finalize against every call site
                # passing a tainted argument into this parameter.
                for pname in taint.params:
                    self._param_sinks[(jc.flow.caller_qn, pname)].add(
                        (sink.kind, dst_identity)
                    )
            return
        # A tainted write through a bound resource handle (issue #1204).
        if handles and self._emit_handle_write(raw, args, handles, jc):
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
            # An UNRESOLVED call may still be a libc arg-shaped handle write
            # (`fwrite(x, 1, n, f)`, `fprintf(f, fmt, x)`). A call that DOES resolve to
            # a project function of the same name is analysed as that function above
            # -- not assumed to be libc (Greptile review, #1204).
            if jc.arg_handle_sinks:
                self._emit_arg_handle_write(raw, node, args, handles, jc)
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
            # Forward parameter-taint (issue #1169), orthogonal to the arg edge:
            # a concrete argument records a call site to compose against the
            # callee's parameter-sink closure; an argument that IS one of this
            # function's parameters records a transitive hand-off so a wrapper of
            # a wrapper still resolves. The token ties a pass-through composition
            # to this exact call so it is not shared across calls (issue #1168).
            if taint.origins or taint.pending:
                token = _passthrough_result_token(jc.flow.caller_qn, node)
                self._param_call_sites.append((taint, callee_qn, via, token))
            for pname in taint.params:
                self._param_flow_edges.append(
                    (jc.flow.caller_qn, pname, callee_qn, via)
                )

    def _lean_handle_binding(
        self, rhs: Node | None, handles: _HandleMap, jc: _JsCtx
    ) -> frozenset[HandleBinding]:
        # The resource handle(s) an RHS expression binds (issue #1204), threaded
        # set-valued so a wrapper around a branch-merged variable inherits every
        # handle it may hold. A Rust ctor arrives Result-wrapped (`File::create(p)?`,
        # `.unwrap()`); peel those (inert elsewhere). Resolves call-shaped ctors
        # (`os.Create`, `Files.newBufferedWriter`), `new`-shaped ctors with their
        # wrappers/identity-carriers (Java/C#), and a plain handle alias (`g = f`).
        if rhs is None:
            return frozenset()
        node = rust_unwrap_result(rhs)
        d = jc.descriptor
        if node.type == d.call_type:
            return self._lean_handle_from_call(node, jc)
        if d.new_expression_type is not None and node.type == d.new_expression_type:
            return self._lean_handle_from_new(node, handles, jc)
        if node.type == d.identifier_type and node.text is not None:
            # `g = f` aliases f's handle(s).
            return handles.get(node.text.decode(cs.ENCODING_UTF8), frozenset())
        return frozenset()

    def _lean_handle_from_call(
        self, node: Node, jc: _JsCtx
    ) -> frozenset[HandleBinding]:
        # A call-shaped ctor (`os.Create("out")`), shadow-aware and import-
        # normalised exactly like sink matching. A READ-only handle (`os.Open`) is
        # never a write sink, so it does not bind and a later `f.Write` emits nothing.
        raw = call_name(node)
        if raw is None:
            return frozenset()
        ctor = self._js_match_sink(raw, jc.handle_ctors, jc)
        if ctor is None or ctor.direction == IODirection.READ:
            return frozenset()
        return frozenset(
            {HandleBinding(ctor.kind, self._lean_ctor_identity(node, ctor, jc))}
        )

    def _lean_handle_from_new(
        self, node: Node, handles: _HandleMap, jc: _JsCtx
    ) -> frozenset[HandleBinding]:
        # A `new`-shaped handle (Java `new FileWriter("x")`, C# `new StreamWriter`).
        # A wrapper type delegates to arg0 (a nested ctor or a bound handle var);
        # a non-ctor identity-carrier (`new File("x")`) designates the resource under
        # a wrapper; a `handle_arg` ctor (`new SqlCommand(sql, conn)`) inherits the
        # bound handle at that argument. A READ-only handle never binds as a sink.
        d = jc.descriptor
        type_node = node.child_by_field_name(cs.TS_FIELD_TYPE)
        if type_node is None or type_node.text is None:
            return frozenset()
        type_name = type_node.text.decode(cs.ENCODING_UTF8)
        if type_name in jc.new_wrappers:
            inner = self._lean_handle_binding(
                self._first_positional_arg(node), handles, jc
            )
            if inner:
                return inner
        ctor = jc.new_handle_ctors.get(type_name)
        if ctor is None:
            kind = jc.identity_new_types.get(type_name)
            if kind is None:
                return frozenset()
            return frozenset({HandleBinding(kind, self._lean_literal_arg0(node, jc))})
        if ctor.direction == IODirection.READ:
            return frozenset()
        if ctor.handle_arg is not None:
            inner = positional_arg_node(node, ctor.handle_arg, d.argument_wrapper_type)
            parent = self._lean_handle_binding(inner, handles, jc)
            # The bound handle at handle_arg may itself hold several resources after a
            # branch merge; inherit ALL of their identities (one edge each), not an
            # order-dependent single pick. <dynamic> when the argument is untracked.
            if not parent:
                return frozenset({HandleBinding(ctor.kind, DYNAMIC_TARGET)})
            return frozenset(HandleBinding(ctor.kind, p.identity) for p in parent)
        return frozenset(
            {HandleBinding(ctor.kind, self._lean_ctor_identity(node, ctor, jc))}
        )

    def _lean_ctor_identity(
        self, node: Node, ctor: HandleConstructor, jc: _JsCtx
    ) -> str:
        # The handle's resource identity: the constructor's literal target, or -- when
        # that is a factory call / `new` identity-carrier one level down
        # (`Files.newBufferedWriter(Path.of("cfg"))`, `new PrintWriter(new File("x"))`)
        # -- the literal it carries.
        d = jc.descriptor
        identity = literal_target(
            node,
            ctor.target_arg,
            ctor.target_kw,
            string_type=d.string_type,
            content_type=d.string_content_type,
            keyword_arg_type=d.keyword_arg_type,
            wrapper_type=d.argument_wrapper_type,
            template_type=d.template_string_type,
            substitution_type=d.template_substitution_type,
        )
        if identity != DYNAMIC_TARGET or ctor.target_arg != 0:
            return identity
        arg = self._first_positional_arg(node)
        if arg is None:
            return identity
        if jc.identity_calls and arg.type == d.call_type:
            raw = call_name(arg)
            # A static import (`import static java.nio.file.Path.of`) spells the
            # factory bare (`of("cfg")`); resolve it through the import map to its
            # qualified form before the identity-call check, so the literal is still
            # recovered (Greptile review, #1204).
            if raw is not None and (
                raw in jc.identity_calls
                or jc.flow.import_map.get(raw) in jc.identity_calls
            ):
                return self._lean_literal_arg0(arg, jc)
        if (
            jc.identity_new_types
            and d.new_expression_type is not None
            and arg.type == d.new_expression_type
        ):
            inner_type = arg.child_by_field_name(cs.TS_FIELD_TYPE)
            if (
                inner_type is not None
                and inner_type.text is not None
                and inner_type.text.decode(cs.ENCODING_UTF8) in jc.identity_new_types
            ):
                return self._lean_literal_arg0(arg, jc)
        return identity

    @staticmethod
    def _first_positional_arg(call_node: Node) -> Node | None:
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return None
        return next((c for c in args.named_children if c.type != cs.TS_COMMENT), None)

    @staticmethod
    def _lean_literal_arg0(node: Node, jc: _JsCtx) -> str:
        d = jc.descriptor
        return literal_target(
            node,
            0,
            None,
            string_type=d.string_type,
            content_type=d.string_content_type,
            keyword_arg_type=d.keyword_arg_type,
            wrapper_type=d.argument_wrapper_type,
            template_type=d.template_string_type,
            substitution_type=d.template_substitution_type,
        )

    def _emit_handle_write(
        self,
        raw: str,
        args: list[tuple[str, Taint | None]],
        handles: _HandleMap,
        jc: _JsCtx,
    ) -> bool:
        # A tainted write through a bound handle (`f.Write(secret)`) is a flow to
        # the handle's resource (issue #1204). Split the callee into receiver +
        # method; for EACH handle the receiver may hold (a name can hold different
        # handles on different branches), if the method WRITES (or is read/write
        # like a DB execute), route each tainted arg to that resource. Pure READ
        # methods are not sinks; untainted args emit nothing.
        recv, sep, method = raw.rpartition(jc.descriptor.handle_method_separator)
        if not sep:
            return False
        bindings = handles.get(recv)
        if not bindings:
            return False
        emitted = False
        for binding in bindings:
            direction = jc.handle_methods.get(binding.kind, {}).get(method)
            if direction is None or direction == IODirection.READ:
                continue
            emitted = True
            for _via, taint in args:
                if taint is not None:
                    self._emit_taint_to_sink(
                        taint, binding.kind, binding.identity, jc.flow.caller_qn
                    )
        return emitted

    def _emit_arg_handle_write(
        self,
        raw: str,
        node: Node,
        args: list[tuple[str, Taint | None]],
        handles: _HandleMap,
        jc: _JsCtx,
    ) -> bool:
        # libc FILE* write: the handle rides an ARGUMENT (`fwrite(data, 1, n, f)`,
        # `fprintf(f, fmt, secret)`), not a receiver. Route the taint of every
        # NON-handle arg to the handle's resource. Only WRITE arg sinks are taint
        # destinations -- fread/fgets READ into a buffer, not a resource write.
        # Match through _js_match_sink so this path honours the SAME shadowing /
        # import rules as every other sink -- a local named `fwrite` is not the libc
        # symbol (CodeRabbit review, #1204).
        sink = self._js_match_sink(raw, jc.arg_handle_sinks, jc)
        if sink is None or sink.direction == IODirection.READ:
            return False
        arguments = node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        arg_nodes = self._named_no_comments(arguments) if arguments is not None else []
        handle_text = (
            safe_decode_text(arg_nodes[sink.handle_arg])
            if sink.handle_arg < len(arg_nodes)
            else None
        )
        # The handle argument resolves to its resource(s): a bound handle (possibly
        # several after a branch merge), a pre-bound std stream (`fprintf(stderr,..)`),
        # or an untracked FILE* known only by signature (<dynamic>).
        bindings = handles.get(handle_text) if handle_text is not None else None
        if bindings:
            targets = [(b.kind, b.identity) for b in bindings]
        elif handle_text in LIBC_STD_STREAMS:
            targets = [(LIBC_STD_STREAMS[handle_text], DYNAMIC_TARGET)]
        else:
            targets = [(ResourceKind.FILE, DYNAMIC_TARGET)]
        for index, (_via, taint) in enumerate(args):
            # Only the DATA payload flows to the file: `fwrite(buf, size, n, f)`
            # writes arg 0, so a tainted `size`/`n` is control metadata, not a leak.
            # data_args=None means every non-handle arg is payload (fprintf's format +
            # varargs).
            is_payload = (
                index in sink.data_args
                if sink.data_args is not None
                else index != sink.handle_arg
            )
            if not is_payload or taint is None:
                continue
            for kind, identity in targets:
                self._emit_taint_to_sink(taint, kind, identity, jc.flow.caller_qn)
        return True

    def _bind_lean_type_decl(self, node: Node, handles: _HandleMap, jc: _JsCtx) -> None:
        # C++ type-declaration handles: `std::ofstream out("x.txt")` (init_declarator
        # with an argument_list value) and the most-vexing-parse `std::ofstream
        # dyn(path)` (a function_declarator), each binding a FILE handle -- <dynamic>
        # when the identity is not a string literal (issue #1220). Mirrors io_access's
        # _bind_type_decl_handle, but writes the set-valued path-sensitive map.
        type_node = node.child_by_field_name(cs.TS_FIELD_TYPE)
        if type_node is None or type_node.text is None:
            return
        kind = jc.type_ctors.get(type_node.text.decode(cs.ENCODING_UTF8))
        if kind is None:
            return
        for child in node.named_children:
            if child.type == cs.TS_CPP_INIT_DECLARATOR:
                name = cpp_declarator_name(
                    child.child_by_field_name(cs.FIELD_DECLARATOR)
                )
                identity = self._first_string_arg(
                    child.child_by_field_name(cs.FIELD_VALUE), jc
                )
            elif child.type == cs.TS_CPP_FUNCTION_DECLARATOR:
                name = cpp_declarator_name(
                    child.child_by_field_name(cs.FIELD_DECLARATOR)
                )
                identity = DYNAMIC_TARGET
            else:
                continue
            if name is not None:
                handles[name] = frozenset({HandleBinding(kind, identity)})

    @staticmethod
    def _decl_type_is_handle(decl: Node | None, jc: _JsCtx) -> bool:
        # True when `decl` is a C++ declaration whose written type is a stream handle
        # constructor -- its init_declarator is bound by _bind_lean_type_decl, so the
        # normal declarator binding must skip it rather than pop the handle (#1220).
        if decl is None or decl.type != cs.TS_CPP_DECLARATION:
            return False
        type_node = decl.child_by_field_name(cs.TS_FIELD_TYPE)
        return (
            type_node is not None
            and type_node.text is not None
            and type_node.text.decode(cs.ENCODING_UTF8) in jc.type_ctors
        )

    @staticmethod
    def _first_string_arg(args: Node | None, jc: _JsCtx) -> str:
        # The first string-literal argument of a constructor's argument_list
        # (`std::ofstream in("x.txt", std::ios::binary)` -> x.txt).
        if args is None:
            return DYNAMIC_TARGET
        d = jc.descriptor
        for child in args.named_children:
            if child.type == d.string_type:
                return string_literal(child, d.string_type, d.string_content_type)
        return DYNAMIC_TARGET

    # --- Dart lean walk (issue #1173). Dart has no call-expression node: calls,
    # member reads and bindings are flat sibling `selector` chains, so it takes a
    # dedicated leaf path reusing the dart/utils.py reconstructors. ---

    def _dart_body_statements(self, caller_node: Node) -> list[Node]:
        # The captured Dart signature's sibling `function_body` -> its `block`'s
        # statements. A `program` (module-level) caller has no function body.
        body = dart_body_node(caller_node)
        if body is None:
            return []
        block = next(
            (c for c in body.named_children if c.type == cs.TS_DART_BLOCK), None
        )
        if block is not None:
            return list(block.named_children)
        # An expression-bodied declaration `=> expr` (function/method/getter) has no
        # block: the whole `function_body` is walked for the expression's call
        # effects and evaluated as the implicit return value (Greptile review, #1173).
        return [body]

    @staticmethod
    def _dart_selector_is_call(node: Node) -> bool:
        return node.type == cs.TS_DART_SELECTOR and any(
            c.type == cs.TS_DART_ARGUMENT_PART for c in node.named_children
        )

    def _apply_dart_leaf(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        # Dart leaf effects: a `var x = <chain>` / `x = <chain>` binding, a call
        # (a `selector` holding an `argument_part`), or a return.
        node_type = node.type
        if node_type in (
            cs.TS_DART_INITIALIZED_VARIABLE_DEFINITION,
            cs.TS_DART_ASSIGNMENT_EXPRESSION,
        ):
            self._dart_bind(node, tainted, handles, jc)
        elif self._dart_selector_is_call(node):
            self._dart_call(node, tainted, handles, jc)
        elif node_type in (cs.TS_RETURN_STATEMENT, cs.TS_DART_FUNCTION_BODY):
            # `return expr;` and an arrow body `=> expr` both contribute the
            # expression's taint to the return summary (issue #1173).
            returned = self._dart_return_taint(node, tainted, jc)
            if returned is not None:
                self._acc_returns_taint = True
                self._acc_return_taint = _merge_taint(self._acc_return_taint, returned)

    @staticmethod
    def _dart_binding_name_and_rhs(node: Node) -> tuple[str | None, list[Node]]:
        # `var k = <rhs...>` / `k = <rhs...>`: the name is the last identifier before
        # `=` (unwrapping an `assignable_expression` LHS), the RHS is the named
        # children after `=` (a spread selector chain).
        eq_index = next(
            (i for i, c in enumerate(node.children) if c.type == _DART_ASSIGN_OP),
            None,
        )
        if eq_index is None:
            return None, []
        name: str | None = None
        for child in node.children[:eq_index]:
            target = child
            if target.type == cs.TS_DART_ASSIGNABLE_EXPRESSION:
                target = next(
                    (
                        c
                        for c in target.named_children
                        if c.type == cs.TS_DART_IDENTIFIER
                    ),
                    target,
                )
            if target.type == cs.TS_DART_IDENTIFIER and target.text is not None:
                name = target.text.decode(cs.ENCODING_UTF8)
        rhs = [c for c in node.children[eq_index + 1 :] if c.is_named]
        return name, rhs

    def _dart_bind(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        name, rhs = self._dart_binding_name_and_rhs(node)
        if name is None:
            return
        taint, bindings = self._dart_rhs(rhs, tainted, handles, jc)
        if taint is not None:
            tainted[name] = taint
        else:
            tainted.pop(name, None)
        if bindings:
            handles[name] = bindings
        else:
            handles.pop(name, None)

    def _dart_rhs(
        self,
        rhs: list[Node],
        tainted: _TaintMap,
        handles: _HandleMap,
        jc: _JsCtx,
    ) -> tuple[Taint | None, frozenset[HandleBinding]]:
        # The taint and/or handle a Dart binding RHS yields: a `Platform.environment`
        # member source, a `File(path)` handle constructor, a read-source call, a
        # project-callee return (pending), or a plain identifier alias.
        if not rhs:
            return None, frozenset()
        if len(rhs) == 1 and rhs[0].type in (
            cs.TS_DART_UNARY_EXPRESSION,
            cs.TS_DART_AWAIT_EXPRESSION,
        ):
            # `await Socket.connect(...)` wraps the chain one node deep per
            # level; the awaited (or negated) value carries the chain's taint
            # and handle, so unwrap and re-evaluate (issue #1224).
            inner = [c for c in rhs[0].named_children if c.type != cs.TS_COMMENT]
            return self._dart_rhs(inner, tainted, handles, jc)
        source = self._dart_member_source(rhs, jc)
        if source is not None:
            return Taint(frozenset({source}), frozenset()), frozenset()
        call_sel = next((n for n in rhs if self._dart_selector_is_call(n)), None)
        if call_sel is not None:
            raw = dart_call_name(call_sel)
            if raw is not None:
                ctor = self._js_match_sink(raw, jc.handle_ctors, jc)
                if ctor is not None and ctor.direction != IODirection.READ:
                    identity = self._dart_first_string_arg(call_sel)
                    return None, frozenset({HandleBinding(ctor.kind, identity)})
                read = self._js_match_sink(raw, jc.flow.read_sinks, jc)
                if read is not None:
                    identity = (
                        self._dart_first_string_arg(call_sel)
                        if read.target_arg == 0
                        else DYNAMIC_TARGET
                    )
                    return (
                        Taint(
                            frozenset({HandleBinding(read.kind, identity)}), frozenset()
                        ),
                        frozenset(),
                    )
                handle_read = self._dart_handle_read_taint(raw, handles, jc)
                if handle_read is not None:
                    return handle_read, frozenset()
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
                    return Taint(frozenset(), frozenset({callee[1]})), frozenset()
            return None, frozenset()
        if len(rhs) == 1 and rhs[0].type == cs.TS_DART_IDENTIFIER and rhs[0].text:
            alias = rhs[0].text.decode(cs.ENCODING_UTF8)
            return tainted.get(alias), handles.get(alias, frozenset())
        if len(rhs) == 1 and rhs[0].type == cs.TS_DART_STRING_LITERAL:
            return self._dart_interpolation_taint(rhs[0], tainted, jc), frozenset()
        return None, frozenset()

    def _dart_interpolation_taint(
        self, literal: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # `'$k'` and `'${a.b}'` embed expressions inside the literal; shell
        # payloads are routinely built this way, so each substitution is
        # evaluated and the taints merged (issue #1224 review). The bare
        # `$k` form parses its name as identifier_dollar_escaped, which the
        # alias path does not recognise, so it is looked up by text.
        merged: Taint | None = None
        for child in literal.named_children:
            if child.type != cs.TS_DART_TEMPLATE_SUBSTITUTION:
                continue
            inner = [c for c in child.named_children if c.type != cs.TS_COMMENT]
            if (
                len(inner) == 1
                and inner[0].type == cs.TS_DART_IDENTIFIER_DOLLAR_ESCAPED
                and inner[0].text
            ):
                taint = tainted.get(inner[0].text.decode(cs.ENCODING_UTF8))
            else:
                taint, _ = self._dart_rhs(inner, tainted, {}, jc)
            if taint is not None:
                merged = taint if merged is None else _merge_taint(merged, taint)
        return merged

    def _dart_walk_read_callbacks(
        self,
        raw: str,
        selector: Node,
        tainted: _TaintMap,
        handles: _HandleMap,
        jc: _JsCtx,
    ) -> None:
        # `s.listen((data) { ... })` delivers data FROM the handle's resource
        # into the callback parameter. The lean walk skips nested callable
        # bodies by design, so the callback block is walked here with a COPY
        # of the state whose parameters are seeded by the handle's bindings
        # (issue #1316); the copy keeps the seeding scoped to the lambda.
        recv, sep, method = raw.rpartition(jc.descriptor.handle_method_separator)
        if not sep:
            return
        origins = {
            binding
            for binding in handles.get(recv, frozenset())
            if jc.handle_methods.get(binding.kind, {}).get(method) == IODirection.READ
        }
        if not origins:
            return
        arguments = self._dart_arguments(selector)
        if arguments is None:
            return
        seed = Taint(frozenset(origins), frozenset())
        for arg in arguments.named_children:
            if arg.type != cs.TS_DART_ARGUMENT:
                continue
            fn = next(
                (
                    c
                    for c in arg.named_children
                    if c.type == cs.TS_DART_FUNCTION_EXPRESSION
                ),
                None,
            )
            if fn is None:
                continue
            names = self._dart_lambda_param_names(fn)
            inner = dict(tainted)
            inner_handles = dict(handles)
            added_locals = [n for n in names if n not in jc.local_names]
            for name in names:
                # The parameter SHADOWS every outer meaning of the name: a
                # same-named outer handle must not receive the callback's
                # calls, and a parameter named after a builtin sink must not
                # match it (review on #1317).
                inner[name] = seed
                inner_handles.pop(name, None)
                jc.local_names.add(name)
            try:
                state = _LeanState(taint=inner, handles=inner_handles)
                for stmt in self._dart_lambda_body_statements(fn):
                    state = self._walk_flat_stmt(stmt, state, jc)
            finally:
                for name in added_locals:
                    jc.local_names.discard(name)

    @staticmethod
    def _dart_lambda_param_names(fn: Node) -> list[str]:
        plist = next(
            (
                c
                for c in fn.named_children
                if c.type == cs.TS_DART_FORMAL_PARAMETER_LIST
            ),
            None,
        )
        if plist is None:
            return []
        # Optional-positional (`[data]`) and named (`{data}`) parameters sit
        # under wrapper nodes, so the list is walked recursively; a typed
        # parameter's NAME is its last identifier (`String data`), never the
        # type (review on #1317).
        names: list[str] = []
        stack = list(plist.named_children)
        while stack:
            node = stack.pop()
            if node.type == cs.TS_DART_FORMAL_PARAMETER:
                idents = [
                    c
                    for c in node.named_children
                    if c.type == cs.TS_DART_IDENTIFIER and c.text
                ]
                if idents:
                    names.append(idents[-1].text.decode(cs.ENCODING_UTF8))
            elif node.type == cs.TS_DART_IDENTIFIER and node.text:
                names.append(node.text.decode(cs.ENCODING_UTF8))
            else:
                stack.extend(node.named_children)
        return names

    @staticmethod
    def _dart_lambda_body_statements(fn: Node) -> list[Node]:
        body = next(
            (
                c
                for c in fn.named_children
                if c.type == cs.TS_DART_FUNCTION_EXPRESSION_BODY
            ),
            None,
        )
        if body is None:
            return []
        block = next(
            (c for c in body.named_children if c.type == cs.TS_DART_BLOCK), None
        )
        if block is not None:
            return list(block.named_children)
        return [body]

    def _dart_handle_read_taint(
        self, raw: str, handles: _HandleMap, jc: _JsCtx
    ) -> Taint | None:
        # A read METHOD on a bound handle yields data FROM the resource
        # (`var d = f.readAsString()`), the read mirror of
        # `_emit_handle_write` (issue #1316). READ_WRITE methods count: a DB
        # execute's result is resource data too.
        recv, sep, method = raw.rpartition(jc.descriptor.handle_method_separator)
        if not sep:
            return None
        origins = {
            binding
            for binding in handles.get(recv, frozenset())
            if jc.handle_methods.get(binding.kind, {}).get(method)
            in (IODirection.READ, IODirection.READ_WRITE)
        }
        if not origins:
            return None
        return Taint(frozenset(origins), frozenset())

    def _dart_member_source(
        self, nodes: list[Node], jc: _JsCtx
    ) -> HandleBinding | None:
        # `Platform.environment['K']`: a bound member read. `dart_member_read_name`
        # on the `.environment` selector yields the prefix; the key comes from a
        # trailing `index_selector`'s string literal.
        for index, node in enumerate(nodes):
            if node.type != cs.TS_DART_SELECTOR:
                continue
            name = dart_member_read_name(node)
            if name is None:
                continue
            for prefix, kind in jc.member_reads:
                if name == prefix:
                    identity = self._dart_index_key(nodes[index + 1 :])
                    return HandleBinding(kind=kind, identity=identity)
        return None

    def _dart_index_key(self, following: list[Node]) -> str:
        # The string key inside a trailing `index_selector` (`['K']` -> `K`).
        for node in following:
            index_sel = self._find_dart_index_selector(node)
            if index_sel is not None:
                for child in index_sel.named_children:
                    if child.type == cs.TS_DART_STRING_LITERAL:
                        return self._dart_string_literal(child)
        return DYNAMIC_TARGET

    @staticmethod
    def _find_dart_index_selector(node: Node) -> Node | None:
        if node.type == cs.TS_DART_INDEX_SELECTOR:
            return node
        for child in node.named_children:
            if child.type == cs.TS_DART_INDEX_SELECTOR:
                return child
            nested = FlowProcessor._find_dart_index_selector(child)
            if nested is not None:
                return nested
        return None

    def _dart_call(
        self, selector: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        raw = dart_call_name(selector)
        if raw is None:
            return
        args = self._dart_arg_taints(selector, tainted, jc)
        if (sink := self._js_match_sink(raw, jc.flow.write_sinks, jc)) is not None:
            dst = (
                self._dart_first_string_arg(selector)
                if sink.target_arg == 0
                else DYNAMIC_TARGET
            )
            for _via, taint in args:
                if taint is not None:
                    self._emit_taint_to_sink(taint, sink.kind, dst, jc.flow.caller_qn)
            return
        if handles and self._emit_handle_write(raw, args, handles, jc):
            return
        self._dart_walk_read_callbacks(raw, selector, tainted, handles, jc)
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
            if taint.origins or taint.pending:
                token = _passthrough_result_token(jc.flow.caller_qn, selector)
                self._param_call_sites.append((taint, callee_qn, via, token))
            for pname in taint.params:
                self._param_flow_edges.append(
                    (jc.flow.caller_qn, pname, callee_qn, via)
                )

    @staticmethod
    def _dart_arguments(selector: Node) -> Node | None:
        # The `arguments` node inside a call selector's `argument_part`.
        argpart = next(
            (c for c in selector.named_children if c.type == cs.TS_DART_ARGUMENT_PART),
            None,
        )
        if argpart is None:
            return None
        return next(
            (c for c in argpart.named_children if c.type == cs.TS_DART_ARGUMENTS), None
        )

    @staticmethod
    def _dart_named_arg(arg: Node) -> tuple[str | None, list[Node]]:
        # `named_argument` -> `label`(identifier `:`) + the value CHAIN (which may be
        # a selector chain such as `message: Platform.environment['K']`).
        name: str | None = None
        value: list[Node] = []
        for child in arg.named_children:
            if child.type == cs.TS_DART_LABEL:
                ident = next(
                    (
                        c
                        for c in child.named_children
                        if c.type == cs.TS_DART_IDENTIFIER
                    ),
                    None,
                )
                if ident is not None and ident.text is not None:
                    name = ident.text.decode(cs.ENCODING_UTF8)
            elif child.type != cs.TS_COMMENT:
                value.append(child)
        return name, value

    def _dart_arg_taints(
        self, selector: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> list[tuple[str, Taint | None]]:
        # A Dart call's arguments: positional `argument` wrappers (via `arg:<index>`)
        # and `named_argument` values (`sink(message: x)`, via `kw:<name>`). Each
        # argument is a full expression CHAIN (a bare identifier, a
        # `Platform.environment['K']` source, or a `src()` call), evaluated via
        # `_dart_rhs` so inline sources reach the sink too (issue #1173).
        arguments = self._dart_arguments(selector)
        if arguments is None:
            return []
        out: list[tuple[str, Taint | None]] = []
        positional = 0
        for arg in arguments.named_children:
            if arg.type == cs.TS_DART_ARGUMENT:
                chain = [c for c in arg.named_children if c.type != cs.TS_COMMENT]
                via = VIA_ARG_FORMAT.format(index=positional)
                positional += 1
            elif arg.type == cs.TS_DART_NAMED_ARGUMENT:
                name, chain = self._dart_named_arg(arg)
                if name is None:
                    continue
                via = VIA_KW_FORMAT.format(name=name)
            else:
                continue
            taint, _ = self._dart_rhs(chain, tainted, {}, jc)
            if (
                taint is None
                and len(chain) == 1
                and chain[0].type == cs.TS_DART_LIST_LITERAL
            ):
                taint = self._dart_list_literal_taint(chain[0], tainted, jc)
            out.append((via, taint))
        return out

    def _dart_list_literal_taint(
        self, literal: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # A `Process.run('sh', ['-c', k])`-style call carries its payload
        # INSIDE a list literal; each element expression is evaluated on its
        # own so a tainted element taints the argument (issue #1224).
        merged: Taint | None = None
        for element in literal.named_children:
            if element.type == cs.TS_COMMENT:
                continue
            taint, _ = self._dart_rhs([element], tainted, {}, jc)
            if taint is not None:
                merged = taint if merged is None else _merge_taint(merged, taint)
        return merged

    def _dart_return_taint(
        self, node: Node, tainted: _TaintMap, jc: _JsCtx
    ) -> Taint | None:
        # A Dart `return <chain>;` contributes the chain's taint to the summary.
        rhs = [c for c in node.named_children if c.type != cs.TS_COMMENT]
        taint, _ = self._dart_rhs(rhs, tainted, {}, jc)
        return taint

    def _dart_first_string_arg(self, selector: Node) -> str:
        arguments = self._dart_arguments(selector)
        if arguments is None:
            return DYNAMIC_TARGET
        for arg in arguments.named_children:
            if arg.type == cs.TS_DART_ARGUMENT:
                chain = [c for c in arg.named_children if c.type != cs.TS_COMMENT]
            elif arg.type == cs.TS_DART_NAMED_ARGUMENT:
                _, chain = self._dart_named_arg(arg)
            else:
                continue
            first = chain[0] if chain else None
            if first is not None and first.type == cs.TS_DART_STRING_LITERAL:
                return self._dart_string_literal(first)
        return DYNAMIC_TARGET

    @staticmethod
    def _dart_string_literal(node: Node) -> str:
        # A Dart string_literal has no content child; the text (with quotes) is
        # inline. Interpolation (`'$x'`) collapses to <dynamic>.
        if node.text is None:
            return DYNAMIC_TARGET
        if any(c.type == cs.TS_DART_TEMPLATE_SUBSTITUTION for c in node.named_children):
            return DYNAMIC_TARGET
        return node.text.decode(cs.ENCODING_UTF8).strip(cs.DART_QUOTE_CHARS)

    def _emit_taint_to_sink(
        self, taint: Taint, kind: ResourceKind, identity: str, caller_qn: str
    ) -> None:
        # A tainted value reaching a write sink: emit resolved origins now, defer
        # pending callee returns to the fixpoint (mirrors the _js_call write branch).
        for origin in taint.origins:
            self._emit_resource_flow(origin, kind, identity)
        if taint.pending:
            self._deferred_resource_flows.append((taint.pending, kind, identity))
        # A parameter reaching this macro/stream sink is a parameter-to-sink
        # summary (issue #1169), composed at finalize like the _js_call branch.
        for pname in taint.params:
            self._param_sinks[(caller_qn, pname)].add((kind, identity))

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
                    self._emit_taint_to_sink(
                        taint, sink.kind, DYNAMIC_TARGET, jc.flow.caller_qn
                    )

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

    def _flow_keyword_write(self, node: Node, tainted: _TaintMap, jc: _JsCtx) -> None:
        # A keyword output construct (PHP `echo $a, $b;` / `print $x`) writes each
        # operand to STDOUT with no callee name (issue #1174). `echo` takes several
        # operands wrapped in a `sequence_expression`; `print` a single operand.
        for child in node.named_children:
            if child.type == cs.TS_COMMENT:
                continue
            operands = (
                child.named_children
                if child.type == cs.TS_PHP_SEQUENCE_EXPRESSION
                else (child,)
            )
            for operand in operands:
                taint = self._js_expr_taint(operand, tainted, jc)
                if taint is not None:
                    self._emit_taint_to_sink(
                        taint, ResourceKind.STDOUT, DYNAMIC_TARGET, jc.flow.caller_qn
                    )

    def _flow_stream(
        self, node: Node, tainted: _TaintMap, handles: _HandleMap, jc: _JsCtx
    ) -> None:
        # A C++ stream sink (`std::cout << a << b`) nests left-associatively. Act
        # only at the TOP of the `<<` chain, walk the `left` spine to the base
        # operand; if it is a stream sink (cout/cerr) OR a bound file handle
        # (`out << x` on a std::ofstream, issue #1220), flow the taint of every
        # inserted operand to that resource. A non-stream base (arithmetic `x << 2`)
        # misses.
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
        base_text = base.text.decode(cs.ENCODING_UTF8)
        sink = self._js_match_sink(base_text, jc.flow.stream_sinks, jc)
        # cout/cerr write STDOUT/STDERR with no concrete resource; a bound handle
        # base carries its file's identity (possibly several after a branch merge).
        targets: list[tuple[ResourceKind, str]]
        if sink is not None:
            targets = [(sink.kind, DYNAMIC_TARGET)]
        elif bindings := handles.get(base_text):
            targets = [(b.kind, b.identity) for b in bindings]
        else:
            return
        for operand in operands:
            taint = self._js_expr_taint(operand, tainted, jc)
            if taint is not None:
                for kind, identity in targets:
                    self._emit_taint_to_sink(taint, kind, identity, jc.flow.caller_qn)

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
        if obj is None and node.type == jc.descriptor.subscript_type:
            # PHP `$_ENV["K"]` is a FIELDLESS subscript: the indexed object is the
            # first named child (issue #1174). Guarded so field-based grammars
            # (JS/Java/…) never take this path.
            obj = next(
                (c for c in node.named_children if c.type != cs.TS_COMMENT), None
            )
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
        if index is None:
            # Fieldless subscript (PHP `$_ENV["K"]`): the key is the first
            # string-literal child.
            index = next(
                (c for c in node.named_children if c.type == d.string_type), None
            )
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
    def _js_match_sink[T](raw: str, sink_map: dict[str, T], jc: _JsCtx) -> T | None:
        # Same shadow-aware matching as io_access: a locally-bound name is not
        # the builtin; the import-normalised name matches first (so an aliased
        # builtin resolves), then the raw dotted name only when its head resolves
        # to the genuine module (node:/require/ESM), never a shadowing local module.
        # Rust (scope_separator="::") keys sinks under the full `std::` form: expand
        # the head through the import map on `::` (`use std::env; env::var` ->
        # `std::env::var`; a fully-qualified `std::env::var` has an unimported `std`
        # head and stays as-is); a head bound to a local name is shadowed.
        if jc.descriptor.case_insensitive_call_names:
            # PHP function names are case-insensitive (`GETENV` == `getenv`); the
            # registry keys are lowercase (issue #1174).
            raw = raw.lower()
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

    @staticmethod
    def _merge_lean(states: list[_LeanState]) -> _LeanState:
        # MAY union of both maps across branches (issue #1204). Taint reuses
        # _merge; handles union per name, so a variable that may hold either of
        # two handles on different paths writes to BOTH at a later method call.
        handles: _HandleMap = {}
        for s in states:
            for name, bindings in s.handles.items():
                existing = handles.get(name)
                handles[name] = existing | bindings if existing else bindings
        return _LeanState(FlowProcessor._merge([s.taint for s in states]), handles)

    def _walk_stmt(self, node: Node, state: _TaintMap, ctx: _FlowCtx) -> _TaintMap:
        node_type = node.type
        if node_type in PY_SCOPE_BOUNDARIES:
            # Nested def/class: only its header executes in this scope. Record any
            # enclosing taint its body captures BEFORE walking the header, using
            # the def-site state (issue #1197); the body is left to its own pass.
            self._record_captures(node, state, ctx)
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
        # A closure carried ANYWHERE in the RHS (holder.callback = send,
        # d[k] = (send), x = send if c else other) escapes, even when the
        # non-identifier target makes the assignment otherwise untrackable
        # (issue #1211 review); direct-call callees are exempt.
        self._note_capture_escapes_in(right)
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
            text = node.text.decode(cs.ENCODING_UTF8)
            # A VALUE use of a locally-defined closure name escapes it: it may
            # be stored/passed/returned and run later with any cell value, so
            # its capture falls back to the def-site MAY snapshot (#1211).
            self._note_capture_escape(text)
            return tainted.get(text)
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
                # The result also carries a per-call-site pass-through token so a
                # tainted argument to a return-parameter reaches THIS call's
                # consumers only (issue #1168); it resolves to nothing when the
                # callee is not a pass-through, so non-pass-through calls are
                # unaffected.
                token = _passthrough_result_token(ctx.caller_qn, node)
                return Taint(frozenset(), frozenset({callee[1], token}))
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
        # BEFORE the no-args early return: a zero-arg `send()` is exactly the
        # direct closure invocation whose cell state matters (issue #1211).
        self._note_capture_call(raw, tainted)
        # Evaluate every argument through the value-taint evaluator, so a source
        # or tainted callee written inline -- log_it(os.getenv("K")) -- is seen,
        # not only a bare tainted identifier (CodeRabbit review on PR #1167).
        arg_taints = self._arg_taints(node, tainted, ctx)
        if not arg_taints:
            return
        sink = registry_match(ctx.write_sinks, raw, ctx.import_map)
        if sink is not None:
            dst_identity = literal_target(node, sink.target_arg, sink.target_kw)
            for taint, _via in arg_taints:
                # Resolved origins emit resource flows now; pending callees defer
                # to finalize, when the (possibly forward) callee's origins resolve.
                for source in taint.origins:
                    self._emit_resource_flow(source, sink.kind, dst_identity)
                if taint.pending:
                    self._deferred_resource_flows.append(
                        (taint.pending, sink.kind, dst_identity)
                    )
                # A parameter reaching this sink is a parameter-to-sink summary
                # (issue #1142): composed at finalize against every call site
                # that passes a tainted argument into this parameter.
                for pname in taint.params:
                    self._param_sinks[(ctx.caller_qn, pname)].add(
                        (sink.kind, dst_identity)
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
        for taint, via in arg_taints:
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
            # Forward parameter-taint (issue #1142), orthogonal to the arg edge:
            # a concrete argument records a call site to compose against the
            # callee's parameter-sink closure; an argument that IS one of this
            # function's parameters records a transitive hand-off so a wrapper of
            # a wrapper still resolves. The token ties a pass-through composition
            # to this exact call so it is not shared across calls (issue #1168).
            if taint.origins or taint.pending:
                token = _passthrough_result_token(ctx.caller_qn, node)
                self._param_call_sites.append((taint, callee_qn, via, token))
            for pname in taint.params:
                self._param_flow_edges.append((ctx.caller_qn, pname, callee_qn, via))

    def _arg_taints(
        self, call_node: Node, tainted: _TaintMap, ctx: _FlowCtx
    ) -> list[tuple[Taint, str]]:
        # The Taint each argument carries, tagged with its `via` channel. Unlike
        # the identifier-only reading, this evaluates the full argument
        # expression, so an inline source or tainted call is tracked; positional
        # index counts positional args only (keywords never advance it), keeping
        # arg:<i> aligned with the callee's parameter order.
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return []
        out: list[tuple[Taint, str]] = []
        index = 0
        for child in args.named_children:
            # Comments are named children; skip them so they neither consume a
            # positional index nor get evaluated as a value.
            if child.type == cs.TS_COMMENT:
                continue
            if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                key = child.child_by_field_name(cs.TS_FIELD_NAME)
                value = child.child_by_field_name(cs.FIELD_VALUE)
                if key is not None and key.text is not None and value is not None:
                    taint = self._py_value_taint(value, tainted, ctx)
                    if taint is not None:
                        out.append(
                            (
                                taint,
                                VIA_KW_FORMAT.format(
                                    name=key.text.decode(cs.ENCODING_UTF8)
                                ),
                            )
                        )
                continue
            taint = self._py_value_taint(child, tainted, ctx)
            if taint is not None:
                out.append((taint, VIA_ARG_FORMAT.format(index=index)))
            index += 1
        return out

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
            # Any returned expression may hand the closure to the caller
            # (`return send`, `return send if c else other`): it escapes.
            self._note_capture_escapes_in(child)
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
                    # `return redact(x)`: record that each of this function's
                    # parameters appearing in the returned call reaches its return
                    # through the callee, so a pass-through chain resolves (#1168).
                    for pnames, via in self._returned_arg_params(child, tainted):
                        for pname in pnames:
                            self._return_param_edges.append(
                                (ctx.caller_qn, pname, callee[1], via)
                            )
        return result if tainted_here else None

    def _returned_arg_params(
        self, call_node: Node, tainted: _TaintMap
    ) -> list[tuple[frozenset[str], str]]:
        # The enclosing function's parameters each argument of a returned call
        # carries, tagged with the argument's `via`. Params-only (never touches
        # the deferred-candidate lists), because the value/return-edge side of the
        # returned call is already handled by the walk's descent into it.
        args = call_node.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
        if args is None:
            return []
        out: list[tuple[frozenset[str], str]] = []
        index = 0
        for child in args.named_children:
            if child.type == cs.TS_COMMENT:
                continue
            if child.type == cs.TS_PY_KEYWORD_ARGUMENT:
                key = child.child_by_field_name(cs.TS_FIELD_NAME)
                value = child.child_by_field_name(cs.FIELD_VALUE)
                if key is not None and key.text is not None and value is not None:
                    params = self._value_params(value, tainted)
                    if params:
                        out.append(
                            (
                                params,
                                VIA_KW_FORMAT.format(
                                    name=key.text.decode(cs.ENCODING_UTF8)
                                ),
                            )
                        )
                continue
            params = self._value_params(child, tainted)
            if params:
                out.append((params, VIA_ARG_FORMAT.format(index=index)))
            index += 1
        return out

    def _value_params(self, node: Node, tainted: _TaintMap) -> frozenset[str]:
        # The parameter names a value expression carries, following the same
        # aliasing forms _py_value_taint threads (identifier, parenthesis, and the
        # value-selection unions) but reading only the seeded `params` field.
        if node.type == cs.TS_PY_IDENTIFIER and node.text is not None:
            taint = tainted.get(node.text.decode(cs.ENCODING_UTF8))
            return taint.params if taint is not None else frozenset()
        if node.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
            inner = next(iter(node.named_children), None)
            return self._value_params(inner, tainted) if inner else frozenset()
        if node.type == cs.TS_PY_CONDITIONAL_EXPRESSION:
            values = node.named_children
            if len(values) != 3:
                return frozenset()
            return self._value_params(values[0], tainted) | self._value_params(
                values[2], tainted
            )
        if node.type == cs.TS_PY_BOOLEAN_OPERATOR:
            left = node.child_by_field_name(cs.TS_FIELD_LEFT)
            right = node.child_by_field_name(cs.TS_FIELD_RIGHT)
            return (
                self._value_params(left, tainted) if left is not None else frozenset()
            ) | (
                self._value_params(right, tainted) if right is not None else frozenset()
            )
        return frozenset()

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
        # Forward parameter-to-return (issue #1168): fold each call site whose
        # argument enters a pass-through parameter (one that reaches the callee's
        # return) into that callee's return summary, so a caller consuming the
        # callee's return resolves through to the argument's origins.
        return_params = self._resolve_return_params()
        extra_origins: dict[str, set[HandleBinding]] = defaultdict(set)
        extra_pending: dict[str, set[str]] = defaultdict(set)
        for arg_taint, callee_qn, via, token in self._param_call_sites:
            pname = self._param_name_for_via(callee_qn, via)
            if pname is None or (callee_qn, pname) not in return_params:
                continue
            # Keyed by the per-call token (not the callee) so this call's result
            # carries only its own argument's taint (issue #1168).
            extra_origins[token] |= arg_taint.origins
            extra_pending[token] |= arg_taint.pending
        resolved, is_tainted = self._resolve_summaries(extra_origins, extra_pending)
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
        # Forward parameter-taint (issue #1142): close the parameter-sink map
        # over transitive hand-offs, then for each concrete call site emit a
        # resource flow from every origin the argument resolves to (its own plus
        # any pending callee's return origins) into every sink the matched
        # parameter reaches.
        param_sink_closure = self._resolve_param_sinks()
        for arg_taint, callee_qn, via, _token in self._param_call_sites:
            pname = self._param_name_for_via(callee_qn, via)
            if pname is None:
                continue
            reached = param_sink_closure.get((callee_qn, pname))
            if not reached:
                continue
            origins = set(arg_taint.origins)
            for p in arg_taint.pending:
                origins |= resolved.get(p, frozenset())
            for origin in origins:
                for sink_kind, sink_identity in reached:
                    self._emit_resource_flow(origin, sink_kind, sink_identity)
        # One-shot per run: clear so a reused processor never re-emits.
        self._return_edge_candidates.clear()
        self._deferred_resource_flows.clear()
        self._deferred_arg_edges.clear()
        self._param_sinks.clear()
        self._param_flow_edges.clear()
        self._return_param_edges.clear()
        self._param_call_sites.clear()
        self._positional_params.clear()
        self._variadic_params.clear()

    def _resolve_summaries(
        self,
        extra_origins: dict[str, set[HandleBinding]] | None = None,
        extra_pending: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, frozenset[HandleBinding]], dict[str, bool]]:
        # Worklist fixpoint over the serialized summaries: a function's resolved
        # origins are its own plus every pending callee's, and it is tainted if
        # it has an origin or any pending callee is tainted. Origins/taintedness
        # only grow, so this converges through recursion. Re-queue only a
        # callee's callers when it changes, keeping the whole pass O(V + E).
        # extra_origins/extra_pending inject pass-through argument taint into a
        # callee's return summary (issue #1168); a callee may appear only there.
        extra_origins = extra_origins or {}
        extra_pending = extra_pending or {}
        all_qns = set(self._summaries) | set(extra_origins) | set(extra_pending)
        base: dict[str, frozenset[HandleBinding]] = {}
        pend: dict[str, set[str]] = {}
        for qn in all_qns:
            summary = self._summaries.get(qn)
            own_origins = summary.origins if summary is not None else frozenset()
            own_pending = summary.pending if summary is not None else frozenset()
            base[qn] = own_origins | frozenset(extra_origins.get(qn, ()))
            pend[qn] = set(own_pending) | set(extra_pending.get(qn, ()))
        resolved = dict(base)
        is_tainted = {qn: bool(base[qn]) for qn in all_qns}
        callers_of: dict[str, set[str]] = defaultdict(set)
        for qn in all_qns:
            for callee_qn in pend[qn]:
                callers_of[callee_qn].add(qn)
        worklist = deque(qn for qn in all_qns if pend[qn])
        queued = set(worklist)
        while worklist:
            qn = worklist.popleft()
            queued.discard(qn)
            new_origins = base[qn]
            new_tainted = bool(base[qn])
            for callee_qn in pend[qn]:
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

    def _resolve_return_params(self) -> set[tuple[str, str]]:
        # The (function, parameter) pairs whose value reaches the function's
        # return: directly (`return p`, captured in the summary's params field)
        # or transitively (`return other(p)` and pass-through chains). Monotone
        # growth over a finite pair universe, so the closure terminates (mirrors
        # _resolve_param_sinks); all facts are global once every body is walked.
        rp: set[tuple[str, str]] = set()
        for qn, summary in self._summaries.items():
            for pname in summary.params:
                rp.add((qn, pname))
        changed = True
        while changed:
            changed = False
            for f_qn, p_name, c_qn, via in self._return_param_edges:
                callee_param = self._param_name_for_via(c_qn, via)
                if callee_param is None:
                    continue
                if (c_qn, callee_param) in rp and (f_qn, p_name) not in rp:
                    rp.add((f_qn, p_name))
                    changed = True
        return rp

    def _resolve_param_sinks(
        self,
    ) -> dict[tuple[str, str], set[tuple[ResourceKind, str]]]:
        # Close the (function, parameter) -> sinks map over transitive hand-offs:
        # if F's parameter P is passed into callee C's parameter Q, every sink Q
        # reaches is also reached by P. Monotone growth, so the naive fixpoint
        # converges and recursion/cycles terminate (mirrors the return fixpoint).
        closure: dict[tuple[str, str], set[tuple[ResourceKind, str]]] = {
            key: set(sinks) for key, sinks in self._param_sinks.items()
        }
        changed = True
        while changed:
            changed = False
            for f_qn, p_name, c_qn, via in self._param_flow_edges:
                callee_param = self._param_name_for_via(c_qn, via)
                if callee_param is None:
                    continue
                src = closure.get((c_qn, callee_param))
                if not src:
                    continue
                dst = closure.setdefault((f_qn, p_name), set())
                if not src <= dst:
                    dst |= src
                    changed = True
        return closure

    @staticmethod
    def _nested_def_name(def_node: Node) -> tuple[str, Node] | None:
        # The bound name and function_definition node of a nested def, unwrapping a
        # decorated_definition. None for a nested CLASS (its methods' qns are
        # class-nested, out of scope) or an unnamed def.
        inner = def_node
        if def_node.type == cs.TS_PY_DECORATED_DEFINITION:
            inner = next(
                (
                    c
                    for c in def_node.children
                    if c.type
                    in (cs.TS_PY_FUNCTION_DEFINITION, cs.TS_PY_CLASS_DEFINITION)
                ),
                def_node,
            )
        if inner.type != cs.TS_PY_FUNCTION_DEFINITION:
            return None
        name_node = inner.child_by_field_name(cs.FIELD_NAME)
        name = safe_decode_text(name_node) if name_node is not None else None
        return (name, inner) if name else None

    def _record_captures(self, def_node: Node, state: _TaintMap, ctx: _FlowCtx) -> None:
        # A nested function may capture tainted enclosing-scope variables through
        # its environment. A closure captures a CELL, not a value, so the state
        # that matters is the cell's value when the closure RUNS (issue #1211):
        # a direct same-scope call commits the capture from the state AT that
        # call, while a closure that escapes (stored, passed, returned) or is
        # never called locally keeps the def-site MAY snapshot of #1197 -- the
        # walk cannot know when an escaped closure runs, so the safe
        # over-approximation stands. Redefinition commits the old record first.
        resolved = self._nested_def_name(def_node)
        if resolved is None:
            return
        name, inner = resolved
        # Use the qn the definition pass registered for this exact node (it carries
        # the duplicate-name suffix a manual reconstruction would miss); fall back to
        # the reconstructed name only when the node was not registered.
        loc = self._function_locations.get(function_span_key(ctx.module_qn, inner))
        nested_qn = (
            loc.qualified_name
            if loc is not None
            else f"{ctx.caller_qn}{cs.SEPARATOR_DOT}{name}"
        )
        free_vars = tuple(python_free_variable_names(inner))
        if not free_vars:
            return
        if (previous := self._pending_captures.pop(name, None)) is not None:
            self._commit_pending_capture(previous)
        snapshot = {
            fv: taint for fv in free_vars if (taint := state.get(fv)) is not None
        }
        self._pending_captures[name] = _PendingCapture(
            nested_qn=nested_qn,
            def_node=def_node,
            caller_qn=ctx.caller_qn,
            free_vars=free_vars,
            snapshot=snapshot,
        )

    def _commit_capture_state(
        self, record: _PendingCapture, cell_state: dict[str, Taint]
    ) -> None:
        # Keyed by the nested function's qn and via kw:<name>, so the existing
        # parameter-summary finalize composes it exactly as a keyword argument
        # would (issue #1197). A capture carrying concrete origins/pending is a
        # resolvable call site; one carrying only the enclosing function's own
        # parameters records a transitive flow edge, so a variable captured
        # through two nested levels still composes.
        for fv in record.free_vars:
            taint = cell_state.get(fv)
            if taint is None:
                continue
            via = VIA_KW_FORMAT.format(name=fv)
            if taint.origins or taint.pending:
                token = _passthrough_result_token(record.caller_qn, record.def_node)
                self._param_call_sites.append((taint, record.nested_qn, via, token))
            for pname in taint.params:
                self._param_flow_edges.append(
                    (record.caller_qn, pname, record.nested_qn, via)
                )

    def _commit_pending_capture(self, record: _PendingCapture) -> None:
        self._commit_capture_state(record, record.snapshot)

    def _note_capture_call(self, raw: str, state: _TaintMap) -> None:
        # A direct call of a locally-defined closure: the cell holds the
        # CURRENT threaded state, so this invocation composes with it -- the
        # path-sensitive branch copies make a partially-cleaned merge stay MAY.
        record = self._pending_captures.get(raw)
        if record is None:
            return
        record.called = True
        cell_state = {
            fv: taint for fv in record.free_vars if (taint := state.get(fv)) is not None
        }
        self._commit_capture_state(record, cell_state)

    def _note_capture_escape(self, name: str) -> None:
        record = self._pending_captures.get(name)
        if record is not None:
            record.escaped = True

    def _note_capture_escapes_in(self, expr: Node | None) -> None:
        # Every identifier VALUE anywhere in this expression escapes its
        # closure: wrappers, conditionals, containers, and call ARGUMENTS all
        # hand the function object onward. The one exception is a call's
        # callee position -- `send()` is the direct invocation the
        # call-relative path handles -- so calls recurse into arguments only.
        # Over-marking is conservative-safe (escape means the def-site MAY
        # snapshot), never a lost flow.
        if expr is None:
            return
        if expr.type == cs.TS_PY_CALL:
            self._note_capture_escapes_in(
                expr.child_by_field_name(cs.TS_FIELD_ARGUMENTS)
            )
            return
        if expr.type == cs.TS_PY_IDENTIFIER and expr.text is not None:
            self._note_capture_escape(expr.text.decode(cs.ENCODING_UTF8))
            return
        if expr.type == cs.TS_PY_KEYWORD_ARGUMENT:
            # `other(send=None)`: the label names a PARAMETER, not the
            # closure; only the value can carry it onward.
            self._note_capture_escapes_in(expr.child_by_field_name(cs.FIELD_VALUE))
            return
        for child in expr.named_children:
            self._note_capture_escapes_in(child)

    def _flush_pending_captures(self) -> None:
        # Scope end: an escaped closure may run at ANY time with any cell
        # value, and a never-called one may still be reached through its qn,
        # so both keep the def-site MAY snapshot (#1197 semantics). A closure
        # only ever called directly already committed per-invocation states.
        for record in self._pending_captures.values():
            if record.escaped or not record.called:
                self._commit_pending_capture(record)
        self._pending_captures.clear()

    def _param_name_for_via(self, callee_qn: str, via: str) -> str | None:
        # Map an argument's `via` tag to the callee's parameter name: `kw:<name>`
        # names the parameter directly (so keyword-only parameters still match);
        # `arg:<index>` resolves through the callee's positional parameter names,
        # which stop at the first variadic/keyword-only boundary so a positional
        # argument never binds to a keyword-only parameter. A callee with no
        # recorded positional names yields None.
        kind, _, rest = via.partition(_VIA_SEPARATOR)
        if kind == _VIA_KW_KIND:
            return rest or None
        if kind == _VIA_ARG_KIND:
            names = self._positional_params.get(callee_qn)
            if names is None:
                return None
            try:
                index = int(rest)
            except ValueError:
                return None
            # Every argument at or after a variadic/rest slot binds to that one
            # parameter (issue #1169); the Python path records no variadic index.
            variadic = self._variadic_params.get(callee_qn)
            if variadic is not None and index >= variadic:
                index = variadic
            if 0 <= index < len(names):
                # None marks a slot that binds no simple name (unnamed C++/Go
                # parameter, JS/TS destructuring): no parameter to compose, so
                # no edge -- never a shifted (wrong) name.
                return names[index]
        return None

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
