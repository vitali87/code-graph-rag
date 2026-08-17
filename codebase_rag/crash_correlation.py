# Traceback-to-graph correlation (issue #227). A traceback is the ground-truth
# call path of the crashing thread; the graph knows who else can reach the
# failing frame (CALLS) and where the failing value could have come from
# (FLOWS_TO). Frame resolution reuses the dynamic-trace resolver, and
# reachability runs CLIENT-side over linear scans, the same discipline as
# flow_verdict.py and dead_code.py.

from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import NamedTuple

from . import constants as cs
from .flow_verdict import CYPHER_FLOW_COVERAGE_GAPS, CYPHER_FLOW_EDGES, QueryFn
from .trace.ingest import load_callables
from .trace.records import FramePoint
from .trace.resolution import CallableNode, FrameResolver, ResolutionStats

# All CALLS pairs of one project, static and runtime-discovered alike: an edge
# a previous trace ingestion created is a real observed call path, exactly the
# evidence a root-cause walk wants (unlike ingest, which excludes them so a
# re-ingest cannot reclassify its own edges).
CYPHER_CRASH_CALLS = f"""MATCH (a)-[:{cs.RelationshipType.CALLS.value}]->(b)
WHERE a.qualified_name STARTS WITH $prefix
  AND b.qualified_name STARTS WITH $prefix
RETURN a.qualified_name AS from_qn, b.qualified_name AS to_qn
"""

# ``  File "/app/service.py", line 14, in handle_request``
_TB_FRAME = re.compile(
    r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<name>.+)$'
)
_TB_HEADER = "Traceback (most recent call last):"
_TB_GROUP_HEADER = "Exception Group Traceback (most recent call last):"
# ``ValueError: boom`` or a bare ``KeyboardInterrupt``; the leading class of
# characters admits Unicode identifiers, which Python allows in class names.
_TB_EXCEPTION = re.compile(r"^(?P<type>[^\W\d][\w.]*)(?::\s?(?P<message>.*))?$")
# ExceptionGroup rendering draws a box: ``  + Exception Group Traceback ...``,
# ``  |   File ...``, ``  +-+------- 1 -------``. Stripping the margin turns
# each sub-exception back into a plain traceback section.
_TB_GROUP_MARGIN = re.compile(r"^\s*(?:\+-)?[+|]\s?")

_REASON_ON_STACK = "on the crashing stack, {depth} frame(s) above the failure"
_REASON_CALLER = "can reach the failing frame through CALLS, depth {depth}"
_REASON_FLOW = "FLOWS_TO source into the failing frame"

_SCORE_FLOW = 0.6
_SCORE_ON_STACK = 0.3
_SCORE_CALLER_BASE = 0.4
_RANK_LIMIT = 10
_CALLER_DEPTH_LIMIT = 3


class ParsedTraceback(NamedTuple):
    """The last (propagated) traceback section, outermost frame first."""

    frames: tuple[FramePoint, ...]
    exception_type: str
    exception_message: str


class FrameContext(NamedTuple):
    """One traceback frame with its graph neighbourhood."""

    path: str
    line: int
    name: str
    qualified_name: str | None
    label: str | None
    unresolved_reason: str | None
    callers: tuple[str, ...]
    callees: tuple[str, ...]
    flow_sources: tuple[str, ...]


class TracebackReport(NamedTuple):
    exception_type: str
    exception_message: str
    frames: tuple[FrameContext, ...]
    flow_gaps: tuple[str, ...]


class RootCause(NamedTuple):
    qualified_name: str
    path: str | None
    line: int | None
    score: float
    reasons: tuple[str, ...]
    call_path: tuple[str, ...]


class RootCauseReport(NamedTuple):
    """Ranked writers/callers that can explain the failure.

    ``failing`` is the innermost frame the graph resolves -- the deepest
    point the analysis can anchor on. When the crash site itself is deeper
    (a library frame, or an in-repo frame the graph cannot match),
    ``anchor_is_crash_site`` is false so the ranking reads as "relative to
    the deepest resolvable frame", never as a claim about the crash line.
    ``flow_used`` distinguishes "no FLOWS_TO evidence considered" from "flow
    considered and empty": when the project has no flow edges at all the
    ranking degrades to a CALLS-only walk. ``flow_gaps`` always names the
    files outside flow-analysis coverage so an absent flow signal is not
    read as evidence.
    """

    exception_type: str
    exception_message: str
    failing: str | None
    anchor_is_crash_site: bool
    candidates: tuple[RootCause, ...]
    flow_used: bool
    flow_gaps: tuple[str, ...]


def parse_python_traceback(text: str) -> ParsedTraceback:
    """Parse CPython traceback text into frames and the exception line.

    Chained tracebacks (``During handling ...``/``direct cause``) contain
    several sections; the last one is the failure that propagated, so its
    frames and its trailing exception line are the ones returned. An
    ``ExceptionGroup`` rendering is normalised by stripping its box margin,
    after which the same rule picks the last sub-exception's traceback --
    the deepest real cause, not the group wrapper.
    """
    lines = [
        _TB_GROUP_MARGIN.sub("", line) for line in text.splitlines() if line.strip()
    ]
    last_header = -1
    for index, line in enumerate(lines):
        if line.strip() in (_TB_HEADER, _TB_GROUP_HEADER):
            last_header = index
    frames: list[FramePoint] = []
    exception_type = ""
    exception_message = ""
    for line in lines[last_header + 1 :]:
        if match := _TB_FRAME.match(line):
            frames.append(
                FramePoint(
                    path=match.group("path"),
                    qualname=match.group("name").strip(),
                    line=int(match.group("line")),
                )
            )
        # Source snippets and caret markers are indented; the exception line
        # is the first flush-left line after the frames.
        elif not line[:1].isspace() and (match := _TB_EXCEPTION.match(line)):
            exception_type = match.group("type")
            exception_message = match.group("message") or ""
            break
    return ParsedTraceback(tuple(frames), exception_type, exception_message)


def _anchored(frame: FramePoint, repo_root: Path) -> FramePoint:
    """A frame whose relative path is joined to the repository root.

    Tracebacks from a process started inside the repository carry paths
    relative to it; absolute paths and synthetic files (``<stdin>``) pass
    through and resolve (or fail containment) as they are.
    """
    if Path(frame.path).is_absolute() or frame.path.startswith("<"):
        return frame
    return FramePoint(
        path=(repo_root / frame.path).as_posix(),
        qualname=frame.qualname,
        line=frame.line,
    )


class _CrashGraph:
    """The project slice a correlation needs, fetched once."""

    def __init__(self, fetch_all: QueryFn, project_name: str) -> None:
        prefix = f"{project_name}{cs.SEPARATOR_DOT}"
        params = {
            cs.KEY_PROJECT_PREFIX: prefix,
            cs.KEY_PROJECT_NAME: project_name,
        }
        self.nodes: list[CallableNode] = load_callables(fetch_all, prefix)
        self.by_qn: dict[str, CallableNode] = {
            node.qualified_name: node for node in self.nodes
        }
        self.callers: dict[str, list[str]] = {}
        self.callees: dict[str, list[str]] = {}
        for row in fetch_all(CYPHER_CRASH_CALLS, {cs.KEY_PREFIX: prefix}):
            from_qn, to_qn = row.get("from_qn"), row.get("to_qn")
            if isinstance(from_qn, str) and isinstance(to_qn, str):
                self.callers.setdefault(to_qn, []).append(from_qn)
                self.callees.setdefault(from_qn, []).append(to_qn)
        self.flow_sources: dict[str, list[str]] = {}
        for row in fetch_all(CYPHER_FLOW_EDGES, params):
            source, target = row.get("source"), row.get("target")
            if isinstance(source, str) and isinstance(target, str):
                self.flow_sources.setdefault(target, []).append(source)
        # Gaps are fetched unconditionally: a flow edge elsewhere in the
        # project must not hide that the failing file sits outside coverage.
        gap_rows = fetch_all(CYPHER_FLOW_COVERAGE_GAPS, params)
        self.flow_gaps: tuple[str, ...] = tuple(
            sorted(
                path
                for row in gap_rows
                if isinstance(path := row.get(cs.KEY_PATH), str)
            )
        )


def _resolve_stack(
    parsed: ParsedTraceback, graph: _CrashGraph, repo_root: Path
) -> list[tuple[FramePoint, str | None, str | None, str | None]]:
    """Each frame with (resolved qn, label, unresolved reason)."""
    resolver = FrameResolver(repo_root, graph.nodes)
    resolved: list[tuple[FramePoint, str | None, str | None, str | None]] = []
    for frame in parsed.frames:
        stats = ResolutionStats()
        match = resolver.resolve(_anchored(frame, repo_root), stats)
        if match is not None:
            resolved.append((frame, match.qualified_name, match.label, None))
        else:
            reason = next(iter(stats.unresolved), None)
            resolved.append((frame, None, None, reason))
    return resolved


def explain_traceback(
    fetch_all: QueryFn,
    project_name: str,
    repo_root: Path,
    traceback_text: str,
) -> TracebackReport:
    """Resolve each traceback frame and attach its graph neighbourhood."""
    parsed = parse_python_traceback(traceback_text)
    graph = _CrashGraph(fetch_all, project_name)
    contexts = [
        FrameContext(
            path=frame.path,
            line=frame.line,
            name=frame.qualname,
            qualified_name=qn,
            label=label,
            unresolved_reason=reason,
            callers=tuple(sorted(graph.callers.get(qn, ()))) if qn else (),
            callees=tuple(sorted(graph.callees.get(qn, ()))) if qn else (),
            flow_sources=tuple(sorted(graph.flow_sources.get(qn, ()))) if qn else (),
        )
        for frame, qn, label, reason in _resolve_stack(parsed, graph, repo_root)
    ]
    return TracebackReport(
        exception_type=parsed.exception_type,
        exception_message=parsed.exception_message,
        frames=tuple(contexts),
        flow_gaps=graph.flow_gaps,
    )


def _reverse_reachable(
    graph: _CrashGraph, failing: str
) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Callers that reach the failing node within the depth limit.

    Maps each caller to its depth and the forward call path it takes to the
    failure (``candidate -> ... -> failing``), from a breadth-first walk over
    reversed CALLS edges, so the recorded path is a shortest one.
    """
    reached: dict[str, tuple[int, tuple[str, ...]]] = {}
    queue: deque[tuple[str, int]] = deque([(failing, 0)])
    seen = {failing}
    while queue:
        current, depth = queue.popleft()
        if depth == _CALLER_DEPTH_LIMIT:
            continue
        # Sorted expansion keeps the recorded shortest path deterministic
        # when several exist; the graph returns edges in no fixed order.
        for caller in sorted(graph.callers.get(current, ())):
            if caller in seen:
                continue
            seen.add(caller)
            _, tail = reached.get(current, (0, (failing,)))
            reached[caller] = (depth + 1, (caller, *tail))
            queue.append((caller, depth + 1))
    return reached


def rank_root_causes(
    fetch_all: QueryFn,
    project_name: str,
    repo_root: Path,
    traceback_text: str,
) -> RootCauseReport:
    """Rank the sites that can explain the failure, best first.

    The score is additive over three signals: being a FLOWS_TO source into
    the failing frame (the failing value's possible producers), sitting on
    the crashing stack itself (the ground-truth path), and reaching the
    failing frame through CALLS (closer callers score higher). The failing
    frame itself is reported separately, not ranked.
    """
    parsed = parse_python_traceback(traceback_text)
    graph = _CrashGraph(fetch_all, project_name)
    stack = _resolve_stack(parsed, graph, repo_root)
    stack_qns = [qn for _frame, qn, _label, _reason in stack if qn]
    failing = stack_qns[-1] if stack_qns else None
    anchor_is_crash_site = bool(stack) and stack[-1][1] is not None
    if failing is None:
        return RootCauseReport(
            exception_type=parsed.exception_type,
            exception_message=parsed.exception_message,
            failing=None,
            anchor_is_crash_site=False,
            candidates=(),
            flow_used=bool(graph.flow_sources),
            flow_gaps=graph.flow_gaps,
        )

    reached = _reverse_reachable(graph, failing)
    flow_into_failing = set(graph.flow_sources.get(failing, ()))
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    paths: dict[str, tuple[str, ...]] = {}

    for qn, (depth, call_path) in reached.items():
        scores[qn] = scores.get(qn, 0.0) + _SCORE_CALLER_BASE / depth
        reasons.setdefault(qn, []).append(_REASON_CALLER.format(depth=depth))
        paths[qn] = call_path
    for distance, qn in enumerate(reversed(stack_qns[:-1]), start=1):
        scores[qn] = scores.get(qn, 0.0) + _SCORE_ON_STACK
        reasons.setdefault(qn, []).append(_REASON_ON_STACK.format(depth=distance))
    for qn in flow_into_failing:
        scores[qn] = scores.get(qn, 0.0) + _SCORE_FLOW
        reasons.setdefault(qn, []).append(_REASON_FLOW)

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    candidates = tuple(
        RootCause(
            qualified_name=qn,
            path=node.path if node else None,
            line=node.start_line if node else None,
            score=round(score, 3),
            reasons=tuple(reasons[qn]),
            call_path=paths.get(qn, ()),
        )
        for qn, score in ranked[:_RANK_LIMIT]
        for node in (graph.by_qn.get(qn),)
    )
    return RootCauseReport(
        exception_type=parsed.exception_type,
        exception_message=parsed.exception_message,
        failing=failing,
        anchor_is_crash_site=anchor_is_crash_site,
        candidates=candidates,
        flow_used=bool(graph.flow_sources),
        flow_gaps=graph.flow_gaps,
    )
