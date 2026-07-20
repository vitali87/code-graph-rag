# (H) MCP server tool names, schema fields, and messages.

from enum import StrEnum


# (H) MCP tool names
class MCPToolName(StrEnum):
    LIST_PROJECTS = "list_projects"
    DELETE_PROJECT = "delete_project"
    WIPE_DATABASE = "wipe_database"
    INDEX_REPOSITORY = "index_repository"
    UPDATE_REPOSITORY = "update_repository"
    QUERY_CODE_GRAPH = "query_code_graph"
    GET_CODE_SNIPPET = "get_code_snippet"
    SURGICAL_REPLACE_CODE = "surgical_replace_code"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIRECTORY = "list_directory"
    SEMANTIC_SEARCH = "semantic_search"
    STRUCTURAL_SEARCH = "structural_search"
    STRUCTURAL_REPLACE = "structural_replace"
    ASK_AGENT = "ask_agent"


# (H) MCP transport selection
class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


# (H) MCP environment variables
class MCPEnvVar(StrEnum):
    TARGET_REPO_PATH = "TARGET_REPO_PATH"
    CLAUDE_PROJECT_ROOT = "CLAUDE_PROJECT_ROOT"
    PWD = "PWD"


# (H) MCP schema types
class MCPSchemaType(StrEnum):
    OBJECT = "object"
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


# (H) MCP schema fields
class MCPSchemaField(StrEnum):
    TYPE = "type"
    PROPERTIES = "properties"
    REQUIRED = "required"
    DESCRIPTION = "description"
    DEFAULT = "default"


# (H) MCP parameter names
class MCPParamName(StrEnum):
    PROJECT_NAME = "project_name"
    CONFIRM = "confirm"
    NATURAL_LANGUAGE_QUERY = "natural_language_query"
    QUALIFIED_NAME = "qualified_name"
    FILE_PATH = "file_path"
    TARGET_CODE = "target_code"
    REPLACEMENT_CODE = "replacement_code"
    OFFSET = "offset"
    LIMIT = "limit"
    CONTENT = "content"
    DIRECTORY_PATH = "directory_path"
    TOP_K = "top_k"
    QUESTION = "question"
    PATTERN = "pattern"
    REWRITE = "rewrite"
    LANGUAGE = "language"
    DRY_RUN = "dry_run"


# (H) MCP server constants
MCP_SERVER_NAME = "code-graph-rag"
MCP_CONTENT_TYPE_TEXT = "text"
MCP_DEFAULT_DIRECTORY = "."
MCP_JSON_INDENT = 2
MCP_LOG_LEVEL_INFO = "INFO"
MCP_LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
MCP_PAGINATION_HEADER = "# Lines {start}-{end} of {total}\n"

# (H) MCP response messages
MCP_INDEX_SUCCESS = "Successfully indexed repository at {path}. Knowledge graph has been updated (previous data cleared)."
MCP_INDEX_SUCCESS_PROJECT = "Successfully indexed repository at {path}. Project '{project_name}' has been updated."
MCP_INDEX_ERROR = "Error indexing repository: {error}"
MCP_WRITE_SUCCESS = "Successfully wrote file: {path}"
MCP_UNKNOWN_TOOL_ERROR = "Unknown tool: {name}"
MCP_TOOL_EXEC_ERROR = "Error executing tool '{name}': {error}"
MCP_UPDATE_SUCCESS = "Successfully updated repository at {path} (no database wipe)."
MCP_UPDATE_ERROR = "Error updating repository: {error}"
MCP_SEMANTIC_NOT_AVAILABLE_RESPONSE = (
    "Semantic search is not available. Install with: uv sync --extra semantic"
)
MCP_ASK_AGENT_ERROR = "Error running ask_agent: {error}"
MCP_PROJECT_DELETED = "Successfully deleted project '{project_name}'."
MCP_WIPE_CANCELLED = "Database wipe cancelled. Set confirm=true to proceed."
MCP_WIPE_SUCCESS = "Database completely wiped. All projects have been removed."
MCP_WIPE_ERROR = "Error wiping database: {error}"

# (H) MCP dict keys and values
MCP_KEY_RESULTS = "results"
MCP_KEY_ERROR = "error"
MCP_KEY_FOUND = "found"
MCP_KEY_ERROR_MESSAGE = "error_message"
MCP_KEY_QUERY_USED = "query_used"
MCP_KEY_SUMMARY = "summary"
MCP_NOT_AVAILABLE = "N/A"
MCP_TOOL_NAME_QUERY = "query"
