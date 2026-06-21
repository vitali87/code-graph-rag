from statistics import fmean
from typing import TypeVar

from codebase_rag import constants as cs

from . import constants as ec
from .types_defs import (
    DiffBucket,
    EdgeKey,
    GraphData,
    LocationStats,
    NameEdge,
    NodeKey,
    ScoreResult,
    ScoreRow,
)

T = TypeVar("T")


def score(cgr: GraphData, oracle: GraphData) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}

    cgr_nodes_all: set[NodeKey] = set()
    oracle_nodes_all: set[NodeKey] = set()
    for kind in ec.SCORED_NODE_KINDS:
        cgr_set = {k for k in cgr.nodes if k.kind == kind.value}
        oracle_set = {k for k in oracle.nodes if k.kind == kind.value}
        cgr_nodes_all |= cgr_set
        oracle_nodes_all |= oracle_set
        row = _prf(ec.Category.NODE.value, kind.value, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_NODE_PREFIX + kind.value] = _node_bucket(
                cgr_set, oracle_set, cgr, oracle
            )
    node_aggregate = _prf(
        ec.Category.NODE.value, ec.AGGREGATE_LABEL, cgr_nodes_all, oracle_nodes_all
    )
    if node_aggregate is not None:
        rows.append(node_aggregate)

    cgr_edges_all: set[EdgeKey] = set()
    oracle_edges_all: set[EdgeKey] = set()
    for edge_type in ec.SCORED_EDGE_TYPES:
        cgr_set_e = {e for e in cgr.edges if e.rel_type == edge_type.value}
        oracle_set_e = {e for e in oracle.edges if e.rel_type == edge_type.value}
        cgr_edges_all |= cgr_set_e
        oracle_edges_all |= oracle_set_e
        row = _prf(ec.Category.EDGE.value, edge_type.value, cgr_set_e, oracle_set_e)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_EDGE_PREFIX + edge_type.value] = _edge_bucket(
                cgr_set_e, oracle_set_e
            )
    edge_aggregate = _prf(
        ec.Category.EDGE.value, ec.AGGREGATE_LABEL, cgr_edges_all, oracle_edges_all
    )
    if edge_aggregate is not None:
        rows.append(edge_aggregate)

    for name_edge_type in ec.SCORED_NAME_EDGE_TYPES:
        cgr_set_n = {e for e in cgr.name_edges if e.rel_type == name_edge_type.value}
        oracle_set_n = {
            e for e in oracle.name_edges if e.rel_type == name_edge_type.value
        }
        row = _prf(
            ec.Category.EDGE.value, name_edge_type.value, cgr_set_n, oracle_set_n
        )
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_NAME_EDGE_PREFIX + name_edge_type.value] = _name_edge_bucket(
                cgr_set_n, oracle_set_n
            )

    return ScoreResult(rows=rows, location=_location_stats(cgr, oracle), diff=diff)


def score_node_kinds(
    cgr: GraphData, oracle: GraphData, kinds: tuple[cs.NodeLabel, ...]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    cgr_all: set[NodeKey] = set()
    oracle_all: set[NodeKey] = set()
    for kind in kinds:
        cgr_set = {k for k in cgr.nodes if k.kind == kind.value}
        oracle_set = {k for k in oracle.nodes if k.kind == kind.value}
        cgr_all |= cgr_set
        oracle_all |= oracle_set
        row = _prf(ec.Category.NODE.value, kind.value, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_NODE_PREFIX + kind.value] = _node_bucket(
                cgr_set, oracle_set, cgr, oracle
            )
    aggregate = _prf(ec.Category.NODE.value, ec.AGGREGATE_LABEL, cgr_all, oracle_all)
    if aggregate is not None:
        rows.append(aggregate)
    return ScoreResult(rows=rows, location=LocationStats(0, 0, 0, 0.0, 0), diff=diff)


def score_edge_types(
    cgr: GraphData, oracle: GraphData, edge_types: tuple[cs.RelationshipType, ...]
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    cgr_all: set[EdgeKey] = set()
    oracle_all: set[EdgeKey] = set()
    for edge_type in edge_types:
        cgr_set = {e for e in cgr.edges if e.rel_type == edge_type.value}
        oracle_set = {e for e in oracle.edges if e.rel_type == edge_type.value}
        cgr_all |= cgr_set
        oracle_all |= oracle_set
        row = _prf(ec.Category.EDGE.value, edge_type.value, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_EDGE_PREFIX + edge_type.value] = _edge_bucket(
                cgr_set, oracle_set
            )
    aggregate = _prf(ec.Category.EDGE.value, ec.AGGREGATE_LABEL, cgr_all, oracle_all)
    if aggregate is not None:
        rows.append(aggregate)
    return ScoreResult(rows=rows, location=LocationStats(0, 0, 0, 0.0, 0), diff=diff)


def score_name_edge_types(
    cgr: GraphData,
    oracle: GraphData,
    name_edge_types: tuple[cs.RelationshipType, ...],
) -> ScoreResult:
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    cgr_all: set[NameEdge] = set()
    oracle_all: set[NameEdge] = set()
    for edge_type in name_edge_types:
        cgr_set = {e for e in cgr.name_edges if e.rel_type == edge_type.value}
        oracle_set = {e for e in oracle.name_edges if e.rel_type == edge_type.value}
        cgr_all |= cgr_set
        oracle_all |= oracle_set
        row = _prf(ec.Category.EDGE.value, edge_type.value, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_NAME_EDGE_PREFIX + edge_type.value] = _name_edge_bucket(
                cgr_set, oracle_set
            )
    aggregate = _prf(ec.Category.EDGE.value, ec.AGGREGATE_LABEL, cgr_all, oracle_all)
    if aggregate is not None:
        rows.append(aggregate)
    return ScoreResult(rows=rows, location=LocationStats(0, 0, 0, 0.0, 0), diff=diff)


_SpanKey = tuple[str, str, int, int]


def score_span(
    cgr: GraphData, oracle: GraphData, kinds: tuple[cs.NodeLabel, ...]
) -> ScoreResult:
    # (H) Grade node SPANS (end_line) only on nodes both sides identify by
    # (H) (kind, file, start), so an end_line disagreement is not masked by, nor
    # (H) conflated with, a node-identity miss. Restricted to the shared key set,
    # (H) fp and fn each count one end_line mismatch (precision == recall).
    rows: list[ScoreRow] = []
    diff: dict[str, DiffBucket] = {}
    cgr_all: set[_SpanKey] = set()
    oracle_all: set[_SpanKey] = set()
    shared = cgr.nodes.keys() & oracle.nodes.keys()
    for kind in kinds:
        keys = {k for k in shared if k.kind == kind.value}
        cgr_set = {(k.kind, k.file, k.start_line, cgr.nodes[k].end_line) for k in keys}
        oracle_set = {
            (k.kind, k.file, k.start_line, oracle.nodes[k].end_line) for k in keys
        }
        cgr_all |= cgr_set
        oracle_all |= oracle_set
        row = _prf(ec.Category.SPAN.value, kind.value, cgr_set, oracle_set)
        if row is not None:
            rows.append(row)
            diff[ec.DIFF_SPAN_PREFIX + kind.value] = _span_bucket(cgr_set, oracle_set)
    aggregate = _prf(ec.Category.SPAN.value, ec.AGGREGATE_LABEL, cgr_all, oracle_all)
    if aggregate is not None:
        rows.append(aggregate)
    return ScoreResult(rows=rows, location=LocationStats(0, 0, 0, 0.0, 0), diff=diff)


def _fmt_span(span: _SpanKey) -> str:
    kind, file, start, end = span
    return ec.SPAN_REPR.format(kind=kind, file=file, start=start, end=end)


def _span_bucket(cgr_set: set[_SpanKey], oracle_set: set[_SpanKey]) -> DiffBucket:
    missing = [_fmt_span(s) for s in sorted(oracle_set - cgr_set)]
    extra = [_fmt_span(s) for s in sorted(cgr_set - oracle_set)]
    return DiffBucket(missing=missing, extra=extra)


def score_structure(
    cgr: GraphData,
    oracle: GraphData,
    node_kinds: tuple[cs.NodeLabel, ...],
    edge_types: tuple[cs.RelationshipType, ...],
    grade_spans: bool = False,
) -> ScoreResult:
    node_result = score_node_kinds(cgr, oracle, node_kinds)
    edge_result = score_edge_types(cgr, oracle, edge_types)
    # (H) Inheritance name-edges only produce rows when a side has them, so this
    # (H) is a no-op for languages without inheritance (Go, Lua).
    name_result = score_name_edge_types(cgr, oracle, ec.INHERITANCE_NAME_EDGE_TYPES)
    # (H) Spans are opt-in per language: only oracles that emit end_line can grade
    # (H) them, else every multi-line node reads as a mismatch against the start.
    span_result = (
        score_span(cgr, oracle, node_kinds)
        if grade_spans
        else ScoreResult(rows=[], location=LocationStats(0, 0, 0, 0.0, 0), diff={})
    )
    return ScoreResult(
        rows=node_result.rows + edge_result.rows + name_result.rows + span_result.rows,
        location=node_result.location,
        diff={
            **node_result.diff,
            **edge_result.diff,
            **name_result.diff,
            **span_result.diff,
        },
    )


def _fmt_name_edge(edge: NameEdge) -> str:
    return ec.NAME_EDGE_REPR.format(
        rel=edge.rel_type,
        sfile=edge.source.file,
        sstart=edge.source.start_line,
        target=edge.target_name,
    )


def _name_edge_bucket(cgr_set: set[NameEdge], oracle_set: set[NameEdge]) -> DiffBucket:
    missing = [_fmt_name_edge(e) for e in sorted(oracle_set - cgr_set)]
    extra = [_fmt_name_edge(e) for e in sorted(cgr_set - oracle_set)]
    return DiffBucket(missing=missing, extra=extra)


def _prf(category: str, label: str, cgr: set[T], oracle: set[T]) -> ScoreRow | None:
    tp = len(cgr & oracle)
    fp = len(cgr - oracle)
    fn = len(oracle - cgr)
    if tp + fp + fn == 0:
        return None
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return ScoreRow(
        category=category,
        label=label,
        tp=tp,
        fp=fp,
        fn=fn,
        precision=round(precision, ec.ROUND_DIGITS),
        recall=round(recall, ec.ROUND_DIGITS),
        f1=round(f1, ec.ROUND_DIGITS),
    )


def _fmt_node(key: NodeKey, name: str) -> str:
    return ec.NODE_REPR.format(
        kind=key.kind, file=key.file, start=key.start_line, name=name
    )


def _fmt_edge(edge: EdgeKey) -> str:
    return ec.EDGE_REPR.format(
        rel=edge.rel_type,
        pfile=edge.parent.file,
        pstart=edge.parent.start_line,
        cfile=edge.child.file,
        cstart=edge.child.start_line,
    )


def _node_bucket(
    cgr_set: set[NodeKey],
    oracle_set: set[NodeKey],
    cgr: GraphData,
    oracle: GraphData,
) -> DiffBucket:
    missing = [_fmt_node(k, oracle.nodes[k].name) for k in sorted(oracle_set - cgr_set)]
    extra = [_fmt_node(k, cgr.nodes[k].name) for k in sorted(cgr_set - oracle_set)]
    return DiffBucket(missing=missing, extra=extra)


def _edge_bucket(cgr_set: set[EdgeKey], oracle_set: set[EdgeKey]) -> DiffBucket:
    missing = [_fmt_edge(e) for e in sorted(oracle_set - cgr_set)]
    extra = [_fmt_edge(e) for e in sorted(cgr_set - oracle_set)]
    return DiffBucket(missing=missing, extra=extra)


def _location_stats(cgr: GraphData, oracle: GraphData) -> LocationStats:
    shared = [
        k
        for k in cgr.nodes.keys() & oracle.nodes.keys()
        if k.kind in ec.SPANNED_NODE_KINDS
    ]
    deltas = [abs(cgr.nodes[k].end_line - oracle.nodes[k].end_line) for k in shared]
    if not deltas:
        return LocationStats(0, 0, 0, 0.0, 0)
    return LocationStats(
        matched=len(deltas),
        end_exact=sum(1 for d in deltas if d == 0),
        end_within_one=sum(1 for d in deltas if d <= 1),
        mean_abs_delta=round(fmean(deltas), ec.ROUND_DIGITS),
        max_abs_delta=max(deltas),
    )
