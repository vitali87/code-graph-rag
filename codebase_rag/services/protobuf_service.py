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
    cs.NodeLabel.EXTERNAL_MODULE: cs.ONEOF_EXTERNAL_MODULE,
    cs.NodeLabel.MODULE_IMPLEMENTATION: cs.ONEOF_MODULE_IMPLEMENTATION,
    cs.NodeLabel.MODULE_INTERFACE: cs.ONEOF_MODULE_INTERFACE,
    cs.NodeLabel.INTERFACE: cs.ONEOF_INTERFACE,
    cs.NodeLabel.ENUM: cs.ONEOF_ENUM,
    cs.NodeLabel.TYPE: cs.ONEOF_TYPE,
    cs.NodeLabel.UNION: cs.ONEOF_UNION,
    cs.NodeLabel.RESOURCE: cs.ONEOF_RESOURCE,
}

ONEOF_FIELD_TO_LABEL: dict[str, cs.NodeLabel] = {
    v: k for k, v in LABEL_TO_ONEOF_FIELD.items()
}

PATH_BASED_LABELS = frozenset({cs.NodeLabel.FOLDER, cs.NodeLabel.FILE})
NAME_BASED_LABELS = frozenset({cs.NodeLabel.EXTERNAL_PACKAGE, cs.NodeLabel.PROJECT})


_REL_TYPE_CACHE: dict = {}
_MSG_CLASS_CACHE: dict[str, type | None] = {}


class ProtobufFileIngestor:
    __slots__ = (
        "output_dir",
        "_nodes",
        "_relationships",
        "split_index",
        "_repo_prefix",
    )

    def __init__(
        self,
        output_path: str,
        split_index: bool = False,
        repo_path: str | None = None,
    ):
        self.output_dir = Path(output_path)
        self._nodes: dict[str, pb.Node] = {}
        self._relationships: dict[tuple[str, int, str], pb.Relationship] = {}
        self.split_index = split_index
        # File/Folder identities are ABSOLUTE paths in the live graph (the
        # realtime watcher deletes by absolute path, issue #1141), which would
        # make the artifact differ per checkout location. The canonical export
        # relativizes every id under the repo root instead (issue #1138).
        self._repo_prefix = (
            str(Path(repo_path).resolve()).replace("\\", "/").rstrip("/") + "/"
            if repo_path
            else None
        )
        logger.info(ls.PROTOBUF_INIT.format(path=self.output_dir))

    def _canonical_ref(self, value: str) -> str:
        # Separator-normalized prefix strip: Windows writers hand native
        # backslash paths while the resolved prefix and POSIX writers use
        # forward slashes, so both sides normalize before the comparison and
        # the artifact always carries forward-slash relative ids.
        if self._repo_prefix is None:
            return value
        normalized = value.replace("\\", "/")
        if normalized.startswith(self._repo_prefix):
            return normalized[len(self._repo_prefix) :]
        # Already-relative Windows ids (src\main.py) must also serialize with
        # forward slashes or the artifact bytes differ per OS; source-derived
        # identities are the same text on every OS, so this normalization is
        # itself deterministic.
        return normalized

    def _get_node_id(self, label: cs.NodeLabel, properties: PropertyDict) -> str:
        if label in PATH_BASED_LABELS:
            return self._canonical_ref(str(properties.get(cs.KEY_PATH, "")))
        if label in NAME_BASED_LABELS:
            return str(properties.get(cs.KEY_NAME, ""))
        return str(properties.get(cs.KEY_QUALIFIED_NAME, ""))

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        node_label = cs.NodeLabel(label)
        node_id = self._get_node_id(node_label, properties)
        if not node_id:
            return

        payload_field_name = LABEL_TO_ONEOF_FIELD.get(node_label)
        if not payload_field_name:
            logger.warning(ls.PROTOBUF_NO_ONEOF_MAPPING.format(label=label))
            return

        # Repeated ensures of one node MERGE, mirroring the graph sink's
        # `MERGE ... SET n += props`: a later batch may carry properties
        # the first did not (the Rust cfg(test) declaration record lands
        # after the module's own node, issue #1010). Each provided key
        # replaces the stored value wholesale, lists included. SAME label
        # only: qn strings collide across labels (Rust `mod run` and
        # `fn run`), and writing through the other oneof field would
        # switch the payload and clear the stored node, so a cross-label
        # ensure keeps the first writer, as before the merge existed.
        if (existing := self._nodes.get(node_id)) is not None:
            if existing.WhichOneof(cs.PROTOBUF_PAYLOAD_ONEOF) != payload_field_name:
                return
            payload_message = getattr(existing, payload_field_name)
        else:
            if label in _MSG_CLASS_CACHE:
                payload_message_class = _MSG_CLASS_CACHE[label]
            else:
                payload_message_class = getattr(pb, label, None)
                _MSG_CLASS_CACHE[label] = payload_message_class
            if not payload_message_class:
                logger.warning(ls.PROTOBUF_NO_MESSAGE_CLASS.format(label=label))
                return
            node = pb.Node()
            payload_message = getattr(node, payload_field_name)
            self._nodes[node_id] = node

        for key, value in properties.items():
            if hasattr(payload_message, key):
                if value is None:
                    continue
                if key == cs.KEY_PATH and isinstance(value, str):
                    # The payload path must match the node's canonical id:
                    # writers already emit repo-relative paths here, but the
                    # export enforces it so an absolute writer path can never
                    # make the artifact checkout-specific.
                    value = self._canonical_ref(value)
                destination_attribute = getattr(payload_message, key)
                if hasattr(destination_attribute, "extend") and isinstance(value, list):
                    del destination_attribute[:]
                    destination_attribute.extend(value)
                else:
                    setattr(payload_message, key, value)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        if rel_type in _REL_TYPE_CACHE:
            rel_type_enum = _REL_TYPE_CACHE[rel_type]
        else:
            resolved = getattr(pb.Relationship.RelationshipType, rel_type, None)
            if resolved is None:
                logger.warning(ls.PROTOBUF_UNKNOWN_REL_TYPE.format(rel_type=rel_type))
                resolved = (
                    pb.Relationship.RelationshipType.RELATIONSHIP_TYPE_UNSPECIFIED
                )
            rel_type_enum = resolved
            _REL_TYPE_CACHE[rel_type] = rel_type_enum

        from_label, _, from_val_raw = from_spec
        to_label, _, to_val_raw = to_spec

        from_val = (
            self._canonical_ref(str(from_val_raw)) if from_val_raw is not None else ""
        )
        to_val = self._canonical_ref(str(to_val_raw)) if to_val_raw is not None else ""

        unique_key = (from_val, rel_type_enum, to_val)
        if unique_key in self._relationships:
            if properties:
                self._relationships[unique_key].properties.update(properties)
            return

        if not from_val.strip() or not to_val.strip():
            logger.warning(
                ls.PROTOBUF_INVALID_REL.format(source_id=from_val, target_id=to_val)
            )
            return

        rel = pb.Relationship()
        rel.type = rel_type_enum
        rel.source_id = from_val
        rel.source_label = str(from_label)
        rel.target_id = to_val
        rel.target_label = str(to_label)
        if properties:
            rel.properties.update(properties)
        self._relationships[unique_key] = rel

    def _sorted_nodes(self) -> list[pb.Node]:
        # Canonical order (issue #1138): node ids are unique within the map, so
        # the id alone is a total order; insertion order (parse order) must
        # never leak into the artifact bytes.
        return [node for _id, node in sorted(self._nodes.items())]

    def _sorted_relationships(self) -> list[pb.Relationship]:
        # The map key IS (source_id, type, target_id): a deterministic total
        # order with the type enum's integer as the middle tiebreak.
        return [rel for _key, rel in sorted(self._relationships.items())]

    def _flush_joint(self) -> None:
        index = pb.GraphCodeIndex()
        index.nodes.extend(self._sorted_nodes())
        index.relationships.extend(self._sorted_relationships())

        serialised_file = index.SerializeToString(deterministic=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.output_dir / cs.PROTOBUF_INDEX_FILE
        with open(out_path, "wb") as f:
            f.write(serialised_file)
        # The two layouts are mutually exclusive: a leftover split pair beside
        # a fresh joint index would double every manifest coverage count.
        (self.output_dir / cs.PROTOBUF_NODES_FILE).unlink(missing_ok=True)
        (self.output_dir / cs.PROTOBUF_RELS_FILE).unlink(missing_ok=True)

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
        nodes_index.nodes.extend(self._sorted_nodes())
        rels_index.relationships.extend(self._sorted_relationships())

        serialised_nodes = nodes_index.SerializeToString(deterministic=True)
        serialised_rels = rels_index.SerializeToString(deterministic=True)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        nodes_path = self.output_dir / cs.PROTOBUF_NODES_FILE
        rels_path = self.output_dir / cs.PROTOBUF_RELS_FILE

        with open(nodes_path, "wb") as f:
            f.write(serialised_nodes)

        with open(rels_path, "wb") as f:
            f.write(serialised_rels)

        (self.output_dir / cs.PROTOBUF_INDEX_FILE).unlink(missing_ok=True)

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
