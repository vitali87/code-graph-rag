from pathlib import Path
from typing import Any

from loguru import logger

import codec.schema_pb2 as pb


class ProtobufFileIngestor:
    """
    Handles parsing graph nodes & relationships directly into a compact Protobuf
    serialsed file for efficient extraction & transmission of data without needing
    to build the graph first
    """

    # This mapping is the single source of truth for connecting the dynamic parser labels
    # to the static, consistent field names in the .proto schema's 'oneof' block.
    # Format: <NodeLabel> : <oneof payload field name>
    LABEL_TO_ONEOF_FIELD: dict[str, str] = {
        "Project": "project",
        "Package": "package",
        "Folder": "folder",
        "Module": "module",
        "Class": "class",
        "Function": "function",
        "Method": "method",
        "File": "file",
        "ExternalPackage": "external_package",
        "ModuleImplementation": "module_implementation",
        "ModuleInterface": "module_interface",
    }

    ONEOF_FIELD_TO_LABEL: dict[str, str] = {
        v: k for k, v in LABEL_TO_ONEOF_FIELD.items()
    }

    def __init__(self, output_path: str, split_index: bool = False):
        self.output_dir = Path(output_path)
        self._nodes: dict[str, pb.Node] = {}
        self._relationships: dict[tuple[str, int, str], pb.Relationship] = {}
        self.split_index = split_index
        logger.info(f"ProtobufFileIngestor initialized to write to: {self.output_dir}")

    def _get_node_id(self, label: str, properties: dict) -> str:
        """Determines the primary/node key for a node."""
        if label in ["Folder", "File"]:
            return str(properties.get("path", ""))
        elif label in ["ExternalPackage", "Project"]:
            return str(properties.get("name", ""))
        else:
            return str(properties.get("qualified_name", ""))

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        """Creates a protobuf Node message and adds it to the in-memory buffer."""
        node_id = self._get_node_id(label, properties)
        if not node_id or node_id in self._nodes:
            return

        payload_message_class = getattr(pb, label, None)
        if not payload_message_class:
            logger.warning(
                f"No Protobuf message class found for label '{label}'. Skipping node."
            )
            return

        payload_message = payload_message_class()

        # Populate the specific payload message (e.g., pb.Class)
        for key, value in properties.items():
            if hasattr(payload_message, key):
                if value is None:
                    continue
                destination_attribute = getattr(payload_message, key)
                if hasattr(destination_attribute, "extend") and isinstance(value, list):
                    # This is a repeated field. Use .extend() to populate it.
                    destination_attribute.extend(value)
                else:
                    # This is a scalar field. Use a simple assignment.
                    setattr(payload_message, key, value)

        node = pb.Node()

        payload_field_name = self.LABEL_TO_ONEOF_FIELD.get(label)
        if not payload_field_name:
            logger.warning(
                f"No 'oneof' field mapping found for label '{label}'. Skipping node."
            )
            return

        # Set the 'oneof' payload field using the correct name
        getattr(node, payload_field_name).CopyFrom(payload_message)

        self._nodes[node_id] = node

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, Any],
        rel_type: str,
        to_spec: tuple[str, str, Any],
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Creates a protobuf Relationship message and adds it to the buffer."""
        rel = pb.Relationship()

        try:
            rel.type = pb.Relationship.RelationshipType.Value(rel_type)
        except ValueError:
            logger.warning(
                f"Unknown relationship type '{rel_type}'. Setting to UNSPECIFIED."
            )
            rel.type = pb.Relationship.RelationshipType.RELATIONSHIP_TYPE_UNSPECIFIED

        rel.source_id = str(from_spec[2])
        rel.target_id = str(to_spec[2])

        if rel.source_id.strip() == "" or rel.target_id.strip() == "":
            logger.warning(
                f"Invalid relationship: source_id={rel.source_id}, target_id={rel.target_id}"
            )
            return

        if properties:
            rel.properties.update(properties)

        unique_key = (rel.source_id, rel.type, rel.target_id)
        if unique_key in self._relationships:
            existing_rel = self._relationships[unique_key]
            if properties:
                existing_rel.properties.update(properties)
        else:
            self._relationships[unique_key] = rel

    def _flush_joint(self) -> None:
        """Assembles index into a single Protobuf file"""

        index = pb.GraphCodeIndex()
        index.nodes.extend(self._nodes.values())
        index.relationships.extend(self._relationships.values())

        serialised_file = index.SerializeToString()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / "index.bin"
        with open(out_path, "wb") as f:
            f.write(serialised_file)

        logger.success(
            f"Successfully flushed {len(self._nodes)} unique nodes and {len(self._relationships)} unique relationships to {self.output_dir}"
        )

    def _flush_split(self) -> None:
        """Assembles index into two separate binary files in the output directory:
        'nodes.bin' and 'relationships.bin'."""

        nodes_index = pb.GraphCodeIndex()
        rels_index = pb.GraphCodeIndex()
        nodes_index.nodes.extend(self._nodes.values())
        rels_index.relationships.extend(self._relationships.values())

        serialised_nodes = nodes_index.SerializeToString()
        serialised_rels = rels_index.SerializeToString()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        nodes_path = self.output_dir / "nodes.bin"
        rels_path = self.output_dir / "relationships.bin"

        with open(nodes_path, "wb") as f:
            f.write(serialised_nodes)

        with open(rels_path, "wb") as f:
            f.write(serialised_rels)

        logger.success(
            f"Successfully flushed {len(self._nodes)} unique nodes and {len(self._relationships)} unique relationships to {self.output_dir}"
        )

    def flush_all(self) -> None:
        """Assembles and writes the final binary file(s)"""
        logger.info(f"Flushing data to {self.output_dir}...")

        if self.split_index:
            return self._flush_split()
        else:
            return self._flush_joint()
