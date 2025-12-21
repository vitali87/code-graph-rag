from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .types_defs import PyInstallerPackage


class ModelRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CYPHER = "cypher"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"


class Color(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    CYAN = "cyan"
    RED = "red"
    MAGENTA = "magenta"


class KeyBinding(StrEnum):
    CTRL_J = "c-j"
    ENTER = "enter"
    CTRL_C = "c-c"


class StyleModifier(StrEnum):
    BOLD = "bold"
    DIM = "dim"
    NONE = ""


DEFAULT_REGION = "us-central1"
DEFAULT_MODEL = "llama3.2"
DEFAULT_API_KEY = "ollama"

UNIXCODER_MODEL = "microsoft/unixcoder-base"
SEMANTIC_EXTRA_ERROR = (
    "Semantic search requires 'semantic' extra: uv sync --extra semantic"
)
DEFAULT_MAX_LENGTH = 512

KEY_NODES = "nodes"
KEY_RELATIONSHIPS = "relationships"
KEY_NODE_ID = "node_id"
KEY_LABELS = "labels"
KEY_PROPERTIES = "properties"
KEY_FROM_ID = "from_id"
KEY_TO_ID = "to_id"
KEY_TYPE = "type"
KEY_METADATA = "metadata"
KEY_TOTAL_NODES = "total_nodes"
KEY_TOTAL_RELATIONSHIPS = "total_relationships"
KEY_NODE_LABELS = "node_labels"
KEY_RELATIONSHIP_TYPES = "relationship_types"
KEY_PARSER = "parser"
KEY_QUALIFIED_NAME = "qualified_name"
KEY_START_LINE = "start_line"
KEY_END_LINE = "end_line"
KEY_PATH = "path"

# (H) File names
INIT_PY = "__init__.py"

# (H) Encoding
ENCODING_UTF8 = "utf-8"

# (H) Error messages
ERR_GRAPH_FILE_NOT_FOUND = "Graph file not found: {path}"
ERR_FAILED_TO_LOAD_DATA = "Failed to load data from file"
ERR_NODES_NOT_LOADED = "Nodes should be loaded"
ERR_RELATIONSHIPS_NOT_LOADED = "Relationships should be loaded"
ERR_DATA_NOT_LOADED = "Data should be loaded"
ERR_PROVIDER_EMPTY = "Provider name cannot be empty in 'provider:model' format."
ERR_BATCH_SIZE_POSITIVE = "batch_size must be a positive integer"
ERR_UNEXPECTED = "An unexpected error occurred: {error}"
ERR_EXPORT_FAILED = "Failed to export graph: {error}"
ERR_EXPORT_ERROR = "Export error: {error}"
ERR_CONFIG = "{role} configuration error: {error}"
ERR_PATH_NOT_IN_QUESTION = (
    "Could not find original path in question for replacement: {path}"
)
ERR_IMAGE_NOT_FOUND = "Image path found, but does not exist: {path}"
ERR_IMAGE_COPY_FAILED = "Failed to copy image to temporary directory: {error}"

LOG_LOADING_GRAPH = "Loading graph from {path}"
LOG_LOADED_GRAPH = "Loaded {nodes} nodes and {relationships} relationships with indexes"
LOG_ENSURING_PROJECT = "Ensuring Project: {name}"
LOG_PASS_1_STRUCTURE = "--- Pass 1: Identifying Packages and Folders ---"
LOG_PASS_2_FILES = (
    "\n--- Pass 2: Processing Files, Caching ASTs, and Collecting Definitions ---"
)
LOG_PASS_3_CALLS = "--- Pass 3: Processing Function Calls from AST Cache ---"
LOG_PASS_4_EMBEDDINGS = "--- Pass 4: Generating semantic embeddings ---"
LOG_FOUND_FUNCTIONS = "\n--- Found {count} functions/methods in codebase ---"
LOG_ANALYSIS_COMPLETE = "\n--- Analysis complete. Flushing all data to database... ---"
LOG_REMOVING_STATE = "Removing in-memory state for: {path}"
LOG_REMOVED_FROM_CACHE = "  - Removed from ast_cache"
LOG_REMOVING_QNS = "  - Removing {count} QNs from function_registry"
LOG_CLEANED_SIMPLE_NAME = "  - Cleaned simple_name '{name}'"
LOG_SEMANTIC_NOT_AVAILABLE = (
    "Semantic search dependencies not available, skipping embedding generation"
)
LOG_INGESTOR_NO_QUERY = (
    "Ingestor does not support querying, skipping embedding generation"
)
LOG_NO_FUNCTIONS_FOR_EMBEDDING = (
    "No functions or methods found for embedding generation"
)
LOG_GENERATING_EMBEDDINGS = "Generating embeddings for {count} functions/methods"
LOG_EMBEDDING_PROGRESS = "Generated {done}/{total} embeddings"
LOG_EMBEDDING_FAILED = "Failed to embed {name}: {error}"
LOG_NO_SOURCE_FOR = "No source code found for {name}"
LOG_EMBEDDINGS_COMPLETE = "Successfully generated {count} semantic embeddings"
LOG_EMBEDDING_GENERATION_FAILED = "Failed to generate semantic embeddings: {error}"
LOG_IMAGE_COPIED = "Copied image to temporary path: {path}"

# (H) CLI error and info messages
CLI_ERR_OUTPUT_REQUIRES_UPDATE = (
    "Error: --output/-o option requires --update-graph to be specified."
)
CLI_ERR_ONLY_JSON = "Error: Currently only JSON format is supported."
CLI_ERR_STARTUP = "Startup Error: {error}"
CLI_ERR_CONFIG = "Configuration Error: {error}"
CLI_ERR_INDEXING = "An error occurred during indexing: {error}"
CLI_ERR_EXPORT_FAILED = "Failed to export graph: {error}"
CLI_ERR_LOAD_GRAPH = "Failed to load graph: {error}"
CLI_ERR_MCP_SERVER = "MCP Server Error: {error}"

CLI_MSG_UPDATING_GRAPH = "Updating knowledge graph for: {path}"
CLI_MSG_CLEANING_DB = "Cleaning database..."
CLI_MSG_EXPORTING_TO = "Exporting graph to: {path}"
CLI_MSG_GRAPH_UPDATED = "Graph update completed!"
CLI_MSG_APP_TERMINATED = "\nApplication terminated by user."
CLI_MSG_INDEXING_AT = "Indexing codebase at: {path}"
CLI_MSG_OUTPUT_TO = "Output will be written to: {path}"
CLI_MSG_INDEXING_DONE = "Indexing process completed successfully!"
CLI_MSG_CONNECTING_MEMGRAPH = "Connecting to Memgraph to export graph..."
CLI_MSG_EXPORTING_DATA = "Exporting graph data..."
CLI_MSG_OPTIMIZATION_TERMINATED = "\nOptimization session terminated by user."
CLI_MSG_MCP_TERMINATED = "\nMCP server terminated by user."
CLI_MSG_HINT_TARGET_REPO = (
    "\nHint: Make sure TARGET_REPO_PATH environment variable is set."
)
CLI_MSG_GRAPH_SUMMARY = "Graph Summary:"

UI_DIFF_FILE_HEADER = "[bold cyan]File: {path}[/bold cyan]"
UI_NEW_FILE_HEADER = "[bold cyan]New file: {path}[/bold cyan]"
UI_SHELL_COMMAND_HEADER = "[bold cyan]Shell command:[/bold cyan]"
UI_TOOL_APPROVAL = "[bold yellow]⚠️  Tool '{tool_name}' requires approval:[/bold yellow]"
UI_FEEDBACK_PROMPT = (
    "[bold yellow]Feedback (why rejected, or press Enter to skip)[/bold yellow]"
)
UI_OPTIMIZATION_START = (
    "[bold green]Starting {language} optimization session...[/bold green]"
)
UI_OPTIMIZATION_PANEL = (
    "[bold yellow]The agent will analyze your codebase{document_info} and propose specific optimizations."
    " You'll be asked to approve each suggestion before implementation."
    " Type 'exit' or 'quit' to end the session.[/bold yellow]"
)
UI_OPTIMIZATION_INIT = "[bold cyan]Initializing optimization session for {language} codebase: {path}[/bold cyan]"
UI_GRAPH_EXPORT_SUCCESS = (
    "[bold green]Graph exported successfully to: {path}[/bold green]"
)
UI_GRAPH_EXPORT_STATS = "[bold cyan]Export contains {nodes} nodes and {relationships} relationships[/bold cyan]"
UI_ERR_UNEXPECTED = "[bold red]An unexpected error occurred: {error}[/bold red]"
UI_ERR_EXPORT_FAILED = "[bold red]Failed to export graph: {error}[/bold red]"
UI_TOOL_ARGS_FORMAT = "    Arguments: {args}"
UI_REFERENCE_DOC_INFO = " using the reference document: {reference_document}"
UI_INPUT_PROMPT_HTML = (
    "<ansigreen><b>{prompt}</b></ansigreen> <ansiyellow>{hint}</ansiyellow>: "
)

# (H) ModelConfig field names
FIELD_PROVIDER = "provider"
FIELD_MODEL_ID = "model_id"
FIELD_API_KEY = "api_key"
FIELD_ENDPOINT = "endpoint"

# (H) Tool argument field names
ARG_TARGET_CODE = "target_code"
ARG_REPLACEMENT_CODE = "replacement_code"
ARG_FILE_PATH = "file_path"
ARG_CONTENT = "content"
ARG_COMMAND = "command"

# (H) Qualified name separators
SEPARATOR_DOT = "."

# (H) Trie internal keys
TRIE_TYPE_KEY = "__type__"
TRIE_QN_KEY = "__qn__"
TRIE_INTERNAL_PREFIX = "__"


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
    DEPENDS_ON_EXTERNAL = "DEPENDS_ON_EXTERNAL"


NODE_PROJECT = NodeLabel.PROJECT

# (H) Cache defaults
DEFAULT_CACHE_ENTRIES = 1000
DEFAULT_CACHE_MEMORY_MB = 500
EMBEDDING_PROGRESS_INTERVAL = 10
BYTES_PER_MB = 1024 * 1024
CACHE_EVICTION_DIVISOR = 10
CACHE_MEMORY_THRESHOLD_RATIO = 0.8

# (H) Property keys
KEY_NAME = "name"

# (H) Dependency files
DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "composer.json",
    }
)
CSPROJ_SUFFIX = ".csproj"

# (H) Cypher queries
CYPHER_QUERY_EMBEDDINGS = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE n:Function OR n:Method
RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
       n.start_line AS start_line, n.end_line AS end_line,
       m.path AS path
ORDER BY n.qualified_name
"""


class SupportedLanguage(StrEnum):
    PYTHON = "python"
    JS = "javascript"
    TS = "typescript"
    RUST = "rust"
    GO = "go"
    SCALA = "scala"
    JAVA = "java"
    CPP = "cpp"
    CSHARP = "c-sharp"
    PHP = "php"
    LUA = "lua"


# (H) Tree-sitter AST node type constants
FUNCTION_NODES_BASIC = ("function_declaration", "function_definition")
FUNCTION_NODES_LAMBDA = (
    "lambda_expression",
    "arrow_function",
    "anonymous_function",
    "closure_expression",
)
FUNCTION_NODES_METHOD = (
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
)
FUNCTION_NODES_TEMPLATE = (
    "template_declaration",
    "function_signature_item",
    "function_signature",
)
FUNCTION_NODES_GENERATOR = ("generator_function_declaration", "function_expression")

CLASS_NODES_BASIC = ("class_declaration", "class_definition")
CLASS_NODES_STRUCT = ("struct_declaration", "struct_specifier", "struct_item")
CLASS_NODES_INTERFACE = ("interface_declaration", "trait_declaration", "trait_item")
CLASS_NODES_ENUM = ("enum_declaration", "enum_item", "enum_specifier")
CLASS_NODES_TYPE_ALIAS = ("type_alias_declaration", "type_item")
CLASS_NODES_UNION = ("union_specifier", "union_item")

CALL_NODES_BASIC = ("call_expression", "function_call")
CALL_NODES_METHOD = (
    "method_invocation",
    "member_call_expression",
    "field_expression",
)
CALL_NODES_OPERATOR = ("binary_expression", "unary_expression", "update_expression")
CALL_NODES_SPECIAL = ("new_expression", "delete_expression", "macro_invocation")

IMPORT_NODES_STANDARD = ("import_declaration", "import_statement")
IMPORT_NODES_FROM = ("import_from_statement",)
IMPORT_NODES_MODULE = ("lexical_declaration", "export_statement")
IMPORT_NODES_INCLUDE = ("preproc_include",)
IMPORT_NODES_USING = ("using_directive",)

# (H) JS/TS specific node types
JS_TS_FUNCTION_NODES = (
    "function_declaration",
    "generator_function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
)
JS_TS_CLASS_NODES = ("class_declaration", "class")
JS_TS_IMPORT_NODES = ("import_statement", "lexical_declaration", "export_statement")

# (H) C++ import node types
CPP_IMPORT_NODES = ("preproc_include", "template_function", "declaration")

# (H) Index file names
INDEX_INIT = "__init__"
INDEX_INDEX = "index"
INDEX_MOD = "mod"

# (H) AST field names for name extraction
NAME_FIELDS = ("identifier", "name", "id")

# (H) Image file extensions for chat image handling
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif")

# (H) CLI exit commands
EXIT_COMMANDS = frozenset({"exit", "quit"})

# (H) UI separators and formatting
HORIZONTAL_SEPARATOR = "─" * 60

# (H) Session log header
SESSION_LOG_HEADER = "=== CODE-GRAPH RAG SESSION LOG ===\n\n"

# (H) Logger format
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}"

# (H) Temporary directory
TMP_DIR = ".tmp"
SESSION_LOG_PREFIX = "session_"
SESSION_LOG_EXT = ".log"

# (H) Session log prefixes
SESSION_PREFIX_USER = "USER: "
SESSION_PREFIX_ASSISTANT = "ASSISTANT: "

# (H) Session context format
SESSION_CONTEXT_START = (
    "\n\n[SESSION CONTEXT - Previous conversation in this session]:\n"
)
SESSION_CONTEXT_END = "\n[END SESSION CONTEXT]\n\n"

# (H) Confirmation status display
CONFIRM_ENABLED = "Enabled"
CONFIRM_DISABLED = "Disabled (YOLO Mode)"

# (H) Diff labels
DIFF_LABEL_BEFORE = "before"
DIFF_LABEL_AFTER = "after"
DIFF_FALLBACK_PATH = "file"


class DiffMarker:
    ADD = "+"
    DEL = "-"
    HUNK = "@"
    HEADER_ADD = "+++"
    HEADER_DEL = "---"


# (H) Table column headers
TABLE_COL_CONFIGURATION = "Configuration"
TABLE_COL_VALUE = "Value"

# (H) Table row labels
TABLE_ROW_TARGET_LANGUAGE = "Target Language"
TABLE_ROW_ORCHESTRATOR_MODEL = "Orchestrator Model"
TABLE_ROW_CYPHER_MODEL = "Cypher Model"
TABLE_ROW_OLLAMA_ENDPOINT = "Ollama Endpoint"
TABLE_ROW_OLLAMA_ORCHESTRATOR = "Ollama Endpoint (Orchestrator)"
TABLE_ROW_OLLAMA_CYPHER = "Ollama Endpoint (Cypher)"
TABLE_ROW_EDIT_CONFIRMATION = "Edit Confirmation"
TABLE_ROW_TARGET_REPOSITORY = "Target Repository"

# (H) UI status messages
MSG_CONNECTED_MEMGRAPH = "Successfully connected to Memgraph."
MSG_THINKING_CANCELLED = "Thinking cancelled."
MSG_TIMEOUT_FORMAT = "Operation timed out after {timeout} seconds."
MSG_CHAT_INSTRUCTIONS = (
    "Ask questions about your codebase graph. Type 'exit' or 'quit' to end."
)

# (H) Default titles and prompts
DEFAULT_TABLE_TITLE = "Graph-Code Initializing..."
OPTIMIZATION_TABLE_TITLE = "Optimization Session Configuration"
PROMPT_ASK_QUESTION = "Ask a question"
PROMPT_YOUR_RESPONSE = "Your response"
MULTILINE_INPUT_HINT = "(Press Ctrl+J to submit, Enter for new line)"

# (H) JSON formatting
JSON_INDENT = 2

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

# (H) Parser loader log messages
LOG_BUILDING_BINDINGS = "Building Python bindings for {lang}..."
LOG_BUILD_FAILED = "Failed to build {lang} bindings: stdout={stdout}, stderr={stderr}"
LOG_BUILD_SUCCESS = "Successfully built {lang} bindings"
LOG_IMPORTING_MODULE = "Attempting to import module: {module}"
LOG_LOADED_FROM_SUBMODULE = (
    "Successfully loaded {lang} from submodule bindings using {attr}"
)
LOG_NO_LANG_ATTR = (
    "Module {module} imported but has no language attribute. Available: {available}"
)
LOG_SUBMODULE_LOAD_FAILED = "Failed to load {lang} from submodule bindings: {error}"
LOG_LIB_NOT_AVAILABLE = "Tree-sitter library for {lang} not available."
LOG_LOCALS_QUERY_FAILED = "Failed to create locals query for {lang}: {error}"
LOG_GRAMMAR_LOADED = "Successfully loaded {lang} grammar."
LOG_GRAMMAR_LOAD_FAILED = "Failed to load {lang} grammar: {error}"
LOG_INITIALIZED_PARSERS = "Initialized parsers for: {languages}"
ERR_NO_LANGUAGES = "No Tree-sitter languages available."

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

# (H) File/directory ignore patterns
IGNORE_PATTERNS = frozenset(
    {
        ".git",
        "venv",
        ".venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".eggs",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".claude",
        ".idea",
        ".vscode",
    }
)
IGNORE_SUFFIXES = frozenset({".tmp", "~"})

PAYLOAD_NODE_ID = "node_id"
PAYLOAD_QUALIFIED_NAME = "qualified_name"

LOG_EMBEDDING_STORE_FAILED = "Failed to store embedding for {name}: {error}"
LOG_EMBEDDING_SEARCH_FAILED = "Failed to search embeddings: {error}"


class EventType(StrEnum):
    MODIFIED = "modified"
    CREATED = "created"


CYPHER_DELETE_MODULE = "MATCH (m:Module {path: $path})-[*0..]->(c) DETACH DELETE m, c"
CYPHER_DELETE_CALLS = "MATCH ()-[r:CALLS]->() DELETE r"

LOG_WATCHER_ACTIVE = "File watcher is now active."
LOG_WATCHER_SKIP_NO_QUERY = (
    "Ingestor does not support querying, skipping real-time update."
)
LOG_CHANGE_DETECTED = "Change detected: {event_type} on {path}. Updating graph."
LOG_DELETION_QUERY = "Ran deletion query for path: {path}"
LOG_RECALC_CALLS = "Recalculating all function call relationships for consistency..."
LOG_GRAPH_UPDATED = "Graph updated successfully for change in: {name}"
LOG_INITIAL_SCAN = "Performing initial full codebase scan..."
LOG_INITIAL_SCAN_DONE = "Initial scan complete. Starting real-time watcher."
LOG_WATCHING = "Watching for changes in: {path}"
LOG_LOGGER_CONFIGURED = "Logger configured for Real-Time Updater."

REALTIME_LOGGER_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

WATCHER_SLEEP_INTERVAL = 1


class Architecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"
    ARM64 = "arm64"
    AMD64 = "amd64"


BINARY_NAME_TEMPLATE = "graph-code-{system}-{machine}"
BINARY_FILE_PERMISSION = 0o755
DIST_DIR = "dist"
BYTES_PER_MB_FLOAT = 1024 * 1024

LOG_BUILD_BINARY = "Building binary: {name}"
LOG_BUILD_PROGRESS = "This may take a few minutes..."
LOG_BUILD_SUCCESS = "Binary built successfully!"
LOG_BUILD_READY = "Binary is ready for distribution!"
LOG_BINARY_INFO = "Binary: {path}"
LOG_BINARY_SIZE = "Size: {size:.1f} MB"
LOG_BUILD_FAILED = "Build failed: {error}"
LOG_BUILD_STDOUT = "STDOUT: {stdout}"
LOG_BUILD_STDERR = "STDERR: {stderr}"

PYPROJECT_PATH = "pyproject.toml"
TREESITTER_EXTRA_KEY = "treesitter-full"
TREESITTER_PKG_PREFIX = "tree-sitter-"

PYINSTALLER_PACKAGES: list["PyInstallerPackage"] = [
    {
        "name": "pydantic_ai",
        "collect_all": True,
        "collect_data": True,
        "hidden_import": "pydantic_ai_slim",
    },
    {"name": "rich", "collect_all": True},
    {"name": "typer", "collect_all": True},
    {"name": "loguru", "collect_all": True},
    {"name": "toml", "collect_all": True},
    {"name": "protobuf", "collect_all": True},
]

ALLOWED_COMMENT_MARKERS = frozenset(
    {"(H)", "type:", "noqa", "pyright", "ty:", "@@protoc"}
)
QUOTE_CHARS = frozenset({'"', "'"})
TRIPLE_QUOTES = ('"""', "'''")
COMMENT_CHAR = "#"
ESCAPE_CHAR = "\\"

LOG_COMMENTS_FOUND = "Comments without (H) marker found:"
LOG_COMMENT_ERROR = "  {error}"
