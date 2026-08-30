from .types_defs import (
    NODE_SCHEMAS,
    RELATIONSHIP_PROPERTY_SCHEMAS,
    RELATIONSHIP_SCHEMAS,
    NodeSchema,
    RelationshipPropertySchema,
    RelationshipSchema,
)


def _format_node_schema(schema: NodeSchema) -> str:
    return f"- {schema.label}: {schema.properties}"


def _format_relationship_schema(schema: RelationshipSchema) -> str:
    sources = "|".join(str(s) for s in schema.sources)
    targets = "|".join(str(t) for t in schema.targets)
    if len(schema.sources) > 1:
        sources = f"({sources})"
    if len(schema.targets) > 1:
        targets = f"({targets})"
    return f"- {sources} -[:{schema.rel_type}]-> {targets}"


def build_node_labels_section() -> str:
    lines = ["Node Labels and Their Key Properties:"]
    lines.extend(_format_node_schema(schema) for schema in NODE_SCHEMAS)
    return "\n".join(lines)


def build_relationships_section() -> str:
    lines = ["Relationships (source)-[REL_TYPE]->(target):"]
    lines.extend(_format_relationship_schema(schema) for schema in RELATIONSHIP_SCHEMAS)
    return "\n".join(lines)


def _format_relationship_property_schema(schema: RelationshipPropertySchema) -> str:
    types = "|".join(str(t) for t in schema.rel_types)
    return f"- {types}: {schema.properties}"


def build_relationship_properties_section() -> str:
    lines = [
        "Relationship properties (one CALLS/REFERENCES/INSTANTIATES edge per "
        "call site, one IMPORTS edge per bound name; use DISTINCT for endpoints):"
    ]
    lines.extend(
        _format_relationship_property_schema(schema)
        for schema in RELATIONSHIP_PROPERTY_SCHEMAS
    )
    return "\n".join(lines)


def build_graph_schema_text() -> str:
    return f"""{build_node_labels_section()}

{build_relationships_section()}

{build_relationship_properties_section()}"""


GRAPH_SCHEMA_DEFINITION = build_graph_schema_text()
