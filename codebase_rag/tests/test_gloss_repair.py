"""The repair tiers below the name match (issue #1808, stage four).

The fake store answers exactly the fixed queries `gloss_repair` issues and
raises on anything else, so a lookup the tests do not model cannot pass by
returning an empty list. Its writes are applied to its own state, which is
what lets a second pass be run over the result of the first.
"""

from __future__ import annotations

import re

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.gloss_repair import RepairReport, repair_unanchored
from codebase_rag.types_defs import PropertyDict, ResultRow

A = "alpha"
B = "beta"
H = f"{cs.ANCHOR_HASH_VERSION}deadbeef"
H2 = f"{cs.ANCHOR_HASH_VERSION}cafef00d"
LEGACY = "clone-skeleton-without-prefix"


class FakeStore:
    def __init__(self) -> None:
        # qualified_name -> anchor_hash, for every definition in the graph.
        self.definitions: dict[str, str | None] = {}
        # gloss key -> properties, plus the set of attached keys.
        self.glosses: dict[str, PropertyDict] = {}
        self.attached: set[str] = set()
        self.reads: list[tuple[str, PropertyDict | None]] = []
        self.writes: list[tuple[str, PropertyDict | None]] = []

    def define(self, qn: str, anchor_hash: str | None) -> None:
        self.definitions[qn] = anchor_hash

    def note(
        self,
        key: str,
        target_qn: str,
        target_hash: str | None,
        state: str = cs.GlossAnchorState.EXACT.value,
        attached: bool = False,
        candidate_qns: list[str] | None = None,
        moved_from: str | None = None,
    ) -> None:
        self.glosses[key] = {
            cs.KEY_TARGET_QN: target_qn,
            cs.KEY_TARGET_HASH: target_hash,
            cs.KEY_ANCHOR_STATE: state,
            cs.KEY_CANDIDATE_QNS: candidate_qns,
            cs.KEY_MOVED_FROM: moved_from,
        }
        if attached:
            self.attached.add(key)

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        self.reads.append((query, params))
        if query == cq.CYPHER_UNANCHORED_GLOSSES:
            rows: list[ResultRow] = [
                {cs.KEY_QUALIFIED_NAME: key, **props}
                for key, props in self.glosses.items()
                if key not in self.attached
            ]
            return list(reversed(rows))  # unsorted on purpose
        if query == cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH:
            assert params is not None
            wanted = set(params[cs.KEY_HASHES])  # type: ignore[arg-type]
            prefix = str(params[cs.KEY_PROJECT_PREFIX])
            return [
                {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_ANCHOR_HASH: h}
                for qn, h in self.definitions.items()
                if h in wanted and qn.startswith(prefix)
            ]
        raise AssertionError(f"unexpected query: {query[:60]}")

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, params))
        assert params is not None
        key = str(params[cs.KEY_QN])
        props = self.glosses[key]
        if query == cq.CYPHER_GLOSS_MOVE:
            new_qn = str(params[cs.KEY_NEW_QN])
            assert new_qn in self.definitions, "MOVE matched a definition"
            props[cs.KEY_MOVED_FROM] = (
                props.get(cs.KEY_MOVED_FROM) or props[cs.KEY_TARGET_QN]
            )
            props[cs.KEY_TARGET_QN] = new_qn
            props[cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.MOVED.value
            props[cs.KEY_CANDIDATE_QNS] = None
            self.attached.add(key)
        elif query == cq.CYPHER_GLOSS_MARK:
            props[cs.KEY_ANCHOR_STATE] = params[cs.KEY_ANCHOR_STATE]
            props[cs.KEY_CANDIDATE_QNS] = params[cs.KEY_CANDIDATE_QNS]
        else:
            raise AssertionError(f"unexpected write: {query[:60]}")


def _run(store: FakeStore) -> RepairReport:
    return repair_unanchored(store.fetch_all, store.execute_write)


def _writes(store: FakeStore, query: str) -> list[PropertyDict | None]:
    return [p for q, p in store.writes if q == query]


# --- the three verdicts ------------------------------------------------------


def test_one_definition_with_the_hash_means_the_note_follows_it() -> None:
    store = FakeStore()
    store.define(f"{A}.new.place", H)
    store.note("gloss:1", f"{A}.old.place", H)
    report = _run(store)
    assert report == RepairReport(moved=["gloss:1"], ambiguous=[], lost=[])
    assert _writes(store, cq.CYPHER_GLOSS_MOVE) == [
        {cs.KEY_QN: "gloss:1", cs.KEY_NEW_QN: f"{A}.new.place"}
    ]
    g = store.glosses["gloss:1"]
    assert g[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.MOVED.value
    assert g[cs.KEY_TARGET_QN] == f"{A}.new.place"
    assert g[cs.KEY_MOVED_FROM] == f"{A}.old.place", "the move stays visible"
    assert "gloss:1" in store.attached


def test_two_definitions_with_the_hash_is_ambiguous_and_binds_nothing() -> None:
    store = FakeStore()
    store.define(f"{A}.z.place", H)
    store.define(f"{A}.a.place", H)
    store.note("gloss:1", f"{A}.old.place", H)
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=["gloss:1"], lost=[])
    assert _writes(store, cq.CYPHER_GLOSS_MOVE) == []
    assert _writes(store, cq.CYPHER_GLOSS_MARK) == [
        {
            cs.KEY_QN: "gloss:1",
            cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.AMBIGUOUS.value,
            cs.KEY_CANDIDATE_QNS: [f"{A}.a.place", f"{A}.z.place"],
        }
    ], "candidates are sorted, so the same graph gives the same write"
    assert "gloss:1" not in store.attached


def test_no_definition_with_the_hash_is_lost() -> None:
    store = FakeStore()
    store.define(f"{A}.other", H2)
    store.note("gloss:1", f"{A}.old.place", H)
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=[], lost=["gloss:1"])
    assert _writes(store, cq.CYPHER_GLOSS_MARK) == [
        {
            cs.KEY_QN: "gloss:1",
            cs.KEY_ANCHOR_STATE: cs.GlossAnchorState.LOST.value,
            cs.KEY_CANDIDATE_QNS: None,
        }
    ]
    assert "gloss:1" not in store.attached


def test_a_note_without_a_comparable_hash_is_lost_without_a_lookup() -> None:
    # A class or module subject has no hash; a note from before the hash
    # format existed recorded the clone skeleton. Neither can be matched, and
    # neither should cost a query.
    for target_hash in (None, LEGACY):
        store = FakeStore()
        store.define(f"{A}.anything", H)
        store.note("gloss:1", f"{A}.old.Klass", target_hash)
        report = _run(store)
        assert report.lost == ["gloss:1"], target_hash
        assert all(q != cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH for q, _ in store.reads)


# --- idempotence and scoping -------------------------------------------------


def test_a_second_pass_over_an_unchanged_graph_writes_nothing() -> None:
    store = FakeStore()
    store.define(f"{A}.a.place", H)
    store.define(f"{A}.z.place", H)
    store.note("gloss:amb", f"{A}.old.place", H)
    store.note("gloss:lost", f"{A}.gone", H2)
    _run(store)
    first = len(store.writes)
    assert first == 2
    report = _run(store)
    assert len(store.writes) == first, "an unchanged verdict is not re-written"
    assert report == RepairReport(
        moved=[], ambiguous=["gloss:amb"], lost=["gloss:lost"]
    ), "the report still names them: the state is read, not re-derived"


def test_a_changed_verdict_is_written_over_the_old_one() -> None:
    store = FakeStore()
    store.define(f"{A}.a.place", H)
    store.define(f"{A}.z.place", H)
    store.note(
        "gloss:1",
        f"{A}.old.place",
        H,
        state=cs.GlossAnchorState.AMBIGUOUS.value,
        candidate_qns=[f"{A}.a.place", f"{A}.z.place"],
    )
    assert _run(store).ambiguous == ["gloss:1"]
    assert store.writes == []
    # One candidate disappears: the note now has a single home.
    del store.definitions[f"{A}.z.place"]
    assert _run(store).moved == ["gloss:1"]
    assert store.glosses["gloss:1"][cs.KEY_TARGET_QN] == f"{A}.a.place"


def test_one_hash_lookup_per_project_covering_all_its_notes() -> None:
    store = FakeStore()
    store.define(f"{A}.x", H)
    store.define(f"{B}.y", H2)
    store.note("gloss:a1", f"{A}.old1", H)
    store.note("gloss:a2", f"{A}.old2", H2)
    store.note("gloss:b1", f"{B}.old", H2)
    _run(store)
    lookups = [p for q, p in store.reads if q == cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH]
    assert lookups == [
        {cs.KEY_HASHES: [H2, H], cs.KEY_PROJECT_PREFIX: f"{A}."},
        {cs.KEY_HASHES: [H2], cs.KEY_PROJECT_PREFIX: f"{B}."},
    ]


def test_a_definition_in_another_project_is_never_a_candidate() -> None:
    # Two projects can hold byte-identical functions. A note about alpha's
    # says nothing about beta's, so beta's copy is neither a MOVED target nor
    # an AMBIGUOUS candidate.
    store = FakeStore()
    store.define(f"{B}.same", H)
    store.note("gloss:1", f"{A}.gone", H)
    report = _run(store)
    assert report.lost == ["gloss:1"]
    assert _writes(store, cq.CYPHER_GLOSS_MOVE) == []
    store.define(f"{A}.here", H)
    assert _run(store).moved == ["gloss:1"]
    assert store.glosses["gloss:1"][cs.KEY_TARGET_QN] == f"{A}.here"


def test_a_moved_note_keeps_its_first_origin_across_a_second_move() -> None:
    store = FakeStore()
    store.define(f"{A}.second", H)
    store.note("gloss:1", f"{A}.first", H, moved_from=f"{A}.zeroth")
    _run(store)
    assert store.glosses["gloss:1"][cs.KEY_MOVED_FROM] == f"{A}.zeroth"
    assert "coalesce(g.moved_from, g.target_qn)" in cq.CYPHER_GLOSS_MOVE


def test_notes_are_visited_in_key_order() -> None:
    store = FakeStore()
    store.note("gloss:b", f"{A}.gone", None)
    store.note("gloss:a", f"{A}.gone", None)
    assert _run(store).lost == ["gloss:a", "gloss:b"]
    assert [p[cs.KEY_QN] for p in _writes(store, cq.CYPHER_GLOSS_MARK) if p] == [
        "gloss:a",
        "gloss:b",
    ]


def test_an_attached_note_is_not_touched() -> None:
    store = FakeStore()
    store.define(f"{A}.elsewhere", H)
    store.note("gloss:1", f"{A}.live", H, attached=True)
    assert _run(store) == RepairReport(moved=[], ambiguous=[], lost=[])
    assert store.writes == []


def test_the_name_tier_owns_a_note_whose_name_came_back() -> None:
    # Documented boundary: this pass sees only what CYPHER_REANCHOR_GLOSSES
    # left unattached. A note whose original name exists again is attached
    # by that query first and never reaches the hash tiers, so a MOVED note
    # is not moved back here even if its old name reappears.
    assert "WHERE subjects = 0" in cq.CYPHER_UNANCHORED_GLOSSES
    assert "WHERE subjects = 0" in cq.CYPHER_REANCHOR_GLOSSES


# --- query shapes -------------------------------------------------------------

_NEW_QUERIES = (
    cq.CYPHER_UNANCHORED_GLOSSES,
    cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH,
    cq.CYPHER_GLOSS_MOVE,
    cq.CYPHER_GLOSS_MARK,
    cq.CYPHER_GLOSSES_ORPHANED_ON,
)


def test_no_repair_query_deletes_anything() -> None:
    for q in _NEW_QUERIES:
        assert "DELETE" not in q, q


def test_unattached_is_asked_with_a_count_not_a_pattern_predicate() -> None:
    # Memgraph 3 rejects a relationship pattern inside WHERE; the portable
    # form is OPTIONAL MATCH plus count, as the existing gloss queries use.
    for q in (cq.CYPHER_UNANCHORED_GLOSSES, cq.CYPHER_GLOSSES_ORPHANED_ON):
        assert "WITH g, count(subject) AS subjects" in q
        assert "WHERE subjects = 0" in q
        for predicate in re.findall(r"WHERE([^\n]*)", q):
            assert "]->(" not in predicate, predicate


def test_the_hash_lookup_is_project_scoped_and_label_bound() -> None:
    q = cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH
    assert "t.qualified_name STARTS WITH $project_prefix" in q
    assert "t.anchor_hash IN $hashes" in q
    assert (
        f"(t:{cs.NodeLabel.FUNCTION.value}|" in q
        or f"|{cs.NodeLabel.FUNCTION.value}" in q
    )
    assert cs.NodeLabel.GLOSS.value not in q.split("MATCH", 1)[1].split(")")[0]


def test_the_move_binds_the_edge_to_the_matched_definition_only() -> None:
    q = cq.CYPHER_GLOSS_MOVE
    assert "{qualified_name: $new_qn}" in q
    assert f"SET g.anchor_state = '{cs.GlossAnchorState.MOVED.value}'" in q
    assert "g.target_qn = $new_qn" in q
    assert "g.candidate_qns = null" in q
    assert f"MERGE (g)-[:{cs.RelationshipType.ANNOTATES.value}]->(t)" in q


def test_the_mark_writes_state_and_candidates_and_no_edge() -> None:
    q = cq.CYPHER_GLOSS_MARK
    assert "SET g.anchor_state = $anchor_state, g.candidate_qns = $candidate_qns" in q
    assert "MERGE" not in q
    assert cs.RelationshipType.ANNOTATES.value not in q


def test_the_orphan_read_returns_the_same_row_shape_as_the_other_reads() -> None:
    q = cq.CYPHER_GLOSSES_ORPHANED_ON
    assert "WHERE g.target_qn = $qn" in q
    assert "g.moved_from AS moved_from" in q
    assert "g.candidate_qns AS candidate_qns" in q
    assert "collect(m.qualified_name) AS mentions" in q
    assert q.split("RETURN", 1)[1] == cq.CYPHER_GLOSSES_ANNOTATING.split("RETURN", 1)[1]
