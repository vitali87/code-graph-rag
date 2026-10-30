"""The graph schema text the query-generation prompt carries: one line per
declared node label, relationship and edge-property group, with a
multi-label end wrapped in parentheses so `|` cannot read as binding to
the arrow."""

from __future__ import annotations

import pytest

from codebase_rag import schema_builder
from codebase_rag.types_defs import (
    NODE_SCHEMAS,
    RELATIONSHIP_PROPERTY_SCHEMAS,
    RELATIONSHIP_SCHEMAS,
    NodeLabel,
    NodeSchema,
    RelationshipPropertySchema,
    RelationshipSchema,
    RelationshipType,
)


@pytest.fixture
def small_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        schema_builder,
        "NODE_SCHEMAS",
        (
            NodeSchema(NodeLabel.MODULE, "{qualified_name: string}"),
            NodeSchema(NodeLabel.FUNCTION, "{name: string, line: int?}"),
        ),
    )
    monkeypatch.setattr(
        schema_builder,
        "RELATIONSHIP_SCHEMAS",
        (
            RelationshipSchema(
                (NodeLabel.MODULE,), RelationshipType.DEFINES, (NodeLabel.FUNCTION,)
            ),
            RelationshipSchema(
                (NodeLabel.FUNCTION, NodeLabel.METHOD),
                RelationshipType.CALLS,
                (NodeLabel.FUNCTION,),
            ),
            RelationshipSchema(
                (NodeLabel.MODULE,),
                RelationshipType.IMPORTS,
                (NodeLabel.MODULE, NodeLabel.EXTERNAL_MODULE),
            ),
        ),
    )
    monkeypatch.setattr(
        schema_builder,
        "RELATIONSHIP_PROPERTY_SCHEMAS",
        (
            RelationshipPropertySchema(
                (RelationshipType.CALLS, RelationshipType.REFERENCES), "{line: int?}"
            ),
            RelationshipPropertySchema((RelationshipType.IMPORTS,), "{alias: string?}"),
        ),
    )


@pytest.mark.usefixtures("small_schema")
def test_node_labels_are_listed_with_their_properties_in_order() -> None:
    assert schema_builder.build_node_labels_section() == (
        "Node Labels and Their Key Properties:\n"
        "- Module: {qualified_name: string}\n"
        "- Function: {name: string, line: int?}"
    )


@pytest.mark.usefixtures("small_schema")
def test_only_a_multi_label_end_is_parenthesised() -> None:
    assert schema_builder.build_relationships_section() == (
        "Relationships (source)-[REL_TYPE]->(target):\n"
        "- Module -[:DEFINES]-> Function\n"
        "- (Function|Method) -[:CALLS]-> Function\n"
        "- Module -[:IMPORTS]-> (Module|ExternalModule)"
    )


@pytest.mark.usefixtures("small_schema")
def test_edge_properties_name_every_type_they_apply_to() -> None:
    section = schema_builder.build_relationship_properties_section()
    assert section.splitlines()[1:] == [
        "- CALLS|REFERENCES: {line: int?}",
        "- IMPORTS: {alias: string?}",
    ]


@pytest.mark.usefixtures("small_schema")
def test_the_full_text_is_the_three_sections_apart() -> None:
    assert schema_builder.build_graph_schema_text() == "\n\n".join(
        (
            schema_builder.build_node_labels_section(),
            schema_builder.build_relationships_section(),
            schema_builder.build_relationship_properties_section(),
        )
    )


def test_the_prompt_schema_has_a_line_for_every_declared_entry() -> None:
    lines = schema_builder.GRAPH_SCHEMA_DEFINITION.splitlines()
    entries = [line for line in lines if line.startswith("- ")]
    assert len(entries) == (
        len(NODE_SCHEMAS)
        + len(RELATIONSHIP_SCHEMAS)
        + len(RELATIONSHIP_PROPERTY_SCHEMAS)
    )
    for schema in RELATIONSHIP_SCHEMAS:
        assert any(f"-[:{schema.rel_type}]->" in line for line in entries), schema
