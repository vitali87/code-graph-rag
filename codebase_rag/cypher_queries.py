from .constants import CYPHER_DEFAULT_LIMIT, NodeLabel, RelationshipType

CYPHER_DELETE_ALL = "MATCH (n) DETACH DELETE n;"

# Graph structural integrity audit (issue #646). A zero-degree Project is a
# valid empty-repo graph, so the orphan scan exempts it. OPTIONAL MATCH +
# count instead of `WHERE NOT (n)--()`: Memgraph 3.x rejects pattern
# expressions inside WHERE, and this form is accepted by both 2.x and 3.x
# (issue #1257).
CYPHER_AUDIT_ORPHANS = (
    "MATCH (n) WHERE NOT n:Project "
    "OPTIONAL MATCH (n)--(x) "
    "WITH n, count(x) AS degree "
    "WHERE degree = 0 "
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
# The repository a graph project was indexed from (issue #1523): source is
# read from disk only when that root IS the local repository, never on the
# strength of a matching project name alone.
CYPHER_PROJECT_ROOT_PATH = (
    "MATCH (p:Project {name: $project_name}) RETURN p.root_path AS root_path"
)

CYPHER_DELETE_PROJECT = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE|CONTAINS_SECTION*]->(container)
OPTIONAL MATCH (container)-[:DEFINES|DEFINES_METHOD*]->(defined)
DETACH DELETE p, container, defined
"""

CYPHER_SHOW_CONSTRAINTS = "SHOW CONSTRAINT INFO;"

# Damage detectors for the issue #897 migration. Sharing always leaves a
# single-hop signature: the topmost merged node has containment parents in
# two projects (Project roots are never merged, so the parents are distinct
# nodes). Keyless rows match the second purge's predicate directly.
CYPHER_ANY_SHARED_STRUCTURE = (
    "MATCH (parent)-[:CONTAINS_FOLDER|CONTAINS_FILE]->(n) "
    "WHERE (n:Folder OR n:File) "
    "WITH n, count(parent) AS parents "
    "WHERE parents > 1 "
    "RETURN 1 AS damaged LIMIT 1"
)

CYPHER_ANY_KEYLESS_STRUCTURE = (
    "MATCH (n) WHERE (n:Folder OR n:File) AND n.absolute_path IS NULL "
    "RETURN 1 AS damaged LIMIT 1"
)

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

CYPHER_EXAMPLE_FUNCTION_CALLERS = f"""MATCH (caller)-[r:CALLS]->(callee:Function|Method)
WHERE callee.qualified_name ENDS WITH '.process_payment'
RETURN caller.qualified_name AS caller_qualified_name, caller.path AS path,
       callee.qualified_name AS callee_qualified_name,
       type(r) AS relationship, labels(caller) AS caller_type
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


# Trace-ingestion fetches: every callable (plus Module, for module-level
# callers) of one project, and the statically discovered CALLS pairs so
# runtime-only edges can be flagged as static_missed. Edges a previous trace
# ingestion created (static_missed = true) are excluded, otherwise
# re-ingesting a trace would reclassify its own runtime-only edges as
# statically confirmed.
CYPHER_TRACE_CALLABLES = """
MATCH (n)
WHERE (n:Function OR n:Method OR n:Module)
  AND n.qualified_name STARTS WITH $prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line
"""

CYPHER_TRACE_EXISTING_CALLS = """
MATCH (a)-[r:CALLS]->(b)
WHERE a.qualified_name STARTS WITH $prefix
  AND b.qualified_name STARTS WITH $prefix
  AND coalesce(r.static_missed, false) = false
RETURN a.qualified_name AS from_qn, b.qualified_name AS to_qn
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
# base (typing.Protocol), OVERRIDES of external methods, and IMPLEMENTS of an
# external interface (NestJS `...OptionsFactory`) stay visible.
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
        RelationshipType.IMPLEMENTS.value,
    )
)

CYPHER_DEAD_CODE_NODES = f"""MATCH (n:{_DEAD_CODE_NODE_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.name AS name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line,
       n.decorators AS decorators, n.is_exported AS is_exported,
       n.overrides_external AS overrides_external,
       n.rust_cfg_test_mods AS rust_cfg_test_mods,
       n.rust_ungated_mods AS rust_ungated_mods"""

CYPHER_DEAD_CODE_RELS = f"""MATCH (a:{_DEAD_CODE_NODE_LABELS})-[r:{_DEAD_CODE_REL_TYPES}]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
RETURN labels(a)[0] AS from_label, a.qualified_name AS from_qn,
       type(r) AS rel_type, labels(b)[0] AS to_label,
       b.qualified_name AS to_qn, r.resolution AS resolution"""


# Duplicate-detection fetch. Grouping and overlap scoring run client-side in
# codebase_rag/duplicates.py (same reasoning as dead code: keep memgraph
# queries linear). ast-grep-tier symbols carry no fingerprint and are
# excluded here; their count is reported separately as "not analyzed".
_DUPLICATES_NODE_LABELS = "|".join((NodeLabel.FUNCTION.value, NodeLabel.METHOD.value))

CYPHER_DUPLICATE_FINGERPRINTS = f"""MATCH (n:{_DUPLICATES_NODE_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND n.ast_fingerprint IS NOT NULL
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name,
       n.name AS name, n.path AS path,
       n.start_line AS start_line, n.start_col AS start_col,
       n.end_line AS end_line,
       n.ast_fingerprint AS ast_fingerprint,
       n.ast_fingerprint_nodes AS ast_fingerprint_nodes,
       n.ast_branch_fingerprints AS ast_branch_fingerprints"""

CYPHER_DUPLICATE_SKIPPED_COUNT = f"""MATCH (n:{_DUPLICATES_NODE_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND n.ast_fingerprint IS NULL
RETURN count(n) AS skipped"""


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


# Deterministic graph queries for agents (issue #1523). All project-scoped
# through $project_prefix; walks of depth > 1 run client-side in
# codebase_rag/graph_query.py so each query stays linear.
_GRAPH_DEFINITION_LABELS = "|".join(
    (
        NodeLabel.FUNCTION.value,
        NodeLabel.METHOD.value,
        NodeLabel.CLASS.value,
        NodeLabel.INTERFACE.value,
        NodeLabel.ENUM.value,
        NodeLabel.TYPE.value,
        NodeLabel.UNION.value,
        NodeLabel.MODULE.value,
    )
)
CYPHER_GRAPH_RESOLVE_NAME = f"""MATCH (n:{_GRAPH_DEFINITION_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND (n.qualified_name = $qn OR n.qualified_name ENDS WITH $suffix OR n.name = $name)
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line"""
CYPHER_GRAPH_RESOLVE_LOCATION = f"""MATCH (n:{_GRAPH_DEFINITION_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix AND n.path = $path
  AND n.start_line <= $line AND $line <= n.end_line
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line"""
CYPHER_GRAPH_DEFINITION = f"""MATCH (n:{_GRAPH_DEFINITION_LABELS})
WHERE n.qualified_name = $qn AND n.qualified_name STARTS WITH $project_prefix
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.docstring AS docstring
LIMIT 1"""
# One row per call SITE (edges carry the site from issue #1522).
CYPHER_GRAPH_CALLERS = """MATCH (caller)-[r:CALLS]->(callee)
WHERE callee.qualified_name = $qn AND caller.qualified_name STARTS WITH $project_prefix
RETURN labels(caller)[0] AS label, caller.qualified_name AS qualified_name,
       caller.path AS path, r.line AS line, r.col AS col, r.end_line AS end_line,
       r.end_col AS end_col, r.arg_count AS arg_count, r.kwarg_names AS kwarg_names,
       r.resolution AS resolution"""
CYPHER_GRAPH_CALLEES = """MATCH (caller)-[r:CALLS]->(callee)
WHERE caller.qualified_name = $qn AND callee.qualified_name STARTS WITH $project_prefix
RETURN labels(callee)[0] AS label, callee.qualified_name AS qualified_name,
       callee.path AS path, r.line AS line, r.col AS col, r.end_line AS end_line,
       r.end_col AS end_col, r.arg_count AS arg_count, r.kwarg_names AS kwarg_names,
       r.resolution AS resolution"""
CYPHER_GRAPH_IMPLEMENTORS = """MATCH (impl)-[r:INHERITS|IMPLEMENTS]->(base)
WHERE base.qualified_name = $qn AND impl.qualified_name STARTS WITH $project_prefix
RETURN labels(impl)[0] AS label, impl.qualified_name AS qualified_name,
       impl.path AS path, type(r) AS rel_type"""
CYPHER_GRAPH_OVERRIDES = """MATCH (a)-[r:OVERRIDES]-(b)
WHERE b.qualified_name = $qn AND a.qualified_name STARTS WITH $project_prefix
RETURN labels(a)[0] AS label, a.qualified_name AS qualified_name, a.path AS path,
       type(r) AS rel_type"""
# Structural delta after a write (issue #1525): the touched files' definitions
# with the properties the delta compares, every call/reference site touching
# them, and the project's module import graph.
_DELTA_DEFINITION_LABELS = "|".join(
    (
        NodeLabel.FUNCTION.value,
        NodeLabel.METHOD.value,
        NodeLabel.CLASS.value,
        NodeLabel.INTERFACE.value,
        NodeLabel.ENUM.value,
        NodeLabel.TYPE.value,
        NodeLabel.UNION.value,
    )
)
_DELTA_DEFINITION_FIELDS = """RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.positional_params AS positional_params,
       n.ast_fingerprint AS ast_fingerprint,
       n.ast_fingerprint_nodes AS ast_fingerprint_nodes,
       n.ast_branch_fingerprints AS ast_branch_fingerprints"""
CYPHER_DELTA_DEFINITIONS = f"""MATCH (n:{_DELTA_DEFINITION_LABELS})
WHERE n.qualified_name STARTS WITH $project_prefix AND n.path IN $paths
{_DELTA_DEFINITION_FIELDS}"""
# The callees of the touched files' sites that live elsewhere: their
# declared parameters decide the arity verdict of each site.
CYPHER_DELTA_DEFINITIONS_BY_QN = f"""MATCH (n:{_DELTA_DEFINITION_LABELS})
WHERE n.qualified_name IN $qns
{_DELTA_DEFINITION_FIELDS}"""
CYPHER_DELTA_SITES = """MATCH (a)-[r:CALLS|REFERENCES|INSTANTIATES]->(b)
WHERE a.qualified_name STARTS WITH $project_prefix
  AND (a.path IN $paths OR b.path IN $paths)
RETURN a.qualified_name AS from_qn, a.path AS from_path, type(r) AS rel_type,
       b.qualified_name AS to_qn, b.path AS to_path, r.line AS line, r.col AS col,
       r.arg_count AS arg_count, r.kwarg_names AS kwarg_names"""
# One hop of the backward test-reach walk: the callers of a frontier of
# qualified names, with the properties the test classifier reads.
CYPHER_DELTA_CALLERS_OF = """MATCH (a)-[:CALLS|REFERENCES|INSTANTIATES]->(b)
WHERE b.qualified_name IN $qns AND a.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT labels(a)[0] AS label, a.qualified_name AS qualified_name,
       a.name AS name, a.path AS path, a.start_line AS start_line,
       a.end_line AS end_line, a.decorators AS decorators,
       a.is_exported AS is_exported, b.qualified_name AS to_qn"""
# Rust test classification inputs, fetched only when the walk reaches Rust.
CYPHER_DELTA_RUST_MODULES = """MATCH (m:Module)
WHERE m.qualified_name STARTS WITH $project_prefix AND m.path ENDS WITH '.rs'
RETURN labels(m)[0] AS label, m.qualified_name AS qualified_name, m.name AS name,
       m.path AS path, m.decorators AS decorators,
       m.rust_cfg_test_mods AS rust_cfg_test_mods,
       m.rust_ungated_mods AS rust_ungated_mods"""
CYPHER_DELTA_RUST_TEST_FNS = """MATCH (n:Function|Method)
WHERE n.qualified_name STARTS WITH $project_prefix AND n.path IN $paths
RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, n.name AS name,
       n.path AS path, n.start_line AS start_line, n.end_line AS end_line,
       n.decorators AS decorators"""
CYPHER_DELTA_MODULE_IMPORTS = """MATCH (m:Module)-[:IMPORTS]->(t:Module)
WHERE m.qualified_name STARTS WITH $project_prefix
  AND t.qualified_name STARTS WITH $project_prefix
RETURN DISTINCT m.qualified_name AS from_qn, m.path AS from_path,
       t.qualified_name AS to_qn"""
CYPHER_GRAPH_IMPORTERS = """MATCH (m:Module)-[r:IMPORTS]->(target)
WHERE target.qualified_name = $qn AND m.qualified_name STARTS WITH $project_prefix
RETURN m.qualified_name AS qualified_name, m.path AS path, r.line AS line,
       r.col AS col, r.end_line AS end_line, r.end_col AS end_col,
       r.alias AS alias, r.imported_name AS imported_name"""

# Trace write-back (issue #1526): a static edge the runtime observed is
# upgraded in place, on every site it has, so the upgrade never creates a
# site-less duplicate beside the located ones.
# Only the static edge(s) of the pair are confirmed: a trace-only edge from
# an earlier run can sit beside a static one on the same pair and must keep
# its `dynamic` label.
CYPHER_TRACE_CONFIRM_CALLS = """
MATCH (a)-[r:CALLS]->(b)
WHERE a.qualified_name = $from_qn AND b.qualified_name = $to_qn
  AND coalesce(r.static_missed, false) = false
SET r.resolution = $resolution
"""
