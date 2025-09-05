from pathlib import Path
from typing import Any, cast

import codec.schema_pb2 as pb
from codebase_rag.services.protobuf_service import ProtobufFileIngestor

SAMPLE_NODES = {
    "project_node": {
        "label": "Project",
        "properties": {"name": "test_project", "qualified_name": "test_project"},
    },
    "class_node": {
        "label": "Class",
        "properties": {
            "qualified_name": "test_project.UserService",
            "name": "UserService",
            "start_line": 10,
            "end_line": 25,
            "decorators": ["@injectable"],
            "docstring": "A class for users.",
            "is_exported": False,
        },
    },
    "method_node": {
        "label": "Method",
        "properties": {
            "qualified_name": "test_project.UserService.get_user",
            "name": "get_user",
            "start_line": 15,
            "end_line": 20,
            "decorators": [],
            "docstring": "Gets a user.",
        },
    },
}

SAMPLE_RELATIONSHIPS = [
    {
        "from_spec": ("Class", "qualified_name", "test_project.UserService"),
        "rel_type": "DEFINES_METHOD",
        "to_spec": ("Method", "qualified_name", "test_project.UserService.get_user"),
        "properties": None,
    }
]


def test_protobuf_ingestor_joint_serialization_and_deserialization(
    tmp_path: Path,
) -> None:
    """
    Validates the joint output mode with standardized filename: index.bin under the provided directory.
    """
    output_dir = tmp_path / "out_joint"
    output_dir.mkdir(parents=True, exist_ok=True)
    ingestor = ProtobufFileIngestor(str(output_dir), split_index=False)

    for node_data in SAMPLE_NODES.values():
        ingestor.ensure_node_batch(
            str(node_data["label"]), cast(dict[str, Any], node_data["properties"])
        )

    for rel_data in SAMPLE_RELATIONSHIPS:
        ingestor.ensure_relationship_batch(
            cast(tuple[str, str, Any], rel_data["from_spec"]),
            str(rel_data["rel_type"]),
            cast(tuple[str, str, Any], rel_data["to_spec"]),
            cast(dict[str, Any], rel_data["properties"])
            if rel_data["properties"]
            else None,
        )

    ingestor.flush_all()

    output_file = output_dir / "index.bin"
    assert output_file.exists()
    assert output_file.stat().st_size > 0

    # Read and deserialize
    with open(output_file, "rb") as f:
        serialized_data = f.read()

    deserialized_index = pb.GraphCodeIndex()
    deserialized_index.ParseFromString(serialized_data)

    assert len(deserialized_index.nodes) == 3

    # Create a simple lookup map for easy assertion
    deserialized_nodes_map = {}
    for node in deserialized_index.nodes:
        payload_field = node.WhichOneof("payload")
        payload_message = getattr(node, payload_field)
        node_id = getattr(
            payload_message, "qualified_name", getattr(payload_message, "name", None)
        )
        deserialized_nodes_map[node_id] = payload_message

    # Assert project node
    project_payload = deserialized_nodes_map["test_project"]
    assert isinstance(project_payload, pb.Project)
    assert project_payload.name == "test_project"

    class_payload = deserialized_nodes_map["test_project.UserService"]
    assert isinstance(class_payload, pb.Class)
    assert class_payload.name == "UserService"
    assert class_payload.start_line == 10
    assert class_payload.decorators[0] == "@injectable"

    assert len(deserialized_index.relationships) == 1

    rel = deserialized_index.relationships[0]
    assert rel.type == pb.Relationship.RelationshipType.Value("DEFINES_METHOD")
    assert rel.source_id == "test_project.UserService"
    assert rel.target_id == "test_project.UserService.get_user"
    assert rel.source_label == "Class"
    assert rel.target_label == "Method"


def test_protobuf_ingestor_split_index_serialization_and_deserialization(
    tmp_path: Path,
) -> None:
    """
    Validates the split-index output mode with standardized filenames under the provided directory:
    nodes.bin and relationships.bin.
    """
    output_dir = tmp_path / "out_split"
    output_dir.mkdir(parents=True, exist_ok=True)
    ingestor = ProtobufFileIngestor(str(output_dir), split_index=True)

    for node_data in SAMPLE_NODES.values():
        ingestor.ensure_node_batch(
            str(node_data["label"]), cast(dict[str, Any], node_data["properties"])
        )

    for rel_data in SAMPLE_RELATIONSHIPS:
        ingestor.ensure_relationship_batch(
            cast(tuple[str, str, Any], rel_data["from_spec"]),
            str(rel_data["rel_type"]),
            cast(tuple[str, str, Any], rel_data["to_spec"]),
            cast(dict[str, Any], rel_data["properties"])
            if rel_data["properties"]
            else None,
        )

    ingestor.flush_all()

    nodes_path = output_dir / "nodes.bin"
    rels_path = output_dir / "relationships.bin"

    # Assert files exist and are non-empty
    assert nodes_path.exists()
    assert rels_path.exists()
    assert nodes_path.stat().st_size > 0
    assert rels_path.stat().st_size > 0

    # Deserialize nodes file
    nodes_index = pb.GraphCodeIndex()
    with open(nodes_path, "rb") as f:
        nodes_index.ParseFromString(f.read())

    assert len(nodes_index.nodes) == 3
    assert len(nodes_index.relationships) == 0

    # Deserialize relationships file
    rels_index = pb.GraphCodeIndex()
    with open(rels_path, "rb") as f:
        rels_index.ParseFromString(f.read())

    assert len(rels_index.nodes) == 0
    assert len(rels_index.relationships) == 1

    rel = rels_index.relationships[0]
    assert rel.type == pb.Relationship.RelationshipType.Value("DEFINES_METHOD")
    assert rel.source_id == "test_project.UserService"
    assert rel.target_id == "test_project.UserService.get_user"
    assert rel.source_label == "Class"
    assert rel.target_label == "Method"
