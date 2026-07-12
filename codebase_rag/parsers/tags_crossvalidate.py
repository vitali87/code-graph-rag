# (H) Diff cgr's own definitions against a tags.scm @definition.* oracle (issue #524);
# (H) pure diagnostic, no graph mutation. See crossvalidate below.

from __future__ import annotations

from tree_sitter import Query

from codebase_rag.parsers.utils import get_query_cursor, safe_decode_text
from codebase_rag.types_defs import ASTNode

_DEFINITION_PREFIX = "definition."
_NAME_CAPTURE = "name"


def crossvalidate(
    root: ASTNode, tags_query: Query, cgr_defs: set[tuple[str, int]]
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Return (missed, extra) as sets of (name, start_line).

    `cgr_defs` is the (name, 1-indexed start line) of every definition cgr already
    emitted for this file. `missed` is what tags.scm found and cgr did not; `extra`
    is what cgr emitted and tags.scm does not mark.
    """
    cursor = get_query_cursor(tags_query)
    tag_defs: set[tuple[str, int]] = set()
    for _pattern_index, caps in cursor.matches(root):
        if not any(name.startswith(_DEFINITION_PREFIX) for name in caps):
            continue
        name_nodes = caps.get(_NAME_CAPTURE)
        if not name_nodes:
            continue
        node = name_nodes[0]
        text = safe_decode_text(node)
        if text:
            tag_defs.add((text, node.start_point[0] + 1))

    return tag_defs - cgr_defs, cgr_defs - tag_defs
