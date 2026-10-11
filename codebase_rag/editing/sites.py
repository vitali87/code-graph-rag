"""Graph-site helpers the edit operations share.

`rename` (issue #1532) and `change_signature` (issue #1533) both walk a
method's override hierarchy and both need the call node a recorded site
refers to; keeping one copy means the two operations cannot disagree about
which call a `(line, col)` names.
"""

from __future__ import annotations

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
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


def only_guessed(fetch_all: QueryFn, project: str, qn: str) -> bool:
    """Whether every OVERRIDES edge tying `qn` to a hierarchy is a guess, as
    for a Go method matched to an interface's by name and arity alone (issue
    #2483). Renamed with that hierarchy, its declaration is rewritten on a
    guess, which takes the same opt-in as a guessed call site, call sites or
    none."""
    owns = graph_query._owner_check(fetch_all, project)
    rows = fetch_all(
        cq.CYPHER_GRAPH_OVERRIDES,
        {cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}", cs.KEY_QN: qn},
    )
    resolutions = [
        row.get(cs.KEY_RESOLUTION)
        for row in rows
        if owns(str(row.get(cs.KEY_QUALIFIED_NAME) or ""))
    ]
    return bool(resolutions) and all(
        resolution in AMBIGUOUS for resolution in resolutions
    )


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
    calls = calls_starting_at(root, line - 1, col)
    if recorded_end is not None:
        return next((call for call in calls if call.end_point == recorded_end), None)
    if not calls:
        return None
    return max(calls, key=lambda call: call.end_byte)


def calls_starting_at(root: Node, row: int, col: int) -> list[Node]:
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
