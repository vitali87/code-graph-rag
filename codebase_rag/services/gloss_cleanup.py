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

from loguru import logger

from .. import constants as cs
from .. import logs as lg

if TYPE_CHECKING:
    from . import QueryProtocol

# A gloss is live when its ANNOTATES edge still reaches a node. `DETACH
# DELETE` on the subject removes the edge as well, so an orphaned gloss has
# no outgoing ANNOTATES at all -- counting the endpoint is what distinguishes
# it from a gloss whose subject survives. Direction matters: the edge runs
# from the gloss to its subject, and an undirected match would also count a
# gloss annotated BY something else, if that relationship is ever added.
#
# Scoped to the deleted project by the note's own record of its project.
# Since stage four of #1808 an unattached gloss is not necessarily garbage: a
# note graded LOST or AMBIGUOUS in a project that still exists is unattached
# by design and stays readable on its old name. Only the notes about the
# project just deleted go with it. Keyed on the recorded `project`, not a
# qn prefix, because project names may contain dots: deleting `foo` must not
# sweep `foo.bar`'s notes. A note written before `project` was recorded has
# only its qn to go on and takes the prefix.
CYPHER_DELETE_ORPHANED_GLOSSES = (
    f"MATCH (g:{cs.NodeLabel.GLOSS.value}) "
    "WHERE (g.project = $project_name "
    "OR (g.project IS NULL AND g.target_qn STARTS WITH $project_prefix)) "
    f"OPTIONAL MATCH (g)-[:{cs.RelationshipType.ANNOTATES.value}]->(subject) "
    "WITH g, count(subject) AS subjects "
    "WHERE subjects = 0 "
    "DETACH DELETE g"
)


def prune_orphaned_glosses(ingestor: QueryProtocol, project_name: str) -> bool:
    """Delete the deleted project's glosses, leaving every other note intact.

    Never raises. The caller runs this AFTER the project delete has already
    succeeded, and the delete is not undoable: letting a cleanup failure
    propagate would report a completed deletion as failed, and a retry then
    short-circuits on `project not found` before reaching this sweep at all,
    so the orphans would survive indefinitely (raised by CodeRabbit on
    #1856).

    Returns whether the sweep reached the store, so a caller that can
    schedule a retry may, and the log records it either way. Leaving the
    orphans is the mild outcome: they are unreachable rather than wrong, and
    they read as LOST notes on a project that no longer exists until a
    deliberate delete of that project name runs the sweep again.
    """
    try:
        ingestor.execute_write(
            CYPHER_DELETE_ORPHANED_GLOSSES,
            {
                cs.KEY_PROJECT_NAME: project_name,
                cs.KEY_PROJECT_PREFIX: f"{project_name}{cs.SEPARATOR_DOT}",
            },
        )
    except Exception as error:  # noqa: BLE001 -- see docstring
        logger.warning(lg.GLOSS_PRUNE_FAILED.format(error=error))
        return False
    return True
