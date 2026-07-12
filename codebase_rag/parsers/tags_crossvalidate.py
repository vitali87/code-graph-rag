# (H) Diff cgr's own graph against a tags.scm oracle (issue #524); pure diagnostic,
# (H) no graph mutation. crossvalidate covers @definition.*, crossvalidate_calls @reference.call.

from __future__ import annotations

from tree_sitter import Query

from codebase_rag.parsers.utils import get_query_cursor, safe_decode_text
from codebase_rag.types_defs import ASTNode

_DEFINITION_PREFIX = "definition."
_CALL_CAPTURE = "reference.call"
_NAME_CAPTURE = "name"


def _tag_sites(
    root: ASTNode,
    tags_query: Query,
    tag_prefix: str,
    exclude_kinds: frozenset[str] = frozenset(),
) -> set[tuple[str, int]]:
    # (H) Every @name under a match carrying a capture that starts with tag_prefix,
    # (H) paired with its 1-indexed start line. Nested calls yield separate matches.
    # (H) A match whose kind suffix (e.g. "constant" from "definition.constant") is in
    # (H) exclude_kinds is skipped, so callers can drop kinds cgr does not model.
    cursor = get_query_cursor(tags_query)
    sites: set[tuple[str, int]] = set()
    for _pattern_index, caps in cursor.matches(root):
        kinds = [name for name in caps if name.startswith(tag_prefix)]
        if not kinds:
            continue
        if any(kind[len(tag_prefix) :] in exclude_kinds for kind in kinds):
            continue
        for node in caps.get(_NAME_CAPTURE, []):
            text = safe_decode_text(node)
            if text:
                sites.add((text, node.start_point[0] + 1))
    return sites


def crossvalidate(
    root: ASTNode,
    tags_query: Query,
    cgr_defs: set[tuple[str, int]],
    exclude_kinds: frozenset[str] = frozenset(),
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Return (missed, extra) as sets of (name, start_line).

    `cgr_defs` is the (name, 1-indexed start line) of every definition cgr already
    emitted for this file. `missed` is what tags.scm found and cgr did not; `extra`
    is what cgr emitted and tags.scm does not mark. `exclude_kinds` drops tags
    definition kinds cgr does not model (e.g. "constant"), so they are not false drift.
    """
    tag_defs = _tag_sites(root, tags_query, _DEFINITION_PREFIX, exclude_kinds)
    return tag_defs - cgr_defs, cgr_defs - tag_defs


def crossvalidate_calls(
    root: ASTNode, tags_query: Query, cgr_calls: set[tuple[str, int]]
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Return (missed, extra) call sites as sets of (callee_name, start_line).

    `cgr_calls` is the (callee name, 1-indexed line) of every call cgr resolved for
    this file. `missed` is what tags.scm marks and cgr did not resolve; `extra` is
    what cgr emitted and tags.scm does not mark.
    """
    tag_calls = _tag_sites(root, tags_query, _CALL_CAPTURE)
    return tag_calls - cgr_calls, cgr_calls - tag_calls
