"""Ingest a runtime call trace into the graph as provenance-tagged CALLS edges.

Records resolve to existing Function/Method/Module nodes; each resolved pair
becomes one CALLS edge write. MERGE semantics collapse onto an existing static
edge when there is one (the dynamic properties then decorate it), and create
the edge when static analysis missed the relationship, in which case it is
flagged ``static_missed``. Re-ingesting the same trace is idempotent: counts
are SET, not incremented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from loguru import logger

from .. import constants as cs
from ..cypher_queries import CYPHER_TRACE_CALLABLES, CYPHER_TRACE_EXISTING_CALLS
from ..services import IngestorProtocol, QueryProtocol
from .records import read_trace_file
from .resolution import CallableNode, FrameResolver, ResolutionStats

if TYPE_CHECKING:
    from ..types_defs import PropertyValue, ResultRow
    from .resolution import ResolvedFrame


class TraceGraphProtocol(IngestorProtocol, QueryProtocol, Protocol):
    """A graph client that can both query nodes and write edges."""


@dataclass(slots=True)
class _EdgeStats:
    count: int = 0
    workloads: set[str] = field(default_factory=set)
    receiver_types: set[str] = field(default_factory=set)


@dataclass(slots=True)
class TraceIngestSummary:
    records: int = 0
    edges: int = 0
    confirmed_static: int = 0
    static_missed: int = 0
    resolution: ResolutionStats = field(default_factory=ResolutionStats)

    @property
    def unresolved(self) -> int:
        return self.resolution.total


def _load_callables(
    ingestor: TraceGraphProtocol, project_prefix: str
) -> list[CallableNode]:
    rows: list[ResultRow] = ingestor.fetch_all(
        CYPHER_TRACE_CALLABLES, {cs.KEY_PREFIX: project_prefix}
    )
    nodes: list[CallableNode] = []
    for row in rows:
        label = row.get(cs.KEY_LABEL)
        qualified_name = row.get(cs.KEY_QUALIFIED_NAME)
        path = row.get(cs.KEY_PATH)
        start_line = row.get(cs.KEY_START_LINE)
        end_line = row.get(cs.KEY_END_LINE)
        if (
            not isinstance(label, str)
            or not isinstance(qualified_name, str)
            or not isinstance(path, str)
        ):
            continue
        nodes.append(
            CallableNode(
                label=label,
                qualified_name=qualified_name,
                path=path,
                start_line=start_line if isinstance(start_line, int) else None,
                end_line=end_line if isinstance(end_line, int) else None,
            )
        )
    return nodes


def _load_existing_calls(
    ingestor: TraceGraphProtocol, project_prefix: str
) -> set[tuple[str, str]]:
    rows = ingestor.fetch_all(
        CYPHER_TRACE_EXISTING_CALLS, {cs.KEY_PREFIX: project_prefix}
    )
    pairs: set[tuple[str, str]] = set()
    for row in rows:
        from_qn = row.get(cs.KEY_FROM_QN)
        to_qn = row.get(cs.KEY_TO_QN)
        if isinstance(from_qn, str) and isinstance(to_qn, str):
            pairs.add((from_qn, to_qn))
    return pairs


def _edge_properties(
    stats: _EdgeStats, static_missed: bool
) -> dict[str, PropertyValue]:
    workloads = sorted(stats.workloads)[: cs.TRACE_MAX_WORKLOADS_PER_EDGE]
    receivers = sorted(stats.receiver_types)[: cs.TRACE_MAX_RECEIVER_TYPES_PER_EDGE]
    return {
        cs.TRACE_PROP_DYNAMIC: True,
        cs.TRACE_PROP_CALL_COUNT: stats.count,
        cs.TRACE_PROP_WORKLOAD_COUNT: len(stats.workloads),
        cs.TRACE_PROP_WORKLOADS: workloads,
        cs.TRACE_PROP_RECEIVER_TYPES: receivers,
        cs.TRACE_PROP_STATIC_MISSED: static_missed,
    }


def ingest_trace(
    trace_path: Path,
    ingestor: TraceGraphProtocol,
    repo_path: Path,
    project_name: str,
) -> TraceIngestSummary:
    """Resolve ``trace_path`` against ``project_name``'s graph and write edges."""
    header, records = read_trace_file(trace_path)
    repo_root = repo_path.resolve() if repo_path else Path(header.repo_root)
    project_prefix = project_name + cs.SEPARATOR_DOT

    nodes = _load_callables(ingestor, project_prefix)
    existing = _load_existing_calls(ingestor, project_prefix)
    resolver = FrameResolver(repo_root, nodes)

    summary = TraceIngestSummary()
    resolved_frames: dict[tuple[ResolvedFrame, ResolvedFrame], _EdgeStats] = {}
    for record in records:
        summary.records += 1
        caller = resolver.resolve(record.caller, summary.resolution)
        if caller is None:
            continue
        callee = resolver.resolve(record.callee, summary.resolution)
        if callee is None:
            continue
        edge = resolved_frames.setdefault((caller, callee), _EdgeStats())
        edge.count += record.count
        edge.workloads.update(record.workloads)
        edge.receiver_types.update(record.receiver_types)

    for (caller, callee), stats in resolved_frames.items():
        pair = (caller.qualified_name, callee.qualified_name)
        static_missed = pair not in existing
        if static_missed:
            summary.static_missed += 1
        else:
            summary.confirmed_static += 1
        summary.edges += 1
        ingestor.ensure_relationship_batch(
            (caller.label, cs.KEY_QUALIFIED_NAME, caller.qualified_name),
            cs.RelationshipType.CALLS,
            (callee.label, cs.KEY_QUALIFIED_NAME, callee.qualified_name),
            properties=_edge_properties(stats, static_missed),
        )
    ingestor.flush_all()
    logger.info(
        cs.TRACE_MSG_INGEST_SUMMARY.format(
            records=summary.records,
            edges=summary.edges,
            confirmed=summary.confirmed_static,
            missed=summary.static_missed,
            unresolved=summary.unresolved,
        )
    )
    return summary
