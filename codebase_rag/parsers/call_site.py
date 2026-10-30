"""One request to `CallResolver.resolve_function_call`, as its stages read it,
and how its call name splits: into the hops of a receiver chain, and down to
the last segment the simple-name fallback matches on.

The resolver builds one per call it resolves and hands it whole to each
stage, its own and those of the language resolvers it builds (`RustResolver`
in `parsers/rust/resolution.py`, and so on), rather than eight loose
arguments."""

import re
from functools import lru_cache
from typing import NamedTuple

from .. import constants as cs

SEPARATOR_PATTERN = re.compile(r"[.:]|::")
_CHAIN_OPEN_BRACKETS = "([{"
_CHAIN_CLOSE_BRACKETS = ")]}"


class CallSite(NamedTuple):
    """One `_resolve_function_call` request, handed whole to its phases."""

    call_name: str
    module_qn: str
    local_var_types: dict[str, str] | None
    class_context: str | None
    caller_qn: str | None
    language: cs.SupportedLanguage | None
    call_point: int | None
    constructing: bool


@lru_cache(maxsize=cs.RESOLVER_NAME_CACHE_SIZE)
def trie_search_name(call_name: str) -> str:
    # The last name segment, the only part the simple-name fallback matches on.
    return SEPARATOR_PATTERN.split(call_name)[-1]


def split_receiver_chain(expr: str) -> list[str] | None:
    # Split a receiver chain (`c.Find(1.5).Root`) on the `.` separators between
    # hops only, never on a `.` inside call arguments, an index, or a generic
    # (`1.5`, `x.y` args, `List<A.B>`), which a naive str.split would mangle.
    # None for a chain of more than MAX_RECEIVER_CHAIN_HOPS hops, found without
    # scanning past them: each call in a chain splits its own receiver, so an
    # unbounded split made one long chain quadratic (#2262).
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in expr:
        if char in _CHAIN_OPEN_BRACKETS:
            depth += 1
        elif char in _CHAIN_CLOSE_BRACKETS:
            depth = max(0, depth - 1)
        if char == cs.SEPARATOR_DOT and depth == 0:
            parts.append("".join(current))
            if len(parts) >= cs.MAX_RECEIVER_CHAIN_HOPS:
                return None
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts
