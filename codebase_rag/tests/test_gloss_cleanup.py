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
is false only after a real deletion -- and, since stage four of #1808, for a
note graded LOST or AMBIGUOUS, which is unattached by design. So the sweep
also asks whether the note was written about the project just deleted.
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
        self.params: list[object] = []

    def execute_write(self, query: str, params: object = None) -> None:
        self.writes.append(query)
        self.params.append(params)


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


def test_prune_issues_exactly_one_write_scoped_to_the_project() -> None:
    store = _FakeStore()
    assert prune_orphaned_glosses(store, "proj") is True  # type: ignore[arg-type]
    assert store.writes == [CYPHER_DELETE_ORPHANED_GLOSSES]
    assert store.params == [{cs.KEY_PROJECT_PREFIX: "proj."}]


def test_a_failing_sweep_does_not_raise() -> None:
    """The delete has already succeeded by the time this runs (#1856 review).

    Letting a cleanup failure propagate would report a completed, unundoable
    deletion as failed -- and a retry then short-circuits on `project not
    found` BEFORE reaching the sweep, so the orphans would survive
    indefinitely. The mild outcome is the right one: leave them, say so in
    the log, and let the next deliberate delete sweep them.
    """

    class _Refusing:
        def execute_write(self, query: str, params: object = None) -> None:
            raise RuntimeError("store refused the sweep")

    assert prune_orphaned_glosses(_Refusing(), "proj") is False, (  # type: ignore[arg-type]
        "a refused sweep must report failure rather than raise, so the "
        "caller can log it without failing a delete that already happened"
    )


def test_the_sweep_is_scoped_to_the_deleted_project() -> None:
    """An unattached gloss is no longer proof of garbage (stage four of #1808).

    A note graded LOST or AMBIGUOUS is unattached BY DESIGN: its definition's
    name is gone and nothing carries its hash, or several do, and it stays
    readable on the old name so the orphaning is visible. An unscoped sweep
    would destroy every such note in every project the moment any one project
    was deleted. The predicate is therefore 'this gloss has no subject AND it
    was written about the project just deleted', read off the note's own
    `target_qn`, which starts with the project name.

    The cost is the earlier retry story: a sweep that fails once is retried
    only by deleting the same project name again. The orphans it leaves read
    as LOST notes on a project that no longer exists -- visible, not wrong.
    """
    assert "WHERE g.target_qn STARTS WITH $project_prefix" in (
        CYPHER_DELETE_ORPHANED_GLOSSES
    ), (
        "the sweep is unscoped again; deleting one project would destroy the "
        "LOST and AMBIGUOUS notes of every other project"
    )
    # The scope is applied to the gloss before the subject count, so it can
    # never widen the delete: an attached note is still never touched.
    where, _ = CYPHER_DELETE_ORPHANED_GLOSSES.split("OPTIONAL MATCH", 1)
    assert "$project_prefix" in where
    assert "subjects = 0" in CYPHER_DELETE_ORPHANED_GLOSSES


def test_the_deliberate_delete_runs_the_sweep() -> None:
    """Wiring, not just existence.

    A correct sweep that nothing calls fixes nothing -- the same shape as a
    deduplicating appender with no callers.
    """
    import inspect

    from codebase_rag.mcp import tools

    source = inspect.getsource(tools.MCPToolsRegistry._delete_project_sync)
    assert "prune_orphaned_glosses" in source, (
        "the deliberate project delete does not run the gloss sweep, so the "
        "orphan survives the deletion it should die with"
    )


def test_the_shared_ingestor_delete_does_NOT_run_the_sweep() -> None:
    """The half that matters more, and the one my first attempt got wrong.

    `MemgraphIngestor.delete_project` is called by BOTH the deliberate delete
    and `_index_repository_sync`, which deletes and rebuilds. A sweep there
    runs on every reindex, and since the rebuild recreates the symbols AFTER
    the delete, every gloss is an orphan at that moment -- so the sweep would
    destroy all of them, permanently, on a routine reindex.

    That is the exact requirement #1828 names: a gloss must survive
    index/update/reingest because its truth lives only in the graph. Caught
    in review of #1856, where I had wired it into the shared method.
    """
    import inspect

    from codebase_rag.services import graph_service

    source = inspect.getsource(graph_service.MemgraphIngestor.delete_project)
    assert "prune_orphaned_glosses" not in source, (
        "the gloss sweep is wired into the SHARED ingestor delete, which "
        "index_repository also calls -- every reindex would permanently "
        "destroy every saved gloss"
    )


def test_the_index_path_does_not_sweep_glosses() -> None:
    """Stated against the index path directly, not inferred from the above.

    The previous test pins where the sweep is NOT; this pins that the
    rebuild path never reaches it by some other route.
    """
    import inspect

    from codebase_rag.mcp import tools

    source = inspect.getsource(tools.MCPToolsRegistry._index_repository_sync)
    assert "prune_orphaned_glosses" not in source, (
        "the reindex path sweeps glosses; the rebuild recreates symbols "
        "after the delete, so every gloss is momentarily an orphan and "
        "would be destroyed"
    )
