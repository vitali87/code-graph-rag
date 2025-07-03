import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass
class GraphNode:
    """Represents a node in the exported graph."""
    node_id: int
    labels: list[str]
    properties: dict[str, Any]


@dataclass
class GraphRelationship:
    """Represents a relationship in the exported graph."""
    from_id: int
    to_id: int
    type: str
    properties: dict[str, Any]


class GraphLoader:
    """Utility class for loading and working with exported graph data."""

    def __init__(self, file_path: str):
        """Initialize the loader with an exported graph file."""
        self.file_path = Path(file_path)
        self._data: dict[str, Any] | None = None
        self._nodes: list[GraphNode] | None = None
        self._relationships: list[GraphRelationship] | None = None

        # Performance indexes
        self._nodes_by_id: dict[int, GraphNode] = {}
        self._nodes_by_label: dict[str, list[GraphNode]] = defaultdict(list)
        self._outgoing_rels: dict[int, list[GraphRelationship]] = defaultdict(list)
        self._incoming_rels: dict[int, list[GraphRelationship]] = defaultdict(list)
        self._property_indexes: dict[str, dict[Any, list[GraphNode]]] = {}

    def load(self) -> None:
        """Load the graph data from file and build performance indexes."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Graph file not found: {self.file_path}")

        logger.info(f"Loading graph from {self.file_path}")
        with open(self.file_path, encoding='utf-8') as f:
            self._data = json.load(f)

        if self._data is None:
            raise RuntimeError("Failed to load data from file")

        # Parse nodes and build indexes
        self._nodes = []
        for node_data in self._data['nodes']:
            node = GraphNode(
                node_id=node_data['node_id'],
                labels=node_data['labels'],
                properties=node_data['properties']
            )
            self._nodes.append(node)

            # Build indexes
            self._nodes_by_id[node.node_id] = node
            for label in node.labels:
                self._nodes_by_label[label].append(node)

        # Parse relationships and build indexes
        self._relationships = []
        for rel_data in self._data['relationships']:
            rel = GraphRelationship(
                from_id=rel_data['from_id'],
                to_id=rel_data['to_id'],
                type=rel_data['type'],
                properties=rel_data['properties']
            )
            self._relationships.append(rel)

            # Build relationship indexes
            self._outgoing_rels[rel.from_id].append(rel)
            self._incoming_rels[rel.to_id].append(rel)

        logger.info(f"Loaded {len(self._nodes)} nodes and "
                   f"{len(self._relationships)} relationships with indexes")

    def _build_property_index(self, property_name: str) -> None:
        """Build index for a specific property."""
        if property_name in self._property_indexes:
            return

        index: dict[Any, list[GraphNode]] = defaultdict(list)
        for node in self.nodes:
            value = node.properties.get(property_name)
            if value is not None:
                index[value].append(node)
        self._property_indexes[property_name] = dict(index)

    @property
    def nodes(self) -> list[GraphNode]:
        """Get all nodes."""
        if self._nodes is None:
            self.load()
        assert self._nodes is not None, "Nodes should be loaded"
        return self._nodes

    @property
    def relationships(self) -> list[GraphRelationship]:
        """Get all relationships."""
        if self._relationships is None:
            self.load()
        assert self._relationships is not None, "Relationships should be loaded"
        return self._relationships

    @property
    def metadata(self) -> dict[str, Any]:
        """Get metadata about the export."""
        if self._data is None:
            self.load()
        assert self._data is not None, "Data should be loaded"
        return self._data['metadata']

    def find_nodes_by_label(self, label: str) -> list[GraphNode]:
        """Find all nodes with a specific label. O(1) lookup."""
        if self._nodes is None:
            self.load()
        return self._nodes_by_label.get(label, [])

    def find_node_by_property(self, property_name: str, value: Any) -> list[GraphNode]:
        """Find nodes by property value. O(1) lookup after first use."""
        if self._nodes is None:
            self.load()

        self._build_property_index(property_name)
        return self._property_indexes[property_name].get(value, [])

    def get_node_by_id(self, node_id: int) -> GraphNode | None:
        """Get a node by its ID. O(1) lookup."""
        if self._nodes is None:
            self.load()
        return self._nodes_by_id.get(node_id)

    def get_relationships_for_node(self, node_id: int) -> list[GraphRelationship]:
        """Get all relationships (incoming and outgoing) for a node. O(1) lookup."""
        return (self.get_outgoing_relationships(node_id) +
                self.get_incoming_relationships(node_id))

    def get_outgoing_relationships(self, node_id: int) -> list[GraphRelationship]:
        """Get outgoing relationships for a specific node. O(1) lookup."""
        if self._relationships is None:
            self.load()
        return self._outgoing_rels.get(node_id, [])

    def get_incoming_relationships(self, node_id: int) -> list[GraphRelationship]:
        """Get incoming relationships for a specific node. O(1) lookup."""
        if self._relationships is None:
            self.load()
        return self._incoming_rels.get(node_id, [])

    def summary(self) -> dict[str, Any]:
        """Get a summary of the graph structure."""
        node_labels = {label: len(nodes) for label, nodes in self._nodes_by_label.items()}

        relationship_types = {}
        for rel in self.relationships:
            relationship_types[rel.type] = relationship_types.get(rel.type, 0) + 1

        return {
            "total_nodes": len(self.nodes),
            "total_relationships": len(self.relationships),
            "node_labels": node_labels,
            "relationship_types": relationship_types,
            "metadata": self.metadata
        }


def load_graph(file_path: str) -> GraphLoader:
    """Convenience function to load a graph from file."""
    loader = GraphLoader(file_path)
    loader.load()
    return loader


# Example usage
if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python -m codebase_rag.graph_loader <graph_file.json>")
        sys.exit(1)

    graph_file = sys.argv[1]

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        print("Graph Summary:")
        print(f"  Total nodes: {summary['total_nodes']}")
        print(f"  Total relationships: {summary['total_relationships']}")
        print(f"  Node types: {list(summary['node_labels'].keys())}")
        print(f"  Relationship types: {list(summary['relationship_types'].keys())}")
        print(f"  Exported at: {summary['metadata']['exported_at']}")

    except Exception as e:
        logger.error(f"Failed to load graph: {e}")
        sys.exit(1)
