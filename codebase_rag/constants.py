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


class GoogleProviderType(StrEnum):
    GLA = "gla"
    VERTEX = "vertex"


# (H) Provider endpoints
OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"
OLLAMA_DEFAULT_ENDPOINT = f"{OLLAMA_DEFAULT_BASE_URL}/v1"
OLLAMA_HEALTH_PATH = "/api/tags"
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
V1_PATH = "/v1"

# (H) HTTP status codes
HTTP_OK = 200

# (H) Provider error messages
ERR_GOOGLE_GLA_NO_KEY = (
    "Gemini GLA provider requires api_key. "
    "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
)
ERR_GOOGLE_VERTEX_NO_PROJECT = (
    "Gemini Vertex provider requires project_id. "
    "Set ORCHESTRATOR_PROJECT_ID or CYPHER_PROJECT_ID in .env file."
)
ERR_OPENAI_NO_KEY = (
    "OpenAI provider requires api_key. "
    "Set ORCHESTRATOR_API_KEY or CYPHER_API_KEY in .env file."
)
ERR_OLLAMA_NOT_RUNNING = (
    "Ollama server not responding at {endpoint}. "
    "Make sure Ollama is running: ollama serve"
)
ERR_UNKNOWN_PROVIDER = "Unknown provider '{provider}'. Available providers: {available}"
LOG_PROVIDER_REGISTERED = "Registered provider: {name}"

UNIXCODER_MODEL = "microsoft/unixcoder-base"
SEMANTIC_EXTRA_ERROR = (
    "Semantic search requires 'semantic' extra: uv sync --extra semantic"
)

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
KEY_EXPORTED_AT = "exported_at"
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

# (H) Protobuf service logs
LOG_PROTOBUF_INIT = "ProtobufFileIngestor initialized to write to: {path}"
LOG_PROTOBUF_NO_MESSAGE_CLASS = (
    "No Protobuf message class found for label '{label}'. Skipping node."
)
LOG_PROTOBUF_NO_ONEOF_MAPPING = (
    "No 'oneof' field mapping found for label '{label}'. Skipping node."
)
LOG_PROTOBUF_UNKNOWN_REL_TYPE = (
    "Unknown relationship type '{rel_type}'. Setting to UNSPECIFIED."
)
LOG_PROTOBUF_INVALID_REL = (
    "Invalid relationship: source_id={source_id}, target_id={target_id}"
)
LOG_PROTOBUF_FLUSH_SUCCESS = "Successfully flushed {nodes} unique nodes and {rels} unique relationships to {path}"
LOG_PROTOBUF_FLUSHING = "Flushing data to {path}..."

# (H) Protobuf file names
PROTOBUF_INDEX_FILE = "index.bin"
PROTOBUF_NODES_FILE = "nodes.bin"
PROTOBUF_RELS_FILE = "relationships.bin"

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
ONEOF_MODULE_IMPLEMENTATION = "module_implementation"
ONEOF_MODULE_INTERFACE = "module_interface"

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

# (H) Byte size constants
BYTES_PER_MB = 1024 * 1024

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

DEFAULT_NAME = "Unknown"

LOG_GRAPH_SUMMARY = "Graph Summary:"
LOG_GRAPH_TOTAL_NODES = "   Total nodes: {count:,}"
LOG_GRAPH_TOTAL_RELS = "   Total relationships: {count:,}"
LOG_GRAPH_EXPORTED_AT = "   Exported at: {timestamp}"
LOG_GRAPH_NODE_TYPES = "Node Types:"
LOG_GRAPH_NODE_COUNT = "   {label}: {count:,} nodes"
LOG_GRAPH_REL_TYPES = "Relationship Types:"
LOG_GRAPH_REL_COUNT = "   {rel_type}: {count:,} relationships"
LOG_GRAPH_FOUND_NODES = "Found {count} '{label}' nodes."
LOG_GRAPH_EXAMPLE_NAMES = "   Example {label} names:"
LOG_GRAPH_EXAMPLE_NAME = "      - {name}"
LOG_GRAPH_MORE_NODES = "      ... and {count} more"
LOG_GRAPH_ANALYZING = "Analyzing graph from: {path}"
LOG_GRAPH_ANALYSIS_COMPLETE = "Analysis complete!"
LOG_GRAPH_ANALYSIS_ERROR = "Error analyzing graph: {error}"
LOG_GRAPH_FILE_NOT_FOUND = "Graph file not found: {path}"

MODULE_TORCH = "torch"
MODULE_TRANSFORMERS = "transformers"
MODULE_QDRANT_CLIENT = "qdrant_client"

SEMANTIC_DEPENDENCIES = (MODULE_QDRANT_CLIENT, MODULE_TORCH, MODULE_TRANSFORMERS)
ML_DEPENDENCIES = (MODULE_TORCH, MODULE_TRANSFORMERS)

LOG_FQN_RESOLVE_FAILED = "Failed to resolve FQN for node at {path}: {error}"
LOG_FQN_FIND_FAILED = "Failed to find function by FQN {fqn} in {path}: {error}"
LOG_FQN_EXTRACT_FAILED = "Failed to extract function FQNs from {path}: {error}"

LOG_SOURCE_FILE_NOT_FOUND = "Source file not found: {path}"
LOG_SOURCE_INVALID_RANGE = "Invalid line range: {start}-{end}"
LOG_SOURCE_RANGE_EXCEEDS = (
    "Line range {start}-{end} exceeds file length {length} in {path}"
)
LOG_SOURCE_EXTRACT_FAILED = "Failed to extract source from {path}: {error}"
LOG_SOURCE_AST_FAILED = "AST extraction failed for {name}: {error}"

LOG_MG_CONNECTING = "Connecting to Memgraph at {host}:{port}..."
LOG_MG_CONNECTED = "Successfully connected to Memgraph."
LOG_MG_EXCEPTION = "An exception occurred: {error}. Flushing remaining items..."
LOG_MG_DISCONNECTED = "\nDisconnected from Memgraph."
LOG_MG_CYPHER_ERROR = "!!! Cypher Error: {error}"
LOG_MG_CYPHER_QUERY = "    Query: {query}"
LOG_MG_CYPHER_PARAMS = "    Params: {params}"
LOG_MG_BATCH_ERROR = "!!! Batch Cypher Error: {error}"
LOG_MG_BATCH_PARAMS_TRUNCATED = "    Params (first 10 of {count}): {params}..."
LOG_MG_CLEANING_DB = "--- Cleaning database... ---"
LOG_MG_DB_CLEANED = "--- Database cleaned. ---"
LOG_MG_ENSURING_CONSTRAINTS = "Ensuring constraints..."
LOG_MG_CONSTRAINTS_DONE = "Constraints checked/created."
LOG_MG_NODE_BUFFER_FLUSH = (
    "Node buffer reached batch size ({size}). Performing incremental flush."
)
LOG_MG_REL_BUFFER_FLUSH = (
    "Relationship buffer reached batch size ({size}). Performing incremental flush."
)
LOG_MG_NO_CONSTRAINT = (
    "No unique constraint defined for label '{label}'. Skipping flush."
)
LOG_MG_MISSING_PROP = "Skipping {label} node missing required '{key}' property: {props}"
LOG_MG_NODES_FLUSHED = "Flushed {flushed} of {total} buffered nodes."
LOG_MG_NODES_SKIPPED = (
    "Skipped {count} buffered nodes due to missing identifiers or constraints."
)
LOG_MG_CALLS_FAILED = (
    "Failed to create {count} CALLS relationships - nodes may not exist"
)
LOG_MG_CALLS_SAMPLE = "  Sample {index}: {from_label}.{from_val} -> {to_label}.{to_val}"
LOG_MG_RELS_FLUSHED = (
    "Flushed {total} relationships ({success} successful, {failed} failed)."
)
LOG_MG_FLUSH_START = "--- Flushing all pending writes to database... ---"
LOG_MG_FLUSH_COMPLETE = "--- Flushing complete. ---"
LOG_MG_FETCH_QUERY = "Executing fetch query: {query} with params: {params}"
LOG_MG_WRITE_QUERY = "Executing write query: {query} with params: {params}"
LOG_MG_EXPORTING = "Exporting graph data..."
LOG_MG_EXPORTED = "Exported {nodes} nodes and {rels} relationships"

BATCH_SIZE_ERROR = "batch_size must be a positive integer"
CONN_ERROR = "Not connected to Memgraph."

REL_TYPE_CALLS = "CALLS"

NODE_UNIQUE_CONSTRAINTS: dict[str, str] = {
    "Project": "name",
    "Package": "qualified_name",
    "Folder": "path",
    "Module": "qualified_name",
    "Class": "qualified_name",
    "Function": "qualified_name",
    "Method": "qualified_name",
    "File": "path",
    "ExternalPackage": "name",
}

# (H) Cypher response cleaning
CYPHER_PREFIX = "cypher"
CYPHER_SEMICOLON = ";"
CYPHER_BACKTICK = "`"
CYPHER_MATCH_KEYWORD = "MATCH"

# (H) LLM error messages
ERR_LLM_INIT_CYPHER = "Failed to initialize CypherGenerator: {error}"
ERR_LLM_INVALID_QUERY = "LLM did not generate a valid query. Output: {output}"
ERR_LLM_GENERATION_FAILED = "Cypher generation failed: {error}"
ERR_LLM_INIT_ORCHESTRATOR = "Failed to initialize RAG Orchestrator: {error}"

# (H) LLM log messages
LOG_CYPHER_GENERATING = "  [CypherGenerator] Generating query for: '{query}'"
LOG_CYPHER_GENERATED = "  [CypherGenerator] Generated Cypher: {query}"
LOG_CYPHER_ERROR = "  [CypherGenerator] Error: {error}"

# (H) Tool error messages
ERR_FILE_OUTSIDE_ROOT = (
    "Security risk: Attempted to {action} file outside of project root."
)
ERR_FILE_NOT_FOUND = "File not found."
ERR_FILE_NOT_FOUND_OR_DIR = "File not found or is a directory: {path}"
ERR_BINARY_FILE = "File '{path}' is a binary file. Use the 'analyze_document' tool for this file type."
ERR_UNICODE_DECODE = (
    "File '{path}' could not be read as text. It may be a binary file. "
    "If it is a document (e.g., PDF), use the 'analyze_document' tool."
)
ERR_DOCUMENT_UNSUPPORTED = (
    "Document analysis is not supported for the current LLM provider."
)
ERR_DIRECTORY_INVALID = "'{path}' is not a valid directory."
ERR_DIRECTORY_EMPTY = "The directory '{path}' is empty."
ERR_DIRECTORY_LIST_FAILED = "Could not list contents of '{path}'."
ERR_COMMAND_NOT_ALLOWED = "Command '{cmd}' is not in the allowlist.{suggestion} Available commands: {available}"
ERR_COMMAND_EMPTY = "Empty command provided."
ERR_COMMAND_DANGEROUS = "Rejected dangerous command: {cmd}"
ERR_COMMAND_TIMEOUT = "Command '{cmd}' timed out after {timeout} seconds."
ERR_ACCESS_DENIED = "Access denied: Cannot access files outside the project root."

# (H) Tool log messages
LOG_TOOL_FILE_READ = "[FileReader] Attempting to read file: {path}"
LOG_TOOL_FILE_READ_SUCCESS = "[FileReader] Successfully read text from {path}"
LOG_TOOL_FILE_BINARY = "[FileReader] {message}"
LOG_TOOL_FILE_WRITE = "[FileWriter] Creating file: {path}"
LOG_TOOL_FILE_WRITE_SUCCESS = (
    "[FileWriter] Successfully wrote {chars} characters to {path}"
)
LOG_TOOL_FILE_EDIT = "[FileEditor] Attempting full file replacement: {path}"
LOG_TOOL_FILE_EDIT_SUCCESS = "[FileEditor] Successfully replaced entire file: {path}"
LOG_TOOL_FILE_EDIT_SURGICAL = (
    "[FileEditor] Attempting surgical block replacement in: {path}"
)
LOG_TOOL_FILE_EDIT_SURGICAL_SUCCESS = (
    "[FileEditor] Successfully applied surgical block replacement in: {path}"
)
LOG_TOOL_QUERY_RECEIVED = "[Tool:QueryGraph] Received NL query: '{query}'"
LOG_TOOL_QUERY_ERROR = "[Tool:QueryGraph] Error during query execution: {error}"
LOG_TOOL_SHELL_EXEC = "Executing shell command: {cmd}"
LOG_TOOL_SHELL_RETURN = "Return code: {code}"
LOG_TOOL_SHELL_STDOUT = "Stdout: {stdout}"
LOG_TOOL_SHELL_STDERR = "Stderr: {stderr}"
LOG_TOOL_SHELL_TIMEOUT = "Command '{cmd}' timed out after {timeout} seconds."
LOG_TOOL_SHELL_KILLED = "Process killed due to timeout."
LOG_TOOL_SHELL_ALREADY_TERMINATED = (
    "Process already terminated when timeout kill was attempted."
)
LOG_TOOL_SHELL_ERROR = "An error occurred while executing command: {error}"
LOG_TOOL_DOC_ANALYZE = (
    "[DocumentAnalyzer] Analyzing '{path}' with question: '{question}'"
)

# (H) Tool success messages
MSG_SURGICAL_SUCCESS = "Successfully applied surgical code replacement in: {path}"
MSG_SURGICAL_FAILED = (
    "Failed to apply surgical replacement in {path}. "
    "Target code not found or patches failed."
)

# (H) Grep suggestion
GREP_SUGGESTION = " Use 'rg' instead of 'grep' for text searching."

# (H) Shell command constants
SHELL_CMD_GREP = "grep"
SHELL_CMD_GIT = "git"
SHELL_CMD_RM = "rm"
SHELL_RM_RF_FLAG = "-rf"
SHELL_RETURN_CODE_ERROR = -1
LOG_SHELL_TIMING = "'{func}' executed in {time:.2f}ms"

# (H) Query tool messages
QUERY_NOT_AVAILABLE = "N/A"
QUERY_SUMMARY_SUCCESS = "Successfully retrieved {count} item(s) from the graph."
QUERY_SUMMARY_TRANSLATION_FAILED = (
    "I couldn't translate your request into a database query. Error: {error}"
)
QUERY_SUMMARY_DB_ERROR = "There was an error querying the database: {error}"
QUERY_RESULTS_PANEL_TITLE = "[bold blue]Cypher Query Results[/bold blue]"

# (H) File editor constants
TMP_EXTENSION = ".tmp"
LOG_EDITOR_NO_PARSER = "No parser available for {path}"
LOG_EDITOR_NO_LANG_CONFIG = "No language config found for extension {ext}"
LOG_EDITOR_FUNC_NOT_FOUND_AT_LINE = "No function '{name}' found at line {line}"
LOG_EDITOR_FUNC_NOT_FOUND_QN = "No function found with qualified name '{name}'"
LOG_EDITOR_AMBIGUOUS = (
    "Ambiguous function name '{name}' in {path}. "
    "Found {count} matches: {details}. "
    "Using first match. Consider using qualified name (e.g., 'ClassName.{name}') "
    "or specify line number for precise targeting."
)
LOG_EDITOR_FUNC_NOT_IN_FILE = "Function '{name}' not found in {path}."
LOG_EDITOR_PATCHES_NOT_CLEAN = "Patches for function '{name}' did not apply cleanly."
LOG_EDITOR_NO_CHANGES = "No changes detected after replacement."
LOG_EDITOR_REPLACE_SUCCESS = "Successfully replaced function '{name}' in {path}."
LOG_EDITOR_PATCH_FAILED = "Some patches failed to apply cleanly to {path}"
LOG_EDITOR_PATCH_SUCCESS = "Successfully applied patch to {path}"
LOG_EDITOR_PATCH_ERROR = "Error applying patch to {path}: {error}"
LOG_EDITOR_FILE_NOT_FOUND = "File not found: {path}"
LOG_EDITOR_BLOCK_NOT_FOUND = "Target block not found in {path}"
LOG_EDITOR_LOOKING_FOR = "Looking for: {block}"
LOG_EDITOR_MULTIPLE_OCCURRENCES = (
    "Multiple occurrences of target block found. Only replacing first occurrence."
)
LOG_EDITOR_NO_CHANGES_IDENTICAL = (
    "No changes detected - target and replacement are identical"
)
LOG_EDITOR_SURGICAL_FAILED = "Surgical patches failed to apply cleanly"
LOG_EDITOR_SURGICAL_ERROR = "Error during surgical block replacement: {error}"

# (H) Directory lister log messages
LOG_DIR_LISTING = "Listing contents of directory: {path}"
LOG_DIR_LIST_ERROR = "Error listing directory {path}: {error}"

# (H) Semantic search constants
LOG_SEMANTIC_NO_MATCH = "No semantic matches found for query: {query}"
LOG_SEMANTIC_FOUND = "Found {count} semantic matches for: {query}"
LOG_SEMANTIC_FAILED = "Semantic search failed for query '{query}': {error}"
LOG_SEMANTIC_NODE_NOT_FOUND = "No node found with ID: {id}"
LOG_SEMANTIC_INVALID_LOCATION = "Missing or invalid source location info for node {id}"
LOG_SEMANTIC_SOURCE_FAILED = "Failed to get source code for node {id}: {error}"
LOG_SEMANTIC_TOOL_SEARCH = "[Tool:SemanticSearch] Searching for: '{query}'"
LOG_SEMANTIC_TOOL_SOURCE = (
    "[Tool:GetFunctionSource] Retrieving source for node ID: {id}"
)

MSG_SEMANTIC_NO_RESULTS = (
    "No semantic matches found for query: '{query}'. This could mean:\n"
    "1. No functions match this description\n"
    "2. Semantic search dependencies are not installed\n"
    "3. No embeddings have been generated yet"
)
MSG_SEMANTIC_SOURCE_UNAVAILABLE = (
    "Could not retrieve source code for node ID {id}. "
    "The node may not exist or source file may be unavailable."
)
MSG_SEMANTIC_SOURCE_FORMAT = "Source code for node ID {id}:\n\n```\n{code}\n```"
MSG_SEMANTIC_RESULT_HEADER = "Found {count} semantic matches for '{query}':\n\n"
MSG_SEMANTIC_RESULT_FOOTER = "\n\nUse the qualified names above with other tools to get more details or source code."
SEMANTIC_BATCH_SIZE = 100
SEMANTIC_TYPE_UNKNOWN = "Unknown"

# (H) Document analyzer constants
ERR_DOC_UNSUPPORTED_PROVIDER = (
    "DocumentAnalyzer does not support the 'local' LLM provider."
)
ERR_DOC_FILE_NOT_FOUND = "File not found at '{path}'."
ERR_DOC_SECURITY_RISK = "Security risk: file path {path} is outside the project root"
ERR_DOC_ACCESS_OUTSIDE_ROOT = (
    "Security risk: Attempted to access file outside of project root: {path}"
)
ERR_DOC_API_VALIDATION = "API validation failed: {error}"
ERR_DOC_IMAGE_PROCESS = (
    "Unable to process the image file. "
    "The image may be corrupted or in an unsupported format."
)
ERR_DOC_ANALYSIS_FAILED = "An error occurred during analysis: {error}"
ERR_DOC_DURING_ANALYSIS = "Error during document analysis: {error}"
LOG_DOC_COPIED = "Copied external file to: {path}"
LOG_DOC_SUCCESS = "Successfully received analysis for '{path}'."
LOG_DOC_NO_TEXT = "No text found in response: {response}"
LOG_DOC_API_ERROR = "Google GenAI API error for '{path}': {error}"
LOG_DOC_FAILED = "Failed to analyze document '{path}': {error}"
LOG_DOC_RESULT = "[analyze_document] Result type: {type}, content: {preview}..."
LOG_DOC_EXCEPTION = "[analyze_document] Exception during analysis: {error}"
MSG_DOC_NO_CANDIDATES = "No valid text found in response candidates."
MSG_DOC_NO_CONTENT = "No text content received from the API."
MIME_TYPE_DEFAULT = "application/octet-stream"
DOC_PROMPT_PREFIX = (
    "Based on the document provided, please answer the following question: {question}"
)

# (H) Code retrieval constants
ERR_CODE_ENTITY_NOT_FOUND = "Entity not found in graph."
ERR_CODE_MISSING_LOCATION = "Graph entry is missing location data."
LOG_CODE_RETRIEVER_SEARCH = "[CodeRetriever] Searching for: {name}"
LOG_CODE_RETRIEVER_ERROR = "[CodeRetriever] Error: {error}"
LOG_CODE_TOOL_RETRIEVE = "[Tool:GetCode] Retrieving code for: {name}"

# (H) File writer constants
LOG_FILE_WRITER_INIT = "FileWriter initialized with root: {root}"
LOG_FILE_WRITER_CREATE = "[FileWriter] Creating file: {path}"
LOG_FILE_WRITER_SUCCESS = "[FileWriter] Successfully wrote {chars} characters to {path}"
ERR_FILE_WRITER_SECURITY = (
    "Security risk: Attempted to create file outside of project root: {path}"
)
ERR_FILE_WRITER_CREATE = "Error creating file {path}: {error}"
