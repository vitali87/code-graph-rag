from __future__ import annotations

from pathlib import Path

from loguru import logger

import codec.schema_pb2 as pb

from .. import constants as cs
from .. import logs as ls
from ..types_defs import PropertyDict, PropertyValue

LABEL_TO_ONEOF_FIELD: dict[cs.NodeLabel, str] = {
    cs.NodeLabel.PROJECT: cs.ONEOF_PROJECT,
    cs.NodeLabel.PACKAGE: cs.ONEOF_PACKAGE,
    cs.NodeLabel.FOLDER: cs.ONEOF_FOLDER,
    cs.NodeLabel.MODULE: cs.ONEOF_MODULE,
    cs.NodeLabel.CLASS: cs.ONEOF_CLASS,
    cs.NodeLabel.FUNCTION: cs.ONEOF_FUNCTION,
    cs.NodeLabel.METHOD: cs.ONEOF_METHOD,
    cs.NodeLabel.FILE: cs.ONEOF_FILE,
    cs.NodeLabel.EXTERNAL_PACKAGE: cs.ONEOF_EXTERNAL_PACKAGE,
    cs.NodeLabel.MODULE_IMPLEMENTATION: cs.ONEOF_MODULE_IMPLEMENTATION,
    cs.NodeLabel.MODULE_INTERFACE: cs.ONEOF_MODULE_INTERFACE,
}

ONEOF_FIELD_TO_LABEL: dict[str, cs.NodeLabel] = {
    v: k for k, v in LABEL_TO_ONEOF_FIELD.items()
}

PATH_BASED_LABELS = frozenset({cs.NodeLabel.FOLDER, cs.NodeLabel.FILE})
NAME_BASED_LABELS = frozenset({cs.NodeLabel.EXTERNAL_PACKAGE, cs.NodeLabel.PROJECT})


class ProtobufFileIngestor:
    def __init__(self, output_path: str, split_index: bool = False):
        self.output_dir = Path(output_path)
        self._nodes: dict[str, pb.Node] = {}
        self._relationships: dict[tuple[str, int, str], pb.Relationship] = {}
        self.split_index = split_index
        logger.info(ls.PROTOBUF_INIT.format(path=self.output_dir))

    def _get_node_id(self, label: cs.NodeLabel, properties: PropertyDict) -> str:
        if label in PATH_BASED_LABELS:
            return str(properties.get(cs.KEY_PATH, ""))
        elif label in NAME_BASED_LABELS:
            return str(properties.get(cs.KEY_NAME, ""))
        else:
            return str(properties.get(cs.KEY_QUALIFIED_NAME, ""))

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        node_label = cs.NodeLabel(label)
        node_id = self._get_node_id(node_label, properties)
        if not node_id or node_id in self._nodes:
            return

        payload_message_class = getattr(pb, label, None)
        if not payload_message_class:
            logger.warning(ls.PROTOBUF_NO_MESSAGE_CLASS.format(label=label))
            return

        payload_message = payload_message_class()

        for key, value in properties.items():
            if hasattr(payload_message, key):
                if value is None:
                    continue
                destination_attribute = getattr(payload_message, key)
                if hasattr(destination_attribute, "extend") and isinstance(value, list):
                    destination_attribute.extend(value)
                else:
                    setattr(payload_message, key, value)

        node = pb.Node()

        payload_field_name = LABEL_TO_ONEOF_FIELD.get(node_label)
        if not payload_field_name:
            logger.warning(ls.PROTOBUF_NO_ONEOF_MAPPING.format(label=label))
            return

        getattr(node, payload_field_name).CopyFrom(payload_message)

        self._nodes[node_id] = node

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        rel = pb.Relationship()

        rel_type_enum = getattr(pb.Relationship.RelationshipType, rel_type, None)
        if rel_type_enum is None:
            logger.warning(ls.PROTOBUF_UNKNOWN_REL_TYPE.format(rel_type=rel_type))
            rel_type_enum = (
                pb.Relationship.RelationshipType.RELATIONSHIP_TYPE_UNSPECIFIED
            )
        rel.type = rel_type_enum

        from_label, _, from_val = from_spec
        to_label, _, to_val = to_spec

        rel.source_id = str(from_val)
        rel.source_label = str(from_label)
        rel.target_id = str(to_val)
        rel.target_label = str(to_label)

        if not rel.source_id.strip() or not rel.target_id.strip():
            logger.warning(
                ls.PROTOBUF_INVALID_REL.format(
                    source_id=rel.source_id, target_id=rel.target_id
                )
            )
            return

        if properties:
            rel.properties.update(properties)

        unique_key = (rel.source_id, rel.type, rel.target_id)
        if unique_key in self._relationships:
            if properties:
                existing_rel = self._relationships[unique_key]
                existing_rel.properties.update(properties)
        else:
            self._relationships[unique_key] = rel

    def _flush_joint(self) -> None:
        index = pb.GraphCodeIndex()
        index.nodes.extend(self._nodes.values())
        index.relationships.extend(self._relationships.values())

        serialised_file = index.SerializeToString()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / cs.PROTOBUF_INDEX_FILE
        with open(out_path, "wb") as f:
            f.write(serialised_file)

        logger.success(
            ls.PROTOBUF_FLUSH_SUCCESS.format(
                nodes=len(self._nodes),
                rels=len(self._relationships),
                path=self.output_dir,
            )
        )

    def _flush_split(self) -> None:
        nodes_index = pb.GraphCodeIndex()
        rels_index = pb.GraphCodeIndex()
        nodes_index.nodes.extend(self._nodes.values())
        rels_index.relationships.extend(self._relationships.values())

        serialised_nodes = nodes_index.SerializeToString()
        serialised_rels = rels_index.SerializeToString()

        self.output_dir.mkdir(parents=True, exist_ok=True)
        nodes_path = self.output_dir / cs.PROTOBUF_NODES_FILE
        rels_path = self.output_dir / cs.PROTOBUF_RELS_FILE

        with open(nodes_path, "wb") as f:
            f.write(serialised_nodes)

        with open(rels_path, "wb") as f:
            f.write(serialised_rels)

        logger.success(
            ls.PROTOBUF_FLUSH_SUCCESS.format(
                nodes=len(self._nodes),
                rels=len(self._relationships),
                path=self.output_dir,
            )
        )

    def flush_all(self) -> None:
        logger.info(ls.PROTOBUF_FLUSHING.format(path=self.output_dir))

        return self._flush_split() if self.split_index else self._flush_joint()
