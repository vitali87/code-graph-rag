from enum import StrEnum

from .core import _CYPHER_EMBEDDING_BASE

KEY_RELATIONSHIPS = "relationships"
KEY_NODE_ID = "node_id"
KEY_LABELS = "labels"
KEY_LABEL = "label"
KEY_PROPERTIES = "properties"
KEY_FROM_ID = "from_id"
KEY_TO_ID = "to_id"
KEY_TYPE = "type"
KEY_METADATA = "metadata"
KEY_TOTAL_RELATIONSHIPS = "total_relationships"
KEY_NODE_LABELS = "node_labels"
KEY_RELATIONSHIP_TYPES = "relationship_types"
KEY_EXPORTED_AT = "exported_at"
KEY_PARSER = "parser"
KEY_NAME = "name"
KEY_QUALIFIED_NAME = "qualified_name"
KEY_IS_PROPERTY = "is_property"
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
KEY_VERSION_SPEC = "version_spec"
KEY_PREFIX = "prefix"
KEY_PROJECT_NAME = "project_name"

# (H) Protobuf oneof field names
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

# (H) Trie internal keys
TRIE_TYPE_KEY = "__type__"
TRIE_QN_KEY = "__qn__"
TRIE_INTERNAL_PREFIX = "__"


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


NODE_PROJECT = NodeLabel.PROJECT

# (H) Property keys
KEY_PARAMETERS = "parameters"
KEY_DECORATORS = "decorators"
KEY_DOCSTRING = "docstring"
KEY_IS_EXPORTED = "is_exported"

# (H) Cypher queries
CYPHER_DEFAULT_LIMIT = 50

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
# (H) Removes external import-target Module nodes that no module imports anymore
# (H) (e.g. an imported name that was renamed/removed on an incremental rebuild).
CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES = (
    "MATCH (m:ExternalModule) WHERE NOT (m)<--() DETACH DELETE m"
)

# (H) Queries for orphan pruning — returns all paths stored in the graph
CYPHER_ALL_FILE_PATHS = (
    "MATCH (f:File) RETURN f.path AS path, f.absolute_path AS absolute_path"
)
CYPHER_ALL_MODULE_PATHS_INTERNAL = (
    "MATCH (m:Module) RETURN m.path AS path, m.qualified_name AS qualified_name"
)
CYPHER_ALL_FOLDER_PATHS = (
    "MATCH (f:Folder) RETURN f.path AS path, f.absolute_path AS absolute_path"
)

# (H) Rehydrate the in-memory function registry on an incremental run: returns
# (H) every definition node's qualified name and label so call/instantiation
# (H) resolution can see symbols defined in files that were not re-parsed.
CYPHER_ALL_DEFINITION_QNS = (
    "MATCH (n) WHERE n:Function OR n:Method OR n:Class OR n:Interface "
    "OR n:Enum OR n:Type OR n:Union "
    "RETURN n.qualified_name AS qualified_name, head(labels(n)) AS label, "
    "n.is_property AS is_property"
)

# (H) Inbound reference edges (from unchanged files) into symbols defined in one
# (H) of $paths. Captured BEFORE a changed file's subtree is deleted so the exact
# (H) edges can be restored verbatim afterwards (issue #532, inbound half).
# (H) Re-resolving the callers instead would diverge from a clean index, because
# (H) cgr's call resolution is context-sensitive (protocol vs concrete receiver,
# (H) import granularity); the original edges already match a clean re-index.
CYPHER_INBOUND_EDGES = (
    "MATCH (caller)-[r:CALLS|REFERENCES|INSTANTIATES|IMPORTS|INHERITS|OVERRIDES]->(target) "
    "WHERE target.path IN $paths AND caller.qualified_name IS NOT NULL "
    "AND (caller.path IS NULL OR NOT caller.path IN $paths) "
    "RETURN head(labels(caller)) AS caller_label, "
    "caller.qualified_name AS caller_qn, type(r) AS rel, "
    "head(labels(target)) AS target_label, target.qualified_name AS target_qn"
)
# (H) Rehydrate class_inheritance on an incremental run: every INHERITS edge
# (H) (child -> base) with resolved qns, so protocol dispatch and inherited-method
# (H) resolution still see the hierarchy of classes defined in files that were not
# (H) re-parsed. Without it, editing a caller drops the protocol/inheritance
# (H) redirect (issue #532 residual): a call resolves to the Protocol stub instead
# (H) of the concrete implementer because _protocol_classes() is empty. Ordered by
# (H) base_index so multiple-inheritance base order matches the original source,
# (H) which method resolution and override attribution depend on.
CYPHER_ALL_INHERITS = (
    "MATCH (child)-[r:INHERITS]->(base) "
    "WHERE child.qualified_name IS NOT NULL AND base.qualified_name IS NOT NULL "
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

NODE_UNIQUE_CONSTRAINTS: dict[str, str] = {
    label.value: key.value for label, key in _NODE_LABEL_UNIQUE_KEYS.items()
}

# (H) Cypher response cleaning
CYPHER_PREFIX = "cypher"
CYPHER_SEMICOLON = ";"
CYPHER_BACKTICK = "`"
CYPHER_MATCH_KEYWORD = "MATCH"
CYPHER_DANGEROUS_KEYWORDS: frozenset[str] = frozenset(
    {
        "DELETE",
        "DETACH",
        "DROP",
        "CREATE INDEX",
        "CREATE CONSTRAINT",
        "REMOVE",
        "SET",
        "MERGE",
        "CREATE",
        "LOAD CSV",
        "FOREACH",
    }
)

CYPHER_ALLOWED_PROCEDURE_PREFIXES: frozenset[str] = frozenset(
    {
        "algo.",
        "betweenness_centrality.",
        "biconnected_components.",
        "bridges.",
        "community_detection.",
        "cycles.",
        "degree_centrality.",
        "graph_analyzer.",
        "graph_util.",
        "igraphalg.",
        "katz_centrality.",
        "leiden_community_detection.",
        "neighbors.",
        "node_similarity.",
        "nxalg.",
        "pagerank.",
        "path.",
        "schema.",
        "weakly_connected_components.",
        "wcc.",
    }
)
CYPHER_MEMORY_LIMIT_SUFFIX = " QUERY MEMORY LIMIT {mb} MB"
CYPHER_MEMORY_LIMIT_TOKEN = "QUERY MEMORY LIMIT"
