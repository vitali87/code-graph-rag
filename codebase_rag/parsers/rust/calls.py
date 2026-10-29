"""Rust's share of the call pass: the callee a call inside a macro's token
stream spells, which the grammar leaves as loose tokens rather than a call
expression.

`CallProcessor` owns what every language shares (the scan of a caller's call
nodes, the site and resolution each edge carries, the name-based resolver)
and names a macro-internal call through `macro_token_call_name`."""

from __future__ import annotations

from tree_sitter import Node

from ... import constants as cs
from . import utils as rs_utils


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
