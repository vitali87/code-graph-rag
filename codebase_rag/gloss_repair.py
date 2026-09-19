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
from collections.abc import Callable
from typing import NamedTuple

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
from .types_defs import PropertyDict, ResultRow

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
        TextAnchor(quote, prefix, suffix)  # type: ignore[arg-type]
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


def _projects_of(fetch_all: QueryFn, notes: list[_Unanchored]) -> dict[str, str | None]:
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
            self._by_project[project] = self._build(project)
        return self._by_project[project].get(quote, [])

    def _build(self, project: str) -> dict[str, list[_QuoteCandidate]]:
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
        comparable = note.comparable_hash
        project = projects.get(note.qualified_name)
        if project is None:
            _update_anchor_verdict(execute_write, note, cs.GlossAnchorState.LOST, [])
            report.lost.append(note.qualified_name)
            continue
        # Physical rows, not distinct names: a same-name pair is two.
        rows = candidates.get((project, comparable), []) if comparable else []
        if not rows and note.anchor is not None and quotes is not None:
            _repair_by_quote(fetch_all, execute_write, note, project, quotes, report)
            continue
        if len(rows) == 1:
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
    return report


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
