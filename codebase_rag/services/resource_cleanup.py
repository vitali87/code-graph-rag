"""Removal of shared Resource nodes that no code anchors anymore.

Resource nodes carry no project prefix, so a project-scoped delete or an
incremental rebuild only strips their edges and leaves the nodes behind
(dogfood finding: 129 orphaned endpoints plus stale RESOLVES_TO edges after
one ``delete-project``). A Resource is live when it reaches a non-Resource
node directly or through FLOWS_TO edges: a source resource whose only edge
is a taint flow into an anchored sink (``ENV -FLOWS_TO-> STDOUT``) must
survive. RESOLVES_TO is derived from live edges on every sync, so it
neither anchors a resource nor propagates liveness; an endpoint whose
handler is gone dies even while the URL that resolved to it stays live.

The walk runs client-side: resource components are tiny, and an undirected
variable-length Cypher match through a dense URL-to-endpoint web can
explode combinatorially.
"""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from ..constants import KEY_QUALIFIED_NAME

if TYPE_CHECKING:
    from . import QueryProtocol

CYPHER_ALL_RESOURCE_QNS = (
    "MATCH (r:Resource) RETURN r.qualified_name AS qualified_name"
)
CYPHER_ANCHORED_RESOURCE_QNS = (
    "MATCH (s)-[]->(r:Resource) WHERE NOT s:Resource "
    "RETURN DISTINCT r.qualified_name AS qualified_name "
    "UNION "
    "MATCH (r:Resource)-[]->(t) WHERE NOT t:Resource "
    "RETURN DISTINCT r.qualified_name AS qualified_name"
)
CYPHER_RESOURCE_ADJACENCY = (
    "MATCH (a:Resource)-[:FLOWS_TO]-(b:Resource) "
    "RETURN a.qualified_name AS a, b.qualified_name AS b"
)
CYPHER_DELETE_RESOURCES_BY_QN = (
    "MATCH (r:Resource) WHERE r.qualified_name IN $qns DETACH DELETE r"
)


def prune_unanchored_resources(ingestor: QueryProtocol) -> int:
    """Delete Resources whose component never reaches a code node.

    Returns the number of deleted resources.
    """
    all_qns = {
        qn
        for row in ingestor.fetch_all(CYPHER_ALL_RESOURCE_QNS)
        if isinstance(qn := row.get(KEY_QUALIFIED_NAME), str)
    }
    if not all_qns:
        return 0

    adjacency: dict[str, set[str]] = {}
    for row in ingestor.fetch_all(CYPHER_RESOURCE_ADJACENCY):
        a, b = row.get("a"), row.get("b")
        if isinstance(a, str) and isinstance(b, str):
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)

    live = {
        qn
        for row in ingestor.fetch_all(CYPHER_ANCHORED_RESOURCE_QNS)
        if isinstance(qn := row.get(KEY_QUALIFIED_NAME), str)
    }
    frontier = deque(live)
    while frontier:
        for neighbour in adjacency.get(frontier.popleft(), ()):
            if neighbour not in live:
                live.add(neighbour)
                frontier.append(neighbour)

    dead = sorted(all_qns - live)
    if dead:
        ingestor.execute_write(CYPHER_DELETE_RESOURCES_BY_QN, {"qns": dead})
    return len(dead)
