from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache

from tree_sitter import Language, Node, Query, QueryCursor

from . import constants as cs

_MATCH_PREDICATE = re.compile(cs.QUERY_MATCH_PREDICATE_PATTERN)


def rewrite_match_predicates(source: str) -> str:
    # py-tree-sitter evaluates `#match?` by passing the captured node's bytes to
    # `re.search` through a strict UTF-8 conversion. On an undecodable byte the
    # conversion fails with its error left pending and evaluation continues,
    # which surfaces as a SystemError or corrupts the interpreter outright.
    # Renamed, the predicate is a generic one that the binding hands to
    # `query_predicate`, which never sees bytes it cannot decode.
    return _MATCH_PREDICATE.sub(
        rf"\g<1>{cs.QUERY_PREDICATE_REWRITE_PREFIX}\g<2>", source
    )


@lru_cache(maxsize=256)
def _pattern(regex: str) -> re.Pattern[str]:
    return re.compile(regex)


def _holds(
    predicate: str,
    args: Sequence[tuple[str, str]],
    captures: Mapping[str, Sequence[Node]],
) -> bool:
    name = predicate.removeprefix(cs.QUERY_PREDICATE_REWRITE_PREFIX)
    is_any = name.startswith(cs.QUERY_PREDICATE_ANY)
    is_positive = cs.QUERY_PREDICATE_NOT not in name
    (capture, capture_kind), (regex, regex_kind) = args
    if (capture_kind, regex_kind) != (
        cs.QUERY_PREDICATE_ARG_CAPTURE,
        cs.QUERY_PREDICATE_ARG_STRING,
    ):
        return False
    pattern = _pattern(regex)
    nodes = captures.get(capture, ())
    if not nodes:
        return True
    # Same semantics as the binding's own evaluation, on text whose bad bytes
    # are replaced rather than fatal.
    results = (
        (pattern.search((node.text or b"").decode(errors="replace")) is not None)
        == is_positive
        for node in nodes
    )
    return any(results) if is_any else all(results)


def query_predicate(
    predicate: str,
    args: Sequence[tuple[str, str]],
    pattern_index: int,
    captures: Mapping[str, Sequence[Node]],
) -> bool:
    if not predicate.startswith(cs.QUERY_PREDICATE_REWRITE_PREFIX):
        # Without a callback the binding skipped every generic predicate, so a
        # predicate this module did not rename keeps that behaviour.
        return True
    # Called from inside the binding: an exception escaping here would leave
    # the same pending error behind that the rewrite exists to avoid.
    try:
        return _holds(predicate, args, captures)
    except Exception:
        return False


def compile_query(language: Language, source: str) -> Query:
    return Query(language, rewrite_match_predicates(source))


def query_captures(cursor: QueryCursor, node: Node) -> dict[str, list[Node]]:
    return cursor.captures(node, query_predicate)
