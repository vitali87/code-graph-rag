"""Which of a file's call nodes each caller owns.

The call pass collects a file's call nodes once, in start order, and hands
every caller its share: the calls its span holds, less those a nested
function with a caller pass of its own takes, while the module keeps the
calls no function body holds. Each share is a bisection of the start
offsets, never another walk of the tree."""

from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from tree_sitter import Node, Query, QueryCursor

from .. import constants as cs
from ..node_text import sorted_captures
from ..types_defs import LanguageQueries
from .dart import utils as dart_utils
from .julia import utils as julia_utils

# Julia definitions with no body field, whose body is everything after the
# signature, and the shapes whose right-hand side is the body.
_JULIA_SIGNATURE_DEFINITIONS = (
    cs.TS_JULIA_FUNCTION_DEFINITION,
    cs.TS_JULIA_MACRO_DEFINITION,
)
_JULIA_RHS_DEFINITIONS = (
    cs.TS_JULIA_ASSIGNMENT,
    cs.TS_JULIA_ARROW_FUNCTION_EXPRESSION,
)


class RegisteredNames(Protocol):
    """The name the definition pass registered a Julia function node under,
    or None while it stays anonymous."""

    def registered_name(self, func_node: Node, module_qn: str) -> str | None: ...


def collect_all_call_nodes(
    root_node: Node,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> tuple[list[Node], list[int]]:
    """The file's call nodes in start order, and their start offsets."""
    calls_query = queries[language].get(cs.QUERY_CALLS)
    if not calls_query:
        return [], []
    cursor = QueryCursor(calls_query)
    captures = sorted_captures(cursor, root_node)
    call_nodes = captures.get(cs.CAPTURE_CALL, [])
    call_starts = [n.start_byte for n in call_nodes]
    return call_nodes, call_starts


def file_combined_captures(
    file_path: Path,
    root_node: Node,
    language_queries: LanguageQueries,
    func_class_captures_cache: dict[Path, dict] | None,
) -> dict[str, list[Node]]:
    """The file's function, class and call captures: the definition pass's,
    when it cached them, else the combined query's."""
    if func_class_captures_cache is not None and file_path in func_class_captures_cache:
        return func_class_captures_cache[file_path]
    combined_query = language_queries[cs.QUERY_FUNC_CLASS]
    if combined_query:
        cursor = QueryCursor(combined_query)
        return sorted_captures(cursor, root_node)
    return {}


def file_call_nodes(
    combined_captures: dict[str, list[Node]],
    root_node: Node,
    language: cs.SupportedLanguage,
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
) -> tuple[list[Node], list[int] | None, list[int] | None]:
    """The file's call nodes plus the call/function start offsets the
    per-scope filters bisect; both offset lists stay None when the file
    captured no function or class to filter by."""
    cached_calls = combined_captures.get(cs.CAPTURE_CALL)
    if cached_calls is not None:
        all_call_nodes = cached_calls
        call_starts: list[int] | None = None
    else:
        all_call_nodes, call_starts = collect_all_call_nodes(
            root_node, language, queries
        )

    sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)
    if sorted_func_nodes or combined_captures.get(cs.CAPTURE_CLASS):
        if cached_calls is not None:
            call_starts = [n.start_byte for n in all_call_nodes]
        func_node_starts = (
            [n.start_byte for n in sorted_func_nodes] if sorted_func_nodes else None
        )
    else:
        call_starts = None
        func_node_starts = None
    return all_call_nodes, call_starts, func_node_starts


def _start_byte(node: Node) -> int:
    return node.start_byte


def _calls_in_span(
    all_call_nodes: list[Node], call_starts: list[int], start: int, end: int
) -> list[Node]:
    lo = bisect_left(call_starts, start)
    hi = bisect_right(call_starts, end)
    return [n for n in all_call_nodes[lo:hi] if n.end_byte <= end]


def calls_in_node(
    all_call_nodes: list[Node], call_starts: list[int], container: Node
) -> list[Node]:
    """The calls `container`'s span holds, out of the file's sorted calls."""
    start = container.start_byte
    end = container.end_byte
    # A Dart definition is a signature node whose body is a SIBLING;
    # widen to the body's end or every body call escapes its caller.
    if container.type in cs.DART_SIGNATURE_TYPES:
        end = dart_utils.definition_end_byte(container)
    return _calls_in_span(all_call_nodes, call_starts, start, end)


def top_level_calls(
    all_call_nodes: list[Node],
    call_starts: list[int],
    func_nodes: list[Node],
    module_qn: str,
    julia: RegisteredNames,
) -> list[Node]:
    """The calls the module owns: every call outside the bodies of
    `func_nodes`."""
    # Calls inside a function's BODY belong to that function, not the
    # module; only genuine top-level calls are module-attributed. The body
    # (not the whole node) is the boundary so def-time calls in the
    # signature (default args like `def f(x=make_default())` and
    # decorators) run at module load and stay module-attributed. A node
    # with no body is not a real function scope (e.g. a file-scope
    # declaration `int x = top();` captured as a function); its calls run
    # at load time, so it excludes nothing.
    nested_starts: set[int] = set()
    for func_node in func_nodes:
        # A Dart constructor's `: this(...)` / `: super(...)` / field
        # initializer clauses run with it, outside its body (issue #2482).
        for clause in dart_utils.constructor_clauses(func_node):
            nested_starts.update(
                call.start_byte
                for call in calls_in_node(all_call_nodes, call_starts, clause)
            )
        nested_starts.update(
            call.start_byte
            for call in _body_calls(
                all_call_nodes, call_starts, func_node, module_qn, julia
            )
        )
    return [c for c in all_call_nodes if c.start_byte not in nested_starts]


def _body_calls(
    all_call_nodes: list[Node],
    call_starts: list[int],
    func_node: Node,
    module_qn: str,
    julia: RegisteredNames,
) -> list[Node]:
    # The calls a function's body holds, which run when it is called rather
    # than at module load; none for a node without a body.
    if (body := _definition_body(func_node)) is not None:
        return calls_in_node(all_call_nodes, call_starts, body)
    if func_node.type in _JULIA_SIGNATURE_DEFINITIONS:
        # No body field: the body is everything after the signature.
        # A head the naming pass refuses owns no body: its calls
        # bubble to the module (issue #1882).
        if func_node.named_children and julia_utils.function_head_name(func_node):
            return _calls_in_span(
                all_call_nodes,
                call_starts,
                func_node.named_children[0].end_byte,
                func_node.end_byte,
            )
        return []
    if func_node.type in _JULIA_RHS_DEFINITIONS and len(func_node.named_children) >= 2:
        # The RHS is the body; an anonymous arrow's calls stay
        # module-attributed or they drop entirely.
        if julia.registered_name(func_node, module_qn) is None:
            return []
        return calls_in_node(all_call_nodes, call_starts, func_node.named_children[-1])
    return []


def _definition_body(func_node: Node) -> Node | None:
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if body is None and func_node.type == cs.TS_SQL_CREATE_FUNCTION:
        # a SQL routine's body is an unnamed `function_body` child;
        # missing it kept every body call module-attributed as well
        body = next(
            (
                child
                for child in func_node.named_children
                if child.type == cs.TS_SQL_FUNCTION_BODY
            ),
            None,
        )
    if body is None:
        # a Dart body is a SIBLING of its signature, not a field
        body = dart_utils.body_node(func_node)
    return body


def calls_owned_by(
    func_node: Node,
    sibling_func_nodes: list[Node],
    all_call_nodes: list[Node],
    call_starts: list[int],
) -> list[Node]:
    """The calls inside `func_node`, less those a function nested in it owns."""
    # Calls inside func_node MINUS calls owned by functions nested within
    # it, so a call in a nested function is attributed only to the nested
    # function, never also to the enclosing one.
    own = calls_in_node(all_call_nodes, call_starts, func_node)
    # The siblings come sorted by (start, end) (`sorted_captures`), so the
    # ones starting inside func_node are one slice. Scanning them all for
    # every function made a class of N methods cost O(N^2) (issue #2453).
    start, end = func_node.start_byte, func_node.end_byte
    lo = bisect_left(sibling_func_nodes, start, key=_start_byte)
    hi = bisect_right(sibling_func_nodes, end, lo=lo, key=_start_byte)
    descendant_bodies = [
        span
        for n in sibling_func_nodes[lo:hi]
        if n is not func_node
        and n.end_byte <= end
        and (span := _func_body_span(n)) is not None
    ]
    if not descendant_bodies:
        return own
    return [
        call
        for call in own
        if not any(
            body_start <= call.start_byte and call.end_byte <= body_end
            for body_start, body_end in descendant_bodies
        )
    ]


def _func_body_span(func_node: Node) -> tuple[int, int] | None:
    body = func_node.child_by_field_name(cs.FIELD_BODY)
    if body is not None:
        return body.start_byte, body.end_byte
    # No body field: the body is the span between the signature and the
    # trailing `end` (an arrow's body is its last named child).
    if func_node.type == cs.TS_JULIA_FUNCTION_DEFINITION:
        named = func_node.named_children
        if named and named[0].type == cs.TS_JULIA_SIGNATURE:
            end_token = next(
                (c for c in func_node.children if c.type == cs.TS_JULIA_END_KEYWORD),
                None,
            )
            if end_token is not None:
                return named[0].end_byte, end_token.start_byte
    if func_node.type == cs.TS_JULIA_ARROW_FUNCTION_EXPRESSION:
        # An arrow has two named children (parameter, body); the body is last.
        named = func_node.named_children
        if len(named) >= 2:
            body = named[-1]
            return body.start_byte, body.end_byte
    if (
        func_node.type == cs.TS_JULIA_ASSIGNMENT
        and func_node.named_children
        and julia_utils.function_head_name(func_node) is not None
    ):
        # A concise method `f(x) = ...` (also `f(x)::Int`,
        # `f(x) where T`): the right side is the body.
        body = func_node.named_children[-1]
        return body.start_byte, body.end_byte
    return None


def body_function_nodes(
    body_node: Node,
    function_query: Query | None,
    sorted_func_nodes: list[Node] | None,
    func_node_starts: list[int] | None,
) -> list[Node] | None:
    """The function nodes inside a class body; None when the language has no
    function query at all."""
    # Sliced from the pre-sorted capture list when the caller has one, else
    # queried directly.
    if sorted_func_nodes is not None and func_node_starts is not None:
        body_end = body_node.end_byte
        lo = bisect_left(func_node_starts, body_node.start_byte)
        hi = bisect_right(func_node_starts, body_end)
        return [n for n in sorted_func_nodes[lo:hi] if n.end_byte <= body_end]
    if not function_query:
        return None
    captures = sorted_captures(QueryCursor(function_query), body_node)
    return captures.get(cs.CAPTURE_FUNCTION, [])
