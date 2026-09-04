# MCP server tool names, schema fields, and messages.

from enum import StrEnum


class MCPToolName(StrEnum):
    LIST_PROJECTS = "list_projects"
    DELETE_PROJECT = "delete_project"
    WIPE_DATABASE = "wipe_database"
    INDEX_REPOSITORY = "index_repository"
    UPDATE_REPOSITORY = "update_repository"
    REINGEST = "reingest"
    # Deterministic graph queries (issue #1523): fixed Cypher, no LLM.
    RESOLVE = "resolve"
    DEFINITION = "definition"
    CALLERS = "callers"
    CALLEES = "callees"
    IMPLEMENTORS = "implementors"
    OVERRIDES = "overrides"
    IMPORTERS = "importers"
    TESTS_REACHING = "tests_reaching"
    QUERY_CODE_GRAPH = "query_code_graph"
    GET_CODE_SNIPPET = "get_code_snippet"
    SURGICAL_REPLACE_CODE = "surgical_replace_code"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    LIST_DIRECTORY = "list_directory"
    SEMANTIC_SEARCH = "semantic_search"
    STRUCTURAL_SEARCH = "structural_search"
    STRUCTURAL_REPLACE = "structural_replace"
    FIND_DUPLICATE_CODE = "find_duplicate_code"
    GET_FUNCTION_SOURCE = "get_function_source"
    ASK_AGENT = "ask_agent"
    FLOW_VERDICT = "flow_verdict"
    EXPLAIN_TRACEBACK = "explain_traceback"
    RANK_ROOT_CAUSES = "rank_root_causes"


class MCPTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class MCPEnvVar(StrEnum):
    TARGET_REPO_PATH = "TARGET_REPO_PATH"
    CLAUDE_PROJECT_ROOT = "CLAUDE_PROJECT_ROOT"
    PWD = "PWD"


class MCPSchemaType(StrEnum):
    OBJECT = "object"
    STRING = "string"
    INTEGER = "integer"
    # JSON Schema spells a float 'number'; 'integer' would reject a 0.8
    # similarity threshold at the client before the call is even made.
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"


class MCPSchemaField(StrEnum):
    TYPE = "type"
    PROPERTIES = "properties"
    REQUIRED = "required"
    DESCRIPTION = "description"
    DEFAULT = "default"


class MCPParamName(StrEnum):
    PROJECT_NAME = "project_name"
    # The per-request retrieval scope (issue #1494). Distinct from
    # PROJECT_NAME, which names the target of delete_project.
    PROJECT = "project"
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
    SOURCE_QN = "source_qualified_name"
    SINK_QN = "sink_qualified_name"
    DRY_RUN = "dry_run"
    TRACEBACK_TEXT = "traceback_text"
    NODE_ID = "node_id"
    THRESHOLD = "threshold"
    MIN_SIZE = "min_size"
    PATHS = "paths"
    DELETED = "deleted"
    TARGET = "target"
    DEPTH = "depth"
    MODULE_QN = "module_qualified_name"


# MCP server constants
MCP_SERVER_NAME = "code-graph-rag"
MCP_CONTENT_TYPE_TEXT = "text"
MCP_DEFAULT_DIRECTORY = "."
MCP_JSON_INDENT = 2
MCP_LOG_LEVEL_INFO = "INFO"
MCP_LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
MCP_PAGINATION_HEADER = "# Lines {start}-{end} of {total}\n"

# MCP response messages
MCP_INDEX_SUCCESS = "Successfully indexed repository at {path}. Knowledge graph has been updated (previous data cleared)."
MCP_INDEX_SUCCESS_PROJECT = "Successfully indexed repository at {path}. Project '{project_name}' has been updated."
MCP_INDEX_ERROR = "Error indexing repository: {error}"
MCP_WRITE_SUCCESS = "Successfully wrote file: {path}"
MCP_UNKNOWN_TOOL_ERROR = "Unknown tool: {name}"
MCP_TOOL_EXEC_ERROR = "Error executing tool '{name}': {error}"
MCP_UPDATE_SUCCESS = "Successfully updated repository at {path} (no database wipe)."
MCP_UPDATE_ERROR = "Error updating repository: {error}"
MCP_REINGEST_ERROR = "Error re-ingesting files: {error}"
# Structural delta appended to write tools (issue #1525).
MCP_DELTA_HEADER = "Structural delta:"
MCP_DELTA_ERROR = "Structural delta unavailable: {error}"
MCP_REINGEST_NEEDS_INDEX = (
    "Project {project} is not indexed; run index_repository or update_repository "
    "before reingest"
)
MCP_REINGEST_AFTER_FAILED_RUN = (
    "The last index or update of project {project} failed part way, so its "
    "graph is incomplete; run update_repository before reingest"
)
REINGEST_OUTSIDE_REPO = "Path is outside the repository: {path}"
REINGEST_IS_DIRECTORY = "Path is a directory, not a file: {path}"
MCP_GRAPH_QUERY_ERROR = "Error running {tool}: {error}"
GRAPH_QUERY_MAX_DEPTH = 5
MCP_SEMANTIC_NOT_AVAILABLE_RESPONSE = (
    "Semantic search is not available. Install with: uv sync --extra semantic"
)
MCP_ASK_AGENT_ERROR = "Error running ask_agent: {error}"
# Refused rather than answered with zero rows: an empty result for a
# misspelled project name is indistinguishable from a genuine empty result.
MCP_UNKNOWN_PROJECT = "Unknown project {project!r}. Indexed projects: {known}"
MCP_PROJECT_DELETED = "Successfully deleted project '{project_name}'."
MCP_WIPE_CANCELLED = "Database wipe cancelled. Set confirm=true to proceed."
MCP_WIPE_SUCCESS = "Database completely wiped. All projects have been removed."
MCP_WIPE_ERROR = "Error wiping database: {error}"

# MCP dict keys and values
MCP_KEY_RESULTS = "results"
MCP_KEY_ERROR = "error"
MCP_KEY_FOUND = "found"
MCP_KEY_ERROR_MESSAGE = "error_message"
MCP_KEY_QUERY_USED = "query_used"
MCP_KEY_SUMMARY = "summary"
MCP_NOT_AVAILABLE = "N/A"
MCP_TOOL_NAME_QUERY = "query"
