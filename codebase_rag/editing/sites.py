"""Graph-site helpers the edit operations share.

`rename` (issue #1532) and `change_signature` (issue #1533) both walk a
method's override hierarchy and both need the call node a recorded site
refers to; keeping one copy means the two operations cannot disagree about
which call a `(line, col)` names.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from tree_sitter import Node

from .. import constants as cs
from .. import cypher_queries as cq
from .. import graph_query
from ..graph_query import QueryFn
from ..parsers.utils import call_site_start

# Resolutions that bound a site by guesswork: an operation rewrites through
# them only when the caller accepts the risk with `allow_heuristic`.
AMBIGUOUS = frozenset(
    {
        cs.EdgeResolution.HEURISTIC.value,
        cs.EdgeResolution.OVERLOAD.value,
        cs.EdgeResolution.DYNAMIC.value,
    }
)


class Hierarchy(NamedTuple):
    """A method's override hierarchy: `qn` first, then every method it
    overrides or is overridden by, transitively. `guessed` holds the members
    no chain of OVERRIDES edges free of guesses reaches from `qn`, as for a
    Go method matched to an interface's by name alone (issue #2483): their
    declarations are rewritten on a guess, which takes the same opt-in as a
    guessed call site."""

    members: list[str]
    guessed: frozenset[str]


def _no_links(_qn: str) -> list[str]:
    return []


def hierarchy(
    fetch_all: QueryFn,
    project: str,
    qn: str,
    linked: Callable[[str], list[str]] = _no_links,
) -> Hierarchy:
    # Each edge keeps its own confidence. Asking per member whether all of
    # its edges were guesses missed two hierarchies a guess joins: every
    # member had a proven edge of its own, and each was renamed unasked.
    # `linked` names the members another way certainly joins (each build's
    # declaration of one Go method).
    owns = graph_query._owner_check(fetch_all, project)
    edges: dict[str, list[tuple[str, bool]]] = {}
    # `found` keeps the members in the order reached, `seen` answers
    # membership: a list alone was quadratic in a large hierarchy.
    found: list[str] = [qn]
    seen = {qn}
    frontier = [qn]
    while frontier:
        current = frontier.pop()
        edges[current] = _override_edges(fetch_all, project, current, owns) + [
            (other, False) for other in linked(current)
        ]
        for other, _guessed in edges[current]:
            if other not in seen:
                seen.add(other)
                found.append(other)
                frontier.append(other)
    return Hierarchy(found, frozenset(found) - _proven_reach(qn, edges))


def _override_edges(
    fetch_all: QueryFn, project: str, qn: str, owns: Callable[[str], bool]
) -> list[tuple[str, bool]]:
    # (method, whether a guess ties it to `qn`) for each OVERRIDES edge.
    rows = fetch_all(
        cq.CYPHER_GRAPH_OVERRIDES,
        {cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}", cs.KEY_QN: qn},
    )
    return sorted(
        (other, row.get(cs.KEY_RESOLUTION) in AMBIGUOUS)
        for row in rows
        if owns(other := str(row.get(cs.KEY_QUALIFIED_NAME) or ""))
    )


def _proven_reach(qn: str, edges: dict[str, list[tuple[str, bool]]]) -> set[str]:
    reached = {qn}
    frontier = [qn]
    while frontier:
        for other, guessed in edges.get(frontier.pop(), []):
            if not guessed and other not in reached:
                reached.add(other)
                frontier.append(other)
    return reached


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
    # A member call's site starts at its name (issue #3166); a graph indexed
    # before that recorded the call node's own start, so both name the call.
    calls: list[Node] = []
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        if node.child_by_field_name(cs.FIELD_FUNCTION) is not None and (
            node.start_point == (row, col) or call_site_start(node) == (row, col)
        ):
            calls.append(node)
        if node.start_point[0] <= row <= node.end_point[0]:
            stack.extend(node.children)
    return calls
