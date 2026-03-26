from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from codebase_rag.graph_loader import GraphLoader, load_graph
from codebase_rag.types_defs import GraphData


def create_test_graph() -> GraphData:
    return {
        "nodes": [
            {
                "node_id": 1,
                "labels": ["Function"],
                "properties": {"name": "foo", "qualified_name": "mod.foo"},
            },
            {
                "node_id": 2,
                "labels": ["Function"],
                "properties": {"name": "bar", "qualified_name": "mod.bar"},
            },
            {
                "node_id": 3,
                "labels": ["Class"],
                "properties": {"name": "MyClass", "qualified_name": "mod.MyClass"},
            },
            {
                "node_id": 4,
                "labels": ["Module"],
                "properties": {"name": "mod", "path": "mod.py"},
            },
        ],
        "relationships": [
            {"from_id": 4, "to_id": 1, "type": "DEFINES", "properties": {}},
            {"from_id": 4, "to_id": 2, "type": "DEFINES", "properties": {}},
            {"from_id": 4, "to_id": 3, "type": "DEFINES", "properties": {}},
            {"from_id": 1, "to_id": 2, "type": "CALLS", "properties": {"line": 10}},
        ],
        "metadata": {
            "total_nodes": 4,
            "total_relationships": 4,
            "exported_at": "2025-01-01T00:00:00Z",
        },
    }


@pytest.fixture
def graph_file() -> Generator[str, None, None]:
    data = create_test_graph()
    with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        f.flush()
        yield f.name
    Path(f.name).unlink()


@pytest.fixture
def loader(graph_file: str) -> GraphLoader:
    return load_graph(graph_file)


class TestGraphLoaderLoad:
    def test_load_parses_nodes(self, loader: GraphLoader) -> None:
        assert len(loader.nodes) == 4

    def test_load_parses_relationships(self, loader: GraphLoader) -> None:
        assert len(loader.relationships) == 4

    def test_load_parses_metadata(self, loader: GraphLoader) -> None:
        assert loader.metadata["exported_at"] == "2025-01-01T00:00:00Z"

    def test_load_file_not_found_raises(self) -> None:
        loader = GraphLoader("/nonexistent/path.json")
        with pytest.raises(FileNotFoundError):
            loader.load()

    def test_lazy_loading(self, graph_file: str) -> None:
        loader = GraphLoader(graph_file)
        assert loader._data is None
        _ = loader.nodes
        assert loader._data is not None


class TestGraphLoaderNodeLookup:
    def test_find_nodes_by_label(self, loader: GraphLoader) -> None:
        functions = loader.find_nodes_by_label("Function")
        assert len(functions) == 2
        names = {n.properties["name"] for n in functions}
        assert names == {"foo", "bar"}

    def test_find_nodes_by_label_empty(self, loader: GraphLoader) -> None:
        result = loader.find_nodes_by_label("NonexistentLabel")
        assert result == []

    def test_get_node_by_id(self, loader: GraphLoader) -> None:
        node = loader.get_node_by_id(1)
        assert node is not None
        assert node.properties["name"] == "foo"

    def test_get_node_by_id_not_found(self, loader: GraphLoader) -> None:
        node = loader.get_node_by_id(999)
        assert node is None

    def test_find_node_by_property(self, loader: GraphLoader) -> None:
        nodes = loader.find_node_by_property("name", "foo")
        assert len(nodes) == 1
        assert nodes[0].node_id == 1

    def test_find_node_by_property_multiple_matches(self, graph_file: str) -> None:
        data = create_test_graph()
        data["nodes"].append(
            {
                "node_id": 5,
                "labels": ["Function"],
                "properties": {"name": "foo", "qualified_name": "other.foo"},
            }
        )
        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            f.flush()
            loader = load_graph(f.name)
        Path(f.name).unlink()

        nodes = loader.find_node_by_property("name", "foo")
        assert len(nodes) == 2

    def test_find_node_by_property_not_found(self, loader: GraphLoader) -> None:
        nodes = loader.find_node_by_property("name", "nonexistent")
        assert nodes == []


class TestGraphLoaderRelationshipLookup:
    def test_get_outgoing_relationships(self, loader: GraphLoader) -> None:
        rels = loader.get_outgoing_relationships(4)
        assert len(rels) == 3
        assert all(r.type == "DEFINES" for r in rels)

    def test_get_outgoing_relationships_empty(self, loader: GraphLoader) -> None:
        rels = loader.get_outgoing_relationships(999)
        assert rels == []

    def test_get_incoming_relationships(self, loader: GraphLoader) -> None:
        rels = loader.get_incoming_relationships(1)
        assert len(rels) == 1
        assert rels[0].type == "DEFINES"

    def test_get_incoming_relationships_empty(self, loader: GraphLoader) -> None:
        rels = loader.get_incoming_relationships(999)
        assert rels == []

    def test_get_relationships_for_node(self, loader: GraphLoader) -> None:
        rels = loader.get_relationships_for_node(1)
        assert len(rels) == 2

    def test_relationship_properties(self, loader: GraphLoader) -> None:
        rels = loader.get_outgoing_relationships(1)
        assert len(rels) == 1
        assert rels[0].properties.get("line") == 10


class TestGraphLoaderSummary:
    def test_summary_total_nodes(self, loader: GraphLoader) -> None:
        summary = loader.summary()
        assert summary["total_nodes"] == 4

    def test_summary_total_relationships(self, loader: GraphLoader) -> None:
        summary = loader.summary()
        assert summary["total_relationships"] == 4

    def test_summary_node_labels(self, loader: GraphLoader) -> None:
        summary = loader.summary()
        assert summary["node_labels"]["Function"] == 2
        assert summary["node_labels"]["Class"] == 1
        assert summary["node_labels"]["Module"] == 1

    def test_summary_relationship_types(self, loader: GraphLoader) -> None:
        summary = loader.summary()
        assert summary["relationship_types"]["DEFINES"] == 3
        assert summary["relationship_types"]["CALLS"] == 1

    def test_summary_includes_metadata(self, loader: GraphLoader) -> None:
        summary = loader.summary()
        assert summary["metadata"]["exported_at"] == "2025-01-01T00:00:00Z"


class TestLoadGraphFunction:
    def test_load_graph_returns_loaded_loader(self, graph_file: str) -> None:
        loader = load_graph(graph_file)
        assert loader._data is not None
        assert len(loader.nodes) == 4
