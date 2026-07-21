# Graph schema: node labels, relationships, keys, and Cypher queries.

from enum import StrEnum

KEY_NODES = "nodes"
KEY_RELATIONSHIPS = "relationships"
KEY_NODE_ID = "node_id"
KEY_LABELS = "labels"
KEY_LABEL = "label"
KEY_PROPERTIES = "properties"
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
KEY_END_LINE = "end_line"
KEY_PATH = "path"
KEY_ABSOLUTE_PATH = "absolute_path"
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

ERR_SUBSTR_ALREADY_EXISTS = "already exists"
ERR_SUBSTR_CONSTRAINT = "constraint"

PROTOBUF_INDEX_FILE = "index.bin"
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


class UniqueKeyType(StrEnum):
    NAME = KEY_NAME
    PATH = KEY_PATH
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
    # ast-grep findings (issue #413): quality/security signals attached to a
    # Module. Opt-in via CaptureGroup.FINDINGS.
    PATTERN = "Pattern"
    CODE_SMELL = "CodeSmell"
    SECURITY_ISSUE = "SecurityIssue"


_NODE_LABEL_UNIQUE_KEYS: dict[NodeLabel, UniqueKeyType] = {
    NodeLabel.PROJECT: UniqueKeyType.NAME,
    NodeLabel.PACKAGE: UniqueKeyType.QUALIFIED_NAME,
    NodeLabel.FOLDER: UniqueKeyType.PATH,
    NodeLabel.FILE: UniqueKeyType.PATH,
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
    DEFINES = "DEFINES"
    DEFINES_METHOD = "DEFINES_METHOD"
    IMPORTS = "IMPORTS"
    EXPORTS = "EXPORTS"
    EXPORTS_MODULE = "EXPORTS_MODULE"
    IMPLEMENTS_MODULE = "IMPLEMENTS_MODULE"
    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    OVERRIDES = "OVERRIDES"
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
        }
    ),
    CaptureGroup.IMPORTS: frozenset(
        {
            RelationshipType.IMPORTS,
            RelationshipType.EXPORTS,
            RelationshipType.EXPORTS_MODULE,
            RelationshipType.DEPENDS_ON_EXTERNAL,
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
KEY_DECORATORS = "decorators"
KEY_MODIFIERS = "modifiers"
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
    "MATCH (m:Module {path: $path}) "
    "OPTIONAL MATCH (m)-[:DEFINES|DEFINES_METHOD*0..]->(c) "
    "DETACH DELETE m, c"
)
CYPHER_DELETE_FILE = "MATCH (f:File {path: $path}) DETACH DELETE f"
CYPHER_DELETE_FOLDER = "MATCH (f:Folder {path: $path}) DETACH DELETE f"
CYPHER_DELETE_CALLS = "MATCH ()-[r:CALLS]->() DELETE r"
# Removes external import-target Module nodes that no module imports anymore
# (e.g. an imported name that was renamed/removed on an incremental rebuild).
CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES = (
    "MATCH (m:ExternalModule) WHERE NOT (m)<--() DETACH DELETE m"
)
# Removes shared (prefix-less) Resource nodes that no code node points at
# anymore. Resource-to-Resource edges (RESOLVES_TO, FLOWS_TO) are derived
# and must not keep a node alive on their own.
CYPHER_DELETE_UNANCHORED_RESOURCES = (
    "MATCH (r:Resource) OPTIONAL MATCH (s)-[]->(r) "
    "WITH r, sum(CASE WHEN s IS NULL OR s:Resource THEN 0 ELSE 1 END) AS anchors "
    "WHERE anchors = 0 DETACH DELETE r"
)

# Queries for orphan pruning: return all paths stored in the graph
CYPHER_ALL_FILE_PATHS = (
    "MATCH (f:File) RETURN f.path AS path, f.absolute_path AS absolute_path"
)
CYPHER_ALL_MODULE_PATHS_INTERNAL = (
    "MATCH (m:Module) RETURN m.path AS path, m.qualified_name AS qualified_name"
)
CYPHER_ALL_FOLDER_PATHS = (
    "MATCH (f:Folder) RETURN f.path AS path, f.absolute_path AS absolute_path"
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
    "n.start_line AS start_line, n.end_line AS end_line"
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
CYPHER_INBOUND_EDGES = (
    "MATCH (caller)-[r:CALLS|REFERENCES|INSTANTIATES|IMPORTS|INHERITS|OVERRIDES]->(target) "
    "WHERE target.path IN $paths AND caller.qualified_name IS NOT NULL "
    "AND (caller.path IS NULL OR NOT caller.path IN $paths) "
    "RETURN head(labels(caller)) AS caller_label, "
    "caller.qualified_name AS caller_qn, type(r) AS rel, "
    "head(labels(target)) AS target_label, target.qualified_name AS target_qn"
)
# Rehydrate class_inheritance on an incremental run: every INHERITS edge
# (child -> base) with resolved qns, so protocol dispatch and inherited-method
# resolution still see the hierarchy of classes defined in files that were not
# re-parsed. Without it, editing a caller drops the protocol/inheritance
# redirect (issue #532 residual): a call resolves to the Protocol stub instead
# of the concrete implementer because _protocol_classes() is empty. Ordered by
# base_index so multiple-inheritance base order matches the original source,
# which method resolution and override attribution depend on.
CYPHER_ALL_INHERITS = (
    "MATCH (child)-[r:INHERITS]->(base) "
    "WHERE child.qualified_name IS NOT NULL AND base.qualified_name IS NOT NULL "
    "AND child.qualified_name STARTS WITH $project_prefix "
    "RETURN child.qualified_name AS child_qn, base.qualified_name AS base_qn, "
    "r.base_index AS base_index "
    "ORDER BY child_qn, base_index"
)
KEY_CHILD_QN = "child_qn"
KEY_BASE_QN = "base_qn"
KEY_BASE_INDEX = "base_index"

CYPHER_PARAM_PATHS = "paths"
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
}

NODE_UNIQUE_CONSTRAINTS: dict[str, str] = {
    label.value: key.value for label, key in _NODE_LABEL_UNIQUE_KEYS.items()
}

CYPHER_MEMORY_LIMIT_SUFFIX = " QUERY MEMORY LIMIT {mb} MB"
CYPHER_MEMORY_LIMIT_TOKEN = "QUERY MEMORY LIMIT"
