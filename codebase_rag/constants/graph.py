# Graph schema: node labels, relationships, keys, and Cypher queries.

from enum import StrEnum

KEY_NODES = "nodes"
KEY_RELATIONSHIPS = "relationships"
KEY_NODE_ID = "node_id"
KEY_LABELS = "labels"
KEY_LABEL = "label"
KEY_PROPERTIES = "properties"
KEY_PURGED = "purged"
KEY_FROM_ID = "from_id"
KEY_TO_ID = "to_id"
KEY_TYPE = "type"
KEY_METADATA = "metadata"
KEY_TOTAL_NODES = "total_nodes"
KEY_TOTAL_RELATIONSHIPS = "total_relationships"
KEY_NODE_LABELS = "node_labels"
KEY_RELATIONSHIP_TYPES = "relationship_types"
KEY_EXPORTED_AT = "exported_at"
KEY_PARSER = "parser"
KEY_NAME = "name"
KEY_ROOT_PATH = "root_path"
KEY_QUALIFIED_NAME = "qualified_name"
KEY_IS_PROPERTY = "is_property"
KEY_IS_MACRO = "is_macro"
KEY_QUERY = "query"
KEY_RESPONSE = "response"
KEY_START_LINE = "start_line"
# Column of the definition's own start token, and of its NAME token where the
# two differ (Go keys semantic call targets at the name identifier while span
# keys sit at the `func` keyword). Persisted so incremental runs can rehydrate
# the col-keyed location indexes for unchanged files (issue #1240).
KEY_START_COL = "start_col"
KEY_NAME_START_LINE = "name_start_line"
KEY_NAME_START_COL = "name_start_col"
KEY_END_LINE = "end_line"
# Edge-site location properties (issue #1522). Every CALLS / REFERENCES /
# INSTANTIATES edge records the span of the expression that produced it, and
# every IMPORTS edge the span of its import statement, so a consumer can jump
# to, verify, or rewrite the exact site. Lines are 1-based and columns 0-based,
# matching the node `start_line` / `start_col` convention. Sites are stored as
# ONE EDGE PER SITE: the site props join the MERGE key (see
# MERGE_KEY_PROPS_BY_REL), so a caller invoking one callee twice carries two
# parallel edges. Edges emitted without a site (libclang macro uses, Roslyn
# facts, dynamic-trace write-back, inferred C# namespace imports) carry none of
# these keys and keep collapsing on their endpoints.
KEY_LINE = "line"
# Fixed-Cypher parameter names for the deterministic graph queries (#1523).
KEY_QN = "qn"
# How a CALLS/REFERENCES/INSTANTIATES edge was resolved (issue #1526).
KEY_RESOLUTION = "resolution"
KEY_UNLOCATABLE = "unlocatable"
KEY_DISPATCH_LITERAL = "dispatch_literal"


class EdgeResolution(StrEnum):
    """Confidence of a call/reference edge, set where it is emitted.

    `exact`: the resolver bound the target through scope, import, type or
    signature. `overload`: one edge per same-named candidate. `heuristic`:
    a name-only match (trie suffix, wildcard import, package member).
    `trace_confirmed`: a static edge a runtime trace observed. `dynamic`: a
    call only a trace saw, with the dispatch literal's site when found.
    """

    EXACT = "exact"
    OVERLOAD = "overload"
    HEURISTIC = "heuristic"
    TRACE_CONFIRMED = "trace_confirmed"
    DYNAMIC = "dynamic"


# Ordering for `--min-resolution`: an edge with no label is a legacy static
# edge and ranks as exact; a trace-only edge ranks with the confirmed ones,
# a runtime having observed it.
RESOLUTION_RANK: dict[str, int] = {
    EdgeResolution.HEURISTIC: 1,
    EdgeResolution.OVERLOAD: 2,
    EdgeResolution.EXACT: 3,
    EdgeResolution.DYNAMIC: 4,
    EdgeResolution.TRACE_CONFIRMED: 4,
}
KEY_SUFFIX = "suffix"
KEY_COL = "col"
KEY_END_COL = "end_col"
KEY_ARG_COUNT = "arg_count"
KEY_KWARG_NAMES = "kwarg_names"
# IMPORTS only: the name the statement binds in the importing scope (the
# `as` name when renamed, else the imported/module name) and, for
# symbol-level imports (`from x import y`, `import { y }`, `use a::b::y`),
# the symbol's own name. A whole-module import has no `imported_name`.
KEY_ALIAS = "alias"
KEY_IMPORTED_NAME = "imported_name"
# `imported_name` of a wildcard import (`from x import *`, `export * from`).
IMPORTED_NAME_WILDCARD = "*"
KEY_PATH = "path"
# Literal relative specifiers this module imported that named no file on disk
# when it was parsed ("./index"). A dropped IMPORTS edge leaves no row for the
# target-side lookup to match, so the waiting importer is recorded here and
# stays findable from the created file's path alone (issue #1714).
KEY_UNRESOLVED_SPECIFIERS = "unresolved_specifiers"
KEY_ABSOLUTE_PATH = "absolute_path"
# Whether flow analysis covered a Module: its language is in the source/sink
# registry AND the FLOWS_TO capture group was enabled at indexing. Read by
# the three-verdict flow reachability query (issue #1050).
KEY_FLOW_COVERED = "flow_covered"
KEY_GENERATED = "generated"
KEY_GENERATOR = "generator"
KEY_EXTENSION = "extension"
KEY_MODULE_TYPE = "module_type"
KEY_IMPLEMENTS_MODULE = "implements_module"
KEY_PROPS = "props"
KEY_CREATED = "created"
KEY_FROM_VAL = "from_val"
KEY_TO_VAL = "to_val"
KEY_FROM_LABEL = "from_label"
KEY_FROM_QN = "from_qn"
KEY_REL_TYPE = "rel_type"
KEY_TO_LABEL = "to_label"
KEY_TO_QN = "to_qn"
KEY_PROJECT_PREFIX = "project_prefix"
KEY_VERSION_SPEC = "version_spec"
KEY_PREFIX = "prefix"
KEY_PROJECT_NAME = "project_name"
# ast-grep finding node properties (issue #413)
KEY_MESSAGE = "message"
KEY_SNIPPET = "snippet"
# Structural clone-detection fingerprints, stamped on Function/Method at
# ingest and read back by `cgr duplicates` and the find_duplicate_code tool.
KEY_AST_FINGERPRINT = "ast_fingerprint"
KEY_AST_FINGERPRINT_NODES = "ast_fingerprint_nodes"
KEY_AST_BRANCH_FINGERPRINTS = "ast_branch_fingerprints"

ERR_SUBSTR_ALREADY_EXISTS = "already exists"
ERR_SUBSTR_CONSTRAINT = "constraint"

PROTOBUF_INDEX_FILE = "index.bin"
PROTOBUF_PAYLOAD_ONEOF = "payload"
PROTOBUF_NODES_FILE = "nodes.bin"
PROTOBUF_RELS_FILE = "relationships.bin"

ONEOF_PROJECT = "project"
ONEOF_PACKAGE = "package"
ONEOF_FOLDER = "folder"
ONEOF_MODULE = "module"
ONEOF_CLASS = "class_node"
ONEOF_FUNCTION = "function"
ONEOF_METHOD = "method"
ONEOF_FILE = "file"
ONEOF_EXTERNAL_PACKAGE = "external_package"
ONEOF_EXTERNAL_MODULE = "external_module"
ONEOF_MODULE_IMPLEMENTATION = "module_implementation"
ONEOF_MODULE_INTERFACE = "module_interface"
ONEOF_INTERFACE = "interface_node"
ONEOF_ENUM = "enum_node"
ONEOF_TYPE = "type_node"
ONEOF_UNION = "union_node"
ONEOF_RESOURCE = "resource"
ONEOF_SECTION = "section"
# ast-grep findings (issue #413). Without these mappings the three finding
# labels have no protobuf payload, and ensure_node_batch drops the node
# entirely rather than storing it untyped (issue #1452).
ONEOF_PATTERN = "pattern"
ONEOF_CODE_SMELL = "code_smell"
ONEOF_SECURITY_ISSUE = "security_issue"


class UniqueKeyType(StrEnum):
    NAME = KEY_NAME
    PATH = KEY_PATH
    ABSOLUTE_PATH = KEY_ABSOLUTE_PATH
    QUALIFIED_NAME = KEY_QUALIFIED_NAME


class NodeLabel(StrEnum):
    PROJECT = "Project"
    PACKAGE = "Package"
    FOLDER = "Folder"
    FILE = "File"
    MODULE = "Module"
    CLASS = "Class"
    FUNCTION = "Function"
    METHOD = "Method"
    INTERFACE = "Interface"
    ENUM = "Enum"
    TYPE = "Type"
    UNION = "Union"
    MODULE_INTERFACE = "ModuleInterface"
    MODULE_IMPLEMENTATION = "ModuleImplementation"
    EXTERNAL_PACKAGE = "ExternalPackage"
    EXTERNAL_MODULE = "ExternalModule"
    RESOURCE = "Resource"
    # A heading and the prose beneath it in a document (issue #1426). Sections
    # nest by heading level, so a Section CONTAINS_SECTION its subsections.
    SECTION = "Section"
    # ast-grep findings (issue #413): quality/security signals attached to a
    # Module. Opt-in via CaptureGroup.FINDINGS.
    PATTERN = "Pattern"
    CODE_SMELL = "CodeSmell"
    SECURITY_ISSUE = "SecurityIssue"


_NODE_LABEL_UNIQUE_KEYS: dict[NodeLabel, UniqueKeyType] = {
    NodeLabel.PROJECT: UniqueKeyType.NAME,
    NodeLabel.PACKAGE: UniqueKeyType.QUALIFIED_NAME,
    # Folder and File identity must be per checkout: keyed on the bare
    # relative path, two same-layout projects in the shared graph merge
    # onto one node and delete-project crosses into the sibling's subtree
    # (issue #897).
    NodeLabel.FOLDER: UniqueKeyType.ABSOLUTE_PATH,
    NodeLabel.FILE: UniqueKeyType.ABSOLUTE_PATH,
    NodeLabel.MODULE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.CLASS: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.FUNCTION: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.METHOD: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.INTERFACE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.ENUM: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.TYPE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.UNION: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.MODULE_INTERFACE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.MODULE_IMPLEMENTATION: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.EXTERNAL_PACKAGE: UniqueKeyType.NAME,
    NodeLabel.EXTERNAL_MODULE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.RESOURCE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.SECTION: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.PATTERN: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.CODE_SMELL: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.SECURITY_ISSUE: UniqueKeyType.QUALIFIED_NAME,
}

_missing_keys = set(NodeLabel) - set(_NODE_LABEL_UNIQUE_KEYS.keys())
if _missing_keys:
    raise RuntimeError(
        f"NodeLabel(s) missing from _NODE_LABEL_UNIQUE_KEYS: {sorted(_missing_keys)}. "
        "Every NodeLabel MUST have a unique key defined."
    )


class RelationshipType(StrEnum):
    CONTAINS_PACKAGE = "CONTAINS_PACKAGE"
    CONTAINS_FOLDER = "CONTAINS_FOLDER"
    CONTAINS_FILE = "CONTAINS_FILE"
    CONTAINS_MODULE = "CONTAINS_MODULE"
    CONTAINS_SECTION = "CONTAINS_SECTION"
    DEFINES = "DEFINES"
    DEFINES_METHOD = "DEFINES_METHOD"
    IMPORTS = "IMPORTS"
    EXPORTS = "EXPORTS"
    EXPORTS_MODULE = "EXPORTS_MODULE"
    IMPLEMENTS_MODULE = "IMPLEMENTS_MODULE"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    OVERRIDES = "OVERRIDES"
    # Function/Method -> the project type its annotation names (issue #1527).
    RETURNS = "RETURNS"
    ACCEPTS = "ACCEPTS"
    CALLS = "CALLS"
    REFERENCES = "REFERENCES"
    INSTANTIATES = "INSTANTIATES"
    DEPENDS_ON_EXTERNAL = "DEPENDS_ON_EXTERNAL"
    READS_FROM = "READS_FROM"
    WRITES_TO = "WRITES_TO"
    FLOWS_TO = "FLOWS_TO"
    EXPOSES = "EXPOSES"
    RESOLVES_TO = "RESOLVES_TO"
    IMPLEMENTS_PATTERN = "IMPLEMENTS_PATTERN"
    HAS_SMELL = "HAS_SMELL"
    HAS_VULNERABILITY = "HAS_VULNERABILITY"
    # A relative link from a document to another file in the repository
    # (issue #164). The document equivalent of an import: it is how a README
    # or a guide states which files it is about.
    LINKS_TO = "LINKS_TO"


class CaptureGroup(StrEnum):
    STRUCTURE = "structure"
    CALLS = "calls"
    TYPES = "types"
    IMPORTS = "imports"
    IO = "io"
    FINDINGS = "findings"


# Each relationship type belongs to exactly one capture group. The guard below
# enforces total coverage, so a new RelationshipType cannot silently escape the
# capture model.
CAPTURE_GROUP_RELS: dict[CaptureGroup, frozenset[RelationshipType]] = {
    CaptureGroup.STRUCTURE: frozenset(
        {
            RelationshipType.CONTAINS_PACKAGE,
            RelationshipType.CONTAINS_FOLDER,
            RelationshipType.CONTAINS_FILE,
            RelationshipType.CONTAINS_MODULE,
            RelationshipType.CONTAINS_SECTION,
            RelationshipType.DEFINES,
            RelationshipType.DEFINES_METHOD,
        }
    ),
    CaptureGroup.CALLS: frozenset(
        {
            RelationshipType.CALLS,
            RelationshipType.REFERENCES,
            RelationshipType.INSTANTIATES,
        }
    ),
    CaptureGroup.TYPES: frozenset(
        {
            RelationshipType.INHERITS,
            RelationshipType.IMPLEMENTS,
            RelationshipType.IMPLEMENTS_MODULE,
            RelationshipType.OVERRIDES,
            RelationshipType.RETURNS,
            RelationshipType.ACCEPTS,
        }
    ),
    CaptureGroup.IMPORTS: frozenset(
        {
            RelationshipType.IMPORTS,
            RelationshipType.EXPORTS,
            RelationshipType.EXPORTS_MODULE,
            RelationshipType.DEPENDS_ON_EXTERNAL,
            RelationshipType.LINKS_TO,
        }
    ),
    CaptureGroup.IO: frozenset(
        {
            RelationshipType.READS_FROM,
            RelationshipType.WRITES_TO,
            RelationshipType.FLOWS_TO,
            RelationshipType.EXPOSES,
            RelationshipType.RESOLVES_TO,
        }
    ),
    CaptureGroup.FINDINGS: frozenset(
        {
            RelationshipType.IMPLEMENTS_PATTERN,
            RelationshipType.HAS_SMELL,
            RelationshipType.HAS_VULNERABILITY,
        }
    ),
}

# Node labels a group exclusively owns; the label is captured only while the
# owning group has an enabled relationship. Labels owned by no group are always
# captured.
CAPTURE_GROUP_NODE_LABELS: dict[CaptureGroup, frozenset[NodeLabel]] = {
    CaptureGroup.IO: frozenset({NodeLabel.RESOURCE}),
    CaptureGroup.FINDINGS: frozenset(
        {NodeLabel.PATTERN, NodeLabel.CODE_SMELL, NodeLabel.SECURITY_ISSUE}
    ),
}

# Groups enabled when the user configures nothing. Add-ons (io) are opt-in.
DEFAULT_CAPTURE_GROUPS: frozenset[CaptureGroup] = frozenset(
    {
        CaptureGroup.STRUCTURE,
        CaptureGroup.CALLS,
        CaptureGroup.TYPES,
        CaptureGroup.IMPORTS,
    }
)

CAPTURE_TOKEN_ALL = "all"
CAPTURE_TOKEN_NONE = "none"
CAPTURE_DROP_PREFIX = "-"
CAPTURE_ADD_PREFIX = "+"
CAPTURE_TOKEN_SEPARATORS = ",; "

_capture_covered = frozenset().union(*CAPTURE_GROUP_RELS.values())
_capture_missing = set(RelationshipType) - _capture_covered
if _capture_missing:
    raise RuntimeError(
        f"RelationshipType(s) missing from CAPTURE_GROUP_RELS: {_capture_missing}. "
        "Every RelationshipType MUST belong to exactly one capture group."
    )


class AuditCheck(StrEnum):
    ORPHAN_NODE = "orphan_node"
    UNDOCUMENTED_LABEL = "undocumented_label"
    UNDOCUMENTED_PROPERTY = "undocumented_property"
    MISSING_REQUIRED_PROPERTY = "missing_required_property"
    UNDOCUMENTED_RELATIONSHIP = "undocumented_relationship"
    DANGLING_RELATIONSHIP = "dangling_relationship"


# Graph audit violation details (issue #646)
AUDIT_DETAIL_ORPHAN = "{label} '{key}' has no relationships"
AUDIT_DETAIL_UNDOCUMENTED_LABEL = "label '{label}' is not documented in NODE_SCHEMAS"
AUDIT_DETAIL_UNDOCUMENTED_PROPERTY = (
    "{label} '{key}' has undocumented property '{prop}'"
)
AUDIT_DETAIL_MISSING_REQUIRED = "{label} '{key}' is missing required property '{prop}'"
AUDIT_DETAIL_UNDOCUMENTED_RELATIONSHIP = (
    "({from_label})-[:{rel_type}]->({to_label}) is not documented"
    " in RELATIONSHIP_SCHEMAS"
)
AUDIT_DETAIL_DANGLING = (
    "({from_label} '{from_key}')-[:{rel_type}]->({to_label} '{to_key}')"
    " references a nonexistent node and would be dropped by the database"
)

# Live-graph audit details (doctor)
AUDIT_DETAIL_ORPHAN_COUNT = "{count} {label} node(s) have no relationships"
AUDIT_DETAIL_UNDOCUMENTED_PROPERTY_LIVE = (
    "{label} nodes carry undocumented property '{prop}'"
)
AUDIT_DETAIL_MISSING_REQUIRED_LIVE = (
    "{count} {label} node(s) are missing required properties"
)

# Node schema property-string tokens ("{name: string, extension: string?}")
SCHEMA_PROPS_BRACES = "{}"
SCHEMA_OPTIONAL_SUFFIX = "?"

NODE_PROJECT = NodeLabel.PROJECT

KEY_PARAMETERS = "parameters"
# Declared Markdown front-matter, as sorted "key=value" entries (issue #1448).
KEY_FRONT_MATTER = "front_matter"
KEY_DECORATORS = "decorators"
# Return and parameter annotations as written (issue #1527). `return_type` is
# absent when the definition has none; `param_types` is parallel to the
# declared parameters ("" for an unannotated one) and absent, not empty, for
# languages the extractor does not read (the positional_params rule).
KEY_RETURN_TYPE = "return_type"
KEY_PARAM_TYPES = "param_types"
# Declared POSITIONAL parameter names of a Python function, receiver included,
# for arity-TypeError diagnosis (issue #227). Positional-only because CPython's
# "takes N positional arguments" counts nothing after `*`/`*args`, and
# receiver-inclusive because it counts the bound `self`.
#
# Absent on every other language rather than empty: absent means "kinds
# unknown", which `diagnose_arity` answers with "cannot corroborate", whereas
# an empty list would assert "declares zero positional parameters" and produce
# a false mismatch on correct code.
KEY_POSITIONAL_PARAMS = "positional_params"
# Target-module qn candidates of `#[cfg(test)] mod NAME;` declarations in a
# Rust file, stored on the DECLARING module's node (issue #1010). The
# ungated counterpart lets a production target's declaration of the SAME
# file module win over another target's gate (src/lib.rs gating what
# src/main.rs compiles for real).
KEY_RUST_CFG_TEST_MODS = "rust_cfg_test_mods"
KEY_RUST_UNGATED_MODS = "rust_ungated_mods"
KEY_MODIFIERS = "modifiers"
# Depth of a document heading, 1-6 (issue #1426). Kept distinct from the
# nesting a Section's CONTAINS_SECTION edges describe: skipped levels mean a
# level-3 heading can be the direct child of a level-1 one.
KEY_HEADING_LEVEL = "heading_level"
KEY_DOCSTRING = "docstring"
KEY_IS_EXPORTED = "is_exported"
# Marks a method that overrides a method of an EXTERNAL stdlib base class
# (click's textwrap.TextWrapper subclass): invoked by the base's machinery,
# never by first-party code, so dead-code reachability roots it.
KEY_OVERRIDES_EXTERNAL = "overrides_external"

CYPHER_DEFAULT_LIMIT = 50

_CYPHER_EMBEDDING_BASE = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE (n:Function OR n:Method)
  AND m.qualified_name STARTS WITH ($project_name + '.')
"""

CYPHER_QUERY_EMBEDDINGS = (
    _CYPHER_EMBEDDING_BASE
    + """RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
       n.start_line AS start_line, n.end_line AS end_line,
       m.path AS path
"""
)

CYPHER_QUERY_PROJECT_NODE_IDS = _CYPHER_EMBEDDING_BASE + "RETURN id(n) AS node_id\n"

PAYLOAD_NODE_ID = "node_id"
PAYLOAD_QUALIFIED_NAME = "qualified_name"

CYPHER_DELETE_MODULE = (
    # Scoped to the project: two projects in the shared graph can hold the
    # same relative path, and a path-only match would take the sibling's
    # module subtree with it. A repository-root __init__.py's module qn IS
    # the bare project name (no trailing dot), so the prefix test alone
    # would miss it.
    "MATCH (m:Module {path: $path}) "
    "WHERE m.qualified_name = $project_name "
    "OR m.qualified_name STARTS WITH $project_prefix "
    # CONTAINS_SECTION is in the walk because document headings hang off the
    # Module through it, not DEFINES; without it a re-indexed document keeps
    # every Section from its previous parse (issue #1426).
    "OPTIONAL MATCH (m)-[:DEFINES|DEFINES_METHOD|CONTAINS_SECTION*0..]->(c) "
    "DETACH DELETE m, c"
)
# Keyed on absolute_path: the relative path is shared across same-layout
# projects, and a path-only delete would take the sibling's node (issue #897).
CYPHER_DELETE_FILE = "MATCH (f:File {absolute_path: $path}) DETACH DELETE f"
CYPHER_DELETE_FOLDER = "MATCH (f:Folder {absolute_path: $path}) DETACH DELETE f"
CYPHER_DELETE_PACKAGE = "MATCH (p:Package {absolute_path: $path}) DETACH DELETE p"
# Removes external import-target Module nodes that no module imports anymore
# (e.g. an imported name that was renamed/removed on an incremental rebuild).
# OPTIONAL MATCH + count instead of `WHERE NOT (m)<--()`: Memgraph 3.x
# rejects pattern expressions inside WHERE, and this form is accepted by
# both 2.x and 3.x (issue #1257).
CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES = (
    "MATCH (m:ExternalModule) "
    "OPTIONAL MATCH (x)-->(m) "
    "WITH m, count(x) AS inbound "
    "WHERE inbound = 0 "
    "DETACH DELETE m"
)
CYPHER_PROJECT_MODULE_PATHS = (
    # The bare-name alternative covers the repository-root __init__.py,
    # whose module qn is the project name itself.
    "MATCH (m:Module) WHERE m.qualified_name = $project_name "
    "OR m.qualified_name STARTS WITH $project_prefix "
    "RETURN m.path AS path"
)
CYPHER_COUNT_PROJECT_MODULES = (
    "MATCH (m:Module) WHERE m.qualified_name = $project_name "
    "OR m.qualified_name STARTS WITH $project_prefix "
    "RETURN count(m) AS count"
)

# Queries for orphan pruning: return all paths stored in the graph
CYPHER_ALL_FILE_PATHS = (
    "MATCH (f:File) RETURN f.path AS path, f.absolute_path AS absolute_path"
)
# Containers of one File key, for legacy-identity sweep attribution: File
# nodes MERGE globally on absolute_path, so a key can be shared with another
# project and must not be deleted from under it (issue #1156).
CYPHER_FILE_CONTAINERS = (
    "MATCH (p)-[:CONTAINS_FILE]->(f:File {absolute_path: $path}) "
    "RETURN labels(p) AS labels, p.name AS name, "
    "p.absolute_path AS absolute_path"
)
CYPHER_ALL_MODULE_PATHS_INTERNAL = (
    "MATCH (m:Module) RETURN m.path AS path, m.qualified_name AS qualified_name"
)
CYPHER_ALL_FOLDER_PATHS = (
    "MATCH (f:Folder) RETURN f.path AS path, f.absolute_path AS absolute_path"
)
# Package nodes are pruned against the CURRENT structure, not the disk: a
# directory whose indicator file (__init__.py, Cargo.toml, ...) went away
# still exists, as a Folder (issue #1570).
CYPHER_ALL_PACKAGE_PATHS = (
    "MATCH (p:Package) RETURN p.path AS path, p.absolute_path AS absolute_path, "
    "p.qualified_name AS qualified_name"
)

# Rehydrate the in-memory function registry on an incremental run: returns
# every definition node's qualified name and label so call/instantiation
# resolution can see symbols defined in files that were not re-parsed. The
# $project_prefix filter scopes it to the project being indexed; without it,
# another project's same-named symbols pollute the resolver trie and the
# bare-name fallback binds calls across the project boundary (issue #711).
CYPHER_ALL_DEFINITION_QNS = (
    "MATCH (n) WHERE (n:Function OR n:Method OR n:Class OR n:Interface "
    "OR n:Enum OR n:Type OR n:Union) "
    "AND n.qualified_name STARTS WITH $project_prefix "
    "RETURN n.qualified_name AS qualified_name, head(labels(n)) AS label, "
    "n.is_property AS is_property, n.is_macro AS is_macro, n.path AS path, "
    "n.start_line AS start_line, n.end_line AS end_line, "
    "n.return_type AS return_type, n.param_types AS param_types"
)

# Module-level qns (plus C++20 module interfaces) for incremental runs:
# deferred import verification must count modules in UNCHANGED files as
# targets, or editing one file would drop cross-file IMPORTS edges.
CYPHER_ALL_MODULE_QNS = (
    "MATCH (n) WHERE (n:Module OR n:ModuleInterface) "
    "AND n.qualified_name STARTS WITH $project_prefix "
    "RETURN n.qualified_name AS qualified_name, head(labels(n)) AS label"
)

# Inbound reference edges (from unchanged files) into symbols defined in one
# of $paths. Captured BEFORE a changed file's subtree is deleted so the exact
# edges can be restored verbatim afterwards (issue #532, inbound half).
# Re-resolving the callers instead would diverge from a clean index, because
# cgr's call resolution is context-sensitive (protocol vs concrete receiver,
# import granularity); the original edges already match a clean re-index.
#
# The STRUCTURE of this query and of CYPHER_AFFECTED_CALLER_PATHS below is not
# covered by the unit suite, only their relation lists are. The eval emulator
# (evals/cgr_graph.py) matches these constants whole, in a `case` that compares
# the query by VALUE, and never parses the Cypher: it reimplements the
# semantics in Python over its own frozensets. So an edit to the text here is
# followed automatically by the `case` while the emulator's behaviour stays
# hardcoded, and a structural edit keeps every unit test green while breaking
# production. Measured 2026-08-30: flipping the arrow here to
# `(caller)<-[r:...]-(target)` left all of test_incremental_implements_edge.py
# passing (7 passed), and deleting the `caller.path` guard from
# CYPHER_AFFECTED_CALLER_PATHS left 16 passed across `pytest
# test_incremental_implements_edge.py test_graph_updater_incremental_rename.py
# test_cacheless_lookup_failure.py`. Both queries do reach a real backend,
# via `ingestor.fetch_all` in graph_updater.py (the affected-caller read and
# `_capture_inbound_edges`), so such an edit ships a broken incremental
# restore. Verify a change to the MATCH shape or the WHERE clauses against a
# real graph (the Docker-backed integration tier), not against a green unit
# run. Two edits the unit suite DOES catch: adding or removing a relation
# type, and renaming this query's `props` key (test_edge_site_properties.py).
# CYPHER_AFFECTED_CALLER_PATHS' `caller_path` projection IS pinned, expression
# and alias both, by test_incremental_implements_edge.py; its other RETURN
# aliases are not.
CYPHER_INBOUND_EDGES = (
    # RETURNS and ACCEPTS join the restore (issue #1527): a function whose
    # annotation names a type resolves it by unique suffix without importing
    # its module, so its file is not a dependent and the edge would otherwise
    # die with the recreated type node.
    "MATCH (caller)-[r:CALLS|REFERENCES|INSTANTIATES|IMPORTS|INHERITS|IMPLEMENTS|OVERRIDES"
    "|RETURNS|ACCEPTS]->(target) "
    "WHERE target.path IN $paths AND caller.qualified_name IS NOT NULL "
    "AND (caller.path IS NULL OR NOT caller.path IN $paths) "
    "RETURN head(labels(caller)) AS caller_label, "
    "caller.qualified_name AS caller_qn, type(r) AS rel, "
    "head(labels(target)) AS target_label, target.qualified_name AS target_qn, "
    "caller.path AS caller_path, properties(r) AS props"
)
# Files whose code DEPENDS on a re-indexed file (issue #1229 phase 4): a
# change there can rebind their calls (a new override shadowing an inherited
# method), so restoring their old edges verbatim would freeze a stale
# binding. They are re-parsed instead, one level deep: their own definitions
# are unchanged, so their callers' bindings cannot move. IMPLEMENTS counts
# like INHERITS: an implementor in the same package holds no import edge into
# its interface's file, and without this it was neither re-parsed nor
# restored when that file was re-indexed (issue #1565).
CYPHER_AFFECTED_CALLER_PATHS = (
    "MATCH (caller)-[:CALLS|REFERENCES|INSTANTIATES|IMPORTS|INHERITS|IMPLEMENTS]->(target) "
    "WHERE target.path IN $paths AND caller.path IS NOT NULL "
    "AND NOT caller.path IN $paths "
    "AND caller.qualified_name STARTS WITH $project_prefix "
    "AND target.qualified_name STARTS WITH $project_prefix "
    "RETURN DISTINCT caller.path AS caller_path"
)
# Rehydrate class_inheritance on an incremental run: every INHERITS edge
# (child -> base) with resolved qns, so protocol dispatch and inherited-method
# resolution still see the hierarchy of classes defined in files that were not
# re-parsed. Without it, editing a caller drops the protocol/inheritance
# redirect (issue #532 residual): a call resolves to the Protocol stub instead
# of the concrete implementer because _protocol_classes() is empty. Ordered by
# base_index so multiple-inheritance base order matches the original source,
# which method resolution and override attribution depend on.
# A file CREATED by a scoped re-ingest has no inbound edges yet, so
# CYPHER_AFFECTED_CALLER_PATHS cannot find the importer that referenced it
# before it existed -- `main.py` importing `./util` written before `util.ts`.
# That importer's IMPORTS edge points at an UNRESOLVED target (one outside the
# project prefix) whose name is the new module, so it is findable from the
# target side instead (issue #1682). Self-selecting: once the module exists and
# the importer has been re-parsed the edge resolves into the project prefix and
# stops matching, so this returns nothing for files that are not new.
CYPHER_UNRESOLVED_IMPORTER_PATHS = (
    "MATCH (importer)-[:IMPORTS]->(target) "
    "WHERE importer.path IS NOT NULL "
    "AND importer.qualified_name STARTS WITH $project_prefix "
    "AND NOT target.qualified_name STARTS WITH $project_prefix "
    "AND ANY(name IN $module_names WHERE target.qualified_name = name "
    "OR target.qualified_name STARTS WITH name + '.') "
    "RETURN DISTINCT importer.path AS caller_path"
)
# Modules carrying at least one unresolved relative specifier, with the
# specifiers themselves. The specifier is relative to the IMPORTING module's
# directory, so the match against a created file is resolved in Python rather
# than attempted as path arithmetic in Cypher; the row count is small because
# only modules with a dropped relative import carry the property (issue #1714).
CYPHER_UNRESOLVED_SPECIFIER_IMPORTERS = (
    "MATCH (importer) "
    "WHERE importer.path IS NOT NULL "
    "AND importer.qualified_name STARTS WITH $project_prefix "
    "AND importer.unresolved_specifiers IS NOT NULL "
    "AND size(importer.unresolved_specifiers) > 0 "
    "RETURN importer.path AS caller_path, "
    "importer.unresolved_specifiers AS specifiers"
)
CYPHER_KEY_SPECIFIERS = "specifiers"
CYPHER_ALL_INHERITS = (
    "MATCH (child)-[r:INHERITS]->(base) "
    "WHERE child.qualified_name IS NOT NULL AND base.qualified_name IS NOT NULL "
    "AND child.qualified_name STARTS WITH $project_prefix "
    "RETURN child.qualified_name AS child_qn, base.qualified_name AS base_qn, "
    "r.base_index AS base_index "
    "ORDER BY child_qn, base_index"
)

# C# type declaration locations for incremental runs: _join_csharp_partials
# resolves each Roslyn partial-declaration location against csharp_type_locations,
# which Pass 2 fills only for RE-PARSED files. Rebuild the (path, start_line) ->
# qn entries for types in UNCHANGED .cs files from the persisted graph so a
# partial part in an unchanged file still joins its group (issue #1229).
CYPHER_ALL_CSHARP_TYPE_LOCATIONS = (
    "MATCH (n) WHERE (n:Class OR n:Interface OR n:Enum) "
    "AND n.qualified_name STARTS WITH $project_prefix "
    "AND n.path ENDS WITH '.cs' "
    "RETURN n.qualified_name AS qualified_name, n.path AS path, "
    "n.start_line AS start_line"
)

# Col-keyed location rehydration fetches (issue #1240). Both guard for
# start_col's absence in Python: a pre-#1240 graph has none and degrades to
# today's behavior until re-indexed.
CYPHER_ALL_GO_TYPE_LOCATIONS = (
    "MATCH (n) WHERE (n:Class OR n:Interface OR n:Enum OR n:Type OR n:Union) "
    "AND n.qualified_name STARTS WITH $project_prefix "
    "RETURN labels(n)[0] AS label, n.qualified_name AS qualified_name, "
    "n.path AS path, n.start_line AS start_line, n.start_col AS start_col"
)
CYPHER_ALL_FUNCTION_LOCATIONS = (
    "MATCH (m:Module)-[:DEFINES]->(f:Function) "
    "WHERE m.qualified_name STARTS WITH $project_prefix "
    "RETURN labels(f)[0] AS label, f.qualified_name AS qualified_name, "
    "m.qualified_name AS module_qn, f.start_line AS start_line, "
    "f.start_col AS start_col, f.name_start_line AS name_start_line, "
    "f.name_start_col AS name_start_col"
)
CYPHER_ALL_METHOD_LOCATIONS = (
    "MATCH (m:Module)-[:DEFINES]->(c)-[:DEFINES_METHOD]->(f:Method) "
    "WHERE m.qualified_name STARTS WITH $project_prefix "
    "RETURN labels(f)[0] AS label, f.qualified_name AS qualified_name, "
    "c.qualified_name AS container_qn, "
    "m.qualified_name AS module_qn, f.start_line AS start_line, "
    "f.start_col AS start_col, f.name_start_line AS name_start_line, "
    "f.name_start_col AS name_start_col"
)
KEY_CHILD_QN = "child_qn"
KEY_BASE_QN = "base_qn"
KEY_BASE_INDEX = "base_index"
# Result columns of the location-rehydration and module-count queries. Named
# so the readers and the eval double cannot drift apart on the spelling, which
# is the failure these queries were already prone to (issue #1716).
KEY_MODULE_QN = "module_qn"
KEY_CONTAINER_QN = "container_qn"
KEY_COUNT = "count"

CYPHER_PARAM_PATHS = "paths"
CYPHER_PARAM_MODULE_NAMES = "module_names"
KEY_CALLER_PATH = "caller_path"
KEY_CALLER_LABEL = "caller_label"
KEY_CALLER_QN = "caller_qn"
KEY_REL = "rel"
KEY_TARGET_LABEL = "target_label"
KEY_TARGET_QN = "target_qn"

REL_TYPE_CALLS = "CALLS"

# Rel types where multiple semantically-distinct edges may exist between the
# same node pair; these props join the MERGE key so parallel edges are not
# collapsed at write time (issue #722). Props absent from a batch's rows are
# dropped from the key at flush time, so resource-level FLOWS_TO (no `via`)
# still dedups on endpoints.
MERGE_KEY_PROPS_BY_REL: dict[str, tuple[str, ...]] = {
    RelationshipType.FLOWS_TO.value: ("via", "kind"),
    # One edge per call/reference/instantiation site (issue #1522); a row
    # without a site still merges on its endpoints alone.
    RelationshipType.CALLS.value: (KEY_LINE, KEY_COL),
    RelationshipType.REFERENCES.value: (KEY_LINE, KEY_COL),
    RelationshipType.INSTANTIATES.value: (KEY_LINE, KEY_COL),
    # One edge per bound name: `from x import a, b` shares a statement span
    # but binds two names, each its own edge.
    RelationshipType.IMPORTS.value: (KEY_LINE, KEY_COL, KEY_ALIAS),
}

NODE_UNIQUE_CONSTRAINTS: dict[str, str] = {
    label.value: key.value for label, key in _NODE_LABEL_UNIQUE_KEYS.items()
}

# Generated Cypher overwhelmingly filters on bare `name` ("who calls X" →
# WHERE f.name = 'X'); without a label+name index every such query is a full
# label scan. Labels whose unique key already IS `name` are covered by the
# constraint indexes derived from NODE_UNIQUE_CONSTRAINTS.
NODE_NAME_INDEXES: tuple[str, ...] = tuple(
    label.value
    for label, key in _NODE_LABEL_UNIQUE_KEYS.items()
    if key is not UniqueKeyType.NAME
)

# Superseded unique constraints that must be dropped from existing shared
# databases; a leftover Folder/File path constraint would keep rejecting the
# second same-relative-path node the fix now creates (issue #897).
LEGACY_NODE_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    (NodeLabel.FOLDER.value, UniqueKeyType.PATH.value),
    (NodeLabel.FILE.value, UniqueKeyType.PATH.value),
)

CYPHER_MEMORY_LIMIT_SUFFIX = " QUERY MEMORY LIMIT {mb} MB"
CYPHER_MEMORY_LIMIT_TOKEN = "QUERY MEMORY LIMIT"
