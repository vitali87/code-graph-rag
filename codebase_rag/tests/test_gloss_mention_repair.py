"""The mention pass's writes, against a store that answers only its queries.

A gloss's mentions were restored by name alone after every sync, so a
renamed or moved mention lost its edge and a newcomer with the old name took
it (issue #3230). The real-database behaviour is pinned in
`integration/test_gloss_mentions_e2e.py`; this pins what the pass writes:
nothing when nothing changed, and a move bound only while its target still
carries the hash the pass read.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.gloss_repair import repair_mentions
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"
RUN = f"{P}.b.run"
RUN_HASH = f"{cs.ANCHOR_HASH_VERSION}run"


class _Store:
    def __init__(
        self, note: ResultRow, current: list[ResultRow], by_hash: list[ResultRow]
    ) -> None:
        self.note = note
        self.current = current
        self.by_hash = by_hash
        self.writes: list[tuple[str, PropertyDict | None]] = []

    def fetch_all(self, query: str, params: PropertyDict | None = None) -> list:
        if query == cq.CYPHER_GLOSS_MENTION_ANCHORS:
            return [self.note]
        if query == cq.CYPHER_DEFINITIONS_BY_QNS:
            return self.current
        if query == cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH:
            return self.by_hash
        raise AssertionError(f"unexpected query: {query}")

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, params))


def _note(attached: list[str]) -> ResultRow:
    return {
        cs.KEY_QUALIFIED_NAME: "gloss:n",
        cs.KEY_TARGET_QN: f"{P}.a.helper",
        cs.KEY_PROJECT: P,
        cs.KEY_MENTION_QNS: [RUN],
        cs.KEY_MENTION_HASHES: [RUN_HASH],
        cs.KEY_MENTION_QUOTES: [""],
        cs.KEY_ATTACHED: attached,
    }


def _definition(qn: str, anchor_hash: str) -> ResultRow:
    return {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_ANCHOR_HASH: anchor_hash}


def test_an_unchanged_mention_writes_nothing() -> None:
    store = _Store(_note([RUN]), [_definition(RUN, RUN_HASH)], [])

    report = repair_mentions(store.fetch_all, store.execute_write)

    assert store.writes == []
    assert report.moved == []
    assert report.lost == []


def test_a_mention_whose_edge_was_dropped_is_rebound_by_name() -> None:
    store = _Store(_note([]), [_definition(RUN, RUN_HASH)], [])

    repair_mentions(store.fetch_all, store.execute_write)

    ((query, params),) = store.writes
    assert query == cq.CYPHER_GLOSS_SET_MENTIONS
    assert params is not None
    # Kept by name: no hash to re-validate.
    assert params[cs.KEY_ATTACH_QNS] == [RUN]
    assert params[cs.KEY_ATTACH_KEYS] == []
    assert params[cs.KEY_MENTIONS_LOST] is None


def test_a_moved_mention_is_bound_only_while_it_carries_the_hash() -> None:
    moved = f"{P}.c.run"
    store = _Store(_note([]), [], [_definition(moved, RUN_HASH)])

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    assert params is not None
    assert params[cs.KEY_MENTION_QNS] == [moved]
    assert params[cs.KEY_ATTACH_QNS] == []
    assert params[cs.KEY_ATTACH_KEYS] == [
        f"{moved}{cs.GLOSS_MENTION_KEY_SEPARATOR}{RUN_HASH}"
    ]
    assert params[cs.KEY_KEY_SEPARATOR] == cs.GLOSS_MENTION_KEY_SEPARATOR
    assert params[cs.KEY_PROJECT_PREFIX] == f"{P}."
    assert report.moved == ["gloss:n"]
    assert report.lost == []


def test_a_mention_with_no_home_is_recorded_lost() -> None:
    store = _Store(_note([]), [], [])

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    assert params is not None
    assert params[cs.KEY_MENTION_QNS] == [RUN]
    assert params[cs.KEY_MENTIONS_LOST] == [RUN]
    assert params[cs.KEY_ATTACH_QNS] == []
    assert params[cs.KEY_ATTACH_KEYS] == []
    assert report.lost == ["gloss:n"]


def test_a_mention_edited_in_place_renews_its_recorded_hash() -> None:
    # The old code is nowhere, so the name keeps the mention, and the hash
    # recorded from now on is the edited one (no reader, so the quote stays).
    edited = f"{cs.ANCHOR_HASH_VERSION}edited"
    store = _Store(_note([RUN]), [_definition(RUN, edited)], [])

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    assert params is not None
    assert params[cs.KEY_MENTION_HASHES] == [edited]
    assert params[cs.KEY_ATTACH_QNS] == [RUN]
    assert params[cs.KEY_ATTACH_KEYS] == []
    assert report.moved == []
    assert report.lost == []


def test_the_set_statement_re_validates_a_moved_binding() -> None:
    q = cq.CYPHER_GLOSS_SET_MENTIONS
    assert "(m.qualified_name + $key_separator + m.anchor_hash) IN $attach_keys" in q
    assert "m.qualified_name STARTS WITH $project_prefix" in q
    # Targets are matched before any edge goes, and only stale edges go.
    assert q.index("collect(DISTINCT m) AS targets") < q.index("DELETE edge")
    assert "WHERE NOT t IN targets" in q


def test_the_pass_carries_the_write_id_it_read() -> None:
    # The write is skipped when the note was rewritten after this read, so
    # the pass hands back the nonce it saw; a note from before nonces has "".
    note = {**_note([]), cs.KEY_WRITE_ID: "w-read"}
    store = _Store(note, [_definition(RUN, RUN_HASH)], [])

    repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    assert params is not None
    assert params[cs.KEY_WRITE_ID] == "w-read"

    unversioned = _Store(_note([]), [_definition(RUN, RUN_HASH)], [])
    repair_mentions(unversioned.fetch_all, unversioned.execute_write)
    ((_query, params),) = unversioned.writes
    assert params is not None
    assert params[cs.KEY_WRITE_ID] == ""


# Where the pass reads and where it writes are two statements, and an
# `annotate` from another server, or a sync of another checkout, can land in
# between. The statement itself is what refuses a stale result, so these run
# it on the embedded engine.
_MOVED = f"{P}.c.run"
_OTHER = f"{P}.c.other"
_NOTE_EDGES = """MATCH (g:Gloss {qualified_name: 'gloss:n'})
OPTIONAL MATCH (g)-[:MENTIONS]->(m)
RETURN g.mention_qns AS qns, g.mentions_lost AS lost,
       collect(m.qualified_name) AS edges"""


def _set_params(
    write_id: str,
    mention_qns: list[str],
    attach_qns: list[str],
    attach_keys: list[str],
) -> PropertyDict:
    return {
        cs.KEY_QN: "gloss:n",
        cs.KEY_WRITE_ID: write_id,
        cs.KEY_MENTION_QNS: mention_qns,
        cs.KEY_MENTION_HASHES: [RUN_HASH] * len(mention_qns),
        cs.KEY_MENTION_QUOTES: [""] * len(mention_qns),
        cs.KEY_MENTIONS_LOST: None,
        cs.KEY_ATTACH_QNS: attach_qns,
        cs.KEY_ATTACH_KEYS: attach_keys,
        cs.KEY_KEY_SEPARATOR: cs.GLOSS_MENTION_KEY_SEPARATOR,
        cs.KEY_PROJECT_PREFIX: f"{P}.",
    }


def _graph(conn: EmbeddedConnection, write_id: str) -> None:
    conn.run(
        "CREATE (:Function {qualified_name: $run, anchor_hash: $h}), "
        "(:Function {qualified_name: $other, anchor_hash: $h})",
        {"run": RUN, "other": _OTHER, "h": RUN_HASH},
    )
    conn.run(
        "MATCH (r:Function {qualified_name: $run}) "
        "CREATE (g:Gloss {qualified_name: 'gloss:n', write_id: $w, "
        "mention_qns: [$run]})-[:MENTIONS]->(r)",
        {"run": RUN, "w": write_id},
    )


def test_the_set_statement_skips_a_note_rewritten_since_the_read() -> None:
    with EmbeddedConnection(":memory:") as conn:
        # An `annotate` replaced the note's nonce after the pass read "w-read".
        _graph(conn, "w-newer")
        conn.run(
            cq.CYPHER_GLOSS_SET_MENTIONS,
            _set_params("w-read", [_OTHER], [_OTHER], []),
        )
        assert conn.run(_NOTE_EDGES).rows == [([RUN], None, [RUN])]


def test_the_set_statement_applies_while_the_note_is_as_read() -> None:
    with EmbeddedConnection(":memory:") as conn:
        _graph(conn, "w-read")
        conn.run(
            cq.CYPHER_GLOSS_SET_MENTIONS,
            _set_params("w-read", [_OTHER], [_OTHER], []),
        )
        assert conn.run(_NOTE_EDGES).rows == [([_OTHER], None, [_OTHER])]


def test_a_followed_target_replaced_since_the_read_changes_nothing() -> None:
    # The pass followed the lost RUN to _MOVED by its hash; before the write,
    # another sync replaced _MOVED with unrelated code. Saving the new name
    # anyway would let the next pass read that code as an in-place edit and
    # bind the mention to it, so the whole result is dropped instead.
    with EmbeddedConnection(":memory:") as conn:
        conn.run(
            "CREATE (:Function {qualified_name: $moved, anchor_hash: 'replaced'})",
            {"moved": _MOVED},
        )
        conn.run(
            "CREATE (:Gloss {qualified_name: 'gloss:n', write_id: 'w-read', "
            "mention_qns: [$run], mentions_lost: [$run]})",
            {"run": RUN},
        )
        conn.run(
            cq.CYPHER_GLOSS_SET_MENTIONS,
            _set_params(
                "w-read",
                [_MOVED],
                [],
                [f"{_MOVED}{cs.GLOSS_MENTION_KEY_SEPARATOR}{RUN_HASH}"],
            ),
        )
        assert conn.run(_NOTE_EDGES).rows == [([RUN], [RUN], [])]


def test_a_followed_target_still_carrying_the_hash_is_bound() -> None:
    with EmbeddedConnection(":memory:") as conn:
        conn.run(
            "CREATE (:Function {qualified_name: $moved, anchor_hash: $h})",
            {"moved": _MOVED, "h": RUN_HASH},
        )
        conn.run(
            "CREATE (:Gloss {qualified_name: 'gloss:n', write_id: 'w-read', "
            "mention_qns: [$run], mentions_lost: [$run]})",
            {"run": RUN},
        )
        conn.run(
            cq.CYPHER_GLOSS_SET_MENTIONS,
            _set_params(
                "w-read",
                [_MOVED],
                [],
                [f"{_MOVED}{cs.GLOSS_MENTION_KEY_SEPARATOR}{RUN_HASH}"],
            ),
        )
        assert conn.run(_NOTE_EDGES).rows == [([_MOVED], None, [_MOVED])]
