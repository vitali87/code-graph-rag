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


class FileAction(StrEnum):
    READ = "read"
    EDIT = "edit"


BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".tiff",
        ".webp",
    }
)


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

UNIXCODER_MODEL = "microsoft/unixcoder-base"

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


class EventType(StrEnum):
    MODIFIED = "modified"
    CREATED = "created"


CYPHER_DELETE_MODULE = "MATCH (m:Module {path: $path})-[*0..]->(c) DETACH DELETE m, c"
CYPHER_DELETE_CALLS = "MATCH ()-[r:CALLS]->() DELETE r"

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

DEFAULT_NAME = "Unknown"

MODULE_TORCH = "torch"
MODULE_TRANSFORMERS = "transformers"
MODULE_QDRANT_CLIENT = "qdrant_client"

SEMANTIC_DEPENDENCIES = (MODULE_QDRANT_CLIENT, MODULE_TORCH, MODULE_TRANSFORMERS)
ML_DEPENDENCIES = (MODULE_TORCH, MODULE_TRANSFORMERS)

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

# (H) Semantic search constants
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
MSG_DOC_NO_CANDIDATES = "No valid text found in response candidates."
MSG_DOC_NO_CONTENT = "No text content received from the API."
MIME_TYPE_DEFAULT = "application/octet-stream"
DOC_PROMPT_PREFIX = (
    "Based on the document provided, please answer the following question: {question}"
)

# (H) Language CLI paths and patterns
LANG_GRAMMARS_DIR = "grammars"
LANG_CONFIG_FILE = "codebase_rag/language_spec.py"
LANG_TREE_SITTER_JSON = "tree-sitter.json"
LANG_NODE_TYPES_JSON = "node-types.json"
LANG_SRC_DIR = "src"
LANG_GIT_MODULES_PATH = ".git/modules/{path}"
LANG_DEFAULT_GRAMMAR_URL = "https://github.com/tree-sitter/tree-sitter-{name}"
LANG_TREE_SITTER_URL_MARKER = "github.com/tree-sitter/tree-sitter"

# (H) Language CLI default node types
LANG_DEFAULT_FUNCTION_NODES = ("function_definition", "method_definition")
LANG_DEFAULT_CLASS_NODES = ("class_declaration",)
LANG_DEFAULT_MODULE_NODES = ("compilation_unit",)
LANG_DEFAULT_CALL_NODES = ("invocation_expression",)
LANG_FALLBACK_METHOD_NODE = "method_declaration"

# (H) Language CLI node type detection keywords
LANG_FUNCTION_KEYWORDS = frozenset(
    {
        "function",
        "method",
        "constructor",
        "destructor",
        "lambda",
        "arrow_function",
        "anonymous_function",
        "closure",
    }
)
LANG_CLASS_KEYWORDS = frozenset(
    {
        "class",
        "interface",
        "struct",
        "enum",
        "trait",
        "object",
        "type",
        "impl",
        "union",
    }
)
LANG_CALL_KEYWORDS = frozenset({"call", "invoke", "invocation"})
LANG_MODULE_KEYWORDS = frozenset(
    {"program", "source_file", "compilation_unit", "module", "chunk"}
)
LANG_EXCLUSION_KEYWORDS = frozenset({"access", "call"})

# (H) Language CLI messages
LANG_MSG_USING_DEFAULT_URL = "Using default tree-sitter URL: {url}"
LANG_MSG_CUSTOM_URL_WARNING = (
    "WARNING: You are adding a grammar from a custom URL. "
    "This may execute code from the repository. Only proceed if you trust the source."
)
LANG_MSG_ADDING_SUBMODULE = "Adding submodule from {url}..."
LANG_MSG_SUBMODULE_SUCCESS = "Successfully added submodule at {path}"
LANG_MSG_SUBMODULE_EXISTS = (
    "Submodule already exists at {path}. Forcing re-installation..."
)
LANG_MSG_REMOVING_ENTRY = "   -> Removing existing submodule entry..."
LANG_MSG_READDING_SUBMODULE = "   -> Re-adding submodule..."
LANG_MSG_REINSTALL_SUCCESS = "Successfully re-installed submodule at {path}"
LANG_MSG_AUTO_DETECTED_LANG = "Auto-detected language: {name}"
LANG_MSG_USING_LANG_NAME = "Using language name: {name}"
LANG_MSG_AUTO_DETECTED_EXT = "Auto-detected file extensions: {extensions}"
LANG_MSG_FOUND_NODE_TYPES = "Found {count} total node types in grammar"
LANG_MSG_SEMANTIC_CATEGORIES = "Tree-sitter semantic categories:"
LANG_MSG_CATEGORY_FORMAT = "  {category}: {subtypes} ({count} total)"
LANG_MSG_MAPPED_CATEGORIES = "\nMapped to our categories:"
LANG_MSG_FUNCTIONS = "Functions: {nodes}"
LANG_MSG_CLASSES = "Classes: {nodes}"
LANG_MSG_MODULES = "Modules: {nodes}"
LANG_MSG_CALLS = "Calls: {nodes}"
LANG_MSG_LANG_ADDED = "\nLanguage '{name}' has been added to the configuration!"
LANG_MSG_UPDATED_CONFIG = "Updated {path}"
LANG_MSG_REVIEW_PROMPT = "Please review the detected node types:"
LANG_MSG_REVIEW_HINT = "   The auto-detection is good but may need manual adjustments."
LANG_MSG_EDIT_HINT = "   Edit the configuration in: {path}"
LANG_MSG_COMMON_ISSUES = "Look for these common issues:"
LANG_MSG_ISSUE_MISCLASSIFIED = (
    "   - Remove misclassified types (e.g., table_constructor in functions)"
)
LANG_MSG_ISSUE_MISSING = "   - Add missing types that should be included"
LANG_MSG_ISSUE_CLASS_TYPES = (
    "   - Verify class_node_types includes all relevant class-like constructs"
)
LANG_MSG_ISSUE_CALL_TYPES = (
    "   - Check call_node_types covers all function call patterns"
)
LANG_MSG_LIST_HINT = (
    "You can run 'cgr language list-languages' to see the current config."
)
LANG_MSG_LANG_NOT_FOUND = "Language '{name}' not found."
LANG_MSG_AVAILABLE_LANGS = "Available languages: {langs}"
LANG_MSG_REMOVED_FROM_CONFIG = "Removed language '{name}' from configuration file."
LANG_MSG_REMOVING_SUBMODULE = "Removing git submodule '{path}'..."
LANG_MSG_CLEANED_MODULES = "Cleaned up git modules directory: {path}"
LANG_MSG_SUBMODULE_REMOVED = "Successfully removed submodule '{path}'"
LANG_MSG_NO_SUBMODULE = "No submodule found at '{path}'"
LANG_MSG_KEEPING_SUBMODULE = "Keeping submodule (--keep-submodule flag used)"
LANG_MSG_LANG_REMOVED = "Language '{name}' has been removed successfully!"
LANG_MSG_NO_MODULES_DIR = "No grammars modules directory found."
LANG_MSG_NO_GITMODULES = "No .gitmodules file found."
LANG_MSG_NO_ORPHANS = "No orphaned modules found!"
LANG_MSG_FOUND_ORPHANS = "Found {count} orphaned module(s): {modules}"
LANG_MSG_REMOVED_ORPHAN = "Removed orphaned module: {module}"
LANG_MSG_CLEANUP_COMPLETE = "Cleanup complete!"
LANG_MSG_CLEANUP_CANCELLED = "Cleanup cancelled."

# (H) Language CLI error messages
LANG_ERR_MISSING_ARGS = "Error: Either language_name or --grammar-url must be provided"
LANG_ERR_REINSTALL_FAILED = "Failed to reinstall submodule: {error}"
LANG_ERR_MANUAL_REMOVE_HINT = "You may need to remove it manually and try again:"
LANG_ERR_REPO_NOT_FOUND = "Error: Repository not found at {url}"
LANG_ERR_CUSTOM_URL_HINT = "Try using a custom URL with: --grammar-url <your-repo-url>"
LANG_ERR_GIT = "Git error: {error}"
LANG_ERR_NODE_TYPES_WARNING = (
    "Warning: node-types.json not found in any expected location for {name}"
)
LANG_ERR_TREE_SITTER_JSON_WARNING = "Warning: tree-sitter.json not found in {path}"
LANG_ERR_NO_GRAMMARS_WARNING = "Warning: No grammars found in tree-sitter.json"
LANG_ERR_PARSE_NODE_TYPES = "Error parsing node-types.json: {error}"
LANG_ERR_UPDATE_CONFIG = "Error updating config file: {error}"
LANG_ERR_CONFIG_NOT_FOUND = "Could not find LANGUAGE_SPECS dictionary end"
LANG_ERR_REMOVE_CONFIG = "Failed to update config file: {error}"
LANG_ERR_REMOVE_SUBMODULE = "Failed to remove submodule: {error}"

# (H) Language CLI prompts
LANG_PROMPT_LANGUAGE_NAME = "Language name (e.g., 'c-sharp', 'python')"
LANG_PROMPT_COMMON_NAME = "What is the common name for this language?"
LANG_PROMPT_EXTENSIONS = (
    "What file extensions should be associated with this language? (comma-separated)"
)
LANG_PROMPT_FUNCTIONS = "Select nodes representing FUNCTIONS (comma-separated)"
LANG_PROMPT_CLASSES = "Select nodes representing CLASSES (comma-separated)"
LANG_PROMPT_MODULES = "Select nodes representing MODULES (comma-separated)"
LANG_PROMPT_CALLS = "Select nodes representing FUNCTION CALLS (comma-separated)"
LANG_PROMPT_CONTINUE = "Do you want to continue?"
LANG_PROMPT_REMOVE_ORPHANS = "Do you want to remove these orphaned modules?"

# (H) Language CLI fallback manual add message
LANG_FALLBACK_MANUAL_ADD = (
    "FALLBACK: Please manually add the following entry to "
    "'LANGUAGE_SPECS' in 'codebase_rag/language_spec.py':"
)

# (H) Language CLI table configuration
LANG_TABLE_TITLE = "Configured Languages"
LANG_TABLE_COL_LANGUAGE = "Language"
LANG_TABLE_COL_EXTENSIONS = "Extensions"
LANG_TABLE_COL_FUNCTION_TYPES = "Function Types"
LANG_TABLE_COL_CLASS_TYPES = "Class Types"
LANG_TABLE_COL_CALL_TYPES = "Call Types"
LANG_TABLE_PLACEHOLDER = "—"

# (H) Git submodule regex
LANG_GITMODULES_REGEX = r"path = (grammars/tree-sitter-[^\\n]+)"
