from enum import StrEnum

KEY_NODES = "nodes"
KEY_TOTAL_NODES = "total_nodes"
CLI_STATS_TOTAL_NODES = "Total Nodes"

# (H) ModelConfig field names
FIELD_PROVIDER = "provider"
FIELD_MODEL_ID = "model_id"
FIELD_API_KEY = "api_key"
FIELD_ENDPOINT = "endpoint"

# (H) JS/TS specific node types
JS_TS_FUNCTION_NODES = (
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
)
JS_TS_CLASS_NODES = ("class_declaration", "class")
JS_TS_IMPORT_NODES = (
    "import_statement",
    "lexical_declaration",
    "variable_declaration",
    "export_statement",
)

# (H) C++ import node types
CPP_IMPORT_NODES = ("preproc_include", "template_function", "declaration")

# (H) Index file names
INDEX_INIT = "__init__"
INDEX_INDEX = "index"
INDEX_MOD = "mod"

# (H) AST field names for name extraction
NAME_FIELDS = ("identifier", "name", "id")

# (H) Tree-sitter field name constants for child_by_field_name
FIELD_OBJECT = "object"
FIELD_PROPERTY = "property"
FIELD_NAME = "name"
FIELD_ALIAS = "alias"
FIELD_MODULE_NAME = "module_name"
FIELD_ARGUMENTS = "arguments"
FIELD_BODY = "body"
FIELD_RETURN_TYPE = "return_type"
FIELD_CONSTRUCTOR = "constructor"
FIELD_DECLARATOR = "declarator"
FIELD_PARAMETERS = "parameters"
FIELD_RECEIVER = "receiver"
FIELD_TYPE = "type"
# (H) The wrapped function/class inside a Python decorated_definition node.
FIELD_DEFINITION = "definition"
FIELD_RESULT = "result"
# (H) Rust impl `trait`/`type` fields and a trait's supertrait `bounds`.
FIELD_TRAIT = "trait"
FIELD_BOUNDS = "bounds"
TS_RS_TRAIT_BOUNDS = "trait_bounds"
FIELD_VALUE = "value"
FIELD_LEFT = "left"
FIELD_RIGHT = "right"
FIELD_FIELD = "field"
FIELD_SCOPE = "scope"
FIELD_SUPERCLASS = "superclass"
FIELD_SUPERCLASSES = "superclasses"
FIELD_INTERFACES = "interfaces"

# (H) Method name constants for getattr/hasattr
METHOD_FIND_WITH_PREFIX = "find_with_prefix"
METHOD_ITEMS = "items"

# (H) Parser loader paths and args
GRAMMARS_DIR = "grammars"
TREE_SITTER_PREFIX = "tree-sitter-"
TREE_SITTER_MODULE_PREFIX = "tree_sitter_"
BINDINGS_DIR = "bindings"
SETUP_PY = "setup.py"
BUILD_EXT_CMD = "build_ext"
INPLACE_FLAG = "--inplace"
LANG_ATTR_PREFIX = "language_"
LANG_ATTR_TYPESCRIPT = "language_typescript"
LANG_ATTR_TSX = "language_tsx"
LANG_ATTR_PHP = "language_php"


class TreeSitterModule(StrEnum):
    PYTHON = "tree_sitter_python"
    JS = "tree_sitter_javascript"
    TS = "tree_sitter_typescript"
    RUST = "tree_sitter_rust"
    GO = "tree_sitter_go"
    SCALA = "tree_sitter_scala"
    JAVA = "tree_sitter_java"
    C = "tree_sitter_c"
    CPP = "tree_sitter_cpp"
    LUA = "tree_sitter_lua"
    PHP = "tree_sitter_php"


# (H) Query dict keys
QUERY_FUNCTIONS = "functions"
QUERY_CLASSES = "classes"
QUERY_CALLS = "calls"
QUERY_IMPORTS = "imports"
QUERY_LOCALS = "locals"
QUERY_CONFIG = "config"
QUERY_LANGUAGE = "language"

# (H) Query capture names
CAPTURE_FUNCTION = "function"
CAPTURE_CLASS = "class"
CAPTURE_CALL = "call"
CAPTURE_IMPORT = "import"
CAPTURE_IMPORT_FROM = "import_from"

# (H) Locals query patterns for JS/TS
JS_LOCALS_PATTERN = """
; Variable definitions
(variable_declarator name: (identifier) @local.definition)
(function_declaration name: (identifier) @local.definition)
(class_declaration name: (identifier) @local.definition)

; Variable references
(identifier) @local.reference
"""

TS_LOCALS_PATTERN = """
; Variable definitions (TypeScript has multiple declaration types)
(variable_declarator name: (identifier) @local.definition)
(lexical_declaration (variable_declarator name: (identifier) @local.definition))
(variable_declaration (variable_declarator name: (identifier) @local.definition))

; Function definitions
(function_declaration name: (identifier) @local.definition)

; Class definitions (uses type_identifier for class names)
(class_declaration name: (type_identifier) @local.definition)

; Variable references
(identifier) @local.reference
"""

# (H) Query tool messages
QUERY_NOT_AVAILABLE = "N/A"
QUERY_SUMMARY_SUCCESS = "Successfully retrieved {count} item(s) from the graph."
QUERY_SUMMARY_TRUNCATED = (
    "Results truncated: showing {kept} of {total} items (~{tokens} tokens, limit {max_tokens}). "
    "Refine your query for more specific results."
)
QUERY_SUMMARY_TRANSLATION_FAILED = (
    "I couldn't translate your request into a database query. Error: {error}"
)
QUERY_SUMMARY_DB_ERROR = "There was an error querying the database: {error}"
QUERY_SUMMARY_TIMEOUT = (
    "Query exceeded the {timeout:.1f}s timeout and was cancelled. "
    "Avoid unbounded traversals; add depth bounds or use a graph-algorithm procedure."
)
QUERY_RESULTS_PANEL_TITLE = "[bold blue]Cypher Query Results[/bold blue]"

# (H) Language CLI default node types
LANG_DEFAULT_FUNCTION_NODES = ("function_definition", "method_definition")
LANG_DEFAULT_CLASS_NODES = ("class_declaration",)
LANG_DEFAULT_MODULE_NODES = ("compilation_unit",)
LANG_DEFAULT_CALL_NODES = ("invocation_expression",)

LANG_MSG_AVAILABLE_NODES = "Available nodes for mapping:"
FIELD_OPERAND = "operand"

FIELD_OPERATOR = "operator"

QUERY_CAPTURE_CLASS = "class"
QUERY_CAPTURE_FUNCTION = "function"
QUERY_KEY_CLASSES = "classes"
QUERY_KEY_FUNCTIONS = "functions"

# (H) JS/TS ingest query capture names
CAPTURE_CHILD_CLASS = "child_class"
CAPTURE_PARENT_CLASS = "parent_class"
CAPTURE_CONSTRUCTOR_NAME = "constructor_name"
CAPTURE_PROTOTYPE_KEYWORD = "prototype_keyword"
CAPTURE_METHOD_NAME = "method_name"
CAPTURE_METHOD_FUNCTION = "method_function"
CAPTURE_MEMBER_EXPR = "member_expr"
CAPTURE_FUNCTION_EXPR = "function_expr"
CAPTURE_ARROW_FUNCTION = "arrow_function"

# (H) Tree-sitter field names for module system
FIELD_FUNCTION = "function"
FIELD_KEY = "key"

# (H) Query capture names for module system
CAPTURE_FUNC = "func"
CAPTURE_VARIABLE_DECLARATOR = "variable_declarator"
CAPTURE_EXPORTS_OBJ = "exports_obj"
CAPTURE_MODULE_OBJ = "module_obj"
CAPTURE_EXPORTS_PROP = "exports_prop"
CAPTURE_EXPORT_NAME = "export_name"
CAPTURE_EXPORT_FUNCTION = "export_function"
