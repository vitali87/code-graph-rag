"""Rust's share of the call pass: the callee a call inside a macro's token
stream spells, which the grammar leaves as loose tokens rather than a call
expression, the callee a member call or macro invocation names, the type
an impl block's methods are attributed to, and the calls a closure leaves to
its enclosing function.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver),
names a macro-internal call through `macro_token_call_name` and hands the
rest of Rust's part to the `RustCallResolution` it builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tree_sitter import Node

from ... import constants as cs
from ...node_text import decode_node_text
from ..language_calls import LanguageCalls
from . import utils as rs_utils

if TYPE_CHECKING:
    from ..call_resolver import CallResolver


class RustCallResolution(LanguageCalls):
    """Attributes Rust callers as the definition pass registers them and
    reads the bindings a match arm or closure gives a name over its span.

    Built and owned by `CallProcessor`. The resolver is the pass's own, held
    by reference: its type inference reads the registry the definition pass
    fills."""

    __slots__ = ("_resolver",)

    # A named nested `fn` gets its own caller node and keeps its own calls.
    owns_nested_calls = True
    types_locals = True

    def __init__(self, resolver: CallResolver) -> None:
        self._resolver = resolver

    def span_bindings(
        self,
        caller_node: Node,
        module_qn: str,
        class_context: str | None,
        local_var_types: dict[str, str],
    ) -> list[tuple[int, int, str, str]]:
        return self._resolver.type_inference.collect_rust_span_bindings(
            caller_node, module_qn, class_context
        )

    def class_name(self, class_node: Node) -> str | None:
        if class_node.type == cs.TS_IMPL_ITEM:
            # Use the same bare-type extraction as the definition pass
            # (rs_utils.extract_impl_target), which strips generic arguments
            # (`Chars<'a>` -> `Chars`). A name keeping the generic text
            # attributed a call inside a generic impl block to a caller qn
            # bearing the generics (crate.lib.Chars<'a>.go) matching no
            # registered node, silently dropping the CALLS edge.
            return rs_utils.extract_impl_target(class_node)
        return super().class_name(class_node)

    def function_field_name(self, func_child: Node) -> str | None:
        if (
            func_child.type == cs.TS_RS_FIELD_EXPRESSION
            and (text := func_child.text) is not None
        ):
            # Rust member call `a.b.method()`: use the full dotted receiver
            # chain as the call name so the resolver can map the receiver to
            # its inferred type (`self.shutdown.is_shutdown`, `cmd.apply`). A
            # chain containing a call (`x.f().g`) is left to the chained-call
            # path; a paren-free chain ends at the bare-method trie fallback
            # when the receiver type is unknown.
            return decode_node_text(text)
        return None

    def node_call_name(self, call_node: Node) -> str | None:
        # `square!(3)`: the callee lives in the `macro` field (no
        # `function`/`name` field), so the invocation was captured as a
        # call but dropped nameless; unresolvable even now that
        # macro_rules! definitions register as Function nodes.
        if call_node.type == cs.TS_RS_MACRO_INVOCATION:
            macro_node = call_node.child_by_field_name(cs.FIELD_MACRO)
            if macro_node is not None and macro_node.text is not None:
                return decode_node_text(macro_node.text)
        return None

    def attributable(self, func_nodes: list[Node], module_qn: str) -> list[Node]:
        # A Rust closure (`expire.map(|_| state.next_expiration())`) is unnamed,
        # so the call loop skips it (no caller node); like JS/TS anon arrows its
        # calls must bubble to the enclosing fn/method (which holds the captured
        # locals' types) instead of being excluded and dropped. Named nested
        # `fn`s stay attributable and keep their own calls.
        return [n for n in func_nodes if n.type != cs.TS_RS_CLOSURE_EXPRESSION]


def macro_token_call_name(ident: Node) -> str | None:
    # Attribute arguments share the token_tree shape (`skip(self)` in
    # `#[instrument(skip(self))]`) but name no function; binding them
    # by bare name invented module-level CALLS (issue #2541).
    if rs_utils.in_attribute_arguments(ident):
        return None
    if not _macro_turbofish_reaches_args(ident):
        return None
    return _macro_call_name(ident)


def _macro_call_name(ident: Node) -> str | None:
    # Rebuild the callee a macro token stream spells, walking the method
    # identifier's preceding siblings, into the name the same call gets
    # outside a macro: `server . run` -> "server.run", `Config :: load` ->
    # "Config::load", `Circle :: new ( 2.0 ) . area` ->
    # "Circle::new(2.0).area". Stopping at `::` dropped `Type::assoc()` and
    # anything chained on it, since a bare method name never binds (issue
    # #2698). A method with no preceding `.` or `::` stays bare.
    if not (method := ident.text.decode(cs.ENCODING_UTF8) if ident.text else None):
        return None
    parts = [method]
    cur = ident.prev_sibling
    while cur is not None and cur.type in cs.RS_MACRO_CHAIN_SEPARATORS:
        if (receiver := _macro_receiver(cur)) is None:
            break
        text, head = receiver
        parts += [cur.type, text]
        cur = head.prev_sibling
    parts.reverse()
    return "".join(parts)


def _macro_receiver(sep: Node) -> tuple[str, Node] | None:
    # The receiver token(s) just before a `.` / `::` in a macro token
    # stream, as (text, first token): a path segment (`Config`, `Self`,
    # `crate`, `super`) before `::`; before `.`, an identifier or `self`,
    # or a call result `new ( 2.0 )` (callee plus its argument group).
    if (recv := sep.prev_sibling) is None or not recv.text:
        return None
    if sep.type == cs.TS_RS_TOKEN_SCOPE:
        if recv.type not in cs.RS_MACRO_PATH_SEGMENT_TYPES:
            return None
        return recv.text.decode(cs.ENCODING_UTF8), recv
    if recv.type in cs.RS_MACRO_RECEIVER_TYPES:
        return recv.text.decode(cs.ENCODING_UTF8), recv
    if (
        recv.type == cs.TS_RS_TOKEN_TREE
        and recv.child_count > 0
        and recv.children[0].type == cs.CHAR_PAREN_OPEN
        and (callee := recv.prev_sibling) is not None
        and callee.type == cs.TS_IDENTIFIER
        and callee.text
    ):
        return (
            callee.text.decode(cs.ENCODING_UTF8) + recv.text.decode(cs.ENCODING_UTF8),
            callee,
        )
    return None


def _macro_turbofish_reaches_args(ident: Node) -> bool:
    """Whether a macro-token identifier is a call, through any turbofish.

    `make::<u64>()` in a macro is the flat stream `make :: < u64 > ( )`, so
    the query's identifier-then-`(` shape missed it and every turbofish call
    inside `assert!`/`println!` lost its edge (issue #3212). An identifier
    followed by `::<` is a call only when the generic arguments close straight
    into an argument group; `a::<B>::c()` continues the path instead. A
    comment between them (`make::<u64> /* type */ ()`) is a token too.
    """
    scope = _next_macro_token(ident)
    if scope is None or scope.type != cs.TS_RS_TOKEN_SCOPE:
        return True
    depth = 0
    token = _next_macro_token(scope)
    while token is not None:
        depth += cs.RS_MACRO_ANGLE_DEPTH.get(token.type, 0)
        if depth <= 0:
            break
        token = _next_macro_token(token)
    if token is None or depth != 0 or (args := _next_macro_token(token)) is None:
        return False
    return (
        args.type == cs.TS_RS_TOKEN_TREE
        and args.child_count > 0
        and args.children[0].type == cs.CHAR_PAREN_OPEN
    )


def _next_macro_token(token: Node) -> Node | None:
    # The token after `token` in a macro's stream, comments skipped.
    following = token.next_sibling
    while following is not None and following.type in cs.RS_COMMENT_TYPES:
        following = following.next_sibling
    return following
