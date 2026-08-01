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
cannot anchor a resource between a snapshot read and the delete. The
variable-length walk is restricted to FLOWS_TO, which is sparse and
chain-shaped; an unrestricted walk through the URL-to-endpoint RESOLVES_TO
web could explode combinatorially.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import QueryProtocol

# Resource-to-resource flow chains are one or two hops in practice (env to
# file, file to stdout); the cap only bounds the traversal, not liveness of
# realistic graphs.
_FLOW_COMPONENT_DEPTH = 8

CYPHER_DELETE_UNANCHORED_RESOURCES = (
    "MATCH (r:Resource) "
    f"OPTIONAL MATCH (r)-[:FLOWS_TO*0..{_FLOW_COMPONENT_DEPTH}]-(m:Resource)-[]-(x) "
    "WHERE NOT x:Resource "
    "WITH r, count(x) AS anchors "
    "WHERE anchors = 0 "
    "DETACH DELETE r"
)


def prune_unanchored_resources(ingestor: QueryProtocol) -> None:
    """Delete Resources whose flow component never reaches a code node."""
    ingestor.execute_write(CYPHER_DELETE_UNANCHORED_RESOURCES)
