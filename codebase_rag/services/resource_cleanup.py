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

Liveness check and delete run as one statement so a concurrent writer
cannot anchor a resource between a snapshot read and the delete.

Liveness spreads out from the anchored Resources one breadth-first level
per flow, each level holding the distinct Resources reached so far, so a
level costs the flow edges of what it holds however many paths lead there.
The variable-length match it replaced (``FLOWS_TO*0..8`` from every
Resource) gave a row per path instead, and parallel flows multiply paths:
in golang/tools the dynamic FILE resource flows into itself from eleven
call sites, so it alone starts 6.6 million paths of eight flows, each
joined to its 290 anchors, and the walk passed 11 GB (#3820).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import QueryProtocol

# Resource-to-resource flow chains are one or two hops in practice (env to
# file, file to stdout); the cap only bounds the traversal, not liveness of
# realistic graphs. It is also the number of levels below, and the embedded
# engine's SQL reads each level's input twice, which SQLite inlines when it
# prepares the statement: about 0.1 s and 40 MB at 8 on any graph, doubling
# with each level added.
_FLOW_COMPONENT_DEPTH = 8

# One level: the live Resources and those one flow away, each once. `*0..1`
# keeps a Resource with no flow of its own.
_SPREAD_ONE_FLOW = "MATCH (m)-[:FLOWS_TO*0..1]-(n:Resource) WITH DISTINCT n AS m "

CYPHER_DELETE_UNANCHORED_RESOURCES = (
    "MATCH (m:Resource)--(x) WHERE NOT x:Resource "
    "WITH DISTINCT m "
    f"{_SPREAD_ONE_FLOW * _FLOW_COMPONENT_DEPTH}"
    "WITH collect(m) AS live "
    "MATCH (r:Resource) "
    # Every Resource once and each live one again, so one seen once is
    # dead: a grouping, linear on every engine, where a list membership
    # test per Resource need not be.
    "WITH live, collect(r) AS resources "
    "UNWIND live + resources AS r "
    "WITH r, count(*) AS seen "
    "WHERE seen = 1 "
    "DETACH DELETE r"
)


def prune_unanchored_resources(ingestor: QueryProtocol) -> None:
    """Delete Resources whose flow component never reaches a code node."""
    ingestor.execute_write(CYPHER_DELETE_UNANCHORED_RESOURCES)
