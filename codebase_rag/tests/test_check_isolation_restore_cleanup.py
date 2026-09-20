from __future__ import annotations

import re
from pathlib import Path

from check_isolation_helpers import (
    PROJECT,
    _check,
    _edit,
    _state,
)

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.check_isolation import IsolationGuard
from evals import cgr_graph
from evals.cgr_graph import _StatefulIngestor

# --- what the restore must not commit -----------------------------------------


class _BufferingStore:
    """A store with production's buffering semantics, over the eval double.

    `MemgraphIngestor` queues batch writes and applies them at `flush_all`
    (or at `batch_size`), while `execute_write` goes straight through. The
    eval double writes batches through immediately and its `flush_all` is a
    no-op, so it cannot express a re-ingest that raised with writes still
    queued -- the case where the restore's own flush would commit a failed
    parse (issue #1718, greptile-local).
    """

    def __init__(self, inner: _StatefulIngestor) -> None:
        self._inner = inner
        self.node_buffer: list[tuple[str, dict]] = []
        self._rel_groups: dict[tuple, list[tuple]] = {}
        self._rel_count = 0

    def fetch_all(self, query: str, params: dict | None = None) -> list:
        return self._inner.fetch_all(query, params)

    def execute_write(self, query: str, params: dict | None = None) -> None:
        self._inner.execute_write(query, params)

    def ensure_node_batch(self, label: str, properties: dict) -> None:
        self.node_buffer.append((label, dict(properties)))

    def ensure_relationship_batch(
        self,
        from_spec: tuple,
        rel_type: str,
        to_spec: tuple,
        properties: dict | None = None,
    ) -> None:
        self._rel_groups.setdefault((from_spec[0], rel_type, to_spec[0]), []).append(
            (from_spec, rel_type, to_spec, properties)
        )
        self._rel_count += 1

    def flush_all(self) -> None:
        for label, props in self.node_buffer:
            self._inner.ensure_node_batch(label, props)
        self.node_buffer.clear()
        for rows in self._rel_groups.values():
            for from_spec, rel_type, to_spec, properties in rows:
                self._inner.ensure_relationship_batch(
                    from_spec, rel_type, to_spec, properties
                )
        self._rel_groups.clear()
        self._rel_count = 0


def test_a_failed_reingests_queued_writes_are_not_committed_by_the_restore(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The restore flushes, so it must first drop what the failed run queued.

    A re-ingest that raises after its first write leaves parsed entities in
    the store's buffer. The restore issues its deletes immediately and then
    flushes its own re-emissions; without dropping the buffer, that flush
    commits the failed parse straight after the deletes meant to remove it.
    """
    root, inner = indexed
    store = _BufferingStore(inner)
    guard = IsolationGuard(store, PROJECT, root)
    before = _state(inner)

    guard.capture(["pkg/util.py"])
    # What a re-ingest does: delete the subtree, then queue the re-parse.
    store.execute_write(
        cs.CYPHER_DELETE_MODULE,
        {
            cs.KEY_PATH: "pkg/util.py",
            cs.KEY_PROJECT_NAME: PROJECT,
            cs.KEY_PROJECT_PREFIX: PROJECT + ".",
        },
    )
    store.ensure_node_batch(
        cs.NodeLabel.FUNCTION.value,
        {
            cs.KEY_QUALIFIED_NAME: f"{PROJECT}.pkg.util.assist",
            cs.KEY_PATH: "pkg/util.py",
        },
    )
    # The re-parse raises here; nothing is flushed.

    guard.restore()

    assert (
        cs.NodeLabel.FUNCTION.value,
        f"{PROJECT}.pkg.util.assist",
    ) not in inner.nodes
    assert _state(inner) == before


def test_the_buffering_double_commits_on_flush(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """Known positive for the double: without the restore, the queued write
    does land on flush. Otherwise the test above would pass against a store
    that simply drops everything."""
    root, inner = indexed
    store = _BufferingStore(inner)

    store.ensure_node_batch(
        cs.NodeLabel.FUNCTION.value,
        {
            cs.KEY_QUALIFIED_NAME: f"{PROJECT}.pkg.util.assist",
            cs.KEY_PATH: "pkg/util.py",
        },
    )
    assert (
        cs.NodeLabel.FUNCTION.value,
        f"{PROJECT}.pkg.util.assist",
    ) not in inner.nodes

    store.flush_all()

    assert (cs.NodeLabel.FUNCTION.value, f"{PROJECT}.pkg.util.assist") in inner.nodes


def test_the_finding_cleanup_spares_another_projects_findings(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """Findings are keyed on a repo-relative path, and two projects in the
    shared graph can hold the same one. The cleanup must scope by project
    the way the module delete beside it does, or an isolated check on one
    project deletes the sibling's findings (greptile-local, #1718).

    What this can and cannot prove: the eval store dispatches on query
    IDENTITY, so editing the Cypher's text makes it match no case and do
    nothing, which passes this test for the wrong reason. It therefore
    discriminates against the store's MODELLING of the query (mutating the
    project test out of `_delete_check_findings` reddens it), while the
    production Cypher's own scoping is covered by the integration test.
    """
    root, store = indexed
    mine = (cs.NodeLabel.CODE_SMELL.value, f"{PROJECT}.pkg.util.3.0.bare_except")
    theirs = (cs.NodeLabel.CODE_SMELL.value, "other.pkg.util.3.0.bare_except")
    for label, qn in (mine, theirs):
        store.ensure_node_batch(
            label, {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: "pkg/util.py"}
        )
    guard = IsolationGuard(store, PROJECT, root)

    guard.capture(["pkg/util.py"])
    guard.restore()

    assert theirs in store.nodes, "the sibling project's finding was deleted"
    assert mine not in store.nodes, (
        "this project's uncaptured finding should have been cleaned up, "
        "or the test proves nothing about scoping"
    )


def test_an_unrelated_orphan_survives_an_isolated_check(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The restore removes the shared nodes THIS check created, not every
    orphan in the graph (Greptile, #1718).

    A repo-wide `CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES` plus an unanchored
    Resource sweep would collect a pre-existing orphan the check never
    touched, which is a persistent change to shared state made by a mode
    whose whole purpose is to make none.
    """
    root, store = indexed
    store.ensure_node_batch(
        cs.NodeLabel.EXTERNAL_MODULE.value,
        {cs.KEY_QUALIFIED_NAME: "unrelated_orphan"},
    )
    orphan = (cs.NodeLabel.EXTERNAL_MODULE.value, "unrelated_orphan")
    assert orphan in store.nodes
    _edit(root)

    _check(root, store, isolated=True)

    assert orphan in store.nodes, "an unrelated orphan was swept by the restore"


def test_a_findings_properties_come_back_when_its_name_survives(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """A finding is keyed on file, line, column and rule, so an edit that
    moves the code without moving the match keeps the qualified name. The
    cleanup spares it as captured, so only its captured PROPERTIES can undo
    the re-parse's rewrite of its snippet and span (Greptile, #1718)."""
    root, store = indexed
    qn = f"{PROJECT}.pkg.util.1.0.bare_except"
    store.ensure_node_batch(
        cs.NodeLabel.CODE_SMELL.value,
        {
            cs.KEY_QUALIFIED_NAME: qn,
            cs.KEY_PATH: "pkg/util.py",
            cs.KEY_SNIPPET: "original snippet",
        },
    )
    store.ensure_relationship_batch(
        (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, f"{PROJECT}.pkg.util"),
        cs.RelationshipType.HAS_SMELL.value,
        (cs.NodeLabel.CODE_SMELL.value, cs.KEY_QUALIFIED_NAME, qn),
    )
    guard = IsolationGuard(store, PROJECT, root)

    guard.capture(["pkg/util.py"])
    # What a re-parse does to a finding whose key survives: same node,
    # rewritten detail.
    store.ensure_node_batch(
        cs.NodeLabel.CODE_SMELL.value,
        {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_SNIPPET: "rewritten by the check"},
    )
    guard.restore()

    smell = store.nodes.get((cs.NodeLabel.CODE_SMELL.value, qn))
    assert smell is not None, "the captured finding was deleted"
    assert smell.get(cs.KEY_SNIPPET) == "original snippet"


def test_the_capture_walks_exactly_what_the_delete_walks() -> None:
    """The capture's relation list must equal the module delete's.

    The re-ingest deletes a module subtree with `CYPHER_DELETE_MODULE`; the
    guard restores what it captured. Any relation the delete walks and the
    capture does not is a node deleted and never restored, which is silent
    data loss in the one mode that promises none.

    This drifted once already: `HAS_FIELD` joined the delete with Field
    nodes (#1899) and the capture kept its older list (CodeRabbit, #1718).
    Asserted against the query text so the next addition to either side
    fails here rather than in a graph.
    """
    pattern = re.compile(r"\[:([A-Z_|]+)\*")
    delete = pattern.search(cs.CYPHER_DELETE_MODULE)
    capture = pattern.search(cq.CYPHER_CHECK_SCOPE_NODES)
    assert delete is not None, cs.CYPHER_DELETE_MODULE
    assert capture is not None, cq.CYPHER_CHECK_SCOPE_NODES

    assert set(capture.group(1).split("|")) == set(delete.group(1).split("|"))


def test_the_emulator_walks_every_relation_the_capture_does() -> None:
    """The unit tier's double models the subtree walk in Python, so it
    drifts independently of the queries. A relation the capture reads but
    the double does not makes every unit test here blind to nodes the real
    store would lose.

    Asserted as containment rather than equality: the double omits
    `CONTAINS_SECTION`, which the delete does walk, and that gap is
    pre-existing on `origin/main` and filed separately. Requiring equality
    would make this branch's test fail for a defect it did not introduce.
    """
    pattern = re.compile(r"\[:([A-Z_|]+)\*")
    capture = pattern.search(cq.CYPHER_CHECK_SCOPE_NODES)
    assert capture is not None
    walked = set(capture.group(1).split("|")) - {
        cs.RelationshipType.CONTAINS_SECTION.value
    }

    assert walked <= set(cgr_graph._MODULE_SUBTREE_RELS)


def test_a_dangling_subtree_edge_does_not_crash_the_capture() -> None:
    """The eval store accepts an edge without its endpoints, so an edge can
    point at a node it never recorded. The scope walk read that child
    directly and raised KeyError (CodeRabbit, #1718).

    A real graph cannot hold a dangling edge, so this is a property of the
    double rather than of production -- but a capture that raises takes the
    isolated check down with it, and the double is what the unit tier runs.
    """
    store = _StatefulIngestor()
    store.ensure_node_batch(
        cs.NodeLabel.MODULE.value,
        {cs.KEY_QUALIFIED_NAME: "p.app", cs.KEY_PATH: "app.py"},
    )
    store.ensure_relationship_batch(
        (cs.NodeLabel.MODULE.value, cs.KEY_QUALIFIED_NAME, "p.app"),
        cs.RelationshipType.DEFINES.value,
        (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "p.app.ghost"),
    )

    rows = store.fetch_all(
        cq.CYPHER_CHECK_SCOPE_NODES,
        {
            cs.CYPHER_PARAM_PATHS: ["app.py"],
            cs.KEY_PROJECT_NAME: "p",
            cs.KEY_PROJECT_PREFIX: "p.",
            cs.CYPHER_PARAM_ABSOLUTE_PATHS: [],
        },
    )

    assert [r[cs.KEY_LABEL] for r in rows] == [cs.NodeLabel.MODULE.value]
