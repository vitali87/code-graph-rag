"""The payloads of the MCP analysis tools: the flow verdict between a source
and a sink, and the explained and ranked traceback (issue #227), laid out
as the `crash_correlation` reports they are read from."""

from __future__ import annotations

from typing import TypedDict


class FlowVerdictResult(TypedDict):
    verdict: str
    path: list[str]
    gaps: list[str]
    # Where the path crosses a service boundary (issue #1603).
    remote_hops: list[list[str]]


class ResolutionSection(TypedDict):
    total: int
    resolved: int
    rate: float


class FrameSection(TypedDict):
    """`crash_correlation.FrameContext`, field for field."""

    path: str
    line: int
    name: str
    qualified_name: str | None
    label: str | None
    unresolved_reason: str | None
    callers: tuple[str, ...]
    callees: tuple[str, ...]
    flow_sources: tuple[str, ...]


class ExplainedSection(TypedDict):
    exception_type: str
    exception_message: str
    frames: list[FrameSection]
    resolution: ResolutionSection
    note: str | None
    # An ExceptionGroup's sub-exceptions, each explained on its own.
    members: list[ExplainedSection]
    omitted_members: int


class TracebackExplanation(ExplainedSection):
    flow_gaps: list[str]
    inferred_checkout_root: str | None


class CandidateSection(TypedDict):
    """`crash_correlation.RootCause`, field for field."""

    qualified_name: str
    path: str | None
    line: int | None
    score: float
    reasons: tuple[str, ...]
    call_path: tuple[str, ...]


class RankedSection(TypedDict):
    exception_type: str
    exception_message: str
    failing: str | None
    anchor_is_crash_site: bool
    candidates: list[CandidateSection]
    resolution: ResolutionSection
    note: str | None
    # An ExceptionGroup's sub-exceptions, each ranked on its own.
    members: list[RankedSection]
    omitted_members: int


class RootCauseRanking(RankedSection):
    flow_used: bool
    flow_gaps: list[str]
    inferred_checkout_root: str | None
