# Three-verdict flow reachability (issue #1050). An empty flow result is
# ambiguous: "no flow exists" and "the flow sits outside what the analysis
# covers" look identical, and for assurance questions an absent path must
# never read as a PASS. Reachability runs CLIENT-side over two linear scans,
# the same discipline as dead_code.py: a *BFS expansion inside memgraph is
# what hit the 600s timeout there.
from collections import deque
from typing import NamedTuple, Protocol

from . import constants as cs
from .types_defs import PropertyDict, ResultRow

FLOW_VERDICT_FOUND = "FOUND"
FLOW_VERDICT_NO_FLOW = "NO_FLOW"
FLOW_VERDICT_UNKNOWN = "UNKNOWN"

# Either endpoint may anchor the edge to the project: FLOWS_TO sources can
# be Resource nodes whose qns carry their own scheme, and dropping their
# edges would hide resource-originated flows from the scan.
CYPHER_FLOW_EDGES = f"""MATCH (a)-[:{cs.RelationshipType.FLOWS_TO.value}]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
   OR b.qualified_name STARTS WITH $project_prefix
   OR a.qualified_name = $project_name
   OR b.qualified_name = $project_name
RETURN a.qualified_name AS source, b.qualified_name AS target
"""

# Inline `mod` blocks mint Module nodes with synthetic inline paths and no
# coverage property of their own; their coverage IS their file module's, so
# they are excluded rather than reported as spurious gaps.
# The bare project qn is a real module too (a repository-root __init__.py
# or root-level mod.rs maps to it), so equality joins the prefix filter.
CYPHER_FLOW_COVERAGE_GAPS = f"""MATCH (m:{cs.NodeLabel.MODULE.value})
WHERE (m.qualified_name STARTS WITH $project_prefix
   OR m.qualified_name = $project_name)
  AND coalesce(m.{cs.KEY_FLOW_COVERED}, false) = false
  AND NOT m.path STARTS WITH '{cs.INLINE_MODULE_PATH_PREFIX}'
RETURN m.path AS path
ORDER BY path
"""


# The remote hop (issue #1603): a client's NETWORK resource resolves to the
# ENDPOINT a handler exposes, and an RPC or dispatch resource is exposed by
# its handler directly, so a flow that reaches the resource continues into
# the handler -- in whatever project it lives. Graph-wide by design: the edge
# exists to cross projects. The handler's project is read off its qualified
# name (only ENDPOINT resources carry a `project` property; RPC and dispatch
# ones do not, local review), so its own FLOWS_TO edges can be loaded.
CYPHER_FLOW_REMOTE_EDGES = f"""MATCH (n:{cs.NodeLabel.RESOURCE.value} {{kind: 'NETWORK'}})-[:{cs.RelationshipType.RESOLVES_TO.value}]->(e:{cs.NodeLabel.RESOURCE.value})<-[:{cs.RelationshipType.EXPOSES.value}]-(h)
RETURN n.qualified_name AS source, h.qualified_name AS target
UNION
MATCH (e:{cs.NodeLabel.RESOURCE.value})<-[:{cs.RelationshipType.EXPOSES.value}]-(h)
WHERE e.kind IN ['RPC', 'DISPATCH']
RETURN e.qualified_name AS source, h.qualified_name AS target
"""


class QueryFn(Protocol):
    def __call__(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]: ...


class FlowVerdict(NamedTuple):
    """The answer to "can data flow from source to sink?".

    FOUND carries the qn path. UNKNOWN means no path was found but part of
    the project sat outside flow-analysis coverage, and `gaps` names those
    files; treating it as NO_FLOW would read an analysis ceiling as a
    verified absence. The coverage read is deliberately project-wide rather
    than reachable-surface-only: without path sensitivity, a flow through an
    uncovered file cannot be ruled out from the covered part of the graph.
    The same holds for every project the walk entered through a service
    boundary (issue #1603): its gaps count too.
    """

    verdict: str
    path: tuple[str, ...]
    gaps: tuple[str, ...]
    # The (from, to) pairs on the path that cross a service boundary: a
    # resource in one project continuing into a handler, usually of another
    # (issue #1603). Empty for a path inside one service.
    remote_hops: tuple[tuple[str, str], ...] = ()


def flow_reachability_verdict(
    fetch_all: QueryFn,
    project_name: str,
    source_qn: str,
    sink_qn: str,
) -> FlowVerdict:
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    params = {
        cs.KEY_PROJECT_PREFIX: prefix,
        cs.KEY_PROJECT_NAME: project_name,
    }
    edges: dict[str, list[str]] = {}
    _add_edges(edges, fetch_all(CYPHER_FLOW_EDGES, params))
    # Resource-to-handler hops join the graph up front (one read of edges
    # only); a handler project's own flow edges load when the walk reaches
    # a hop into it, and not before, so a local verdict never reads an
    # unrelated project (issue #1603; bot review on PR #1978).
    remote: dict[str, set[str]] = {}
    for row in fetch_all(CYPHER_FLOW_REMOTE_EDGES, None):
        source, target = row.get("source"), row.get("target")
        if isinstance(source, str) and isinstance(target, str):
            edges.setdefault(source, []).append(target)
            remote.setdefault(source, set()).add(target)
    loaded = {project_name}
    while (
        entered := {
            _project_of(target)
            for resource in _reachable(edges, source_qn)
            for target in remote.get(resource, ())
        }
        - loaded
    ):
        for other in sorted(entered):
            _add_edges(
                edges,
                fetch_all(
                    CYPHER_FLOW_EDGES,
                    {
                        cs.KEY_PROJECT_PREFIX: f"{other}{cs.SEPARATOR_DOT}",
                        cs.KEY_PROJECT_NAME: other,
                    },
                ),
            )
        loaded |= entered

    if path := _bfs_path(edges, source_qn, sink_qn):
        # A hop is remote when the handler's project differs from the one
        # the walk was in before the resource: an HTTP call a service makes
        # to itself is not a service boundary.
        hops = tuple(
            (resource, handler)
            for i, (resource, handler) in enumerate(zip(path, path[1:], strict=False))
            if handler in remote.get(resource, ())
            and _project_of(handler)
            != (_project_of(path[i - 1]) if i else project_name)
        )
        return FlowVerdict(FLOW_VERDICT_FOUND, tuple(path), (), hops)

    # Coverage of every project the walk entered: an uncovered module on
    # the far side of a boundary can hold the continuation just as one on
    # this side can.
    gaps: set[str] = set()
    for project in sorted(loaded):
        gap_rows = fetch_all(
            CYPHER_FLOW_COVERAGE_GAPS,
            {
                cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
                cs.KEY_PROJECT_NAME: project,
            },
        )
        gaps.update(
            path for row in gap_rows if isinstance(path := row.get(cs.KEY_PATH), str)
        )
    if gaps:
        return FlowVerdict(FLOW_VERDICT_UNKNOWN, (), tuple(sorted(gaps)))
    return FlowVerdict(FLOW_VERDICT_NO_FLOW, (), ())


def _project_of(qn: str) -> str:
    return qn.split(cs.SEPARATOR_DOT, 1)[0]


def _reachable(edges: dict[str, list[str]], source_qn: str) -> set[str]:
    seen = {source_qn}
    queue: deque[str] = deque([source_qn])
    while queue:
        for target in edges.get(queue.popleft(), ()):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _add_edges(edges: dict[str, list[str]], rows: list[ResultRow]) -> None:
    for row in rows:
        source, target = row.get("source"), row.get("target")
        if isinstance(source, str) and isinstance(target, str):
            edges.setdefault(source, []).append(target)


def _bfs_path(
    edges: dict[str, list[str]], source_qn: str, sink_qn: str
) -> list[str] | None:
    # The sink test precedes the seen check so a path is always at least one
    # real edge: equal source and sink report FOUND only through a genuine
    # cycle, never by name equality alone.
    parent: dict[str, str] = {}
    queue: deque[str] = deque([source_qn])
    seen = {source_qn}
    while queue:
        current = queue.popleft()
        for target in edges.get(current, ()):
            if target == sink_qn:
                chain = [current]
                while chain[-1] != source_qn:
                    chain.append(parent[chain[-1]])
                chain.reverse()
                return [*chain, target]
            if target in seen:
                continue
            parent[target] = current
            seen.add(target)
            queue.append(target)
    return None
