"""`context(target, budget_tokens)`: a graph-ranked minimal context slice
(issue #1536).

`semantic_search` plus `get_code_snippet` is grep with embeddings: agents
burn context reading whole files to find the three things that matter.
The graph ranks by structural distance instead and hands back a budgeted
slice: the target's source, its direct callers' call lines and callees'
signatures, the types it accepts and returns, the tests that reach it, and
the documentation sections whose file links to it. Trace hotness
(`dynamic_call_count`) breaks ties among callers; for a free-text task the
embedding similarity of each candidate breaks the rest.

Every piece says why it is there, so an agent (or a human) can see what
the slice is built from and drop what it does not need.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple, TypedDict

from . import constants as cs
from . import cypher_queries as cq
from . import graph_query
from .graph_query import QueryFn, ReachIndex
from .types_defs import ResultRow, SemanticSearchResult
from .utils.source_extraction import extract_source_lines
from .utils.token_utils import count_tokens

Searcher = Callable[[str], list[SemanticSearchResult]]

_LOCATION = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+)$")
_IDENTIFIER = r"(?<![\w.])%s(?!\w)"
_DEFINITION_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
        cs.NodeLabel.TYPE.value,
        cs.NodeLabel.UNION.value,
    }
)


class Piece(TypedDict):
    qualified_name: str
    file: str | None
    span: list[int]
    why_included: str
    source: str
    tokens: int


class ContextSlice(TypedDict):
    target: str
    resolved: str | None
    budget_tokens: int
    used_tokens: int
    pieces: list[Piece]
    omitted: list[str]
    truncated: bool


class _Candidate(NamedTuple):
    distance: int
    hotness: int
    similarity: float
    qualified_name: str
    file: str | None
    span: tuple[int, int]
    why: str
    source: str

    @property
    def order(self) -> tuple[int, int, float, str]:
        return (self.distance, -self.hotness, -self.similarity, self.qualified_name)


def _prefix(project: str) -> str:
    return f"{project}{cs.SEPARATOR_DOT}"


def _lines(repo_root: Path | None, path: str | None, start: int, end: int) -> str:
    if repo_root is None or not path or start < 1 or end < start:
        return ""
    text = extract_source_lines(repo_root / path, start, end) or ""
    # extract_source_lines keeps the file's own endings, so a CRLF checkout
    # carried a b"\r" into every excerpt and the trailing .strip("\n") below
    # could not reach it. Excerpts are for display and comparison, so normalise
    # here rather than in the shared reader, whose byte fidelity other callers
    # rely on. Every Windows unit job failed on the stray b"\r".
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _line(repo_root: Path | None, path: str | None, line: int | None) -> str:
    if line is None:
        return ""
    return _lines(repo_root, path, line, line).strip("\n")


def _header(
    repo_root: Path | None, path: str | None, start: int | None, end: int | None
) -> str:
    """The first line(s) of a definition up to and including its opener."""
    if start is None:
        return ""
    text = _lines(repo_root, path, start, min(end or start, start + 4))
    header: list[str] = []
    for line in text.split("\n"):
        header.append(line)
        if line.rstrip().endswith((":", "{", ";")) or ")" in line:
            break
    return "\n".join(header).strip("\n")


# --- resolution ---------------------------------------------------------------


def resolve_target(
    fetch_all: QueryFn,
    project: str,
    target: str,
    search: Searcher | None,
) -> tuple[str | None, dict[str, float]]:
    """(qualified name, similarity by qualified name for free text)."""
    if _LOCATION.match(target) or cs.SEPARATOR_DOT in target or " " not in target:
        rows = graph_query.resolve(fetch_all, project, target)
        rows = [r for r in rows if r["label"] in _DEFINITION_LABELS] or rows
        if rows:
            return rows[0]["qualified_name"], {}
    if search is not None:
        results = search(target)
        if results:
            scores = {r["qualified_name"]: float(r["score"]) for r in results}
            best = max(results, key=lambda r: r["score"])
            return best["qualified_name"], scores
    return None, {}


# --- candidates -----------------------------------------------------------------


def _hotness(fetch_all: QueryFn, project: str, qn: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in fetch_all(
        cq.CYPHER_CONTEXT_HOTNESS,
        {cs.KEY_PROJECT_PREFIX: _prefix(project), cs.KEY_QN: qn},
    ):
        caller = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
        count = row.get(cs.TRACE_PROP_CALL_COUNT)
        if caller and isinstance(count, int):
            out[caller] = out.get(caller, 0) + count
    return out


def _target_piece(
    definition: graph_query.DefinitionRow, repo_root: Path | None
) -> _Candidate:
    start, end = definition["start_line"] or 1, definition["end_line"] or 1
    source = definition["source"] or _lines(repo_root, definition["path"], start, end)
    return _Candidate(
        0,
        0,
        0.0,
        definition["qualified_name"],
        definition["path"],
        (start, end),
        cs.CONTEXT_WHY_TARGET,
        source.rstrip("\n"),
    )


def _caller_pieces(
    fetch_all: QueryFn,
    project: str,
    qn: str,
    repo_root: Path | None,
    hotness: dict[str, int],
) -> list[_Candidate]:
    out: list[_Candidate] = []
    for row in graph_query.callers(fetch_all, project, qn):
        line = row["line"]
        source = _line(repo_root, row["path"], line)
        if line is None:
            definition = graph_query.definition(
                fetch_all, project, row["qualified_name"], repo_root
            )
            line = definition["start_line"]
            source = _header(
                repo_root, row["path"], definition["start_line"], definition["end_line"]
            )
        out.append(
            _Candidate(
                1,
                hotness.get(row["qualified_name"], 0),
                0.0,
                row["qualified_name"],
                row["path"],
                (line or 0, line or 0),
                cs.CONTEXT_WHY_CALLER,
                source,
            )
        )
    return out


def _callee_pieces(
    fetch_all: QueryFn, project: str, qn: str, repo_root: Path | None
) -> list[_Candidate]:
    out: list[_Candidate] = []
    seen: set[str] = set()
    for row in graph_query.callees(fetch_all, project, qn):
        callee = row["qualified_name"]
        if callee in seen:
            continue
        seen.add(callee)
        definition = graph_query.definition(fetch_all, project, callee, None)
        start, end = definition["start_line"], definition["end_line"]
        out.append(
            _Candidate(
                1,
                0,
                0.0,
                callee,
                definition["path"],
                (start or 0, start or 0),
                cs.CONTEXT_WHY_CALLEE,
                _header(repo_root, definition["path"], start, end),
            )
        )
    return out


def _type_pieces(
    fetch_all: QueryFn, project: str, qn: str, repo_root: Path | None
) -> list[_Candidate]:
    out: list[_Candidate] = []
    for row in fetch_all(
        cq.CYPHER_CONTEXT_TYPES,
        {cs.KEY_PROJECT_PREFIX: _prefix(project), cs.KEY_QN: qn},
    ):
        type_qn = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
        path = row.get(cs.KEY_PATH)
        start, end = row.get(cs.KEY_START_LINE), row.get(cs.KEY_END_LINE)
        rel = str(row.get(cs.KEY_REL_TYPE) or "")
        why = (
            cs.CONTEXT_WHY_RETURNS
            if rel == cs.RelationshipType.RETURNS.value
            else cs.CONTEXT_WHY_ACCEPTS
        )
        start_line = start if isinstance(start, int) else 0
        end_line = end if isinstance(end, int) else start_line
        out.append(
            _Candidate(
                1,
                0,
                0.0,
                type_qn,
                path if isinstance(path, str) else None,
                (start_line, start_line),
                why,
                _header(
                    repo_root,
                    path if isinstance(path, str) else None,
                    start_line or None,
                    end_line or None,
                ),
            )
        )
    return out


def _test_pieces(
    fetch_all: QueryFn, project: str, qn: str, repo_root: Path | None
) -> list[_Candidate]:
    out: list[_Candidate] = []
    reach = ReachIndex.build(fetch_all, project)
    for row in reach.tests_reaching(qn):
        definition = graph_query.definition(
            fetch_all, project, row["qualified_name"], repo_root
        )
        start, end = definition["start_line"] or 0, definition["end_line"] or 0
        out.append(
            _Candidate(
                row["depth"] + 1,
                0,
                0.0,
                row["qualified_name"],
                row["path"],
                (start, end),
                cs.CONTEXT_WHY_TEST.format(depth=row["depth"], through=row["through"]),
                (definition["source"] or "").rstrip("\n"),
            )
        )
    return out


def _doc_pieces(
    fetch_all: QueryFn,
    project: str,
    path: str | None,
    name: str,
    repo_root: Path | None,
) -> list[_Candidate]:
    if repo_root is None or not path:
        return []
    absolute = str((repo_root / path).resolve())
    out: list[_Candidate] = []
    rows = fetch_all(
        cq.CYPHER_CONTEXT_DOC_SECTIONS,
        {cs.KEY_PROJECT_PREFIX: _prefix(project), cs.KEY_ABSOLUTE_PATH: absolute},
    )
    by_doc: dict[str, list[ResultRow]] = {}
    for row in rows:
        by_doc.setdefault(str(row.get(cs.KEY_FROM_QN) or ""), []).append(row)
    for doc_qn, sections in by_doc.items():
        mentioning: list[_Candidate] = []
        first: _Candidate | None = None
        for row in sorted(
            sections, key=lambda r: int(str(r.get(cs.KEY_START_LINE) or 0))
        ):
            section_path = row.get(cs.KEY_PATH)
            start, end = row.get(cs.KEY_START_LINE), row.get(cs.KEY_END_LINE)
            if not isinstance(section_path, str) or not isinstance(start, int):
                continue
            text = _lines(
                repo_root, section_path, start, end if isinstance(end, int) else start
            )
            candidate = _Candidate(
                2,
                0,
                0.0,
                str(row.get(cs.KEY_QUALIFIED_NAME) or doc_qn),
                section_path,
                (start, end if isinstance(end, int) else start),
                cs.CONTEXT_WHY_DOC,
                text.rstrip("\n"),
            )
            if first is None:
                first = candidate
            if re.search(_IDENTIFIER % re.escape(name), text):
                mentioning.append(candidate)
        out.extend(mentioning or ([first] if first is not None else []))
    return out


# --- the slice ---------------------------------------------------------------------


def _fit(
    candidates: Iterable[_Candidate], budget: int
) -> tuple[list[Piece], list[str], int, bool]:
    pieces: list[Piece] = []
    omitted: list[str] = []
    used = 0
    truncated = False
    seen: set[tuple[str, str, tuple[int, int]]] = set()
    for candidate in sorted(candidates, key=lambda c: c.order):
        key = (candidate.qualified_name, candidate.why, candidate.span)
        if key in seen or not candidate.source:
            continue
        seen.add(key)
        source = candidate.source
        tokens = count_tokens(source)
        if used + tokens > budget:
            if candidate.distance == 0 and not pieces:
                # The target itself must fit: keep as many of its lines as
                # the budget allows and say so. A budget too small for even
                # its first line leaves it out rather than padding with
                # nothing.
                source, tokens = _trim(source, budget)
                truncated = True
                if not source:
                    omitted.append(f"{candidate.qualified_name} ({candidate.why})")
                    continue
            else:
                omitted.append(f"{candidate.qualified_name} ({candidate.why})")
                continue
        used += tokens
        pieces.append(
            Piece(
                qualified_name=candidate.qualified_name,
                file=candidate.file,
                span=[candidate.span[0], candidate.span[1]],
                why_included=candidate.why,
                source=source,
                tokens=tokens,
            )
        )
    return pieces, omitted, used, truncated


def _trim(source: str, budget: int) -> tuple[str, int]:
    lines = source.split("\n")
    while lines and count_tokens("\n".join(lines)) > budget:
        lines.pop()
    text = "\n".join(lines)
    return text, count_tokens(text) if text else 0


def context(
    fetch_all: QueryFn,
    project: str,
    target: str,
    budget_tokens: int,
    repo_root: Path | None = None,
    search: Searcher | None = None,
) -> ContextSlice:
    """The budgeted, graph-ranked slice around `target`."""
    budget = max(int(budget_tokens), 0)
    qn, scores = resolve_target(fetch_all, project, target, search)
    if qn is None:
        return ContextSlice(
            target=target,
            resolved=None,
            budget_tokens=budget,
            used_tokens=0,
            pieces=[],
            omitted=[],
            truncated=False,
        )
    definition = graph_query.definition(fetch_all, project, qn, repo_root)
    if not definition["found"]:
        return ContextSlice(
            target=target,
            resolved=qn,
            budget_tokens=budget,
            used_tokens=0,
            pieces=[],
            omitted=[],
            truncated=False,
        )
    hotness = _hotness(fetch_all, project, qn)
    name = definition["name"] or qn.rsplit(cs.SEPARATOR_DOT, 1)[-1]
    candidates = [
        _target_piece(definition, repo_root),
        *_caller_pieces(fetch_all, project, qn, repo_root, hotness),
        *_callee_pieces(fetch_all, project, qn, repo_root),
        *_type_pieces(fetch_all, project, qn, repo_root),
        *_test_pieces(fetch_all, project, qn, repo_root),
        *_doc_pieces(fetch_all, project, definition["path"], name, repo_root),
    ]
    if scores:
        candidates = [
            c._replace(similarity=scores.get(c.qualified_name, 0.0)) for c in candidates
        ]
    pieces, omitted, used, truncated = _fit(candidates, budget)
    return ContextSlice(
        target=target,
        resolved=qn,
        budget_tokens=budget,
        used_tokens=used,
        pieces=pieces,
        omitted=omitted,
        truncated=truncated,
    )
