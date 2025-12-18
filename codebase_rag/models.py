from dataclasses import dataclass
from typing import Any


@dataclass
class GraphNode:
    node_id: int
    labels: list[str]
    properties: dict[str, Any]


@dataclass
class GraphRelationship:
    from_id: int
    to_id: int
    type: str
    properties: dict[str, Any]
