from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import (
    _NODE_LABEL_UNIQUE_KEYS,
    KEY_NAME,
    KEY_PATH,
    KEY_QUALIFIED_NAME,
    NODE_UNIQUE_CONSTRAINTS,
    NodeLabel,
    RelationshipType,
    UniqueKeyType,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import NodeType


class TestNodeLabelCoverage:
    def test_all_node_labels_have_unique_key_mapping(self) -> None:
        missing = set(NodeLabel) - set(_NODE_LABEL_UNIQUE_KEYS.keys())

        assert not missing, (
            f"NodeLabel(s) missing from _NODE_LABEL_UNIQUE_KEYS: {missing}. "
            "Every NodeLabel MUST have a unique key defined."
        )

    def test_all_node_labels_in_constraints(self) -> None:
        missing = {label.value for label in NodeLabel} - set(
            NODE_UNIQUE_CONSTRAINTS.keys()
        )

        assert not missing, (
            f"NodeLabel value(s) missing from NODE_UNIQUE_CONSTRAINTS: {missing}. "
            "This would cause nodes to be silently dropped during flush."
        )

    def test_all_node_types_in_constraints(self) -> None:
        missing = {node_type.value for node_type in NodeType} - set(
            NODE_UNIQUE_CONSTRAINTS.keys()
        )

        assert not missing, (
            f"NodeType value(s) missing from NODE_UNIQUE_CONSTRAINTS: {missing}. "
            "This would cause nodes to be silently dropped during flush."
        )

    def test_node_unique_constraints_derived_from_single_source(self) -> None:
        expected = {
            label.value: key.value for label, key in _NODE_LABEL_UNIQUE_KEYS.items()
        }

        assert NODE_UNIQUE_CONSTRAINTS == expected, (
            "NODE_UNIQUE_CONSTRAINTS must be derived from _NODE_LABEL_UNIQUE_KEYS. "
            "Do not maintain NODE_UNIQUE_CONSTRAINTS manually."
        )

    def test_unique_key_types_are_valid(self) -> None:
        valid_keys = set(UniqueKeyType)

        for label, key in _NODE_LABEL_UNIQUE_KEYS.items():
            assert key in valid_keys, (
                f"Invalid unique key type {key} for {label}. "
                f"Must be one of {valid_keys}."
            )


class TestNodeLabelConstraintConsistency:
    @pytest.mark.parametrize("label", list(NodeLabel))
    def test_each_node_label_has_constraint(self, label: NodeLabel) -> None:
        assert label.value in NODE_UNIQUE_CONSTRAINTS, (
            f"NodeLabel.{label.name} ({label.value}) missing from NODE_UNIQUE_CONSTRAINTS. "
            "This would cause nodes of this type to be silently dropped."
        )

    @pytest.mark.parametrize("node_type", list(NodeType))
    def test_each_node_type_has_constraint(self, node_type: NodeType) -> None:
        assert node_type.value in NODE_UNIQUE_CONSTRAINTS, (
            f"NodeType.{node_type.name} ({node_type.value}) missing from NODE_UNIQUE_CONSTRAINTS. "
            "This would cause nodes of this type to be silently dropped."
        )


class TestFlushNodesForAllNodeLabels:
    @pytest.mark.parametrize("label", list(NodeLabel))
    def test_each_node_label_can_be_flushed(self, label: NodeLabel) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        unique_key = NODE_UNIQUE_CONSTRAINTS[label.value]
        node_props = {unique_key: f"test_{label.value}_id", KEY_NAME: "test"}

        ingestor.node_buffer.append((label.value, node_props))
        ingestor.flush_nodes()

        mock_cursor.execute.assert_called_once()
        assert ingestor.node_buffer == []

    @pytest.mark.parametrize("node_type", list(NodeType))
    def test_each_node_type_can_be_flushed(self, node_type: NodeType) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        unique_key = NODE_UNIQUE_CONSTRAINTS[node_type.value]
        node_props = {unique_key: f"test_{node_type.value}_id", KEY_NAME: "test"}

        ingestor.node_buffer.append((node_type.value, node_props))
        ingestor.flush_nodes()

        mock_cursor.execute.assert_called_once()
        assert ingestor.node_buffer == []


class TestFlushRelationshipsForAllTypes:
    @pytest.mark.parametrize("rel_type", list(RelationshipType))
    def test_each_relationship_type_can_be_flushed(
        self, rel_type: RelationshipType
    ) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [(1,)]

        col = MagicMock()
        col.name = "created"
        mock_cursor.description = [col]

        ingestor.conn = mock_conn

        ingestor.relationship_buffer.append(
            (
                (NodeLabel.MODULE.value, KEY_QUALIFIED_NAME, "module.test"),
                rel_type.value,
                (NodeLabel.FUNCTION.value, KEY_QUALIFIED_NAME, "module.test.func"),
                None,
            )
        )
        ingestor.flush_relationships()

        mock_cursor.execute.assert_called_once()
        assert ingestor.relationship_buffer == []


class TestUniqueKeyPropertyNames:
    def test_name_unique_key_uses_correct_property(self) -> None:
        for label in NodeLabel:
            key = _NODE_LABEL_UNIQUE_KEYS[label]
            if key == UniqueKeyType.NAME:
                assert NODE_UNIQUE_CONSTRAINTS[label.value] == KEY_NAME

    def test_path_unique_key_uses_correct_property(self) -> None:
        for label in NodeLabel:
            key = _NODE_LABEL_UNIQUE_KEYS[label]
            if key == UniqueKeyType.PATH:
                assert NODE_UNIQUE_CONSTRAINTS[label.value] == KEY_PATH

    def test_qualified_name_unique_key_uses_correct_property(self) -> None:
        for label in NodeLabel:
            key = _NODE_LABEL_UNIQUE_KEYS[label]
            if key == UniqueKeyType.QUALIFIED_NAME:
                assert NODE_UNIQUE_CONSTRAINTS[label.value] == KEY_QUALIFIED_NAME


class TestNodeLabelEnumCompleteness:
    def test_node_label_count_matches_constraints_count(self) -> None:
        assert len(NodeLabel) == len(NODE_UNIQUE_CONSTRAINTS), (
            f"NodeLabel has {len(NodeLabel)} values but "
            f"NODE_UNIQUE_CONSTRAINTS has {len(NODE_UNIQUE_CONSTRAINTS)} entries. "
            "These must match."
        )

    def test_node_type_is_subset_of_node_label(self) -> None:
        node_type_values = {t.value for t in NodeType}
        node_label_values = {label.value for label in NodeLabel}

        extra_in_node_type = node_type_values - node_label_values

        assert not extra_in_node_type, (
            f"NodeType has values not in NodeLabel: {extra_in_node_type}. "
            "NodeType must be a subset of NodeLabel."
        )


class TestRelationshipTypeCompleteness:
    def test_relationship_types_are_uppercase(self) -> None:
        for rel_type in RelationshipType:
            assert rel_type.value == rel_type.value.upper(), (
                f"RelationshipType.{rel_type.name} has value '{rel_type.value}' "
                "which is not uppercase. Relationship types must be uppercase."
            )

    def test_relationship_type_values_match_names(self) -> None:
        for rel_type in RelationshipType:
            assert rel_type.name == rel_type.value, (
                f"RelationshipType.{rel_type.name} has mismatched value '{rel_type.value}'. "
                "Name and value should match for relationship types."
            )


class TestNodeBufferFlushWithMissingKey:
    @pytest.mark.parametrize("label", list(NodeLabel))
    def test_node_without_unique_key_is_skipped_not_crashed(
        self, label: NodeLabel
    ) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10)
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        ingestor.conn = mock_conn

        node_props = {KEY_NAME: "test_without_unique_key"}

        ingestor.node_buffer.append((label.value, node_props))
        ingestor.flush_nodes()

        assert ingestor.node_buffer == []


class TestEnsureConstraintsForAllLabels:
    def test_ensure_constraints_creates_all_constraints(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        executed_queries: list[str] = []

        def capture_query(query: str) -> None:
            executed_queries.append(query)

        with patch.object(ingestor, "_execute_query", side_effect=capture_query):
            ingestor.ensure_constraints()

        for label in NodeLabel:
            prop = NODE_UNIQUE_CONSTRAINTS[label.value]
            expected = (
                f"CREATE CONSTRAINT ON (n:{label.value}) ASSERT n.{prop} IS UNIQUE;"
            )
            assert expected in executed_queries, (
                f"Missing constraint for {label.value}. Expected query: {expected}"
            )

    def test_ensure_constraints_creates_all_indexes(self) -> None:
        ingestor = MemgraphIngestor(host="localhost", port=7687)
        executed_queries: list[str] = []

        def capture_query(query: str) -> None:
            executed_queries.append(query)

        with patch.object(ingestor, "_execute_query", side_effect=capture_query):
            ingestor.ensure_constraints()

        for label in NodeLabel:
            prop = NODE_UNIQUE_CONSTRAINTS[label.value]
            expected_index = f"CREATE INDEX ON :{label.value}({prop});"
            assert expected_index in executed_queries, (
                f"Missing index for {label.value}. Expected query: {expected_index}. "
                "Indexes are required for efficient MERGE operations in Memgraph."
            )


class TestImportTimeValidation:
    def test_import_time_validation_catches_missing_keys(self) -> None:
        code = """
from enum import StrEnum

class UniqueKeyType(StrEnum):
    NAME = "name"
    QUALIFIED_NAME = "qualified_name"

class NodeLabel(StrEnum):
    PROJECT = "Project"
    NEW_MISSING_LABEL = "NewMissingLabel"

_NODE_LABEL_UNIQUE_KEYS = {
    NodeLabel.PROJECT: UniqueKeyType.NAME,
}

_missing_keys = set(NodeLabel) - set(_NODE_LABEL_UNIQUE_KEYS.keys())
if _missing_keys:
    raise RuntimeError(
        f"NodeLabel(s) missing from _NODE_LABEL_UNIQUE_KEYS: {_missing_keys}"
    )
"""
        with pytest.raises(RuntimeError, match="missing from _NODE_LABEL_UNIQUE_KEYS"):
            exec(code)
