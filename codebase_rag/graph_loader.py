import json
from collections import Counter, defaultdict
from pathlib import Path

from loguru import logger

from . import constants as cs
from . import exceptions as ex
from . import logs as ls
from .models import GraphNode, GraphRelationship
from .types_defs import GraphData, GraphMetadata, GraphSummary, PropertyValue


class GraphLoader:
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
            self._data = json.load(f)

        if self._data is None:
            raise RuntimeError(ex.FAILED_TO_LOAD_DATA)

        self._nodes = []
        for node_data in self._data[cs.KEY_NODES]:
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
        for rel_data in self._data[cs.KEY_RELATIONSHIPS]:
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
    def nodes(self) -> list[GraphNode]:
        self._ensure_loaded()
        assert self._nodes is not None, ex.NODES_NOT_LOADED
        return self._nodes

    @property
    def relationships(self) -> list[GraphRelationship]:
        self._ensure_loaded()
        assert self._relationships is not None, ex.RELATIONSHIPS_NOT_LOADED
        return self._relationships

    @property
    def metadata(self) -> GraphMetadata:
        self._ensure_loaded()
        assert self._data is not None, ex.DATA_NOT_LOADED
        return self._data[cs.KEY_METADATA]

    def find_nodes_by_label(self, label: str) -> list[GraphNode]:
        self._ensure_loaded()
        return self._nodes_by_label.get(label, [])

    def find_node_by_property(
        self, property_name: str, value: PropertyValue
    ) -> list[GraphNode]:
        self._ensure_loaded()
        self._build_property_index(property_name)
        return self._property_indexes[property_name].get(value, [])

    def get_node_by_id(self, node_id: int) -> GraphNode | None:
        self._ensure_loaded()
        return self._nodes_by_id.get(node_id)

    def get_relationships_for_node(self, node_id: int) -> list[GraphRelationship]:
        return self.get_outgoing_relationships(
            node_id
        ) + self.get_incoming_relationships(node_id)

    def get_outgoing_relationships(self, node_id: int) -> list[GraphRelationship]:
        self._ensure_loaded()
        return self._outgoing_rels.get(node_id, [])

    def get_incoming_relationships(self, node_id: int) -> list[GraphRelationship]:
        self._ensure_loaded()
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


def load_graph(file_path: str) -> GraphLoader:
    loader = GraphLoader(file_path)
    loader.load()
    return loader
