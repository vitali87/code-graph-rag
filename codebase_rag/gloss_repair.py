"""The repair tiers below the name match: MOVED, AMBIGUOUS, LOST.

Stage four of issue #1808. After a sync, `CYPHER_REANCHOR_GLOSSES` re-attaches
every note whose subject still exists under the recorded name. This module
takes the notes that pass did not reach -- their name is gone -- and places
each one by the content hash it recorded when it was written:

* exactly one definition in the note's project carries that hash: the
  definition was moved under the same name (a rename changes the hash, since
  the name is part of it), and the note follows it, graded MOVED, with the
  old name kept in `moved_from` so the move stays visible -- or EXACT again
  if the hash has led it back to the name it was first written against;
* several carry it: the note is graded AMBIGUOUS, attached to nothing, with
  the candidates recorded in `candidate_qns` for a reader to pick from;
* none carries it, or the note has no comparable hash (a class or module
  subject, or a note from before hashes existed): graded LOST, attached to
  nothing.

Two rules from the issue hold throughout. A note is never bound to a subject
on a guess: the hash is exact or it is not, and one match means one. And a
note that cannot be placed becomes visibly orphaned -- LOST and AMBIGUOUS are
states a reader sees on the old name (`gloss.glosses_for`), never a silent
deletion or a silent re-bind. Nothing here deletes anything.

The lookup is scoped to the note's own project, because two projects can hold
byte-identical definitions and a note about one of them says nothing about the
other. The project is the one recorded on the note at write time; it is not
read back off `target_qn`, because a project name may itself contain dots
(`foo.bar` and `foo.baz` share a first segment). A note from before the
project was recorded falls back to the longest registered project name that
prefixes its `target_qn`, fetched once per pass and only when such a note
exists; if no project prefixes it, the note is LOST.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from typing import NamedTuple, Protocol

from . import constants as cs
from . import cypher_queries as cq
from .gloss_anchor import (
    ParsedSource,
    SourceReader,
    TextAnchor,
    is_comparable_quote,
    text_anchor,
)
from .graph_query import QueryFn
from .types_defs import PropertyDict, ResultRow, ResultValue

WriteFn = Callable[[str, PropertyDict | None], None]


class RepairReport(NamedTuple):
    """What one repair pass did, by note key, each list sorted."""

    moved: list[str]
    ambiguous: list[str]
    lost: list[str]


class _Unanchored(NamedTuple):
    qualified_name: str
    target_qn: str
    target_hash: str | None
    anchor_state: str
    candidate_qns: list[str]
    # As recorded on the note; None on a note from before it was recorded.
    project: str | None
    # The text-quote anchor (stage five); None on a note written without one.
    anchor: TextAnchor | None

    @property
    def comparable_hash(self) -> str | None:
        """The recorded hash, if it is in the current format; else None."""
        if self.target_hash and self.target_hash.startswith(cs.ANCHOR_HASH_VERSION):
            return self.target_hash
        return None


def _unanchored(row: ResultRow) -> _Unanchored:
    raw = row.get(cs.KEY_CANDIDATE_QNS)
    candidates = (
        sorted(str(c) for c in raw if c is not None) if isinstance(raw, list) else []
    )
    hash_value = row.get(cs.KEY_TARGET_HASH)
    project = row.get(cs.KEY_PROJECT)
    quote = row.get(cs.KEY_ANCHOR_QUOTE)
    prefix = row.get(cs.KEY_ANCHOR_PREFIX)
    suffix = row.get(cs.KEY_ANCHOR_SUFFIX)
    anchor = (
        TextAnchor(quote, prefix, suffix)
        if is_comparable_quote(quote)
        and isinstance(prefix, str)
        and isinstance(suffix, str)
        else None
    )
    return _Unanchored(
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        target_qn=str(row.get(cs.KEY_TARGET_QN, "")),
        target_hash=hash_value if isinstance(hash_value, str) else None,
        anchor_state=str(row.get(cs.KEY_ANCHOR_STATE, "")),
        candidate_qns=candidates,
        project=project if isinstance(project, str) and project else None,
        anchor=anchor,
    )


class _Recorded(Protocol):
    """What `_projects_of` reads off a note: its key, subject and project."""

    @property
    def qualified_name(self) -> str: ...
    @property
    def target_qn(self) -> str: ...
    @property
    def project(self) -> str | None: ...


def _projects_of(
    fetch_all: QueryFn, notes: Sequence[_Recorded]
) -> dict[str, str | None]:
    """Each note's project, by note key.

    The recorded `project` wins. A note without one (written before the
    property existed) takes the longest registered project name that
    prefixes its `target_qn`; the project list is read once, and only if such
    a note exists. None means no project claims the note.
    """
    projects: dict[str, str | None] = {}
    legacy = [n for n in notes if n.project is None]
    names: list[str] = []
    if legacy:
        for row in fetch_all(cq.CYPHER_LIST_PROJECTS, None):
            name = row.get(cs.KEY_NAME)
            if isinstance(name, str) and name:
                names.append(name)
        # Longest first, so `foo.bar` claims `foo.bar.mod.f` ahead of `foo`.
        names.sort(key=len, reverse=True)
    for note in notes:
        if note.project is not None:
            projects[note.qualified_name] = note.project
            continue
        projects[note.qualified_name] = next(
            (
                name
                for name in names
                if note.target_qn.startswith(f"{name}{cs.SEPARATOR_DOT}")
            ),
            None,
        )
    return projects


def _candidates_by_hash(
    fetch_all: QueryFn, notes: list[_Unanchored], projects: dict[str, str | None]
) -> dict[tuple[str, str], list[str]]:
    """Definitions carrying each note's hash, one query per project.

    Keyed on (project, hash) so a definition in another project that happens
    to carry the same hash is never a candidate. The prefix is the full
    project name plus a dot, so `foo.bar.` never admits `foo.baz.x`.
    """
    hashes_by_project: dict[str, set[str]] = defaultdict(set)
    for note in notes:
        comparable = note.comparable_hash
        project = projects.get(note.qualified_name)
        if comparable is not None and project is not None:
            hashes_by_project[project].add(comparable)
    # One entry per PHYSICAL row: two nodes sharing a qualified name (a
    # Function and a Method, say) are two candidates, and the move statement
    # counts them the same way.
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for project in sorted(hashes_by_project):
        rows = fetch_all(
            cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH,
            {
                cs.KEY_HASHES: sorted(hashes_by_project[project]),
                cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
            },
        )
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            anchor_hash = row.get(cs.KEY_ANCHOR_HASH)
            if isinstance(qn, str) and isinstance(anchor_hash, str):
                found[(project, anchor_hash)].append(qn)
    return found


class _QuoteCandidate(NamedTuple):
    qualified_name: str
    anchor: TextAnchor
    # What the index saw, re-checked by the move statement: another
    # updater may have replaced the definition since (bot review, PR #1966).
    path: str
    start_line: int
    end_line: int
    anchor_hash: str | None


class _QuoteIndex:
    """Every definition's current text anchor, per project, built on demand.

    One span query and one read per file, per project, per pass -- and only
    for a project in which a note has reached this tier, so a graph with no
    quoted LOST notes never reads a file. Keyed on the quote digest; one
    entry per PHYSICAL row, so a same-name pair is two candidates, the same
    count the hash tier and the move statement use.
    """

    def __init__(self, fetch_all: QueryFn, read_source: SourceReader) -> None:
        self._fetch_all = fetch_all
        self._read_source = read_source
        self._by_project: dict[str, dict[str, list[_QuoteCandidate]]] = {}

    def candidates(self, project: str, quote: str) -> list[_QuoteCandidate]:
        if project not in self._by_project:
            self._by_project[project] = self._build_quote_index(project)
        return self._by_project[project].get(quote, [])

    def anchor_of(self, project: str, qualified_name: str) -> TextAnchor | None:
        """The current anchor of the one definition under a name, if any."""
        if project not in self._by_project:
            self._by_project[project] = self._build_quote_index(project)
        found = [
            row.anchor
            for rows in self._by_project[project].values()
            for row in rows
            if row.qualified_name == qualified_name
        ]
        return found[0] if len(found) == 1 else None

    def _build_quote_index(self, project: str) -> dict[str, list[_QuoteCandidate]]:
        rows = self._fetch_all(
            cq.CYPHER_DEFINITION_SPANS,
            {cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}"},
        )
        sources: dict[str, ParsedSource | None] = {}
        index: dict[str, list[_QuoteCandidate]] = defaultdict(list)
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            path = row.get(cs.KEY_PATH)
            start = row.get(cs.KEY_START_LINE)
            end = row.get(cs.KEY_END_LINE)
            name = row.get(cs.KEY_NAME)
            if not (
                isinstance(qn, str)
                and isinstance(path, str)
                and isinstance(start, int)
                and isinstance(end, int)
            ):
                continue
            if path not in sources:
                sources[path] = self._read_source(project, path)
            source = sources[path]
            if source is None:
                continue
            anchor = text_anchor(
                source, name if isinstance(name, str) else None, start, end
            )
            if anchor is not None:
                hash_value = row.get(cs.KEY_ANCHOR_HASH)
                index[anchor.quote].append(
                    _QuoteCandidate(
                        qn,
                        anchor,
                        path,
                        start,
                        end,
                        hash_value if isinstance(hash_value, str) else None,
                    )
                )
        return index


def _narrow_by_context(
    anchor: TextAnchor, rows: list[_QuoteCandidate]
) -> list[_QuoteCandidate]:
    """Among identical bodies, the ones with the note's recorded neighbours.

    Only a tie-break: with one body match the context is not consulted (a
    definition that moved within its file keeps its note), and when the
    context narrows to nothing the full set stands, so the verdict is
    AMBIGUOUS with every body match listed rather than LOST.
    """
    if len(rows) < 2:
        return rows
    narrowed = [
        row
        for row in rows
        if row.anchor.prefix == anchor.prefix and row.anchor.suffix == anchor.suffix
    ]
    return narrowed or rows


def _update_anchor_verdict(
    execute_write: WriteFn,
    note: _Unanchored,
    state: cs.GlossAnchorState,
    qns: list[str],
) -> None:
    """Record a state the note does not already carry; a no-op otherwise.

    The pass runs after every sync, and most unattached notes stay unattached
    from one run to the next, so re-writing an unchanged verdict would be
    write churn on every run for nothing.
    """
    if note.anchor_state == state.value and note.candidate_qns == qns:
        return
    execute_write(
        cq.CYPHER_GLOSS_MARK,
        {
            cs.KEY_QN: note.qualified_name,
            cs.KEY_ANCHOR_STATE: state.value,
            cs.KEY_CANDIDATE_QNS: qns or None,
        },
    )


def repair_unanchored(
    fetch_all: QueryFn,
    execute_write: WriteFn,
    read_source: SourceReader | None = None,
) -> RepairReport:
    """Place every unattached note by content hash, then by text quote, or
    mark why it cannot be.

    The quote tier runs for a note the hash tier could not place (no
    comparable hash, or no definition carrying it) that recorded a quote,
    and only when `read_source` can supply the project's files: it digests
    every definition's current text the way the note's was digested at
    write time, so a renamed definition -- whose hash changed with its name
    -- is found by its unchanged body. Identical bodies are told apart by
    the recorded neighbours; several that still tie are AMBIGUOUS.

    The move statement re-validates hash, project and exactly-one physical
    target at write time and binds the node it found, so a match that
    appeared (or a hash that changed) between this pass's read and the write
    makes it a no-op. The note is read back after the move: `moved` lists it
    only if it now records the candidate as its subject -- including a note
    whose hash led it back to its origin, which the store grades EXACT. A
    declined move gets no mark and no report entry; the next pass sees the
    note unattached again and decides on the graph as it is then.
    Deterministic: notes are visited in key order and candidates are sorted,
    so the same graph yields the same writes. Raises nothing of its own; a
    store error propagates to the caller, which logs it and lets the next
    run retry (`GraphUpdater._reanchor_glosses`).
    """
    notes = sorted(
        (_unanchored(row) for row in fetch_all(cq.CYPHER_UNANCHORED_GLOSSES, None)),
        key=lambda n: n.qualified_name,
    )
    projects = _projects_of(fetch_all, notes)
    candidates = _candidates_by_hash(fetch_all, notes, projects)
    quotes = _QuoteIndex(fetch_all, read_source) if read_source is not None else None
    report = RepairReport(moved=[], ambiguous=[], lost=[])
    for note in notes:
        project = projects.get(note.qualified_name)
        if project is None:
            _update_anchor_verdict(execute_write, note, cs.GlossAnchorState.LOST, [])
            report.lost.append(note.qualified_name)
            continue
        _repair_note(
            fetch_all, execute_write, note, project, candidates, quotes, report
        )
    return report


def _repair_note(
    fetch_all: QueryFn,
    execute_write: WriteFn,
    note: _Unanchored,
    project: str,
    candidates: dict[tuple[str, str], list[str]],
    quotes: _QuoteIndex | None,
    report: RepairReport,
) -> None:
    """Place one note whose project is known: by hash, else by quote, else
    marked with why not."""
    comparable = note.comparable_hash
    # Physical rows, not distinct names: a same-name pair is two.
    rows = candidates.get((project, comparable), []) if comparable else []
    if not rows and note.anchor is not None and quotes is not None:
        _repair_by_quote(fetch_all, execute_write, note, project, quotes, report)
    elif len(rows) == 1:
        execute_write(
            cq.CYPHER_GLOSS_MOVE,
            {
                cs.KEY_QN: note.qualified_name,
                cs.KEY_TARGET_HASH: comparable,
                cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
            },
        )
        if _moved_to(fetch_all, note.qualified_name, rows[0]):
            report.moved.append(note.qualified_name)
    elif rows:
        _update_anchor_verdict(
            execute_write, note, cs.GlossAnchorState.AMBIGUOUS, sorted(set(rows))
        )
        report.ambiguous.append(note.qualified_name)
    else:
        _update_anchor_verdict(execute_write, note, cs.GlossAnchorState.LOST, [])
        report.lost.append(note.qualified_name)


def _repair_by_quote(
    fetch_all: QueryFn,
    execute_write: WriteFn,
    note: _Unanchored,
    project: str,
    quotes: _QuoteIndex,
    report: RepairReport,
) -> None:
    assert note.anchor is not None
    rows = _narrow_by_context(
        note.anchor, quotes.candidates(project, note.anchor.quote)
    )
    if len(rows) == 1 and rows[0].anchor_hash is None:
        # The move re-validates the candidate by its hash; without one the
        # body cannot be checked at write time, so the note is left as it is
        # for the next pass rather than bound on a name (bot review).
        return
    if len(rows) == 1:
        found = rows[0]
        execute_write(
            cq.CYPHER_GLOSS_MOVE_TO_QN,
            {
                cs.KEY_QN: note.qualified_name,
                cs.KEY_TARGET_QN: found.qualified_name,
                cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
                cs.KEY_PATH: found.path,
                cs.KEY_START_LINE: found.start_line,
                cs.KEY_END_LINE: found.end_line,
                cs.KEY_ANCHOR_HASH: found.anchor_hash,
                cs.KEY_ANCHOR_PREFIX: found.anchor.prefix,
                cs.KEY_ANCHOR_SUFFIX: found.anchor.suffix,
            },
        )
        if _moved_to(fetch_all, note.qualified_name, found.qualified_name):
            report.moved.append(note.qualified_name)
    elif rows:
        _update_anchor_verdict(
            execute_write,
            note,
            cs.GlossAnchorState.AMBIGUOUS,
            sorted({row.qualified_name for row in rows}),
        )
        report.ambiguous.append(note.qualified_name)
    else:
        _update_anchor_verdict(execute_write, note, cs.GlossAnchorState.LOST, [])
        report.lost.append(note.qualified_name)


def _moved_to(fetch_all: QueryFn, key: str, candidate: str) -> bool:
    """Whether the note now records `candidate` as its subject.

    The move statement may decline (its own count of physical targets was
    not one at write time), and it is silent either way, so the only
    evidence it landed is the note's record read back.
    """
    rows = fetch_all(cq.CYPHER_GLOSS_READ, {cs.KEY_QN: key})
    return bool(rows) and rows[0].get(cs.KEY_TARGET_QN) == candidate


# --- mentions (issue #3230) ---------------------------------------------------


class MentionReport(NamedTuple):
    """What one mention pass did, by note key, each list sorted."""

    # Notes with a mention followed to a new name.
    moved: list[str]
    # Notes with a mention that could not be placed.
    lost: list[str]


class _Mention(NamedTuple):
    qualified_name: str
    # The anchors recorded for it; None where none was (a note from before
    # mentions recorded them, a definition without a hash, a file the writer
    # could not read). An anchor in an older format simply matches nothing,
    # and a mention whose name still holds code then has its anchors renewed.
    anchor_hash: str | None
    quote: str | None
    lost: bool


class _MentionNote(NamedTuple):
    qualified_name: str
    target_qn: str
    project: str | None
    mentions: list[_Mention]
    # As stored, to tell whether the pass changed anything.
    stored: tuple[list[str], list[str], list[str], list[str]]
    attached: frozenset[str]


def _strings(value: ResultValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


def _mention_note(row: ResultRow) -> _MentionNote:
    qns = _strings(row.get(cs.KEY_MENTION_QNS))
    hashes = _strings(row.get(cs.KEY_MENTION_HASHES))
    quotes = _strings(row.get(cs.KEY_MENTION_QUOTES))
    lost = _strings(row.get(cs.KEY_MENTIONS_LOST))
    # Anchors are positional: a list that does not line up with the names
    # (a note from before they were recorded) is read as no anchors at all.
    if len(hashes) != len(qns):
        hashes = [""] * len(qns)
    if len(quotes) != len(qns):
        quotes = [""] * len(qns)
    project = row.get(cs.KEY_PROJECT)
    return _MentionNote(
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        target_qn=str(row.get(cs.KEY_TARGET_QN, "")),
        project=project if isinstance(project, str) and project else None,
        mentions=[
            _Mention(
                qualified_name=qn,
                anchor_hash=h or None,
                quote=q or None,
                lost=qn in lost,
            )
            for qn, h, q in zip(qns, hashes, quotes, strict=True)
        ],
        stored=(qns, hashes, quotes, sorted(lost)),
        attached=frozenset(_strings(row.get(cs.KEY_ATTACHED))),
    )


class _Placed(NamedTuple):
    """A mention after the pass: where it is, and how to bind it."""

    qualified_name: str
    anchor_hash: str
    quote: str
    lost: bool
    # The hash the write re-validates the binding by; "" binds by name.
    expect_hash: str = ""
    moved: bool = False


class _MentionPlacer:
    """Places one note's mentions: by name while the name holds the recorded
    code, else by hash, then by quote, else marked lost."""

    def __init__(
        self,
        fetch_all: QueryFn,
        current: dict[str, list[str | None]],
        quotes: _QuoteIndex | None,
    ) -> None:
        self._fetch_all = fetch_all
        self._current = current
        self._quotes = quotes
        self._by_hash: dict[tuple[str, str], list[str]] = {}

    def place(self, mention: _Mention, project: str | None) -> _Placed:
        hashes = self._current.get(mention.qualified_name, [])
        if hashes and mention.anchor_hash in hashes:
            # The name still holds the recorded code (or, for a definition
            # with no hash, there was none to record): bound by name.
            return _Placed(
                mention.qualified_name,
                mention.anchor_hash or "",
                mention.quote or "",
                lost=False,
            )
        found = self._elsewhere(mention, project) if project is not None else []
        if len(found) == 1:
            qn, anchor_hash = found[0]
            return _Placed(
                qn,
                anchor_hash,
                mention.quote or "",
                lost=False,
                expect_hash=anchor_hash,
                moved=qn != mention.qualified_name,
            )
        if hashes and not mention.lost:
            # The name holds other code and the recorded code went nowhere
            # one can point to: the definition was edited in place, or the
            # note is from before mentions recorded anchors. Its anchors are
            # renewed so a later move is followed from here.
            return self._renewed(mention, project, hashes)
        return _Placed(
            mention.qualified_name,
            mention.anchor_hash or "",
            mention.quote or "",
            lost=True,
        )

    def _renewed(
        self, mention: _Mention, project: str | None, hashes: list[str | None]
    ) -> _Placed:
        anchor_hash = hashes[0] if len(hashes) == 1 else None
        anchor = (
            self._quotes.anchor_of(project, mention.qualified_name)
            if self._quotes is not None and project is not None
            else None
        )
        return _Placed(
            mention.qualified_name,
            anchor_hash or mention.anchor_hash or "",
            anchor.quote if anchor is not None else mention.quote or "",
            lost=False,
        )

    def _elsewhere(self, mention: _Mention, project: str) -> list[tuple[str, str]]:
        """Where the recorded code is now, one entry per physical node."""
        if mention.anchor_hash is not None:
            rows = self._with_hash(project, mention.anchor_hash)
            if rows:
                return [(qn, mention.anchor_hash) for qn in rows]
        if mention.quote is None or self._quotes is None:
            return []
        # A candidate the write cannot re-validate (no hash) is no home.
        return [
            (row.qualified_name, row.anchor_hash)
            for row in self._quotes.candidates(project, mention.quote)
            if row.anchor_hash is not None
        ]

    def _with_hash(self, project: str, anchor_hash: str) -> list[str]:
        key = (project, anchor_hash)
        if key not in self._by_hash:
            rows = self._fetch_all(
                cq.CYPHER_DEFINITIONS_BY_ANCHOR_HASH,
                {
                    cs.KEY_HASHES: [anchor_hash],
                    cs.KEY_PROJECT_PREFIX: f"{project}{cs.SEPARATOR_DOT}",
                },
            )
            self._by_hash[key] = [
                qn
                for row in rows
                if isinstance(qn := row.get(cs.KEY_QUALIFIED_NAME), str)
            ]
        return self._by_hash[key]


def repair_mentions(
    fetch_all: QueryFn,
    execute_write: WriteFn,
    read_source: SourceReader | None = None,
) -> MentionReport:
    """Place every attached note's mentions, then rebuild their edges.

    A mention stays on its name while that name holds the code recorded when
    the note was written. When
    the name is gone, or holds other code, the recorded code is looked for
    in the note's project, by content hash and then by text quote, as the
    subject is: exactly one home is followed, renaming the mention. Failing
    that, a name that is still there was edited in place and keeps the
    mention, with its anchors renewed; a name that is gone leaves the
    mention in `mentions_lost`, with no edge, until its code comes back. A
    name reused by unrelated code therefore never takes a mention by name
    alone: not while the recorded code is elsewhere, and not once the
    mention is lost.

    A note is written only when its mentions or edges differ from what the
    pass read, so an unchanged graph costs one read. Deterministic: notes
    and mentions are visited in key order.
    """
    notes = sorted(
        (
            _mention_note(row)
            for row in fetch_all(cq.CYPHER_GLOSS_MENTION_ANCHORS, None)
        ),
        key=lambda n: n.qualified_name,
    )
    report = MentionReport(moved=[], lost=[])
    if not notes:
        return report
    names = sorted({m.qualified_name for note in notes for m in note.mentions})
    current: dict[str, list[str | None]] = defaultdict(list)
    for row in fetch_all(cq.CYPHER_DEFINITIONS_BY_QNS, {cs.KEY_QNS: names}):
        qn = row.get(cs.KEY_QUALIFIED_NAME)
        anchor_hash = row.get(cs.KEY_ANCHOR_HASH)
        if isinstance(qn, str):
            current[qn].append(anchor_hash if isinstance(anchor_hash, str) else None)
    projects = _projects_of(fetch_all, notes)
    quotes = _QuoteIndex(fetch_all, read_source) if read_source is not None else None
    placer = _MentionPlacer(fetch_all, current, quotes)
    for note in notes:
        project = projects.get(note.qualified_name)
        placed: dict[str, _Placed] = {}
        for mention in note.mentions:
            found = placer.place(mention, project)
            placed.setdefault(found.qualified_name, found)
        _write_mentions(execute_write, note, project, placed)
        if any(p.moved for p in placed.values()):
            report.moved.append(note.qualified_name)
        if any(p.lost for p in placed.values()):
            report.lost.append(note.qualified_name)
    return report


def _write_mentions(
    execute_write: WriteFn,
    note: _MentionNote,
    project: str | None,
    placed: dict[str, _Placed],
) -> None:
    ordered = [placed[qn] for qn in sorted(placed)]
    bound = [p for p in ordered if not p.lost]
    lists = (
        [p.qualified_name for p in ordered],
        [p.anchor_hash for p in ordered],
        [p.quote for p in ordered],
        sorted(p.qualified_name for p in ordered if p.lost),
    )
    if lists == note.stored and note.attached == {p.qualified_name for p in bound}:
        return
    # A note no project claims keeps its names unscoped, as before.
    prefix = f"{project}{cs.SEPARATOR_DOT}" if project is not None else ""
    execute_write(
        cq.CYPHER_GLOSS_SET_MENTIONS,
        {
            cs.KEY_QN: note.qualified_name,
            cs.KEY_MENTION_QNS: lists[0],
            cs.KEY_MENTION_HASHES: lists[1],
            cs.KEY_MENTION_QUOTES: lists[2],
            cs.KEY_MENTIONS_LOST: lists[3] or None,
            cs.KEY_ATTACH_QNS: [p.qualified_name for p in bound],
            cs.KEY_ATTACH_HASHES: [p.expect_hash for p in bound],
            cs.KEY_PROJECT_PREFIX: prefix,
        },
    )
