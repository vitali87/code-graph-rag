from .types_defs import (
    NODE_SCHEMAS,
    RELATIONSHIP_SCHEMAS,
    NodeSchema,
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


def build_graph_schema_text() -> str:
    return f"""{build_node_labels_section()}

{build_relationships_section()}"""


GRAPH_SCHEMA_DEFINITION = build_graph_schema_text()
