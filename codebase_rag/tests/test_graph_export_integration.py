from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from codebase_rag.graph_loader import load_graph
from codebase_rag.tests.conftest import (
    create_and_run_updater,
)
from codebase_rag.types_defs import GraphData, GraphMetadata


def build_graph_data_from_mock(mock_ingestor) -> GraphData:
    nodes = []
    node_id_counter = 1
    key_to_id: dict[tuple[str, str], int] = {}

    for call in mock_ingestor.ensure_node_batch.call_args_list:
        label = str(call[0][0])
        props = call[0][1]

        qn = props.get("qualified_name")
        name = props.get("name")
        path = props.get("path")

        if qn:
            key = (label, qn)
        elif name:
            key = (label, name)
        elif path:
            key = (label, path)
        else:
            continue

        if key not in key_to_id:
            key_to_id[key] = node_id_counter
            node_id_counter += 1

            nodes.append(
                {
                    "node_id": key_to_id[key],
                    "labels": [label],
                    "properties": props,
                }
            )

    relationships = []
    for call in mock_ingestor.ensure_relationship_batch.call_args_list:
        from_tuple = call[0][0]
        rel_type = call[0][1]
        to_tuple = call[0][2]
        props = call[0][3] if len(call[0]) > 3 else {}

        from_key = (str(from_tuple[0]), from_tuple[2])
        to_key = (str(to_tuple[0]), to_tuple[2])

        from_id = key_to_id.get(from_key, 0)
        to_id = key_to_id.get(to_key, 0)

        if from_id and to_id:
            relationships.append(
                {
                    "from_id": from_id,
                    "to_id": to_id,
                    "type": rel_type,
                    "properties": props,
                }
            )

    return GraphData(
        nodes=nodes,
        relationships=relationships,
        metadata=GraphMetadata(
            total_nodes=len(nodes),
            total_relationships=len(relationships),
            exported_at="2025-01-01T00:00:00Z",
        ),
    )


class TestGraphExportIntegration:
    def test_simple_python_function_exports_correctly(
        self, temp_repo: Path, mock_ingestor
    ) -> None:
        (temp_repo / "example.py").write_text(
            """
def greet(name):
    return f"Hello, {name}"
"""
        )

        create_and_run_updater(temp_repo, mock_ingestor)
        graph_data = build_graph_data_from_mock(mock_ingestor)

        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph_data, f)
            f.flush()
            loader = load_graph(f.name)
        Path(f.name).unlink()

        functions = loader.find_nodes_by_label("Function")
        assert len(functions) == 1
        assert functions[0].properties["name"] == "greet"

    def test_python_class_with_methods_exports_correctly(
        self, temp_repo: Path, mock_ingestor
    ) -> None:
        (temp_repo / "myclass.py").write_text(
            """
class Calculator:
    def add(self, a, b):
        return a + b

    def subtract(self, a, b):
        return a - b
"""
        )

        create_and_run_updater(temp_repo, mock_ingestor)
        graph_data = build_graph_data_from_mock(mock_ingestor)

        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph_data, f)
            f.flush()
            loader = load_graph(f.name)
        Path(f.name).unlink()

        classes = loader.find_nodes_by_label("Class")
        assert len(classes) == 1
        assert classes[0].properties["name"] == "Calculator"

        methods = loader.find_nodes_by_label("Method")
        method_names = {m.properties["name"] for m in methods}
        assert method_names == {"add", "subtract"}

    def test_function_call_relationship_exports(
        self, temp_repo: Path, mock_ingestor
    ) -> None:
        (temp_repo / "caller.py").write_text(
            """
def helper():
    return 42

def main():
    result = helper()
    return result
"""
        )

        create_and_run_updater(temp_repo, mock_ingestor)
        graph_data = build_graph_data_from_mock(mock_ingestor)

        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph_data, f)
            f.flush()
            loader = load_graph(f.name)
        Path(f.name).unlink()

        functions = loader.find_nodes_by_label("Function")
        assert len(functions) == 2

        calls_rels = [r for r in loader.relationships if r.type == "CALLS"]
        assert len(calls_rels) >= 1

    def test_module_defines_relationship_exports(
        self, temp_repo: Path, mock_ingestor
    ) -> None:
        (temp_repo / "mod.py").write_text(
            """
def foo():
    pass

class Bar:
    pass
"""
        )

        create_and_run_updater(temp_repo, mock_ingestor)
        graph_data = build_graph_data_from_mock(mock_ingestor)

        with NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph_data, f)
            f.flush()
            loader = load_graph(f.name)
        Path(f.name).unlink()

        defines_rels = [r for r in loader.relationships if r.type == "DEFINES"]
        assert len(defines_rels) >= 2

    def test_exported_json_structure_is_valid(
        self, temp_repo: Path, mock_ingestor
    ) -> None:
        (temp_repo / "simple.py").write_text("x = 1\n")

        create_and_run_updater(temp_repo, mock_ingestor)
        graph_data = build_graph_data_from_mock(mock_ingestor)

        assert "nodes" in graph_data
        assert "relationships" in graph_data
        assert "metadata" in graph_data
        assert "total_nodes" in graph_data["metadata"]
        assert "total_relationships" in graph_data["metadata"]
        assert "exported_at" in graph_data["metadata"]
