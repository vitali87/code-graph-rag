# Three-verdict flow reachability (issue #1050). An empty flow result is
# ambiguous: "no flow exists" and "the flow sits outside what the analysis
# covers" look identical, and for assurance questions an absent path must
# never read as a PASS. Reachability runs CLIENT-side over two linear scans,
# the same discipline as dead_code.py: a *BFS expansion inside memgraph is
# what hit the 600s timeout there.
from collections import deque
from typing import NamedTuple, Protocol

from . import constants as cs
from .cypher_queries import CYPHER_LIST_PROJECTS
from .types_defs import PropertyParams, ResultRow

FLOW_VERDICT_FOUND = "FOUND"
FLOW_VERDICT_NO_FLOW = "NO_FLOW"
FLOW_VERDICT_UNKNOWN = "UNKNOWN"

# Either endpoint may anchor a code edge to the project. A resource-to-
# resource flow (`ENV::K -> STDOUT`) has two `resource::` endpoints, so it is
# anchored by the `scope` it records instead: the project's function whose
# body produced it (issue #2747). That function is also linked to the sink
# its value reached, so a question asked from code (`notify -> STDOUT`, or a
# client function through its NETWORK resource into another service's
# handler) reaches the resource. The source side gets no such link: a
# function that reads two resources would join every source to every sink
# it writes.
# The prefix also matches a dotted sibling project (`p.` matches `p.v2.fn`),
# so the walk keeps only the resource links the project owns exactly: a
# resource-to-resource row by its scope, a function's link to a resource by
# the function (bot review on PR #2762). A code-to-code row is kept as main
# kept it: a function's qn names it wherever the row loads.
CYPHER_FLOW_EDGES = f"""MATCH (a)-[:{cs.RelationshipType.FLOWS_TO.value}]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
   OR b.qualified_name STARTS WITH $project_prefix
   OR a.qualified_name = $project_name
   OR b.qualified_name = $project_name
RETURN a.qualified_name AS source, b.qualified_name AS target, NULL AS scope
UNION
MATCH (a:{cs.NodeLabel.RESOURCE.value})-[r:{cs.RelationshipType.FLOWS_TO.value}]->(b:{cs.NodeLabel.RESOURCE.value})
WHERE r.scope STARTS WITH $project_prefix OR r.scope = $project_name
RETURN a.qualified_name AS source, b.qualified_name AS target, r.scope AS scope
UNION
MATCH (:{cs.NodeLabel.RESOURCE.value})-[r:{cs.RelationshipType.FLOWS_TO.value}]->(b:{cs.NodeLabel.RESOURCE.value})
WHERE r.scope STARTS WITH $project_prefix OR r.scope = $project_name
RETURN r.scope AS source, b.qualified_name AS target, NULL AS scope
"""

# A function also reaches every resource its own body writes. A resource
# flow belongs to the function that read the value, so `send(v)` printing
# the `v` its caller read owns none, and a literal write (`print("hi")`)
# makes no flow at all; without these edges both answered NO_FLOW with no
# gaps (bot review on PR #2762). A write records no value, so which walks
# take them depends on the question (_FlowGraph.writes). Read apart from
# CYPHER_FLOW_EDGES, which explain_traceback reads too: a write is no
# FLOWS_TO evidence there, and a project with writes but no flows must keep
# reporting `flow_used` false.
CYPHER_FLOW_WRITE_EDGES = f"""MATCH (f)-[:{cs.RelationshipType.WRITES_TO.value}]->(b:{cs.NodeLabel.RESOURCE.value})
WHERE f.qualified_name STARTS WITH $project_prefix
   OR f.qualified_name = $project_name
RETURN DISTINCT f.qualified_name AS source, b.qualified_name AS target
"""

_RESOURCE_QN_PREFIX = cs.RESOURCE_QN_FORMAT.partition("{")[0]

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


class _FlowGraph(NamedTuple):
    # Edges any walk may take: code flows, and the hops from a resource into
    # the handler exposing it.
    shared: dict[str, list[str]]
    # Resource-to-resource flows, by the project whose code produced them.
    # Resource names carry no project (`resource::ENV::TOKEN` of one service
    # and of another are one node), so the walk takes such an edge only
    # inside the project it is in (bot review on PR #2762).
    owned: dict[tuple[str, str], list[str]]
    # A function's links to the resources it wrote: its own writes, and the
    # sinks of the resource flows it produced. Neither records which value
    # was written. A question with a resource at either end takes them only
    # from its source: a handler that prints "ok" also writes STDOUT, and a
    # value sent to it reached that sink only by name (CodeRabbit review on
    # PR #2762). A question between two functions keeps main's walk, which
    # goes on from every function it reaches, through a NETWORK resource it
    # writes into the handler behind it (Greptile review on PR #2762).
    writes: dict[str, list[str]]


class QueryFn(Protocol):
    def __call__(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]: ...


class FlowVerdict(NamedTuple):
    """The answer to "can data flow from source to sink?".

    FOUND carries the qn path. UNKNOWN means no path was found but one
    cannot be ruled out, and `gaps` says where: the files outside flow-
    analysis coverage, and, for a question with a resource at either end,
    the functions past which the walk cannot follow the value (see
    _unfollowed). Treating it as NO_FLOW would read an analysis ceiling as
    a verified absence. The coverage read is deliberately project-wide
    rather than reachable-surface-only: without path sensitivity, a flow
    through an uncovered file cannot be ruled out from the covered part of
    the graph. The same holds for every project the walk entered through a
    service boundary (issue #1603): its gaps count too.
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
    projects = _registered_projects(fetch_all, project_name)
    graph = _FlowGraph({}, {}, {})
    every_write = _between_functions(source_qn, sink_qn)
    _load_project(graph, fetch_all, project_name, projects, every_write)
    # Resource-to-handler hops join the graph up front (one read of edges
    # only); a handler project's own flow edges load when the walk reaches
    # a hop into it, and not before, so a local verdict never reads an
    # unrelated project (issue #1603; bot review on PR #1978).
    remote: dict[str, set[str]] = {}
    for row in fetch_all(CYPHER_FLOW_REMOTE_EDGES, None):
        source, target = row.get("source"), row.get("target")
        if isinstance(source, str) and isinstance(target, str):
            graph.shared.setdefault(source, []).append(target)
            remote.setdefault(source, set()).add(target)
    loaded = {project_name}
    while True:
        reached = _reachable(graph, source_qn, sink_qn, project_name, projects)
        entered = {
            _project_of(target, projects)
            for resource in reached
            for target in remote.get(resource, ())
        } - loaded
        if not entered:
            break
        for other in sorted(entered):
            _load_project(graph, fetch_all, other, projects, every_write)
        loaded |= entered

    if path := _bfs_path(graph, source_qn, sink_qn, project_name, projects):
        # A hop is remote when the handler's project differs from the one
        # the walk was in before the resource: an HTTP call a service makes
        # to itself is not a service boundary.
        hops = tuple(
            (resource, handler)
            for i, (resource, handler) in enumerate(zip(path, path[1:], strict=False))
            if handler in remote.get(resource, ())
            and _project_of(handler, projects)
            != (_project_of(path[i - 1], projects) if i else project_name)
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
    gaps |= _unfollowed(graph, reached, remote, source_qn, sink_qn, projects)
    if gaps:
        return FlowVerdict(FLOW_VERDICT_UNKNOWN, (), tuple(sorted(gaps)))
    return FlowVerdict(FLOW_VERDICT_NO_FLOW, (), ())


def _registered_projects(fetch_all: QueryFn, project_name: str) -> tuple[str, ...]:
    """Every project the graph holds, longest name first. Degrades to this
    project alone when the registry cannot be read."""
    names: set[str] = {project_name}
    try:
        rows = fetch_all(CYPHER_LIST_PROJECTS, None)
    except Exception:
        rows = []
    names.update(
        name for row in rows if isinstance(name := row.get(cs.KEY_NAME), str) and name
    )
    return tuple(sorted(names, key=len, reverse=True))


def _project_of(qn: str, projects: tuple[str, ...]) -> str:
    # The LONGEST registered name the qn sits under: project names may
    # contain dots, so the first segment of `svc.v2.api.items` is `svc`, a
    # different project (#1970). A qn no registered project owns falls back
    # to its first segment, as before (bot review).
    for name in projects:
        if qn == name or qn.startswith(f"{name}{cs.SEPARATOR_DOT}"):
            return name
    return qn.split(cs.SEPARATOR_DOT, 1)[0]


def _is_resource(qn: str) -> bool:
    return qn.startswith(_RESOURCE_QN_PREFIX)


def _between_functions(source_qn: str, sink_qn: str) -> bool:
    # The question shape main answers. It keeps main's walk, which takes
    # every edge of every node it reaches.
    return not (_is_resource(source_qn) or _is_resource(sink_qn))


def _unfollowed(
    graph: _FlowGraph,
    reached: set[str],
    remote: dict[str, set[str]],
    source_qn: str,
    sink_qn: str,
    projects: tuple[str, ...],
) -> set[str]:
    """The functions past which a walk with no path cannot rule the value
    out (CodeRabbit review on PR #2762). A code-to-code question has none:
    it keeps the function-level walk it always had."""
    if _between_functions(source_qn, sink_qn):
        return set()
    # A handler takes the value as its request, and no edge records where a
    # request goes on to.
    unfollowed = {handler for qn in reached for handler in remote.get(qn, ())}
    if _is_resource(sink_qn):
        # A function other than the source may write the value or something
        # else, and no edge says which. It is named when what it writes can
        # go on to the sink: the sink itself, a resource whose own flows lead
        # there, or a handler (Greptile review on PR #2762).
        onward: dict[tuple[str, str], bool] = {}
        unfollowed |= {
            qn
            for qn in reached
            if qn != source_qn
            and any(
                _goes_on(
                    graph,
                    (target, _project_of(qn, projects)),
                    sink_qn,
                    projects,
                    onward,
                )
                for target in graph.writes.get(qn, ())
            )
        }
    else:
        # A resource asked about a function: no edge links a resource to the
        # code that reads it (a reader would join every source it reads to
        # every sink it writes), so its absence proves nothing.
        unfollowed.add(sink_qn)
    return unfollowed


def _goes_on(
    graph: _FlowGraph,
    start: tuple[str, str],
    sink_qn: str,
    projects: tuple[str, ...],
    memo: dict[tuple[str, str], bool],
) -> bool:
    # Whether a value written to a resource can reach the sink along
    # resource flows, or enter code (a handler), past which no edge says
    # where it goes.
    if start not in memo:
        seen = {start}
        queue: deque[tuple[str, str]] = deque([start])
        found = start[0] == sink_qn
        while queue and not found:
            for state in _resource_successors(graph, *queue.popleft(), projects):
                if state[0] == sink_qn or not _is_resource(state[0]):
                    found = True
                    break
                if state not in seen:
                    seen.add(state)
                    queue.append(state)
        memo[start] = found
    return memo[start]


def _resource_successors(
    graph: _FlowGraph, qn: str, project: str, projects: tuple[str, ...]
) -> list[tuple[str, str]]:
    # A resource stays in the project of the code that reached it, or of
    # the walk when another resource did.
    out = [
        (target, project if _is_resource(target) else _project_of(target, projects))
        for target in graph.shared.get(qn, ())
    ]
    return out + [(target, project) for target in graph.owned.get((project, qn), ())]


def _successors(
    graph: _FlowGraph,
    qn: str,
    project: str,
    projects: tuple[str, ...],
    source_qn: str,
    sink_qn: str,
) -> list[tuple[str, str]]:
    # (next node, the project the walk is in there): a code node is in its
    # own project.
    if _is_resource(qn):
        return _resource_successors(graph, qn, project, projects)
    # A walk from a resource follows that resource's value, and once the
    # value is in code (the handler it was sent to) no edge says where it
    # goes: the handler's own flows and writes carry other values.
    if _is_resource(source_qn):
        return []
    here = _project_of(qn, projects)
    out = [
        (target, here if _is_resource(target) else _project_of(target, projects))
        for target in graph.shared.get(qn, ())
    ]
    if qn == source_qn or _between_functions(source_qn, sink_qn):
        out += [(target, here) for target in graph.writes.get(qn, ())]
    return out


def _reachable(
    graph: _FlowGraph,
    source_qn: str,
    sink_qn: str,
    project: str,
    projects: tuple[str, ...],
) -> set[str]:
    start = (source_qn, project)
    seen = {start}
    queue: deque[tuple[str, str]] = deque([start])
    while queue:
        current = queue.popleft()
        for state in _successors(graph, *current, projects, source_qn, sink_qn):
            if state not in seen:
                seen.add(state)
                queue.append(state)
    return {qn for qn, _project in seen}


def _load_project(
    graph: _FlowGraph,
    fetch_all: QueryFn,
    project: str,
    projects: tuple[str, ...],
    every_write: bool,
) -> None:
    params = {
        cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
        cs.KEY_PROJECT_NAME: project,
    }
    for query in (CYPHER_FLOW_EDGES, CYPHER_FLOW_WRITE_EDGES):
        _add_edges(graph, fetch_all(query, params), project, projects, every_write)


def _add_edges(
    graph: _FlowGraph,
    rows: list[ResultRow],
    project: str,
    projects: tuple[str, ...],
    every_write: bool,
) -> None:
    # Only the resource links `project` owns exactly: a resource-to-resource
    # flow whose scope is in it, and, for a question with a resource end, a
    # write of a function in it. A function's link to a resource is a write,
    # whichever query read it. A question between two functions takes every
    # row the prefix read returns, code edge or write, as main took them.
    for row in rows:
        source, target = row.get("source"), row.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue
        scope = row.get("scope")
        if isinstance(scope, str):
            if _project_of(scope, projects) == project:
                graph.owned.setdefault((project, source), []).append(target)
        elif not _is_resource(target) or _is_resource(source):
            graph.shared.setdefault(source, []).append(target)
        elif every_write or _project_of(source, projects) == project:
            graph.writes.setdefault(source, []).append(target)


def _bfs_path(
    graph: _FlowGraph,
    source_qn: str,
    sink_qn: str,
    project: str,
    projects: tuple[str, ...],
) -> list[str] | None:
    # The sink test precedes the seen check so a path is always at least one
    # real edge: equal source and sink report FOUND only through a genuine
    # cycle, never by name equality alone.
    start = (source_qn, project)
    parent: dict[tuple[str, str], tuple[str, str]] = {}
    queue: deque[tuple[str, str]] = deque([start])
    seen = {start}
    while queue:
        current = queue.popleft()
        for state in _successors(graph, *current, projects, source_qn, sink_qn):
            if state[0] == sink_qn:
                chain = [current]
                while chain[-1] != start:
                    chain.append(parent[chain[-1]])
                chain.reverse()
                return [qn for qn, _project in chain] + [sink_qn]
            if state in seen:
                continue
            parent[state] = current
            seen.add(state)
            queue.append(state)
    return None
