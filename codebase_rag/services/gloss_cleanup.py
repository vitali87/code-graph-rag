"""Removal of Gloss nodes whose subject no longer exists.

A `Gloss` hangs off the symbol it describes via `ANNOTATES`, which
`CYPHER_DELETE_PROJECT` traverses in neither of its lists (containment, then
DEFINES/DEFINES_METHOD). Deleting a project therefore removes the symbol and
leaves the gloss behind as unreachable garbage, and re-indexing the same
project builds fresh symbol nodes the orphan is not attached to (issue #1828).

Extending that traversal would be the wrong fix, because a gloss has a
lifecycle no other node has:

* it MUST survive `index_repository` / `update_repository` / reingest -- its
  truth lives only in the graph and is never rebuilt from source, so a rebuild
  that deleted it would destroy the only copy;
* it MUST NOT survive deletion of the project it describes, or it is garbage
  no query can reach.

A traversal-based delete cannot separate those: the rebuild path deletes and
recreates the same symbols, so anything reachable from a symbol dies with it.
This is the `prune_unanchored_resources` shape instead -- a separate sweep
that asks whether the SUBJECT still exists, which is false only after a real
deletion and true throughout a rebuild.

Liveness check and delete run as one statement, for the reason the resource
sweep documents: a concurrent writer must not be able to attach a gloss to a
subject between a snapshot read and the delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import constants as cs

if TYPE_CHECKING:
    from . import QueryProtocol

# A gloss is live when its ANNOTATES edge still reaches a node. `DETACH
# DELETE` on the subject removes the edge as well, so an orphaned gloss has
# no outgoing ANNOTATES at all -- counting the endpoint is what distinguishes
# it from a gloss whose subject survives. Direction matters: the edge runs
# from the gloss to its subject, and an undirected match would also count a
# gloss annotated BY something else, if that relationship is ever added.
CYPHER_DELETE_ORPHANED_GLOSSES = (
    f"MATCH (g:{cs.NodeLabel.GLOSS.value}) "
    f"OPTIONAL MATCH (g)-[:{cs.RelationshipType.ANNOTATES.value}]->(subject) "
    "WITH g, count(subject) AS subjects "
    "WHERE subjects = 0 "
    "DETACH DELETE g"
)


def prune_orphaned_glosses(ingestor: QueryProtocol) -> None:
    """Delete glosses whose subject is gone, leaving anchored ones intact."""
    ingestor.execute_write(CYPHER_DELETE_ORPHANED_GLOSSES)
