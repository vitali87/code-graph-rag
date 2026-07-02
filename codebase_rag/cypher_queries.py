from .constants import CYPHER_DEFAULT_LIMIT

CYPHER_DELETE_ALL = "MATCH (n) DETACH DELETE n;"

CYPHER_LIST_PROJECTS = "MATCH (p:Project) RETURN p.name AS name ORDER BY p.name"

CYPHER_DELETE_PROJECT = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[:CONTAINS_PACKAGE|CONTAINS_FOLDER|CONTAINS_FILE|CONTAINS_MODULE*]->(container)
OPTIONAL MATCH (container)-[:DEFINES|DEFINES_METHOD*]->(defined)
DETACH DELETE p, container, defined
"""

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

CYPHER_EXAMPLE_CLASS_METHODS = f"""MATCH (c:Class)-[:DEFINES_METHOD]->(m:Method)
WHERE c.name = 'UserService'
RETURN c.name AS className, m.name AS methodName, m.qualified_name AS qualified_name, labels(m) AS type
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
       n.end_line AS end_line, m.path AS path
"""

CYPHER_FIND_BY_QUALIFIED_NAME = """
MATCH (n) WHERE n.qualified_name = $qn
OPTIONAL MATCH (m:Module)-[*]-(n)
RETURN n.name AS name, n.start_line AS start, n.end_line AS end, m.path AS path, n.docstring AS docstring
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


_DEAD_CODE_TEST_ROOT_CLAUSE = (
    "\n    OR ANY(p IN $test_patterns WHERE n.path CONTAINS p)"
)

# (H) A node reached by a Module node runs at import (top-level statement,
# (H) `if __name__ == "__main__"`, a bare decorator, or a module-scope
# (H) construction), so it is a root. `size([...])` avoids the non-standard
# (H) `exists(pattern)`. When tests are excluded, an edge from a test module must
# (H) NOT keep project code alive, so the test-module variant filters by path.
# (H) `{module_rels}` is the relationship set walked from the module (CALLS, plus
# (H) INSTANTIATES when classes are included so module-scope construction roots a
# (H) class).
_DEAD_CODE_MODULE_ROOT_ANY = "size([(n)<-[:{module_rels}]-(:Module) | 1]) > 0"
_DEAD_CODE_MODULE_ROOT_NON_TEST = (
    "size([(n)<-[:{module_rels}]-(m:Module)"
    " WHERE NOT ANY(p IN $test_patterns WHERE m.path CONTAINS p) | 1]) > 0"
)

# (H) Reachability walks CALLS only by default. With classes included it also
# (H) walks INSTANTIATES (construction keeps a class live) and INHERITS forward
# (H) from subclass to base, so a base is kept live only by a REACHABLE subclass.
# (H) A base whose sole subclass is itself unreachable is therefore reported as
# (H) part of the dead cluster (the subclass is reported too). Classes referenced
# (H) solely via type annotations / isinstance / dynamic lookups are not modelled
# (H) as edges, so class candidates are review hints, not a delete list.
# (H) *BFS visits each reachable node once; plain *0.. enumerates every path and
# (H) times out on real graphs (cycles/diamonds make it combinatorial). *BFS
# (H) excludes the source node, so the roots are unioned into the live set. The
# (H) CASE keeps one row when there are no roots (UNWIND of [] yields zero rows,
# (H) which would drop the final MATCH and wrongly report nothing as dead).
_DEAD_CODE_QUERY_TEMPLATE = """MATCH (n:{labels})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND (
    ANY(d IN n.decorators
        WHERE toLower(last(split(split(replace(d, '@', ''), '(')[0], '.')))
              IN $root_decorators)
    OR n.is_exported = true
    OR (n.name STARTS WITH '__' AND n.name ENDS WITH '__' AND size(n.name) > 4)
    OR ANY(e IN $entry_points WHERE n.qualified_name ENDS WITH e)
    OR {module_clause}{test_clause}
  )
WITH collect(n) AS roots
UNWIND (CASE WHEN size(roots) = 0 THEN [null] ELSE roots END) AS r
OPTIONAL MATCH (r)-[:{traversal}*BFS]->(reached)
WITH roots, collect(DISTINCT reached) AS reached_set
WITH roots + reached_set AS live_set
MATCH (n:{labels})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND NOT n IN live_set
RETURN labels(n)[0] AS label, n.name AS name,
       n.qualified_name AS qualified_name,
       n.start_line AS start_line, n.end_line AS end_line
ORDER BY qualified_name"""


def build_dead_code_query(include_tests: bool, include_classes: bool = False) -> str:
    if include_classes:
        labels = "Function|Method|Class"
        traversal = "CALLS|INSTANTIATES|INHERITS"
        module_rels = "CALLS|INSTANTIATES"
    else:
        labels = "Function|Method"
        traversal = "CALLS"
        module_rels = "CALLS"
    if include_tests:
        module_clause = _DEAD_CODE_MODULE_ROOT_ANY.format(module_rels=module_rels)
        test_clause = _DEAD_CODE_TEST_ROOT_CLAUSE
    else:
        module_clause = _DEAD_CODE_MODULE_ROOT_NON_TEST.format(module_rels=module_rels)
        test_clause = ""
    return _DEAD_CODE_QUERY_TEMPLATE.format(
        labels=labels,
        traversal=traversal,
        module_clause=module_clause,
        test_clause=test_clause,
    )


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
) -> str:
    query = (
        f"MATCH (a:{from_label} {{{from_key}: row.from_val}}), "
        f"(b:{to_label} {{{to_key}: row.to_val}})\n"
        f"MERGE (a)-[r:{rel_type}]->(b)\n"
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
