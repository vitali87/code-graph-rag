from typing import NamedTuple, TypedDict


class NodeKey(NamedTuple):
    kind: str
    file: str
    start_line: int


class DefNode(NamedTuple):
    key: NodeKey
    name: str
    end_line: int


class EdgeKey(NamedTuple):
    rel_type: str
    parent: NodeKey
    child: NodeKey


class NameEdge(NamedTuple):
    rel_type: str
    source: NodeKey
    target_name: str


class GraphData(NamedTuple):
    nodes: dict[NodeKey, DefNode]
    edges: set[EdgeKey]
    name_edges: set[NameEdge]


class ScoreRow(TypedDict):
    category: str
    label: str
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


class LocationStats(NamedTuple):
    matched: int
    end_exact: int
    end_within_one: int
    mean_abs_delta: float
    max_abs_delta: int


class DiffBucket(TypedDict):
    missing: list[str]
    extra: list[str]


class ScoreResult(NamedTuple):
    rows: list[ScoreRow]
    location: LocationStats
    diff: dict[str, DiffBucket]


class OracleRecord(TypedDict):
    kind: str
    file: str
    line: int
    name: str


class OracleNodeRef(TypedDict):
    kind: str
    file: str
    line: int


class OracleEdge(TypedDict):
    rel: str
    parent: OracleNodeRef
    child: OracleNodeRef


class OracleNameEdge(TypedDict):
    rel: str
    source: OracleNodeRef
    target_name: str


class OraclePayload(TypedDict):
    nodes: list[OracleRecord]
    edges: list[OracleEdge]
    name_edges: list[OracleNameEdge]
