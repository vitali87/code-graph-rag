from collections import defaultdict
from enum import StrEnum
from typing import NamedTuple, TypedDict

PropertyValue = str | int | float | bool | None

type SimpleName = str
type QualifiedName = str
type SimpleNameLookup = defaultdict[SimpleName, set[QualifiedName]]


class NodeType(StrEnum):
    FUNCTION = "Function"
    METHOD = "Method"
    CLASS = "Class"
    MODULE = "Module"
    INTERFACE = "Interface"
    PACKAGE = "Package"
    ENUM = "Enum"
    TYPE = "Type"
    UNION = "Union"


type TrieNode = dict[str, TrieNode | QualifiedName | NodeType]
type FunctionRegistry = dict[QualifiedName, NodeType]


class GraphMetadata(TypedDict):
    total_nodes: int
    total_relationships: int
    exported_at: str


class NodeData(TypedDict):
    node_id: int
    labels: list[str]
    properties: dict[str, PropertyValue]


class RelationshipData(TypedDict):
    from_id: int
    to_id: int
    type: str
    properties: dict[str, PropertyValue]


class GraphData(TypedDict):
    nodes: list[NodeData]
    relationships: list[RelationshipData]
    metadata: GraphMetadata


class GraphSummary(TypedDict):
    total_nodes: int
    total_relationships: int
    node_labels: dict[str, int]
    relationship_types: dict[str, int]
    metadata: GraphMetadata


class EmbeddingQueryResult(TypedDict):
    node_id: int
    qualified_name: str
    start_line: int | None
    end_line: int | None
    path: str | None


class CancelledResult(NamedTuple):
    cancelled: bool


ToolArgValue = str | int | float | bool | list[str] | None
