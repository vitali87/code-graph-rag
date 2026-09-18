"""The repair tiers below the name match (issue #1808, stage four).

The fake store answers exactly the fixed queries `gloss_repair` issues and
raises on anything else, so a lookup the tests do not model cannot pass by
returning an empty list. Its writes are applied to its own state, which is
what lets a second pass be run over the result of the first.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.gloss_anchor import TextAnchor, text_anchor
from codebase_rag.gloss_repair import RepairReport, repair_unanchored
from codebase_rag.types_defs import PropertyDict, ResultRow

A = "alpha"
B = "beta"
H = f"{cs.ANCHOR_HASH_VERSION}deadbeef"
H2 = f"{cs.ANCHOR_HASH_VERSION}cafef00d"
LEGACY = "clone-skeleton-without-prefix"
# `note(project=_DERIVED)` records the first segment of the target, which is
# what a real write records for an undotted project name.
_DERIVED = "<derived>"


class FakeStore:
    def __init__(self) -> None:
        # One entry per PHYSICAL definition node: (qualified_name, anchor_hash).
        # Two entries may share a name (a Function and a Method with one qn).
        self.definitions: list[tuple[str, str | None]] = []
        # The span of each physical definition, for the quote tier:
        # (qualified_name, name, path, start_line, end_line).
        self.spans: list[tuple[str, str | None, str, int, int]] = []
        # path -> file text, served by `read_source`; a missing path is None.
        self.files: dict[str, str] = {}
        self.source_reads: list[tuple[str, str]] = []
        # Runs once, right after the quote tier's span query has answered.
        self.after_spans: Callable[[], None] | None = None
        # gloss key -> properties, plus the set of attached keys.
        self.glosses: dict[str, PropertyDict] = {}
        # Registered Project nodes, for the legacy-note fallback.
        self.projects: list[str] = []
        self.attached: set[str] = set()
        self.reads: list[tuple[str, PropertyDict | None]] = []
        self.writes: list[tuple[str, PropertyDict | None]] = []
        # Runs once, right after the hash lookup has answered: another
        # updater changing the graph between the pass's read and its write.
        self.after_lookup: Callable[[], None] | None = None

    def define(
        self,
        qn: str,
        anchor_hash: str | None,
        *,
        name: str | None = None,
        path: str | None = None,
        span: tuple[int, int] | None = None,
    ) -> None:
        self.definitions.append((qn, anchor_hash))
        if path is not None and span is not None:
            self.spans.append((qn, name, path, span[0], span[1]))

    def read_source(self, project: str, path: str) -> str | None:
        self.source_reads.append((project, path))
        return self.files.get(path)

    def undefine(self, qn: str) -> None:
        self.definitions = [(q, h) for q, h in self.definitions if q != qn]

    def note(
        self,
        key: str,
        target_qn: str,
        target_hash: str | None,
        state: str = cs.GlossAnchorState.EXACT.value,
        attached: bool = False,
        candidate_qns: list[str] | None = None,
        moved_from: str | None = None,
        project: str | None = _DERIVED,
        anchor: TextAnchor | None = None,
    ) -> None:
        if project == _DERIVED:
            project = target_qn.split(".", 1)[0]
        self.glosses[key] = {
            cs.KEY_TARGET_QN: target_qn,
            cs.KEY_TARGET_HASH: target_hash,
            cs.KEY_ANCHOR_STATE: state,
            cs.KEY_CANDIDATE_QNS: candidate_qns,
            cs.KEY_MOVED_FROM: moved_from,
            cs.KEY_PROJECT: project,
            cs.KEY_ANCHOR_QUOTE: anchor.quote if anchor else None,
            cs.KEY_ANCHOR_PREFIX: anchor.prefix if anchor else None,
            cs.KEY_ANCHOR_SUFFIX: anchor.suffix if anchor else None,
        }
        if attached:
            self.attached.add(key)

    def _targets(self, target_hash: str, prefix: str) -> list[str]:
        return [
            q for q, h in self.definitions if h == target_hash and q.startswith(prefix)
        ]

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
        if query == cq.CYPHER_LIST_PROJECTS:
            return [
                {cs.KEY_NAME: name, cs.KEY_ROOT_PATH: None} for name in self.projects
            ]
        if query == cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH:
            assert params is not None
            wanted = set(params[cs.KEY_HASHES])  # type: ignore[arg-type]
            prefix = str(params[cs.KEY_PROJECT_PREFIX])
            out: list[ResultRow] = [
                {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_ANCHOR_HASH: h}
                for qn, h in self.definitions
                if h in wanted and qn.startswith(prefix)
            ]
            if self.after_lookup is not None:
                self.after_lookup()
                self.after_lookup = None
            return out
        if query == cq.CYPHER_DEFINITION_SPANS:
            assert params is not None
            prefix = str(params[cs.KEY_PROJECT_PREFIX])
            spans: list[ResultRow] = [
                {
                    cs.KEY_QUALIFIED_NAME: qn,
                    cs.KEY_NAME: name,
                    cs.KEY_PATH: path,
                    cs.KEY_START_LINE: start,
                    cs.KEY_END_LINE: end,
                }
                for qn, name, path, start, end in self.spans
                if qn.startswith(prefix)
            ]
            if self.after_spans is not None:
                self.after_spans()
                self.after_spans = None
            return spans
        if query == cq.CYPHER_GLOSS_READ:
            assert params is not None
            key = str(params[cs.KEY_QN])
            if key not in self.glosses:
                return []
            return [
                {cs.KEY_QUALIFIED_NAME: key, **self.glosses[key], cs.KEY_MENTIONS: []}
            ]
        raise AssertionError(f"unexpected query: {query[:60]}")

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append((query, params))
        assert params is not None
        key = str(params[cs.KEY_QN])
        props = self.glosses[key]
        if query == cq.CYPHER_GLOSS_MOVE:
            # Mirrors the statement: the targets are collected INSIDE the
            # write, and anything but exactly one physical node is a no-op.
            targets = self._targets(
                str(params[cs.KEY_TARGET_HASH]), str(params[cs.KEY_PROJECT_PREFIX])
            )
            if len(targets) != 1:
                return
            # ... and a note that gained a subject since the pass's read is
            # left alone rather than given a second one.
            if key in self.attached:
                return
            new_qn = targets[0]
            origin = props.get(cs.KEY_MOVED_FROM) or props[cs.KEY_TARGET_QN]
            if origin == new_qn:
                props[cs.KEY_MOVED_FROM] = None
                props[cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.EXACT.value
            else:
                props[cs.KEY_MOVED_FROM] = origin
                props[cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.MOVED.value
            props[cs.KEY_TARGET_QN] = new_qn
            props[cs.KEY_CANDIDATE_QNS] = None
            self.attached.add(key)
        elif query == cq.CYPHER_GLOSS_MOVE_TO_QN:
            # Mirrors the statement: exactly one PHYSICAL node under the
            # name, and the note still unattached, or a no-op.
            prefix = str(params[cs.KEY_PROJECT_PREFIX])
            target = str(params[cs.KEY_TARGET_QN])
            targets = [
                q for q, _h in self.definitions if q == target and q.startswith(prefix)
            ]
            if len(targets) != 1 or key in self.attached:
                return
            origin = props.get(cs.KEY_MOVED_FROM) or props[cs.KEY_TARGET_QN]
            if origin == target:
                props[cs.KEY_MOVED_FROM] = None
                props[cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.EXACT.value
            else:
                props[cs.KEY_MOVED_FROM] = origin
                props[cs.KEY_ANCHOR_STATE] = cs.GlossAnchorState.MOVED.value
            props[cs.KEY_TARGET_QN] = target
            props[cs.KEY_CANDIDATE_QNS] = None
            props[cs.KEY_ANCHOR_PREFIX] = params[cs.KEY_ANCHOR_PREFIX]
            props[cs.KEY_ANCHOR_SUFFIX] = params[cs.KEY_ANCHOR_SUFFIX]
            self.attached.add(key)
        elif query == cq.CYPHER_GLOSS_MARK:
            props[cs.KEY_ANCHOR_STATE] = params[cs.KEY_ANCHOR_STATE]
            props[cs.KEY_CANDIDATE_QNS] = params[cs.KEY_CANDIDATE_QNS]
        else:
            raise AssertionError(f"unexpected write: {query[:60]}")


def _run(store: FakeStore, *, with_source: bool = False) -> RepairReport:
    return repair_unanchored(
        store.fetch_all,
        store.execute_write,
        store.read_source if with_source else None,
    )


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
        {cs.KEY_QN: "gloss:1", cs.KEY_TARGET_HASH: H, cs.KEY_PROJECT_PREFIX: f"{A}."}
    ], "the statement re-validates hash and project itself; no name is passed"
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
    store.undefine(f"{A}.z.place")
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
    assert store.glosses["gloss:1"][cs.KEY_ANCHOR_STATE] == (
        cs.GlossAnchorState.MOVED.value
    )
    assert "coalesce(g.moved_from, g.target_qn) AS origin" in cq.CYPHER_GLOSS_MOVE


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


# --- the project is recorded, not read off the name --------------------------


def test_a_dotted_project_name_scopes_to_itself_not_to_its_first_segment() -> None:
    # `--project-name foo.bar` is legal. Splitting `target_qn` on the first
    # dot would scope the lookup to `foo.` and let a note from `foo.bar`
    # re-bind into `foo.baz` (local review). The recorded project is used
    # instead, and the prefix is the whole name plus a dot.
    store = FakeStore()
    store.define("foo.baz.mod.same", H)
    store.note("gloss:1", "foo.bar.mod.gone", H, project="foo.bar")
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=[], lost=["gloss:1"])
    lookups = [p for q, p in store.reads if q == cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH]
    assert [p[cs.KEY_PROJECT_PREFIX] for p in lookups] == ["foo.bar."]
    # And the other direction: the note's own project is where it may land.
    store.define("foo.bar.mod.here", H)
    assert _run(store).moved == ["gloss:1"]
    assert store.glosses["gloss:1"][cs.KEY_TARGET_QN] == "foo.bar.mod.here"


def test_a_legacy_note_without_a_project_takes_the_longest_registered_prefix() -> None:
    # A note written before `project` was recorded has only its qn to go on.
    # Among the registered projects, the longest one that prefixes the qn is
    # the owner: `foo.bar` over `foo`. Read once per pass.
    store = FakeStore()
    store.projects = ["foo", "foo.bar", "other"]
    store.define("foo.bar.mod.here", H)
    store.define("foo.mod.decoy", H)
    store.note("gloss:1", "foo.bar.mod.gone", H, project=None)
    report = _run(store)
    assert report.moved == ["gloss:1"]
    assert store.glosses["gloss:1"][cs.KEY_TARGET_QN] == "foo.bar.mod.here"
    assert [q for q, _ in store.reads].count(cq.CYPHER_LIST_PROJECTS) == 1


def test_a_legacy_note_no_project_prefixes_is_lost() -> None:
    store = FakeStore()
    store.projects = ["other"]
    store.define("foo.bar.mod.here", H)
    store.note("gloss:1", "foo.bar.mod.gone", H, project=None)
    assert _run(store).lost == ["gloss:1"]
    assert all(q != cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH for q, _ in store.reads)


def test_the_project_list_is_not_read_when_every_note_records_its_project() -> None:
    store = FakeStore()
    store.note("gloss:1", f"{A}.gone", H)
    _run(store)
    assert all(q != cq.CYPHER_LIST_PROJECTS for q, _ in store.reads)


# --- physical nodes, not names -----------------------------------------------


def test_two_nodes_sharing_one_name_and_the_hash_are_ambiguous() -> None:
    # A Function and a Method can share a qualified name (`Acc.total`) and,
    # with identical bodies, the hash. Counting distinct NAMES would see one
    # candidate and a name-keyed move would bind BOTH nodes (bot review).
    # Two physical rows are two candidates: AMBIGUOUS, one name listed.
    store = FakeStore()
    store.define(f"{A}.Acc.total", H)
    store.define(f"{A}.Acc.total", H)
    store.note("gloss:1", f"{A}.old.total", H)
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=["gloss:1"], lost=[])
    assert _writes(store, cq.CYPHER_GLOSS_MOVE) == []
    [mark] = _writes(store, cq.CYPHER_GLOSS_MARK)
    assert mark is not None
    assert mark[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.AMBIGUOUS.value
    assert mark[cs.KEY_CANDIDATE_QNS] == [f"{A}.Acc.total"]
    assert "gloss:1" not in store.attached


def test_a_note_attached_by_another_writer_meanwhile_is_left_alone() -> None:
    # Between the pass's lookup and its write an agent files the same note
    # against a definition of its own, so the note now HAS a subject. The
    # statement re-checks that precondition itself and does nothing; a MERGE
    # here would have given the note two subjects. The pass reads it back,
    # finds it recording the other writer's target, reports nothing and
    # writes no MARK.
    store = FakeStore()
    store.define(f"{A}.new.place", H)
    store.define(f"{A}.chosen.place", "ah1:other")
    store.note("gloss:1", f"{A}.old.place", H)

    def attach_elsewhere() -> None:
        store.glosses["gloss:1"][cs.KEY_TARGET_QN] = f"{A}.chosen.place"
        store.attached.add("gloss:1")

    store.after_lookup = attach_elsewhere
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=[], lost=[])
    assert len(_writes(store, cq.CYPHER_GLOSS_MOVE)) == 1, "the move was attempted"
    assert _writes(store, cq.CYPHER_GLOSS_MARK) == []
    g = store.glosses["gloss:1"]
    assert g[cs.KEY_TARGET_QN] == f"{A}.chosen.place"
    assert g[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.EXACT.value
    assert g[cs.KEY_MOVED_FROM] is None
    assert "gloss:1" in store.attached


def test_a_move_declined_at_write_time_is_neither_moved_nor_marked() -> None:
    # Between the pass's lookup and its write another updater adds a second
    # definition with the hash. The statement counts its targets itself,
    # finds two, and does nothing; the pass reads the note back, sees it
    # unmoved, and reports nothing -- no MARK either, since the verdict it
    # read is stale. The next pass decides on the graph as it is then.
    store = FakeStore()
    store.define(f"{A}.new.place", H)
    store.note("gloss:1", f"{A}.old.place", H)
    store.after_lookup = lambda: store.define(f"{A}.other.place", H)
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=[], lost=[])
    assert len(_writes(store, cq.CYPHER_GLOSS_MOVE)) == 1, "the move was attempted"
    assert _writes(store, cq.CYPHER_GLOSS_MARK) == []
    g = store.glosses["gloss:1"]
    assert g[cs.KEY_TARGET_QN] == f"{A}.old.place"
    assert g[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.EXACT.value
    assert "gloss:1" not in store.attached
    # The read-back is what told the pass the move did not land.
    assert any(q == cq.CYPHER_GLOSS_READ for q, _ in store.reads)
    # And the following pass, with both definitions present, marks it.
    assert _run(store).ambiguous == ["gloss:1"]


# --- a hash that leads back home ---------------------------------------------


def test_a_moved_note_whose_hash_returns_to_its_origin_is_exact_again() -> None:
    # MOVED A -> B; B is gone; A is back with the same hash. Following the
    # hash to A is a return, not a second move: EXACT, and no `moved_from`
    # (local review: it read MOVED from itself).
    store = FakeStore()
    store.define(f"{A}.first", H)
    store.note(
        "gloss:1",
        f"{A}.second",
        H,
        state=cs.GlossAnchorState.MOVED.value,
        moved_from=f"{A}.first",
    )
    assert _run(store).moved == ["gloss:1"]
    g = store.glosses["gloss:1"]
    assert g[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.EXACT.value
    assert g[cs.KEY_MOVED_FROM] is None
    assert g[cs.KEY_TARGET_QN] == f"{A}.first"


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
    # The statement re-validates its precondition at write time: it collects
    # the physical nodes carrying the hash in the project and binds only if
    # there is exactly one -- so a same-name pair gets no edge (a name-keyed
    # MATCH would bind both) and a match that appeared since the pass's read
    # makes it a no-op. No name is passed in.
    q = cq.CYPHER_GLOSS_MOVE
    assert "$new_qn" not in q
    assert "t.anchor_hash = $target_hash" in q
    assert "t.qualified_name STARTS WITH $project_prefix" in q
    assert "WITH g, collect(t) AS targets" in q
    assert "WHERE size(targets) = 1" in q
    # The note's own precondition is re-checked inside the write: a note that
    # gained a subject since the pass's read is not given a second one. Asked
    # with an OPTIONAL MATCH and a count, never a pattern in WHERE (Memgraph 3).
    assert "OPTIONAL MATCH (g)-[held:ANNOTATES]->()" in q
    assert "WITH g, t, origin, count(held) AS subjects" in q
    assert "WHERE subjects = 0" in q
    # The origin is read in a WITH before the SETs, so neither SET can see
    # the other's new value.
    assert "WITH g, targets[0] AS t, coalesce(g.moved_from, g.target_qn) AS origin" in q
    assert (
        f"CASE WHEN origin = t.qualified_name\n    THEN '{cs.GlossAnchorState.EXACT.value}' "
        f"ELSE '{cs.GlossAnchorState.MOVED.value}' END" in q
    )
    assert (
        "g.moved_from = CASE WHEN origin = t.qualified_name THEN null ELSE origin END"
        in q
    )
    assert "g.target_qn = t.qualified_name" in q
    assert "g.candidate_qns = null" in q
    assert f"MERGE (g)-[:{cs.RelationshipType.ANNOTATES.value}]->(t)" in q


def test_the_mark_writes_state_and_candidates_and_no_edge() -> None:
    q = cq.CYPHER_GLOSS_MARK
    assert "SET g.anchor_state = $anchor_state, g.candidate_qns = $candidate_qns" in q
    assert "MERGE" not in q
    assert cs.RelationshipType.ANNOTATES.value not in q


def test_the_orphan_read_returns_the_same_row_shape_as_the_other_reads() -> None:
    q = cq.CYPHER_GLOSSES_ORPHANED_ON
    assert "(g.target_qn = $qn OR g.target_qn ENDS WITH $suffix)" in q
    assert "g.project = $project_name" in q
    assert "(g.project IS NULL AND g.target_qn STARTS WITH $project_prefix)" in q
    assert "g.moved_from AS moved_from" in q
    assert "g.candidate_qns AS candidate_qns" in q
    assert "collect(m.qualified_name) AS mentions" in q
    assert q.split("RETURN", 1)[1] == cq.CYPHER_GLOSSES_ANNOTATING.split("RETURN", 1)[1]


# --- the text-quote tier (stage five) ----------------------------------------

# Three non-blank lines follow `run`, the full context window, so growth
# below the window does not move the suffix (a fixed window is a tie-breaker,
# not an anchor: appending code right after a definition changes it).
_ORIGINAL = (
    "import os\n\n\ndef run(v):\n    y = helper(v)\n    return y * 2\n\n\n"
    "def tail():\n    pass\n\n\nclass Other:\n    pass\n"
)
_RENAMED = _ORIGINAL.replace("def run(v):", "def execute(v):")
RUN = f"{A}.mod.run"
EXECUTE = f"{A}.mod.execute"


def _quoted_store(*, source_after: str = _RENAMED) -> FakeStore:
    """A note written on `run` (hash H, quote of its body); `run` is gone and
    `execute` now holds the same body under a different hash."""
    store = FakeStore()
    anchor = text_anchor(_ORIGINAL, "run", 4, 6)
    assert anchor is not None
    store.note("n1", RUN, H, anchor=anchor)
    store.define(EXECUTE, H2, name="execute", path="mod.py", span=(4, 6))
    store.files["mod.py"] = source_after
    return store


# `run` renamed to `execute` AND moved to the end of the file, so its
# neighbours differ from the recorded ones; with one body match the context
# is not consulted, and what the note records afterwards is the new context.
_RENAMED_AND_MOVED = (
    "import os\n\n\ndef tail():\n    pass\n\n\nclass Other:\n    pass\n\n\n"
    "def execute(v):\n    y = helper(v)\n    return y * 2\n"
)


def test_a_renamed_definition_is_found_by_its_body_and_followed() -> None:
    store = _quoted_store(source_after=_RENAMED_AND_MOVED)
    store.definitions, store.spans = [], []
    store.define(EXECUTE, H2, name="execute", path="mod.py", span=(12, 14))
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=["n1"], ambiguous=[], lost=[])
    props = store.glosses["n1"]
    assert props[cs.KEY_TARGET_QN] == EXECUTE
    assert props[cs.KEY_MOVED_FROM] == RUN
    assert props[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.MOVED.value
    assert "n1" in store.attached
    # The hash is NOT re-recorded: the name is part of it, and a rename is
    # a signature change the grading pass should report as STALE.
    assert props[cs.KEY_TARGET_HASH] == H
    # The neighbours are re-recorded for the new location, not kept.
    recorded = text_anchor(_ORIGINAL, "run", 4, 6)
    now = text_anchor(_RENAMED_AND_MOVED, "execute", 12, 14)
    assert recorded is not None and now is not None
    assert (now.prefix, now.suffix) != (recorded.prefix, recorded.suffix)
    assert (props[cs.KEY_ANCHOR_PREFIX], props[cs.KEY_ANCHOR_SUFFIX]) == (
        now.prefix,
        now.suffix,
    )


def test_the_hash_tier_is_tried_first_and_the_quote_never_consulted() -> None:
    store = _quoted_store()
    # Another definition still carries the hash: that is a same-name move
    # the hash tier owns, whatever the bodies say.
    store.define(f"{A}.other.run", H)
    report = _run(store, with_source=True)
    assert report.moved == ["n1"]
    assert store.glosses["n1"][cs.KEY_TARGET_QN] == f"{A}.other.run"
    assert store.source_reads == []
    assert all(q != cq.CYPHER_DEFINITION_SPANS for q, _p in store.reads)


def test_identical_bodies_are_told_apart_by_their_neighbours() -> None:
    # `alpha` has the same body as `run` had, appended at the end of the
    # file; `execute` has it where `run` was, between `import os` and `tail`.
    twin = _RENAMED + "\n\ndef alpha(v):\n    y = helper(v)\n    return y * 2\n"
    store = _quoted_store(source_after=twin)
    store.define(
        f"{A}.mod.alpha", "ah1:aaaa", name="alpha", path="mod.py", span=(17, 19)
    )
    report = _run(store, with_source=True)
    assert report.moved == ["n1"]
    assert store.glosses["n1"][cs.KEY_TARGET_QN] == EXECUTE


def test_identical_bodies_and_neighbours_are_ambiguous_and_bind_nothing() -> None:
    # A second file that is a copy of the first under another name: the same
    # body AND the same neighbours, so nothing tells the two apart.
    store = _quoted_store()
    store.files["twin.py"] = _ORIGINAL.replace("def run(v):", "def again(v):")
    store.define(
        f"{A}.mod.again", "ah1:bbbb", name="again", path="twin.py", span=(4, 6)
    )
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=["n1"], lost=[])
    props = store.glosses["n1"]
    assert props[cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.AMBIGUOUS.value
    assert props[cs.KEY_CANDIDATE_QNS] == [f"{A}.mod.again", EXECUTE]
    assert props[cs.KEY_TARGET_QN] == RUN
    assert "n1" not in store.attached


def test_identical_bodies_with_new_neighbours_are_ambiguous_not_lost() -> None:
    """The context is a tie-breaker only: when it narrows the body matches
    to nothing, every body match stands and is listed."""
    moved_twice = (
        _RENAMED_AND_MOVED + "\n\ndef again(v):\n    y = helper(v)\n    return y * 2\n"
    )
    store = _quoted_store(source_after=moved_twice)
    store.definitions, store.spans = [], []
    store.define(EXECUTE, H2, name="execute", path="mod.py", span=(12, 14))
    store.define(
        f"{A}.mod.again", "ah1:bbbb", name="again", path="mod.py", span=(17, 19)
    )
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=["n1"], lost=[])
    assert store.glosses["n1"][cs.KEY_CANDIDATE_QNS] == [f"{A}.mod.again", EXECUTE]


def test_no_body_match_is_lost() -> None:
    edited = _RENAMED.replace("return y * 2", "return y * 3")
    store = _quoted_store(source_after=edited)
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=[], lost=["n1"])
    assert store.glosses["n1"][cs.KEY_ANCHOR_STATE] == cs.GlossAnchorState.LOST.value


def test_without_a_source_reader_the_quote_tier_does_not_run() -> None:
    store = _quoted_store()
    report = _run(store)
    assert report == RepairReport(moved=[], ambiguous=[], lost=["n1"])
    assert all(q != cq.CYPHER_DEFINITION_SPANS for q, _p in store.reads)


def test_a_note_without_a_quote_reads_no_file() -> None:
    store = _quoted_store()
    store.note("n1", RUN, H)  # re-recorded without an anchor
    report = _run(store, with_source=True)
    assert report.lost == ["n1"]
    assert store.source_reads == []
    assert all(q != cq.CYPHER_DEFINITION_SPANS for q, _p in store.reads)


def test_one_span_query_and_one_read_per_file_per_project() -> None:
    store = _quoted_store()
    anchor = text_anchor(_ORIGINAL, "run", 4, 6)
    store.note("n2", f"{A}.mod.other", "ah1:cccc", anchor=anchor)
    store.define(f"{A}.util.f", "ah1:dddd", name="f", path="util.py", span=(1, 2))
    store.files["util.py"] = "def f():\n    pass\n"
    _run(store, with_source=True)
    assert [q for q, _p in store.reads if q == cq.CYPHER_DEFINITION_SPANS] == [
        cq.CYPHER_DEFINITION_SPANS
    ]
    assert sorted(store.source_reads) == [(A, "mod.py"), (A, "util.py")]


def test_the_reader_is_asked_for_the_notes_own_project_only() -> None:
    store = _quoted_store()
    # A same-body definition in another project is never a candidate: the
    # span query is prefixed by the note's project.
    store.define(f"{B}.mod.execute", H2, name="execute", path="mod.py", span=(4, 6))
    report = _run(store, with_source=True)
    assert report.moved == ["n1"]
    assert store.glosses["n1"][cs.KEY_TARGET_QN] == EXECUTE
    assert store.source_reads == [(A, "mod.py")]


def test_a_file_the_reader_cannot_supply_contributes_no_candidates() -> None:
    store = _quoted_store()
    del store.files["mod.py"]
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=[], lost=["n1"])


def test_a_same_name_pair_with_one_body_is_ambiguous_not_moved() -> None:
    store = _quoted_store()
    store.define(EXECUTE, "ah1:eeee", name="execute", path="mod.py", span=(4, 6))
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=["n1"], lost=[])
    assert store.glosses["n1"][cs.KEY_CANDIDATE_QNS] == [EXECUTE]
    assert "n1" not in store.attached


def test_a_quote_move_declined_at_write_time_is_neither_moved_nor_marked() -> None:
    store = _quoted_store()
    store.after_spans = lambda: store.attached.add("n1")
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=[], lost=[])
    assert _writes(store, cq.CYPHER_GLOSS_MARK) == []
    assert store.glosses["n1"][cs.KEY_TARGET_QN] == RUN


def test_a_second_pass_after_a_quote_move_writes_nothing() -> None:
    store = _quoted_store()
    _run(store, with_source=True)
    before = len(store.writes)
    report = _run(store, with_source=True)
    assert report == RepairReport(moved=[], ambiguous=[], lost=[])
    assert len(store.writes) == before


def test_the_quote_move_is_bound_by_name_and_rewrites_the_context_only() -> None:
    q = cq.CYPHER_GLOSS_MOVE_TO_QN
    assert "DELETE" not in q.upper()
    assert "t.qualified_name = $target_qn" in q
    assert "$target_hash" not in q
    assert "g.target_hash" not in q
    assert "size(targets) = 1" in q
    assert "subjects = 0" in q
    assert "g.anchor_prefix = $anchor_prefix" in q
    assert "g.anchor_suffix = $anchor_suffix" in q
    assert "g.anchor_quote" not in q


def test_the_span_query_is_project_scoped_and_returns_what_the_digest_needs() -> None:
    q = cq.CYPHER_DEFINITION_SPANS
    assert "STARTS WITH $project_prefix" in q
    for field in ("qualified_name", "name", "path", "start_line", "end_line"):
        assert f"AS {field}" in q
    for field in ("anchor_quote", "anchor_prefix", "anchor_suffix"):
        assert f"AS {field}" in cq.CYPHER_UNANCHORED_GLOSSES
