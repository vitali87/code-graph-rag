from .constants import (
    CPP_EXTENSIONS,
    CPP_OPERATOR_PREFIX,
    CYPHER_DEFAULT_LIMIT,
    GO_ROOT_FUNCTION_NAMES,
    JAVA_SERIALIZATION_METHOD_NAMES,
    PROTOCOL_BASE_QNS,
    RUST_ROOT_FUNCTION_NAMES,
    RUST_TRAIT_METHOD_NAMES,
)

CYPHER_DELETE_ALL = "MATCH (n) DETACH DELETE n;"

# (H) Graph structural integrity audit (issue #646). A zero-degree Project is a
# (H) valid empty-repo graph, so the orphan scan exempts it.
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

CYPHER_EXAMPLE_PROJECT_SCOPED = f"""MATCH (c:Class)
WHERE c.qualified_name STARTS WITH 'myproject.'
RETURN c.name AS name, c.qualified_name AS qualified_name, labels(c) AS type
LIMIT {CYPHER_DEFAULT_LIMIT}"""

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


# (H) Match against a leading-slash-normalized path so a `/tests/` pattern also
# (H) matches a ROOT `tests/` dir (Rust integration tests, a top-level tests/
# (H) folder), not just a nested `src/tests/`; `/contests/` still won't match.
_DEAD_CODE_TEST_ROOT_CLAUSE = (
    "\n    OR ANY(p IN $test_patterns WHERE ('/' + coalesce(n.path, '')) CONTAINS p)"
)

# (H) When tests are excluded, a test-file symbol's only callers (test functions)
# (H) are excluded as roots, so reporting it is unconditional noise: test helpers
# (H) and mocks are test infrastructure, not dead production code. Filter them
# (H) from the reported candidates; production code reached only from tests is
# (H) still reported.
# (H) coalesce: a null path must not null out the NOT and silently drop the
# (H) node from the report (NOT null = null in Cypher).
_DEAD_CODE_CANDIDATE_NON_TEST = (
    "\n  AND NOT ANY(p IN $test_patterns WHERE ('/' + coalesce(n.path, '')) CONTAINS p)"
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
    " WHERE NOT ANY(p IN $test_patterns"
    " WHERE ('/' + coalesce(m.path, '')) CONTAINS p) | 1]) > 0"
)

# (H) A method whose class INHERITS typing.Protocol is an interface stub; callers
# (H) resolve to the implementations, never to the stub, so every Protocol method
# (H) is a root.
_DEAD_CODE_PROTOCOL_STUB_CLAUSE = (
    "size([(n)<-[:DEFINES_METHOD]-(:Class)-[:INHERITS]->(p:Class)"
    " WHERE p.qualified_name IN [{protocol_bases}] | 1]) > 0"
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
# (H) After the base round, a second expansion roots decorated functions DEFINED
# (H) by a LIVE function/method (prompt_toolkit @bindings.add, MCP
# (H) @server.list_tools, nested typer/click commands): the enclosing function
# (H) executes the registration, so the closure and its callees are live. The
# (H) exemption is tied to owner liveness on purpose — a decorated closure of a
# (H) DEAD owner never registers and is reported with its whole cluster.
# (H) ponytail: one expansion round, so a registration chain nested two closures
# (H) deep is missed; add a third round (or iterate to fixed point) if real code
# (H) ever registers closures from inside registered closures.
_DEAD_CODE_QUERY_TEMPLATE = """MATCH (n:{labels})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND (
    ANY(d IN n.decorators
        WHERE toLower(last(split(split(replace(d, '@', ''), '(')[0], '.')))
              IN $root_decorators)
    OR n.is_exported = true
    OR {protocol_stub_clause}
    OR ('Method' IN labels(n)
        AND n.name STARTS WITH '__' AND n.name ENDS WITH '__' AND size(n.name) > 4
        AND n.path ENDS WITH '.py')
    OR ('Function' IN labels(n)
        AND n.name IN {go_root_names} AND n.path ENDS WITH '.go')
    OR ('Function' IN labels(n)
        AND n.name IN {rust_root_names} AND n.path ENDS WITH '.rs')
    OR ('Method' IN labels(n)
        AND n.name IN {rust_trait_methods} AND n.path ENDS WITH '.rs')
    OR ('Method' IN labels(n)
        AND n.name IN {java_serialization_methods} AND n.path ENDS WITH '.java')
    OR {cpp_operator_clause}
    OR ANY(e IN $entry_points WHERE n.qualified_name ENDS WITH e)
    OR {module_clause}{test_clause}
  )
WITH collect(n) AS roots
UNWIND (CASE WHEN size(roots) = 0 THEN [null] ELSE roots END) AS r
OPTIONAL MATCH (r)-[:{traversal}*BFS]->(reached)
WITH roots, collect(DISTINCT reached) AS reached_set
WITH roots + reached_set AS live_set
OPTIONAL MATCH (o:Function|Method)-[:DEFINES]->(c:Function|Method)
WHERE o IN live_set AND size(coalesce(c.decorators, [])) > 0
  AND NOT c IN live_set
WITH live_set, collect(DISTINCT c) AS closure_roots
UNWIND (
  CASE WHEN size(closure_roots) = 0 THEN [null] ELSE closure_roots END
) AS cr
OPTIONAL MATCH (cr)-[:{traversal}*BFS]->(cr_reached)
WITH live_set, closure_roots, collect(DISTINCT cr_reached) AS cr_reached_set
WITH live_set + closure_roots + cr_reached_set AS live_set
OPTIONAL MATCH (ov:Function|Method)-[:OVERRIDES*]->(base)
WHERE base IN live_set AND NOT ov IN live_set
WITH live_set, collect(DISTINCT ov) AS override_roots
UNWIND (
  CASE WHEN size(override_roots) = 0 THEN [null] ELSE override_roots END
) AS orr
OPTIONAL MATCH (orr)-[:{traversal}*BFS]->(or_reached)
WITH live_set, override_roots, collect(DISTINCT or_reached) AS or_reached_set
WITH live_set + override_roots + or_reached_set AS live_set
MATCH (n:{labels})
WHERE n.qualified_name STARTS WITH $project_prefix
  AND NOT n IN live_set{candidate_clause}
RETURN labels(n)[0] AS label, n.name AS name,
       n.qualified_name AS qualified_name, n.path AS path,
       n.start_line AS start_line, n.end_line AS end_line
ORDER BY qualified_name"""


def build_dead_code_query(include_tests: bool, include_classes: bool = False) -> str:
    if include_classes:
        labels = "Function|Method|Class"
        traversal = "CALLS|REFERENCES|INSTANTIATES|INHERITS"
        module_rels = "CALLS|REFERENCES|INSTANTIATES"
    else:
        labels = "Function|Method"
        traversal = "CALLS|REFERENCES"
        module_rels = "CALLS|REFERENCES"
    if include_tests:
        module_clause = _DEAD_CODE_MODULE_ROOT_ANY.format(module_rels=module_rels)
        test_clause = _DEAD_CODE_TEST_ROOT_CLAUSE
        candidate_clause = ""
    else:
        module_clause = _DEAD_CODE_MODULE_ROOT_NON_TEST.format(module_rels=module_rels)
        test_clause = ""
        candidate_clause = _DEAD_CODE_CANDIDATE_NON_TEST
    protocol_bases = ", ".join(f"'{qn}'" for qn in PROTOCOL_BASE_QNS)
    go_root_names = _cypher_str_list(GO_ROOT_FUNCTION_NAMES)
    rust_root_names = _cypher_str_list(RUST_ROOT_FUNCTION_NAMES)
    rust_trait_methods = _cypher_str_list(RUST_TRAIT_METHOD_NAMES)
    java_serialization_methods = _cypher_str_list(JAVA_SERIALIZATION_METHOD_NAMES)
    return _DEAD_CODE_QUERY_TEMPLATE.format(
        labels=labels,
        traversal=traversal,
        module_clause=module_clause,
        test_clause=test_clause,
        candidate_clause=candidate_clause,
        go_root_names=go_root_names,
        rust_root_names=rust_root_names,
        rust_trait_methods=rust_trait_methods,
        java_serialization_methods=java_serialization_methods,
        cpp_operator_clause=_cpp_operator_root_clause(),
        protocol_stub_clause=_DEAD_CODE_PROTOCOL_STUB_CLAUSE.format(
            protocol_bases=protocol_bases
        ),
    )


def _cpp_operator_root_clause() -> str:
    # (H) A C++ operator overload / user-defined literal (name headed by the reserved
    # (H) `operator` keyword) is invoked by operator/literal syntax the call graph does
    # (H) not model, so it is a reachability root on any C++ file (member or free).
    exts = " OR ".join(f"n.path ENDS WITH '{ext}'" for ext in CPP_EXTENSIONS)
    return f"(n.name STARTS WITH '{CPP_OPERATOR_PREFIX}' AND ({exts}))"


def _cypher_str_list(values: frozenset[str]) -> str:
    # (H) Render a set of names as a Cypher list literal (`['a', 'b']`); sorted for
    # (H) deterministic query text.
    return "[" + ", ".join(f"'{v}'" for v in sorted(values)) + "]"


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
