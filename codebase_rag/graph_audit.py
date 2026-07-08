# (H) Structural integrity audit of a generated knowledge graph (issue #646).
# (H) Validates recorded node and relationship batches against the documented
# (H) schema (types_defs.NODE_SCHEMAS / RELATIONSHIP_SCHEMAS): orphan nodes,
# (H) required-property completeness, and undocumented labels, properties, and
# (H) relationship endpoint triples.
from collections.abc import Callable, Sequence

from . import constants as cs
from . import cypher_queries as cq
from .types_defs import (
    NODE_SCHEMAS,
    RELATIONSHIP_SCHEMAS,
    AuditViolation,
    GraphNodeRecord,
    GraphRelRecord,
    PropertyValue,
    ResultRow,
)


def documented_node_properties() -> dict[str, dict[str, bool]]:
    """Parse NODE_SCHEMAS property strings into {label: {prop: required}}."""
    parsed: dict[str, dict[str, bool]] = {}
    for schema in NODE_SCHEMAS:
        props: dict[str, bool] = {}
        for entry in schema.properties.strip(cs.SCHEMA_PROPS_BRACES).split(
            cs.SEPARATOR_COMMA
        ):
            # (H) A trailing comma or stray whitespace in a schema string must
            # (H) not register an empty-named required property.
            if not (entry := entry.strip()):
                continue
            name, _, type_part = entry.partition(cs.SEPARATOR_COLON)
            props[name.strip()] = not type_part.strip().endswith(
                cs.SCHEMA_OPTIONAL_SUFFIX
            )
        parsed[schema.label.value] = props
    return parsed


def documented_relationship_triples() -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (source.value, schema.rel_type.value, target.value)
        for schema in RELATIONSHIP_SCHEMAS
        for source in schema.sources
        for target in schema.targets
    )


def _node_key(node: GraphNodeRecord) -> PropertyValue:
    if key_prop := cs.NODE_UNIQUE_CONSTRAINTS.get(node.label):
        return node.properties.get(key_prop)
    return None


def merge_node_records(
    nodes: Sequence[GraphNodeRecord],
) -> list[GraphNodeRecord]:
    """Collapse repeated ensures of one node, merging properties.

    Mirrors the ingestor's MERGE ... SET n += props semantics: the stored
    node carries the union of all batched property dicts.
    """
    merged: dict[tuple[str, PropertyValue], GraphNodeRecord] = {}
    for node in nodes:
        identity = (node.label, _node_key(node))
        if existing := merged.get(identity):
            existing.properties.update(node.properties)
        else:
            merged[identity] = GraphNodeRecord(node.label, dict(node.properties))
    return list(merged.values())


def _node_identities(
    nodes: Sequence[GraphNodeRecord],
) -> set[tuple[str, PropertyValue]]:
    return {(node.label, _node_key(node)) for node in merge_node_records(nodes)}


def find_orphans(
    nodes: Sequence[GraphNodeRecord], relationships: Sequence[GraphRelRecord]
) -> list[GraphNodeRecord]:
    identities = _node_identities(nodes)
    connected: set[tuple[str, PropertyValue]] = set()
    for rel in relationships:
        # (H) The database MERGEs a relationship by MATCHing both endpoints, so
        # (H) an edge with a nonexistent endpoint is silently dropped and must
        # (H) not count as connectivity for the endpoint that does exist.
        endpoints = [(label, value) for label, _, value in (rel.from_spec, rel.to_spec)]
        if all(endpoint in identities for endpoint in endpoints):
            connected.update(endpoints)
    return [
        node
        for node in merge_node_records(nodes)
        # (H) A repo with no indexable content is just its Project root, so a
        # (H) zero-degree Project is valid rather than a construction failure.
        if node.label != cs.NodeLabel.PROJECT.value
        and (node.label, _node_key(node)) not in connected
    ]


def find_dangling_relationships(
    nodes: Sequence[GraphNodeRecord], relationships: Sequence[GraphRelRecord]
) -> list[AuditViolation]:
    identities = _node_identities(nodes)
    violations: list[AuditViolation] = []
    seen: set[tuple[str, PropertyValue, str, str, PropertyValue]] = set()
    for rel in relationships:
        from_label, _, from_key = rel.from_spec
        to_label, _, to_key = rel.to_spec
        if (from_label, from_key) in identities and (to_label, to_key) in identities:
            continue
        signature = (from_label, from_key, rel.rel_type, to_label, to_key)
        if signature in seen:
            continue
        seen.add(signature)
        violations.append(
            AuditViolation(
                cs.AuditCheck.DANGLING_RELATIONSHIP,
                cs.AUDIT_DETAIL_DANGLING.format(
                    from_label=from_label,
                    from_key=from_key,
                    rel_type=rel.rel_type,
                    to_label=to_label,
                    to_key=to_key,
                ),
            )
        )
    return violations


def find_property_violations(
    nodes: Sequence[GraphNodeRecord],
) -> list[AuditViolation]:
    documented = documented_node_properties()
    violations: list[AuditViolation] = []
    for node in merge_node_records(nodes):
        schema_props = documented.get(node.label)
        if schema_props is None:
            violations.append(
                AuditViolation(
                    cs.AuditCheck.UNDOCUMENTED_LABEL,
                    cs.AUDIT_DETAIL_UNDOCUMENTED_LABEL.format(label=node.label),
                )
            )
            continue
        key = _node_key(node)
        for prop in node.properties:
            if prop not in schema_props:
                violations.append(
                    AuditViolation(
                        cs.AuditCheck.UNDOCUMENTED_PROPERTY,
                        cs.AUDIT_DETAIL_UNDOCUMENTED_PROPERTY.format(
                            label=node.label, key=key, prop=prop
                        ),
                    )
                )
        for prop, required in schema_props.items():
            if required and node.properties.get(prop) is None:
                violations.append(
                    AuditViolation(
                        cs.AuditCheck.MISSING_REQUIRED_PROPERTY,
                        cs.AUDIT_DETAIL_MISSING_REQUIRED.format(
                            label=node.label, key=key, prop=prop
                        ),
                    )
                )
    return violations


def find_relationship_violations(
    relationships: Sequence[GraphRelRecord],
) -> list[AuditViolation]:
    documented = documented_relationship_triples()
    violations: list[AuditViolation] = []
    seen: set[tuple[str, str, str]] = set()
    for rel in relationships:
        triple = (rel.from_spec[0], rel.rel_type, rel.to_spec[0])
        if triple in documented or triple in seen:
            continue
        seen.add(triple)
        violations.append(
            AuditViolation(
                cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP,
                cs.AUDIT_DETAIL_UNDOCUMENTED_RELATIONSHIP.format(
                    from_label=triple[0], rel_type=triple[1], to_label=triple[2]
                ),
            )
        )
    return violations


def build_missing_required_query(label: str, required_props: Sequence[str]) -> str:
    conditions = cq.CYPHER_AUDIT_OR.join(
        cq.CYPHER_AUDIT_IS_NULL.format(prop=prop) for prop in required_props
    )
    return cq.CYPHER_AUDIT_MISSING_REQUIRED.format(label=label, conditions=conditions)


def collect_live_violations(
    fetch_all: Callable[[str], Sequence[ResultRow]],
) -> list[AuditViolation]:
    """Run the structural audit against a live graph via Cypher (doctor)."""
    violations: list[AuditViolation] = []
    for row in fetch_all(cq.CYPHER_AUDIT_ORPHANS):
        violations.append(
            AuditViolation(
                cs.AuditCheck.ORPHAN_NODE,
                cs.AUDIT_DETAIL_ORPHAN_COUNT.format(
                    count=row["orphans"], label=row["label"]
                ),
            )
        )
    documented_props = documented_node_properties()
    for row in fetch_all(cq.CYPHER_AUDIT_LABELS):
        if row["label"] not in documented_props:
            violations.append(
                AuditViolation(
                    cs.AuditCheck.UNDOCUMENTED_LABEL,
                    cs.AUDIT_DETAIL_UNDOCUMENTED_LABEL.format(label=row["label"]),
                )
            )
    documented_triples = documented_relationship_triples()
    for row in fetch_all(cq.CYPHER_AUDIT_REL_TRIPLES):
        if (row["src"], row["rel"], row["dst"]) not in documented_triples:
            violations.append(
                AuditViolation(
                    cs.AuditCheck.UNDOCUMENTED_RELATIONSHIP,
                    cs.AUDIT_DETAIL_UNDOCUMENTED_RELATIONSHIP.format(
                        from_label=row["src"],
                        rel_type=row["rel"],
                        to_label=row["dst"],
                    ),
                )
            )
    for row in fetch_all(cq.CYPHER_AUDIT_LABEL_PROPS):
        # (H) Unknown labels are already reported above; only grade documented ones.
        if (schema_props := documented_props.get(str(row["label"]))) and str(
            row["key"]
        ) not in schema_props:
            violations.append(
                AuditViolation(
                    cs.AuditCheck.UNDOCUMENTED_PROPERTY,
                    cs.AUDIT_DETAIL_UNDOCUMENTED_PROPERTY_LIVE.format(
                        label=row["label"], prop=row["key"]
                    ),
                )
            )
    for label, schema_props in documented_props.items():
        required = [prop for prop, is_required in schema_props.items() if is_required]
        # (H) An all-optional schema would render an empty WHERE clause, which
        # (H) is a Cypher syntax error.
        if not required:
            continue
        rows = fetch_all(build_missing_required_query(label, required))
        if rows and (count := rows[0]["missing"]):
            violations.append(
                AuditViolation(
                    cs.AuditCheck.MISSING_REQUIRED_PROPERTY,
                    cs.AUDIT_DETAIL_MISSING_REQUIRED_LIVE.format(
                        count=count, label=label
                    ),
                )
            )
    return violations


def collect_violations(
    nodes: Sequence[GraphNodeRecord], relationships: Sequence[GraphRelRecord]
) -> list[AuditViolation]:
    violations = [
        AuditViolation(
            cs.AuditCheck.ORPHAN_NODE,
            cs.AUDIT_DETAIL_ORPHAN.format(label=node.label, key=_node_key(node)),
        )
        for node in find_orphans(nodes, relationships)
    ]
    violations.extend(find_property_violations(nodes))
    violations.extend(find_relationship_violations(relationships))
    violations.extend(find_dangling_relationships(nodes, relationships))
    return violations
