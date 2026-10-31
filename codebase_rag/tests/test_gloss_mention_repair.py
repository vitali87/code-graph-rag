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
from codebase_rag.gloss_anchor import TextAnchor
from codebase_rag.gloss_repair import (
    _Mention,
    _MentionPlacer,
    _Placed,
    _QuoteCandidate,
    repair_mentions,
)
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
            hashes = (params or {}).get(cs.KEY_HASHES) or []
            return [r for r in self.by_hash if r[cs.KEY_ANCHOR_HASH] in hashes]
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


def test_a_mention_whose_code_has_two_homes_leaves_the_reused_name() -> None:
    # `run`'s recorded code is now in c and in d, and b.run holds other code.
    # The code went somewhere, just not to one place, so this is no edit in
    # place: the name must not keep the mention, and neither copy is a guess
    # worth binding (bot review on PR #3329).
    edited = f"{cs.ANCHOR_HASH_VERSION}edited"
    store = _Store(
        _note([RUN]),
        [_definition(RUN, edited)],
        [_definition(f"{P}.c.run", RUN_HASH), _definition(f"{P}.d.run", RUN_HASH)],
    )

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    assert params is not None
    assert params[cs.KEY_MENTION_QNS] == [RUN]
    assert params[cs.KEY_MENTION_HASHES] == [RUN_HASH]
    assert params[cs.KEY_MENTIONS_LOST] == [RUN]
    assert params[cs.KEY_ATTACH_QNS] == []
    assert params[cs.KEY_ATTACH_KEYS] == []
    assert report.lost == ["gloss:n"]


class _Quotes:
    """The quote tier's answers, without a source reader behind them."""

    def __init__(self, rows: list[_QuoteCandidate]) -> None:
        self.rows = rows

    def candidates(self, project: str, quote: str) -> list[_QuoteCandidate]:
        return [r for r in self.rows if r.anchor.quote == quote]

    def anchor_of(self, project: str, qualified_name: str) -> TextAnchor | None:
        found = [r.anchor for r in self.rows if r.qualified_name == qualified_name]
        return found[0] if len(found) == 1 else None


def test_a_name_among_several_quote_homes_keeps_its_mention() -> None:
    # A decorator change moves the hash but not the quote, so b.run itself is
    # one of the quote's homes, with an identical twin in c. The recorded code
    # is still under the name: renewed there, not lost.
    anchor = TextAnchor(quote="q-run", prefix="", suffix="")
    decorated = f"{cs.ANCHOR_HASH_VERSION}decorated"
    twin = f"{P}.c.run"
    quotes = _Quotes(
        [
            _QuoteCandidate(RUN, anchor, "b.py", 1, 2, decorated),
            _QuoteCandidate(twin, anchor, "c.py", 1, 2, f"{cs.ANCHOR_HASH_VERSION}c"),
        ]
    )
    placer = _MentionPlacer(
        _Store(_note([RUN]), [], []).fetch_all, {RUN: [decorated]}, quotes
    )

    placed = placer.place(_Mention(RUN, RUN_HASH, "q-run", lost=False), P)

    assert placed == _Placed(
        RUN,
        decorated,
        "q-run",
        lost=False,
        fallback=_Placed(RUN, RUN_HASH, "q-run", lost=True),
    )


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


# The note mentions `a` and `run`; `a` is deleted, then `run` is renamed to
# `a`. Both now place under `a`: `run`, followed there by its code, is live,
# and `a` is still lost. Neither may displace the other (bot review on PR
# #3329): the lost record keeps its own hash and quote, so `a`'s code is
# followed if it comes back.
_A = f"{P}.b.a"
_A_HASH = f"{cs.ANCHOR_HASH_VERSION}a"
_A_BACK = f"{P}.c.a"


def _two_mentions(**stored: object) -> ResultRow:
    return {
        **_note([]),
        cs.KEY_MENTION_QNS: [_A, RUN],
        cs.KEY_MENTION_HASHES: [_A_HASH, RUN_HASH],
        cs.KEY_MENTION_QUOTES: ["q-a", ""],
        **stored,
    }


def _as_stored(note: ResultRow, params: PropertyDict) -> ResultRow:
    """The note as the set statement leaves it, for the next pass to read."""
    keys = [str(k) for k in params[cs.KEY_ATTACH_KEYS]]  # type: ignore[union-attr]
    attached = [str(q) for q in params[cs.KEY_ATTACH_QNS]]  # type: ignore[union-attr]
    return {
        **note,
        cs.KEY_MENTION_QNS: params[cs.KEY_MENTION_QNS],
        cs.KEY_MENTION_HASHES: params[cs.KEY_MENTION_HASHES],
        cs.KEY_MENTION_QUOTES: params[cs.KEY_MENTION_QUOTES],
        cs.KEY_MENTIONS_LOST: params[cs.KEY_MENTIONS_LOST],
        cs.KEY_ATTACHED: attached
        + [k.split(cs.GLOSS_MENTION_KEY_SEPARATOR)[0] for k in keys],
    }


def _assert_both_kept_under_a(params: PropertyDict | None) -> None:
    assert params is not None
    # The live entry first, then the lost one, each with its own anchors.
    assert params[cs.KEY_MENTION_QNS] == [_A, _A]
    assert params[cs.KEY_MENTION_HASHES] == [RUN_HASH, _A_HASH]
    assert params[cs.KEY_MENTION_QUOTES] == ["", "q-a"]
    assert params[cs.KEY_MENTIONS_LOST] == [_A]
    assert params[cs.KEY_ATTACH_QNS] == []
    assert params[cs.KEY_ATTACH_KEYS] == [
        f"{_A}{cs.GLOSS_MENTION_KEY_SEPARATOR}{RUN_HASH}"
    ]


def test_a_lost_mention_keeps_its_record_when_another_takes_its_name() -> None:
    note = _two_mentions(**{cs.KEY_MENTIONS_LOST: [_A]})
    store = _Store(note, [_definition(_A, RUN_HASH)], [_definition(_A, RUN_HASH)])

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    _assert_both_kept_under_a(params)
    assert report.moved == ["gloss:n"]
    assert report.lost == ["gloss:n"]

    # Read back as stored, the pair is stable: the next sync writes nothing.
    assert params is not None
    stored = _as_stored(note, params)
    again = _Store(stored, [_definition(_A, RUN_HASH)], [_definition(_A, RUN_HASH)])
    repair_mentions(again.fetch_all, again.execute_write)
    assert again.writes == []

    # `a`'s code comes back in another module: the lost record follows it,
    # and `run`'s code stays on `a`'s name.
    back = _Store(
        stored,
        [_definition(_A, RUN_HASH)],
        [_definition(_A, RUN_HASH), _definition(_A_BACK, _A_HASH)],
    )
    report = repair_mentions(back.fetch_all, back.execute_write)

    ((_query, params),) = back.writes
    assert params is not None
    assert params[cs.KEY_MENTION_QNS] == [_A, _A_BACK]
    assert params[cs.KEY_MENTION_HASHES] == [RUN_HASH, _A_HASH]
    assert params[cs.KEY_MENTION_QUOTES] == ["", "q-a"]
    assert params[cs.KEY_MENTIONS_LOST] is None
    assert params[cs.KEY_ATTACH_QNS] == [_A]
    assert params[cs.KEY_ATTACH_KEYS] == [
        f"{_A_BACK}{cs.GLOSS_MENTION_KEY_SEPARATOR}{_A_HASH}"
    ]
    assert report.moved == ["gloss:n"]
    assert report.lost == []


def test_a_mention_does_not_renew_onto_code_another_mention_was_followed_to() -> None:
    # The same two changes in ONE sync: `a` is not yet lost, so its name,
    # holding other code, would read as an edit in place. But that code is
    # `run`'s, found by `run`'s own hash: `a` was replaced, not edited, and
    # is lost with its own anchors, as if the changes came one sync apart.
    store = _Store(
        _two_mentions(),
        [_definition(_A, RUN_HASH)],
        [_definition(_A, RUN_HASH)],
    )

    report = repair_mentions(store.fetch_all, store.execute_write)

    ((_query, params),) = store.writes
    _assert_both_kept_under_a(params)
    assert report.lost == ["gloss:n"]


def test_a_note_from_before_repeated_names_reads_its_lost_mention_by_name() -> None:
    # Before a name could hold a live and a lost entry, `mentions_lost` named
    # each lost mention once, beside a unique `mention_qns`. Read that way it
    # is unchanged: nothing moved, so nothing is written.
    note = {
        **_note([]),
        cs.KEY_MENTION_QNS: [_A, RUN],
        cs.KEY_MENTION_HASHES: [_A_HASH, RUN_HASH],
        cs.KEY_MENTION_QUOTES: ["", ""],
        cs.KEY_MENTIONS_LOST: [_A],
        cs.KEY_ATTACHED: [RUN],
    }
    store = _Store(note, [_definition(RUN, RUN_HASH)], [])

    report = repair_mentions(store.fetch_all, store.execute_write)

    assert store.writes == []
    assert report.lost == ["gloss:n"]
