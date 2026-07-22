from .constants import CYPHER_DEFAULT_LIMIT, NodeLabel, RelationshipType

CYPHER_DELETE_ALL = "MATCH (n) DETACH DELETE n;"

# Graph structural integrity audit (issue #646). A zero-degree Project is a
# valid empty-repo graph, so the orphan scan exempts it.
CYPHER_AUDIT_ORPHANS = (
    "MATCH (n) WHERE NOT (n)--() AND NOT n:Project "
    "RETURN labels(n)[0] AS label, count(n) AS orphans"
)
CYPHER_AUDIT_LABELS = "MATCH (n) UNWIND labels(n) AS label RETURN DISTINCT label"
CYPHER_AUDIT_REL_TRIPLES = (
    "MATCH (a)-[r]->(b) "
    "RETURN DISTINCT labels(a)[0] AS src, type(r) AS rel, labels(b)[0] AS dst"
)
CYPHER_AUDIT_LABEL_PROPS = (
    "MATCH (n) UNWIND labels(n) AS label UNWIND keys(n) AS key "
    "RETURN DISTINCT label AS label, key AS key"
)
CYPHER_AUDIT_MISSING_REQUIRED = (
    "MATCH (n:{label}) WHERE {conditions} RETURN count(n) AS missing"
)
CYPHER_AUDIT_IS_NULL = "n.{prop} IS NULL"
CYPHER_AUDIT_OR = " OR "

CYPHER_LIST_PROJECTS = (
    "MATCH (p:Project) RETURN p.name AS name, p.root_path AS root_path ORDER BY p.name"
)

CYPHER_DELETE_PROJECT = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(container)
OPTIONAL MATCH (container)-[:DEFINES|DEFINES_METHOD*]->(defined)
DETACH DELETE p, container, defined
"""

CYPHER_SHOW_CONSTRAINTS = "SHOW CONSTRAINT INFO;"

# The superseded relative-path key merged same-layout projects onto shared
# Folder/File nodes (issue #897). A merged node cannot be split, so anything
# the containment walk reaches from more than one Project is purged; the
# next re-index rebuilds it with per-project identity.
CYPHER_PURGE_CROSS_PROJECT_STRUCTURE = (
    "MATCH (p:Project)"
    "-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(n) "
    "WHERE (n:Folder OR n:File) "
    "WITH n, count(DISTINCT p) AS owners "
    "WHERE owners > 1 "
    "DETACH DELETE n RETURN count(n) AS purged"
)

# Rows written before absolute_path existed can never match the current
# delete queries; they are unmanageable and must go with the migration.
CYPHER_PURGE_KEYLESS_STRUCTURE = (
    "MATCH (n) WHERE (n:Folder OR n:File) AND n.absolute_path IS NULL "
    "DETACH DELETE n RETURN count(n) AS purged"
)

CYPHER_EXAMPLE_DECORATED_FUNCTIONS = f"""MATCH (n:Function|Method)
WHERE ANY(d IN n.decorators WHERE toLower(d) IN ['flow', 'task'])
RETURN n.name AS name, n.qualified_name AS qualified_name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CONTENT_BY_PATH = f"""MATCH (n)
WHERE n.path IS NOT NULL AND n.path STARTS WITH 'workflows'
RETURN n.name AS name, n.path AS path, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_KEYWORD_SEARCH = f"""MATCH (n)
WHERE toLower(n.name) CONTAINS 'database' OR (n.qualified_name IS NOT NULL AND toLower(n.qualified_name) CONTAINS 'database')
RETURN n.name AS name, n.qualified_name AS qualified_name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_FIND_FILE = """MATCH (f:File) WHERE toLower(f.name) = 'readme.md' AND f.path = 'README.md'
RETURN f.path as path, f.name as name, labels(f) as type"""

CYPHER_EXAMPLE_README = f"""MATCH (f:File)
WHERE toLower(f.name) CONTAINS 'readme'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_PYTHON_FILES = f"""MATCH (f:File)
WHERE f.extension = '.py'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_TASKS = f"""MATCH (n:Function|Method)
WHERE 'task' IN n.decorators
RETURN n.qualified_name AS qualified_name, n.name AS name, labels(n) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_FILES_IN_FOLDER = f"""MATCH (f:File)
WHERE f.path STARTS WITH 'services'
RETURN f.path AS path, f.name AS name, labels(f) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_LIMIT_ONE = """MATCH (f:File) RETURN f.path as path, f.name as name, labels(f) as type LIMIT 1"""

CYPHER_EXAMPLE_PROJECT_SCOPED = f"""MATCH (c:Class)
WHERE c.qualified_name STARTS WITH 'myproject.'
RETURN c.name AS name, c.qualified_name AS qualified_name, labels(c) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CLASS_METHODS = f"""MATCH (c:Class)-[:DEFINES_METHOD]->(m:Method)
WHERE c.name = 'UserService'
RETURN c.name AS className, m.name AS methodName, m.qualified_name AS qualified_name, labels(m) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

# ast-grep findings (issue #413): Pattern/CodeSmell/SecurityIssue nodes hang
# off a Module via IMPLEMENTS_PATTERN/HAS_SMELL/HAS_VULNERABILITY. The finding
# node's name is the rule id; start_line locates the site.
CYPHER_EXAMPLE_FIND_PATTERN = f"""MATCH (m:Module)-[:IMPLEMENTS_PATTERN]->(p:Pattern)
WHERE p.name = 'singleton'
RETURN m.path AS path, p.name AS pattern, p.start_line AS line, p.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_SECURITY_ISSUES = f"""MATCH (m:Module)-[:HAS_VULNERABILITY]->(s:SecurityIssue)
RETURN m.path AS path, s.name AS rule, s.start_line AS line, s.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXAMPLE_CODE_SMELLS = f"""MATCH (m:Module)-[:HAS_SMELL]->(c:CodeSmell)
RETURN m.path AS path, c.name AS smell, c.start_line AS line, c.message AS message
LIMIT {CYPHER_DEFAULT_LIMIT}"""

CYPHER_EXPORT_NODES = """
MATCH (n)
RETURN id(n) as node_id, labels(n) as labels, properties(n) as properties
"""

CYPHER_EXPORT_RELATIONSHIPS = """
MATCH (a)-[r]->(b)
RETURN id(a) as from_id, id(b) as to_id, type(r) as type, properties(r) as properties
"""

CYPHER_RETURN_COUNT = "RETURN count(r) as created"
CYPHER_SET_PROPS_RETURN_COUNT = "SET r += row.props\nRETURN count(r) as created"

CYPHER_GET_FUNCTION_SOURCE_LOCATION = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE id(n) = $node_id
RETURN n.qualified_name AS qualified_name, n.start_line AS start_line,
       n.end_line AS end_line, m.path AS path, n.absolute_path AS absolute_path
"""

CYPHER_FIND_BY_QUALIFIED_NAME = """
MATCH (n) WHERE n.qualified_name = $qn
OPTIONAL MATCH (m:Module)-[*]-(n)
RETURN n.name AS name, n.start_line AS start, n.end_line AS end, m.path AS path,
       n.absolute_path AS absolute_path, n.docstring AS docstring
LIMIT 1
"""


CYPHER_STATS_NODE_COUNTS = """
MATCH (n)
RETURN labels(n) AS labels, count(*) AS count
ORDER BY count DESC
"""

CYPHER_STATS_RELATIONSHIP_COUNTS = """
MATCH ()-[r]->()
RETURN type(r) AS type, count(*) AS count
ORDER BY count DESC
"""


# Dead-code fetch queries. Reachability itself runs client-side in
# codebase_rag/dead_code.py: the previous single-query formulation expanded
# *BFS from every root, which is O(roots x graph) and hit memgraph's 600s
# query timeout on big projects (django: 31k roots, 101k CALLS edges). These
# two linear scans fetch the project's nodes and edges instead; the target
# of a relationship is deliberately unfiltered so INHERITS to an external
# base (typing.Protocol) and OVERRIDES of external methods stay visible.
_DEAD_CODE_NODE_LABELS = "|".join(
    (
        NodeLabel.FUNCTION.value,
        NodeLabel.METHOD.value,
        NodeLabel.CLASS.value,
        NodeLabel.MODULE.value,
    )
)
_DEAD_CODE_REL_TYPES = "|".join(
    (
        RelationshipType.CALLS.value,
        RelationshipType.REFERENCES.value,
        RelationshipType.INSTANTIATES.value,
        RelationshipType.INHERITS.value,
        RelationshipType.DEFINES.value,
        RelationshipType.DEFINES_METHOD.value,
        RelationshipType.OVERRIDES.value,
    )
)

CYPHER_DEAD_CODE_NODES = f"""MATCH (n:{_DEAD_CODE_NODE_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.name AS name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line,
       n.decorators AS decorators, n.is_exported AS is_exported,
       n.overrides_external AS overrides_external"""

CYPHER_DEAD_CODE_RELS = f"""MATCH (a:{_DEAD_CODE_NODE_LABELS})-[r:{_DEAD_CODE_REL_TYPES}]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
RETURN labels(a)[0] AS from_label, a.qualified_name AS from_qn,
       type(r) AS rel_type, labels(b)[0] AS to_label,
       b.qualified_name AS to_qn"""


def wrap_with_unwind(query: str) -> str:
    return f"UNWIND $batch AS row\n{query}"


def build_nodes_by_ids_query(node_ids: list[int]) -> str:
    placeholders = ", ".join(f"${i}" for i in range(len(node_ids)))
    return f"""
MATCH (n)
WHERE id(n) IN [{placeholders}]
RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
       labels(n) AS type, n.name AS name
ORDER BY n.qualified_name
"""


def build_constraint_query(label: str, prop: str) -> str:
    return f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"


def build_drop_constraint_query(label: str, prop: str) -> str:
    return f"DROP CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"


def build_index_query(label: str, prop: str) -> str:
    return f"CREATE INDEX ON :{label}({prop});"


def build_merge_node_query(label: str, id_key: str) -> str:
    return f"MERGE (n:{label} {{{id_key}: row.id}})\nSET n += row.props"


def build_merge_relationship_query(
    from_label: str,
    from_key: str,
    rel_type: str,
    to_label: str,
    to_key: str,
    has_props: bool = False,
    merge_key_props: tuple[str, ...] = (),
) -> str:
    # merge_key_props: properties that distinguish parallel edges between the
    # same node pair (e.g. FLOWS_TO's `via`). Including them in the MERGE
    # pattern keeps each variant as its own edge instead of collapsing them
    # into one (issue #722). Every row in the batch must carry these keys.
    key_map = ""
    if merge_key_props:
        key_map = " {" + ", ".join(f"{p}: row.props.{p}" for p in merge_key_props) + "}"
    query = (
        f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
        f"(b:{to_label} {{{to_key}: row.to_val}})\n"
        f"MERGE (a)-[r:{rel_type}{key_map}]->(b)\n"
    )
    query += CYPHER_SET_PROPS_RETURN_COUNT if has_props else CYPHER_RETURN_COUNT
    return query


def build_create_node_query(label: str, id_key: str) -> str:
    return f"CREATE (n:{label} {{{id_key}: row.id}})\nSET n += row.props"


def build_create_relationship_query(
    from_label: str,
    from_key: str,
    rel_type: str,
    to_label: str,
    to_key: str,
    has_props: bool = False,
) -> str:
    query = (
        f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
        f"(b:{to_label} {{{to_key}: row.to_val}})\n"
        f"CREATE (a)-[r:{rel_type}]->(b)\n"
    )
    query += CYPHER_SET_PROPS_RETURN_COUNT if has_props else CYPHER_RETURN_COUNT
    return query
