import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from loguru import logger


@dataclass
class GraphNode:
    """Represents a node in the exported graph."""
    node_id: int
    labels: List[str]
    properties: Dict[str, Any]


@dataclass
class GraphRelationship:
    """Represents a relationship in the exported graph."""
    from_id: int
    to_id: int
    type: str
    properties: Dict[str, Any]


class GraphLoader:
    """Utility class for loading and working with exported graph data."""
    
    def __init__(self, file_path: str):
        """Initialize the loader with an exported graph file."""
        self.file_path = Path(file_path)
        self._data: Optional[Dict[str, Any]] = None
        self._nodes: Optional[List[GraphNode]] = None
        self._relationships: Optional[List[GraphRelationship]] = None
    
    def load(self) -> None:
        """Load the graph data from file."""
        if not self.file_path.exists():
            raise FileNotFoundError(f"Graph file not found: {self.file_path}")
        
        logger.info(f"Loading graph from {self.file_path}")
        with open(self.file_path, 'r', encoding='utf-8') as f:
            self._data = json.load(f)
        
        # Ensure data is loaded
        if self._data is None:
            raise RuntimeError("Failed to load data from file")
        
        # Parse nodes
        self._nodes = [
            GraphNode(
                node_id=node['node_id'],
                labels=node['labels'],
                properties=node['properties']
            )
            for node in self._data['nodes']
        ]
        
        # Parse relationships
        self._relationships = [
            GraphRelationship(
                from_id=rel['from_id'],
                to_id=rel['to_id'],
                type=rel['type'],
                properties=rel['properties']
            )
            for rel in self._data['relationships']
        ]
        
        logger.info(f"Loaded {len(self._nodes)} nodes and {len(self._relationships)} relationships")
    
    @property
    def nodes(self) -> List[GraphNode]:
        """Get all nodes."""
        if self._nodes is None:
            self.load()
        assert self._nodes is not None, "Nodes should be loaded"
        return self._nodes
    
    @property
    def relationships(self) -> List[GraphRelationship]:
        """Get all relationships."""
        if self._relationships is None:
            self.load()
        assert self._relationships is not None, "Relationships should be loaded"
        return self._relationships
    
    @property
    def metadata(self) -> Dict[str, Any]:
        """Get metadata about the export."""
        if self._data is None:
            self.load()
        assert self._data is not None, "Data should be loaded"
        return self._data['metadata']
    
    def find_nodes_by_label(self, label: str) -> List[GraphNode]:
        """Find all nodes with a specific label."""
        return [node for node in self.nodes if label in node.labels]
    
    def find_node_by_property(self, property_name: str, value: Any) -> List[GraphNode]:
        """Find nodes by property value."""
        return [
            node for node in self.nodes 
            if node.properties.get(property_name) == value
        ]
    
    def get_relationships_for_node(self, node_id: int) -> List[GraphRelationship]:
        """Get all relationships (incoming and outgoing) for a specific node."""
        return [
            rel for rel in self.relationships 
            if rel.from_id == node_id or rel.to_id == node_id
        ]
    
    def get_outgoing_relationships(self, node_id: int) -> List[GraphRelationship]:
        """Get outgoing relationships for a specific node."""
        return [rel for rel in self.relationships if rel.from_id == node_id]
    
    def get_incoming_relationships(self, node_id: int) -> List[GraphRelationship]:
        """Get incoming relationships for a specific node."""
        return [rel for rel in self.relationships if rel.to_id == node_id]
    
    def summary(self) -> Dict[str, Any]:
        """Get a summary of the graph structure."""
        node_labels = {}
        relationship_types = {}
        
        for node in self.nodes:
            for label in node.labels:
                node_labels[label] = node_labels.get(label, 0) + 1
        
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