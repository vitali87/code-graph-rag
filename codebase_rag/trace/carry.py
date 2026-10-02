"""Carry trace-derived CALLS edges across an incremental re-parse (issue #2429).

A runtime observation has no source to be re-derived from. Re-parsing a file
deletes its Module subtree, and with it every CALLS edge into or out of the
file, then rebuilds only what the static passes see: before this, any edit to
a traced file, even a comment, discarded its dynamic edges and reverted its
`trace_confirmed` edges to `exact`, and nothing said so.

The sync reads the edges before the delete (`capture_trace_edges`) and
re-applies them by qualified name afterwards (`carry_trace_edges`) through
the per-pair write an ingest uses, so each lands as ingesting the same
observation now would: confirming the static edge the re-parse produced, or
runtime-only with its dispatch literal looked up in the caller's current
body. An edge whose endpoint no longer exists is dropped. One whose
endpoint's `anchor_hash` changed is kept but flagged `dynamic_stale`, since
the runtime saw the old definition; the flag is sticky across syncs, so a
later edit back cannot quietly vouch for it, and only a new ingest clears it.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..cypher_queries import (
    CYPHER_TRACE_CARRY_ENDPOINTS,
    CYPHER_TRACE_CARRY_STATIC_PAIRS,
    CYPHER_TRACE_EDGES_AT_PATHS,
)
from ..services import QueryProtocol
from ..types_defs import PropertyDict, ResultRow, ResultValue
from .ingest import TraceGraphProtocol, callable_node, write_trace_edge
from .resolution import CallableNode, ResolvedFrame


@dataclass(frozen=True, slots=True)
class CapturedTraceEdge:
    """One trace-derived caller->callee pair as it stood before the re-parse."""

    caller: ResolvedFrame
    callee: ResolvedFrame
    caller_path: str | None
    callee_path: str | None
    caller_hash: str | None
    callee_hash: str | None
    observation: PropertyDict
    stale: bool


@dataclass(slots=True)
class TraceCarrySummary:
    carried: int = 0
    newly_stale: int = 0
    dropped: int = 0


@dataclass(frozen=True, slots=True)
class _Endpoint:
    node: CallableNode
    anchor_hash: str | None


def capture_trace_edges(
    graph: QueryProtocol, paths: Sequence[str], project_prefix: str
) -> list[CapturedTraceEdge]:
    """The trace-derived edges with an endpoint in `paths`, one per pair."""
    rows = graph.fetch_all(
        CYPHER_TRACE_EDGES_AT_PATHS,
        {cs.CYPHER_PARAM_PATHS: list(paths), cs.KEY_PREFIX: project_prefix},
    )
    by_pair: dict[tuple[ResolvedFrame, ResolvedFrame], CapturedTraceEdge] = {}
    for row in rows:
        edge = _captured(row)
        if edge is None:
            continue
        # A confirmed pair holds the observation on each of its static sites;
        # it is one observation, stale if any site was graded so.
        seen = by_pair.get((edge.caller, edge.callee))
        if seen is None or (edge.stale and not seen.stale):
            by_pair[(edge.caller, edge.callee)] = edge
    return list(by_pair.values())


def carry_trace_edges(
    graph: TraceGraphProtocol,
    captured: Sequence[CapturedTraceEdge],
    repo_root: Path,
    gone_paths: Collection[str] = (),
) -> TraceCarrySummary:
    """Re-apply `captured` against the re-parsed graph and report what happened.

    `gone_paths` are files the sync deleted. An edge touching one is dropped
    even if a same-stem survivor has since taken the definition's qualified
    name, for the reason the inbound-edge restore skips deleted callers
    (issue #1569): the name now belongs to a different definition.
    """
    summary = TraceCarrySummary()
    live = [
        edge
        for edge in captured
        if edge.caller_path not in gone_paths and edge.callee_path not in gone_paths
    ]
    summary.dropped = len(captured) - len(live)
    endpoints = _current_endpoints(graph, live)
    survivors = [
        edge for edge in live if edge.caller in endpoints and edge.callee in endpoints
    ]
    summary.dropped += len(live) - len(survivors)
    static_pairs = _static_pairs(graph, survivors)
    for edge in survivors:
        caller = endpoints[edge.caller]
        callee = endpoints[edge.callee]
        stale = (
            edge.stale
            or _changed(edge.caller_hash, caller.anchor_hash)
            or _changed(edge.callee_hash, callee.anchor_hash)
        )
        if stale and not edge.stale:
            summary.newly_stale += 1
        write_trace_edge(
            graph,
            edge.caller,
            edge.callee,
            {**edge.observation, cs.TRACE_PROP_STALE: stale},
            (edge.caller.qualified_name, edge.callee.qualified_name)
            not in static_pairs,
            repo_root,
            caller.node,
        )
        summary.carried += 1
    graph.flush_all()
    _log_summary(summary)
    return summary


def _captured(row: ResultRow) -> CapturedTraceEdge | None:
    from_label = row.get(cs.KEY_FROM_LABEL)
    from_qn = row.get(cs.KEY_FROM_QN)
    to_label = row.get(cs.KEY_TO_LABEL)
    to_qn = row.get(cs.KEY_TO_QN)
    if not (
        isinstance(from_label, str)
        and isinstance(from_qn, str)
        and isinstance(to_label, str)
        and isinstance(to_qn, str)
    ):
        return None
    props = row.get(cs.KEY_PROPS)
    return CapturedTraceEdge(
        caller=ResolvedFrame(label=from_label, qualified_name=from_qn),
        callee=ResolvedFrame(label=to_label, qualified_name=to_qn),
        caller_path=_text(row.get(cs.KEY_FROM_PATH)),
        callee_path=_text(row.get(cs.KEY_TO_PATH)),
        caller_hash=_text(row.get(cs.KEY_FROM_HASH)),
        callee_hash=_text(row.get(cs.KEY_TO_HASH)),
        observation=_observation(props),
        stale=isinstance(props, dict) and props.get(cs.TRACE_PROP_STALE) is True,
    )


def _observation(props: ResultValue) -> PropertyDict:
    if not isinstance(props, dict):
        return {}
    observation: PropertyDict = {}
    for key in cs.TRACE_CARRIED_PROPS:
        value = props.get(key)
        if isinstance(value, list):
            observation[key] = [item for item in value if isinstance(item, str)]
        elif value is not None:
            observation[key] = value
    return observation


def _text(value: ResultValue) -> str | None:
    return value if isinstance(value, str) else None


def _changed(before: str | None, after: str | None) -> bool:
    # A Module has no anchor hash, and a graph written before hashes existed
    # has none either: an edge only goes stale on evidence of a change.
    return before is not None and after is not None and before != after


def _current_endpoints(
    graph: QueryProtocol, edges: Sequence[CapturedTraceEdge]
) -> dict[ResolvedFrame, _Endpoint]:
    qns = sorted(
        {frame.qualified_name for edge in edges for frame in (edge.caller, edge.callee)}
    )
    if not qns:
        return {}
    endpoints: dict[ResolvedFrame, _Endpoint] = {}
    for row in graph.fetch_all(CYPHER_TRACE_CARRY_ENDPOINTS, {cs.KEY_QNS: qns}):
        node = callable_node(row)
        if node is None:
            continue
        frame = ResolvedFrame(label=node.label, qualified_name=node.qualified_name)
        endpoints[frame] = _Endpoint(node, _text(row.get(cs.KEY_ANCHOR_HASH)))
    return endpoints


def _static_pairs(
    graph: QueryProtocol, edges: Sequence[CapturedTraceEdge]
) -> set[tuple[str, str]]:
    if not edges:
        return set()
    rows = graph.fetch_all(
        CYPHER_TRACE_CARRY_STATIC_PAIRS,
        {
            cs.KEY_FROM_QNS: sorted({edge.caller.qualified_name for edge in edges}),
            cs.KEY_TO_QNS: sorted({edge.callee.qualified_name for edge in edges}),
        },
    )
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        from_qn = row.get(cs.KEY_FROM_QN)
        to_qn = row.get(cs.KEY_TO_QN)
        if isinstance(from_qn, str) and isinstance(to_qn, str):
            pairs.add((from_qn, to_qn))
    return pairs


def _log_summary(summary: TraceCarrySummary) -> None:
    # A warning when the user has something to act on: the trace no longer
    # describes every edge it wrote, and only re-running it refreshes them.
    if summary.newly_stale or summary.dropped:
        logger.warning(
            ls.TRACE_EDGES_OUTDATED,
            carried=summary.carried,
            stale=summary.newly_stale,
            dropped=summary.dropped,
        )
    elif summary.carried:
        logger.info(ls.TRACE_EDGES_CARRIED, carried=summary.carried)
