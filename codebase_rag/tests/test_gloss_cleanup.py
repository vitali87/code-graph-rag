"""A Gloss dies with a project delete but survives a rebuild (issue #1828).

`CYPHER_DELETE_PROJECT` reaches nodes through containment and then
DEFINES/DEFINES_METHOD. `ANNOTATES` is in neither list, so deleting a project
removed the symbol a gloss describes and left the gloss behind, detached and
unreachable; re-indexing then built fresh symbols the orphan was not attached
to.

The two requirements pull apart, which is why this is a separate sweep rather
than an extra relationship in that traversal:

* a gloss MUST survive index/update/reingest -- its truth lives only in the
  graph and is never rebuilt from source;
* a gloss MUST NOT survive deletion of the project it describes.

A traversal-based delete cannot separate those, because a rebuild deletes and
recreates the same symbols. Asking whether the SUBJECT still exists can: that
is false only after a real deletion.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag.services.gloss_cleanup import (
    CYPHER_DELETE_ORPHANED_GLOSSES,
    prune_orphaned_glosses,
)


class _FakeStore:
    """Records the statement, so the query's SHAPE can be asserted.

    The evals emulator does not model `CYPHER_DELETE_PROJECT` at all, so a
    probe built on it reports "nothing was deleted" for every query alike --
    it cannot distinguish this leak from a working delete. The parts that can
    be checked without a live database are the query's shape and that the
    sweep is wired into the delete path, so those are what these assert.
    """

    def __init__(self) -> None:
        self.writes: list[str] = []

    def execute_write(self, query: str, params: object = None) -> None:
        self.writes.append(query)


def test_the_project_delete_does_not_reach_a_gloss() -> None:
    """The leak itself, asserted against the query that causes it.

    If `ANNOTATES` is ever added to this traversal the sweep becomes
    redundant AND harmful -- a rebuild would then delete glosses with the
    symbols it recreates -- so this pins the separation the design depends on.
    """
    from codebase_rag import cypher_queries as cq

    assert cs.RelationshipType.ANNOTATES.value not in cq.CYPHER_DELETE_PROJECT, (
        "ANNOTATES was added to the project-delete traversal; a gloss would "
        "then be destroyed by every rebuild, which deletes and recreates the "
        "symbols it annotates"
    )


def test_the_sweep_deletes_only_glosses_with_no_subject() -> None:
    """The predicate is 'the subject is gone', not 'a gloss exists'."""
    assert f"MATCH (g:{cs.NodeLabel.GLOSS.value})" in CYPHER_DELETE_ORPHANED_GLOSSES
    assert cs.RelationshipType.ANNOTATES.value in CYPHER_DELETE_ORPHANED_GLOSSES
    assert "subjects = 0" in CYPHER_DELETE_ORPHANED_GLOSSES, (
        "the sweep must delete only glosses whose subject count is zero; "
        "without that condition it deletes every gloss on every call"
    )


def test_the_sweep_checks_and_deletes_in_one_statement() -> None:
    """Same reason `prune_unanchored_resources` documents: a concurrent
    writer must not be able to attach a gloss to a subject between a snapshot
    read and the delete."""
    assert CYPHER_DELETE_ORPHANED_GLOSSES.count("DETACH DELETE") == 1
    # One statement: the liveness check and the delete are not separate calls.
    assert "MATCH" in CYPHER_DELETE_ORPHANED_GLOSSES.split("DETACH DELETE")[0]


def test_the_sweep_follows_the_edge_in_its_real_direction() -> None:
    """The edge runs FROM the gloss TO its subject.

    An undirected match would also count a gloss annotated by something else,
    if such a relationship is ever added, and would then keep an orphan alive
    on the strength of an unrelated edge.
    """
    assert f"-[:{cs.RelationshipType.ANNOTATES.value}]->" in (
        CYPHER_DELETE_ORPHANED_GLOSSES
    )


def test_prune_issues_exactly_one_write() -> None:
    store = _FakeStore()
    prune_orphaned_glosses(store)  # type: ignore[arg-type]
    assert store.writes == [CYPHER_DELETE_ORPHANED_GLOSSES]


def test_delete_project_runs_the_sweep() -> None:
    """Wiring, not just existence.

    A correct sweep that nothing calls fixes nothing -- the same shape as a
    deduplicating appender with no callers.
    """
    import inspect

    from codebase_rag.services import graph_service

    source = inspect.getsource(graph_service.MemgraphIngestor.delete_project)
    assert "prune_orphaned_glosses" in source, (
        "delete_project does not run the gloss sweep, so the orphan survives"
    )
