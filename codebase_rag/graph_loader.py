import json
from collections import Counter, defaultdict
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .decorators import ensure_loaded
from .models import GraphNode, GraphRelationship
from .types_defs import (
    GraphData,
    GraphMetadata,
    GraphSummary,
    JsonValue,
    PropertyValue,
)


class GraphFileFormatError(ValueError):
    """The file is JSON but not a graph export; the message names what is off.

    A missing key used to surface as its bare `KeyError` repr (`'nodes'`),
    which does not say that the file has the wrong shape (#2446).
    """


_NODE_KEYS = (cs.KEY_NODE_ID, cs.KEY_LABELS, cs.KEY_PROPERTIES)
_RELATIONSHIP_KEYS = (cs.KEY_FROM_ID, cs.KEY_TO_ID, cs.KEY_TYPE, cs.KEY_PROPERTIES)


class GraphLoader:
    __slots__ = (
        "file_path",
        "_data",
        "_nodes",
        "_relationships",
        "_nodes_by_id",
        "_nodes_by_label",
        "_outgoing_rels",
        "_incoming_rels",
        "_property_indexes",
    )

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self._data: GraphData | None = None
        self._nodes: list[GraphNode] | None = None
        self._relationships: list[GraphRelationship] | None = None

        self._nodes_by_id: dict[int, GraphNode] = {}
        self._nodes_by_label: defaultdict[str, list[GraphNode]] = defaultdict(list)
        self._outgoing_rels: defaultdict[int, list[GraphRelationship]] = defaultdict(
            list
        )
        self._incoming_rels: defaultdict[int, list[GraphRelationship]] = defaultdict(
            list
        )
        self._property_indexes: dict[str, dict[PropertyValue, list[GraphNode]]] = {}

    def _ensure_loaded(self) -> None:
        if self._data is None:
            self.load()

    def load(self) -> None:
        if not self.file_path.exists():
            raise FileNotFoundError(ex.GRAPH_FILE_NOT_FOUND.format(path=self.file_path))

        logger.info(ls.LOADING_GRAPH.format(path=self.file_path))
        with open(self.file_path, encoding=cs.ENCODING_UTF8) as f:
            raw = json.load(f)

        if raw is None:
            raise RuntimeError(ex.FAILED_TO_LOAD_DATA)
        self._check_shape(raw)
        metadata = raw.get(cs.KEY_METADATA)
        if not _is_complete_metadata(metadata):
            # `metadata` is informational: a hand-built or `jq`-sliced graph
            # without it still loads, with counts read from the file (#2446).
            metadata = _filled_metadata(
                metadata, len(raw[cs.KEY_NODES]), len(raw[cs.KEY_RELATIONSHIPS])
            )
        self._data = GraphData(
            nodes=raw[cs.KEY_NODES],
            relationships=raw[cs.KEY_RELATIONSHIPS],
            metadata=metadata,
        )

        self._nodes = []
        for node_data in raw[cs.KEY_NODES]:
            node = GraphNode(
                node_id=node_data[cs.KEY_NODE_ID],
                labels=node_data[cs.KEY_LABELS],
                properties=node_data[cs.KEY_PROPERTIES],
            )
            self._nodes.append(node)

            self._nodes_by_id[node.node_id] = node
            for label in node.labels:
                self._nodes_by_label[label].append(node)

        self._relationships = []
        for rel_data in raw[cs.KEY_RELATIONSHIPS]:
            rel = GraphRelationship(
                from_id=rel_data[cs.KEY_FROM_ID],
                to_id=rel_data[cs.KEY_TO_ID],
                type=rel_data[cs.KEY_TYPE],
                properties=rel_data[cs.KEY_PROPERTIES],
            )
            self._relationships.append(rel)

            self._outgoing_rels[rel.from_id].append(rel)
            self._incoming_rels[rel.to_id].append(rel)

        logger.info(
            ls.LOADED_GRAPH.format(
                nodes=len(self._nodes), relationships=len(self._relationships)
            )
        )

    def _not_an_export(self, reason: str) -> GraphFileFormatError:
        return GraphFileFormatError(
            ex.GRAPH_FILE_NOT_AN_EXPORT.format(name=self.file_path.name, reason=reason)
        )

    def _check_shape(self, raw: JsonValue) -> None:
        if not isinstance(raw, dict):
            raise self._not_an_export(
                ex.GRAPH_FILE_NOT_AN_OBJECT.format(found=type(raw).__name__)
            )
        missing = [
            key for key in (cs.KEY_NODES, cs.KEY_RELATIONSHIPS) if key not in raw
        ]
        if missing:
            raise self._not_an_export(
                ex.GRAPH_FILE_MISSING_KEYS.format(missing=", ".join(missing))
            )
        self._check_items(raw[cs.KEY_NODES], cs.KEY_NODES, _NODE_KEYS)
        self._check_items(
            raw[cs.KEY_RELATIONSHIPS], cs.KEY_RELATIONSHIPS, _RELATIONSHIP_KEYS
        )

    def _check_items(
        self, items: JsonValue, key: str, required: tuple[str, ...]
    ) -> None:
        if not isinstance(items, list):
            raise self._not_an_export(ex.GRAPH_FILE_NOT_AN_ARRAY.format(key=key))
        kind = ex.GRAPH_FILE_ITEM_KINDS[key]
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise self._not_an_export(
                    ex.GRAPH_FILE_ITEM_NOT_AN_OBJECT.format(kind=kind, index=index)
                )
            absent = [name for name in required if name not in item]
            if absent:
                raise self._not_an_export(
                    ex.GRAPH_FILE_ITEM_MISSING.format(
                        kind=kind,
                        index=index,
                        missing=", ".join(f'"{name}"' for name in absent),
                    )
                )

    def _build_property_index(self, property_name: str) -> None:
        if property_name in self._property_indexes:
            return

        index: defaultdict[PropertyValue, list[GraphNode]] = defaultdict(list)
        for node in self.nodes:
            value = node.properties.get(property_name)
            if value is not None:
                index[value].append(node)
        self._property_indexes[property_name] = dict(index)

    @property
    @ensure_loaded
    def nodes(self) -> list[GraphNode]:
        assert self._nodes is not None, ex.NODES_NOT_LOADED
        return self._nodes

    @property
    @ensure_loaded
    def relationships(self) -> list[GraphRelationship]:
        assert self._relationships is not None, ex.RELATIONSHIPS_NOT_LOADED
        return self._relationships

    @property
    @ensure_loaded
    def metadata(self) -> GraphMetadata:
        assert self._data is not None, ex.DATA_NOT_LOADED
        return self._data[cs.KEY_METADATA]

    @ensure_loaded
    def find_nodes_by_label(self, label: str) -> list[GraphNode]:
        return self._nodes_by_label.get(label, [])

    @ensure_loaded
    def find_node_by_property(
        self, property_name: str, value: PropertyValue
    ) -> list[GraphNode]:
        self._build_property_index(property_name)
        return self._property_indexes[property_name].get(value, [])

    @ensure_loaded
    def get_node_by_id(self, node_id: int) -> GraphNode | None:
        return self._nodes_by_id.get(node_id)

    def get_relationships_for_node(self, node_id: int) -> list[GraphRelationship]:
        return self.get_outgoing_relationships(
            node_id
        ) + self.get_incoming_relationships(node_id)

    @ensure_loaded
    def get_outgoing_relationships(self, node_id: int) -> list[GraphRelationship]:
        return self._outgoing_rels.get(node_id, [])

    @ensure_loaded
    def get_incoming_relationships(self, node_id: int) -> list[GraphRelationship]:
        return self._incoming_rels.get(node_id, [])

    def summary(self) -> GraphSummary:
        node_labels = {
            label: len(nodes) for label, nodes in self._nodes_by_label.items()
        }
        relationship_types = dict(Counter(rel.type for rel in self.relationships))

        return GraphSummary(
            total_nodes=len(self.nodes),
            total_relationships=len(self.relationships),
            node_labels=node_labels,
            relationship_types=relationship_types,
            metadata=self.metadata,
        )


def _is_complete_metadata(value: JsonValue) -> bool:
    return isinstance(value, dict) and all(
        key in value for key in GraphMetadata.__required_keys__
    )


def _filled_metadata(
    value: JsonValue, node_count: int, relationship_count: int
) -> GraphMetadata:
    given = value if isinstance(value, dict) else {}
    total_nodes = given.get(cs.KEY_TOTAL_NODES)
    total_relationships = given.get(cs.KEY_TOTAL_RELATIONSHIPS)
    exported_at = given.get(cs.KEY_EXPORTED_AT)
    return GraphMetadata(
        total_nodes=total_nodes if isinstance(total_nodes, int) else node_count,
        total_relationships=(
            total_relationships
            if isinstance(total_relationships, int)
            else relationship_count
        ),
        exported_at=(
            exported_at
            if isinstance(exported_at, str)
            else cs.GRAPH_EXPORTED_AT_UNKNOWN
        ),
    )


def load_graph(file_path: str) -> GraphLoader:
    loader = GraphLoader(file_path)
    loader.load()
    return loader
