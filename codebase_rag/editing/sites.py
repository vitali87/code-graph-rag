"""Graph-site helpers the edit operations share.

`rename` (issue #1532) and `change_signature` (issue #1533) both walk a
method's override hierarchy and both need the call node a recorded site
refers to; keeping one copy means the two operations cannot disagree about
which call a `(line, col)` names.
"""

from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .. import graph_query
from ..graph_query import QueryFn

# Resolutions that bound a site by guesswork: an operation rewrites through
# them only when the caller accepts the risk with `allow_heuristic`.
AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)


def hierarchy(fetch_all: QueryFn, project: str, qn: str) -> list[str]:
    """`qn` plus every method it overrides or is overridden by, transitively."""
    seen: list[str] = [qn]
    frontier = [qn]
    while frontier:
        current = frontier.pop()
        for row in graph_query.overrides(fetch_all, project, current):
            other = row["qualified_name"]
            if other not in seen:
                seen.append(other)
                frontier.append(other)
    return seen


def call_node_at(
    root: Node, line: int, col: int, recorded_end: tuple[int, int] | None
) -> Node | None:
    """The call node at (line, col) that the graph site refers to.

    Several calls can share a start point -- `helper(helper(1))`,
    `helper(2).upper()`, and both links of `obj.helper(1).helper(2)` -- so the
    right one is the call ending where the site recorded its end, or the
    outermost when no end was recorded. A recorded end that matches no call
    names nothing: the position is stale, and rewriting a neighbour that
    happens to share the start would be a guess.
    """
    calls = _calls_starting_at(root, line - 1, col)
    if recorded_end is not None:
        return next((call for call in calls if call.end_point == recorded_end), None)
    return max(calls, key=lambda call: call.end_byte, default=None)


def _calls_starting_at(root: Node, row: int, col: int) -> list[Node]:
    calls: list[Node] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if (
            node.start_point == (row, col)
            and node.child_by_field_name(cs.FIELD_FUNCTION) is not None
        ):
            calls.append(node)
        if node.start_point[0] <= row <= node.end_point[0]:
            stack.extend(node.children)
    return calls
