"""The repair tiers below the name match: MOVED, AMBIGUOUS, LOST.

Stage four of issue #1808. After a sync, `CYPHER_REANCHOR_GLOSSES` re-attaches
every note whose subject still exists under the recorded name. This module
takes the notes that pass did not reach -- their name is gone -- and places
each one by the content hash it recorded when it was written:

* exactly one definition in the note's project carries that hash: the
  definition was renamed or moved, and the note follows it, graded MOVED,
  with the old name kept in `moved_from` so the move stays visible;
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

The lookup is scoped to the note's own project (the first segment of
`target_qn`), because two projects can hold byte-identical definitions and a
note about one of them says nothing about the other.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import NamedTuple

from . import constants as cs
from . import cypher_queries as cq
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

    @property
    def project(self) -> str:
        return self.target_qn.split(cs.SEPARATOR_DOT, 1)[0]

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
    return _Unanchored(
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        target_qn=str(row.get(cs.KEY_TARGET_QN, "")),
        target_hash=hash_value if isinstance(hash_value, str) else None,
        anchor_state=str(row.get(cs.KEY_ANCHOR_STATE, "")),
        candidate_qns=candidates,
    )


def _candidates_by_hash(
    fetch_all: QueryFn, notes: list[_Unanchored]
) -> dict[tuple[str, str], list[str]]:
    """Definitions carrying each note's hash, one query per project.

    Keyed on (project, hash) so a definition in another project that happens
    to carry the same hash is never a candidate.
    """
    hashes_by_project: dict[str, set[str]] = defaultdict(set)
    for note in notes:
        comparable = note.comparable_hash
        if comparable is not None:
            hashes_by_project[note.project].add(comparable)
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


def _mark(
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


def repair_unanchored(fetch_all: QueryFn, execute_write: WriteFn) -> RepairReport:
    """Place every unattached note by content hash, or mark why it cannot be.

    Deterministic: notes are visited in key order and candidates are sorted,
    so the same graph yields the same writes. Raises nothing of its own; a
    store error propagates to the caller, which logs it and lets the next
    run retry (`GraphUpdater._reanchor_glosses`).
    """
    notes = sorted(
        (_unanchored(row) for row in fetch_all(cq.CYPHER_UNANCHORED_GLOSSES, None)),
        key=lambda n: n.qualified_name,
    )
    candidates = _candidates_by_hash(fetch_all, notes)
    report = RepairReport(moved=[], ambiguous=[], lost=[])
    for note in notes:
        comparable = note.comparable_hash
        if comparable is None:
            _mark(execute_write, note, cs.GlossAnchorState.LOST, [])
            report.lost.append(note.qualified_name)
            continue
        qns = sorted(set(candidates.get((note.project, comparable), [])))
        if len(qns) == 1:
            execute_write(
                cq.CYPHER_GLOSS_MOVE,
                {cs.KEY_QN: note.qualified_name, cs.KEY_NEW_QN: qns[0]},
            )
            report.moved.append(note.qualified_name)
        elif qns:
            _mark(execute_write, note, cs.GlossAnchorState.AMBIGUOUS, qns)
            report.ambiguous.append(note.qualified_name)
        else:
            _mark(execute_write, note, cs.GlossAnchorState.LOST, [])
            report.lost.append(note.qualified_name)
    return report
