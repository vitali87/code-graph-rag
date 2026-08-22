"""Every shipped Cypher string must parse and return its declared columns.

The eval harness runs against an in-memory capturing ingestor and never
touches a database, so without this gate a query that no longer parses
reaches users unnoticed on either backend.
"""

from __future__ import annotations

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]

_FN = NodeLabel.FUNCTION.value
_MOD = NodeLabel.MODULE.value
_QN = "qualified_name"

# (query, params, expected column names). Params use values the seeded
# fixture graph actually contains so a shape assertion is meaningful
# rather than trivially empty.
CORPUS: list[tuple[str, dict[str, object], list[str]]] = [
    (cq.CYPHER_LIST_PROJECTS, {}, ["name", "root_path"]),
    (cq.CYPHER_AUDIT_ORPHANS, {}, ["label", "orphans"]),
    (cq.CYPHER_AUDIT_LABELS, {}, ["label"]),
    (cq.CYPHER_AUDIT_REL_TRIPLES, {}, ["src", "rel", "dst"]),
    (cq.CYPHER_AUDIT_LABEL_PROPS, {}, ["label", "key"]),
    (cq.CYPHER_EXPORT_NODES, {}, ["node_id", "labels", "properties"]),
    (
        cq.CYPHER_EXPORT_RELATIONSHIPS,
        {},
        ["from_id", "to_id", "type", "properties"],
    ),
    (cq.CYPHER_STATS_NODE_COUNTS, {}, ["labels", "count"]),
    (cq.CYPHER_STATS_RELATIONSHIP_COUNTS, {}, ["type", "count"]),
    (cq.CYPHER_ANY_SHARED_STRUCTURE, {}, ["damaged"]),
    (cq.CYPHER_ANY_KEYLESS_STRUCTURE, {}, ["damaged"]),
    (
        cq.CYPHER_FIND_BY_QUALIFIED_NAME,
        {"qn": "alpha.mod.fn"},
        ["name", "start", "end", "path", "absolute_path", "docstring"],
    ),
    (
        cq.CYPHER_DEAD_CODE_NODES,
        {"project_prefix": "alpha."},
        [
            "label",
            "qualified_name",
            "name",
            "path",
            "start_line",
            "end_line",
            "decorators",
            "is_exported",
            "overrides_external",
            "rust_cfg_test_mods",
            "rust_ungated_mods",
        ],
    ),
    (
        cq.CYPHER_DEAD_CODE_RELS,
        {"project_prefix": "alpha."},
        ["from_label", "from_qn", "rel_type", "to_label", "to_qn"],
    ),
    (cq.CYPHER_EXAMPLE_DECORATED_FUNCTIONS, {}, ["name", "qualified_name", "type"]),
    (cq.CYPHER_EXAMPLE_CONTENT_BY_PATH, {}, ["name", "path", "type"]),
    (cq.CYPHER_EXAMPLE_KEYWORD_SEARCH, {}, ["name", "qualified_name", "type"]),
    (cq.CYPHER_EXAMPLE_FIND_FILE, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_README, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_PYTHON_FILES, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_TASKS, {}, ["qualified_name", "name", "type"]),
    (cq.CYPHER_EXAMPLE_FILES_IN_FOLDER, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_LIMIT_ONE, {}, ["path", "name", "type"]),
    (cq.CYPHER_EXAMPLE_PROJECT_SCOPED, {}, ["name", "qualified_name", "type"]),
    (
        cq.CYPHER_EXAMPLE_CLASS_METHODS,
        {},
        ["className", "methodName", "qualified_name", "type"],
    ),
    (cq.CYPHER_EXAMPLE_FIND_PATTERN, {}, ["path", "pattern", "line", "message"]),
    (cq.CYPHER_EXAMPLE_SECURITY_ISSUES, {}, ["path", "rule", "line", "message"]),
    (cq.CYPHER_EXAMPLE_CODE_SMELLS, {}, ["path", "smell", "line", "message"]),
]


@pytest.fixture
def seeded(graph_ingestor: GraphIngestor) -> GraphIngestor:
    graph_ingestor.ensure_node_batch(
        NodeLabel.PROJECT.value, {"name": "alpha", "root_path": "/tmp/alpha"}
    )
    graph_ingestor.ensure_node_batch(_MOD, {_QN: "alpha.mod", "path": "alpha/mod.py"})
    graph_ingestor.ensure_node_batch(
        _FN,
        {
            _QN: "alpha.mod.fn",
            "name": "fn",
            "path": "alpha/mod.py",
            "start_line": 1,
            "end_line": 5,
            "decorators": ["task"],
        },
    )
    graph_ingestor.ensure_node_batch(
        NodeLabel.FILE.value,
        {
            "absolute_path": "/tmp/alpha/README.md",
            "path": "README.md",
            "name": "README.md",
            "extension": ".md",
        },
    )
    graph_ingestor.flush_nodes()
    graph_ingestor.ensure_relationship_batch(
        (NodeLabel.PROJECT.value, "name", "alpha"),
        RelationshipType.CONTAINS_MODULE.value,
        (_MOD, _QN, "alpha.mod"),
    )
    graph_ingestor.ensure_relationship_batch(
        (_MOD, _QN, "alpha.mod"),
        RelationshipType.DEFINES.value,
        (_FN, _QN, "alpha.mod.fn"),
    )
    graph_ingestor.flush_all()
    return graph_ingestor


def _corpus_id(query: str, index: int) -> str:
    # A bare first-line slice collides for several corpus entries (multiple
    # queries start with "MATCH (n)"/"MATCH (f:File)"), and pytest errors on
    # duplicate parametrize ids rather than silently disambiguating them.
    # Prefixing with the corpus index keeps ids unique while the slice stays
    # for human readability.
    first_line = query.strip().splitlines()[0][:48]
    return f"{index}-{first_line}"


@pytest.mark.parametrize(
    ("query", "params", "columns"),
    CORPUS,
    ids=[_corpus_id(q, i) for i, (q, _, _) in enumerate(CORPUS)],
)
def test_shipped_query_parses_and_returns_declared_columns(
    seeded: GraphIngestor,
    query: str,
    params: dict[str, object],
    columns: list[str],
) -> None:
    rows = seeded.fetch_all(query, params)  # must not raise
    if rows:
        assert set(rows[0].keys()) == set(columns)
