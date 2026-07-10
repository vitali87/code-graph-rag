from enum import StrEnum


class CLICommandName(StrEnum):
    START = "start"
    INDEX = "index"
    EXPORT = "export"
    OPTIMIZE = "optimize"
    MCP_SERVER = "mcp-server"
    GRAPH_LOADER = "graph-loader"
    LANGUAGE = "language"
    DOCTOR = "doctor"
    STATS = "stats"
    DEAD_CODE = "dead-code"
    DELETE_PROJECT = "delete-project"
    DAEMON = "daemon"
    WORKSPACE = "workspace"
    STOP = "stop"
    STATUS = "status"


APP_DESCRIPTION = (
    "An accurate Retrieval-Augmented Generation (RAG) system that analyzes "
    "multi-language codebases using Tree-sitter, builds comprehensive knowledge "
    "graphs, and enables natural language querying of codebase structure and relationships."
)

CMD_START = "Start interactive chat session with your codebase"
CMD_INDEX = "Index codebase to protobuf files for offline use"
CMD_EXPORT = "Export knowledge graph from Memgraph to JSON file"
CMD_OPTIMIZE = "AI-guided codebase optimization session"
CMD_MCP_SERVER = "Start the MCP server for Claude Code integration"
CMD_GRAPH_LOADER = "Load and display summary of exported graph JSON"
CMD_LANGUAGE = "Manage language grammars (add, remove, list)"
CMD_DOCTOR = "Verify that all dependencies and configurations are properly set up"
CMD_STATS = "Display node and relationship statistics for the indexed graph"
CMD_DEAD_CODE = (
    "Report functions/methods that are unreachable from any entry point "
    "(candidates for review, not a guaranteed delete list)"
)
CMD_DELETE_PROJECT = "Delete a single project from the shared graph database (keeps other projects intact)"

CMD_LANGUAGE_GROUP = "CLI for managing language grammars"
CMD_LANGUAGE_ADD = "Add a new language grammar to the project."
CMD_LANGUAGE_LIST = "List all currently configured languages."
CMD_LANGUAGE_REMOVE = "Remove a language from the project."
CMD_LANGUAGE_CLEANUP = "Clean up orphaned git modules that weren't properly removed."

CMD_DAEMON = "Manage the shared cgr docker stack (memgraph + qdrant)"
CMD_DAEMON_GROUP = "Manage the shared cgr docker stack (memgraph + qdrant)"
CMD_DAEMON_UP = "Start the docker stack and wait until healthy."
CMD_DAEMON_DOWN = "Stop the docker stack (preserves data volumes)."
CMD_DAEMON_STATUS = "Show whether memgraph and qdrant are reachable."
CMD_DAEMON_LOGS = "Tail docker compose logs for the stack."
CMD_DAEMON_RESTART = "Restart the docker stack."

CMD_WORKSPACE = "Manage cgr workspaces (named bundles of repos)"
CMD_WORKSPACE_GROUP = "Manage cgr workspaces (named bundles of repos)"
CMD_WORKSPACE_LIST = "List all workspaces."
CMD_WORKSPACE_CREATE = "Create a new empty workspace."
CMD_WORKSPACE_DELETE = "Delete a workspace TOML (does not touch indexed graph data)."
CMD_WORKSPACE_SHOW = "Show a workspace's repos and project names."
CMD_WORKSPACE_ADD_REPO = "Add a repo to a workspace."
CMD_WORKSPACE_REMOVE_REPO = "Remove a repo from a workspace by path."

HELP_WORKSPACE_DESCRIPTION = "Optional human-readable description."
HELP_WORKSPACE_FORCE = "Overwrite an existing workspace with the same name."
HELP_WORKSPACE_REPO_PROJECT_NAME = (
    "Project name to associate with this repo (defaults to derive_project_name(repo))."
)

MSG_NO_WORKSPACES = "(no workspaces; create one with 'cgr workspace create <name>')"

CMD_STOP = "Alias for `cgr daemon down`: stop the shared docker stack."
CMD_STATUS = "Show daemon stack state plus last-sync timestamp per project."

HELP_DAEMON_LOGS_FOLLOW = "Stream logs continuously (Ctrl+C to stop)."
HELP_DAEMON_LOGS_SERVICE = (
    "Limit logs to a specific service (memgraph, qdrant, lab). Default: all."
)
HELP_NO_START_STACK = (
    "Skip auto-starting the docker stack. Useful when memgraph/qdrant run elsewhere."
)
HELP_NO_SYNC = (
    "Skip the automatic incremental graph sync that runs before the agent starts."
)
HELP_NO_EMBEDDINGS = (
    "Skip the semantic embedding pass after graph sync; the graph itself still "
    "builds fully. Env equivalent: CGR_SKIP_EMBEDDINGS=1."
)
HELP_PROJECTS = (
    "Comma-separated list of project names to scope agent queries to. "
    "Overrides --project-name. If omitted, defaults to the current repo's project."
)
HELP_WORKSPACE = (
    "Open the agent over all projects defined in a cgr workspace TOML "
    "(stored under ~/.cgr/workspaces/<name>.toml)."
)

HELP_BATCH_SIZE = "Number of buffered nodes/relationships before flushing to Memgraph"
HELP_MEMGRAPH_HOST = "Memgraph host"
HELP_MEMGRAPH_PORT = "Memgraph port"
HELP_ORCHESTRATOR = (
    "Specify orchestrator as provider:model "
    "(e.g., ollama:llama3.2, openai:gpt-4, google:gemini-3.1-pro-preview)"
)
HELP_CYPHER_MODEL = (
    "Specify cypher model as provider:model "
    "(e.g., ollama:codellama, google:gemini-3-flash-preview)"
)
HELP_NO_CONFIRM = "Disable confirmation prompts for edit operations (YOLO mode)"
HELP_NO_INSTRUCTIONS = (
    "Skip loading project instructions from ~/.cgr.md and <repo>/.cgr.md "
    "(useful when the consolidated memories are bloating the system prompt)"
)

HELP_REPO_PATH_RETRIEVAL = (
    "Path to the target repository for code retrieval (defaults to current directory)"
)
HELP_REPO_PATH_INDEX = (
    "Path to the target repository to index (defaults to current directory)."
)
HELP_REPO_PATH_OPTIMIZE = (
    "Path to the repository to optimize (defaults to current directory)"
)
HELP_REPO_PATH_WATCH = "Path to the repository to watch."
HELP_VERSION = "Show the version and exit."

HELP_DEBOUNCE = "Debounce delay in seconds. Set to 0 to disable debouncing."
HELP_MAX_WAIT = (
    "Maximum wait time in seconds before forcing an update during continuous edits."
)

HELP_UPDATE_GRAPH = "Update the knowledge graph by parsing the repository"
HELP_CLEAN_DB = "Clean the database before updating (use when adding first repo)"
HELP_OUTPUT_GRAPH = "Export graph to JSON file after updating (requires --update-graph)"
HELP_OUTPUT_PATH = "Output file path for the exported graph"
HELP_OUTPUT_PROTO_DIR = (
    "Required. Path to the output directory for the protobuf index file(s)."
)
HELP_SPLIT_INDEX = "Write index to separate nodes.bin and relationships.bin files."
HELP_FORMAT_JSON = "Export in JSON format"
HELP_LANGUAGE_ARG = (
    "Programming language to optimize for (e.g., python, java, javascript, cpp)"
)
HELP_REFERENCE_DOC = "Path to reference document/book for optimization guidance"
HELP_GRAPH_FILE = "Path to the exported graph JSON file"
HELP_EXPORTED_GRAPH_FILE = "Path to the exported_graph.json file."

HELP_GRAMMAR_URL = (
    "URL to the tree-sitter grammar repository. If not provided, "
    "will use https://github.com/tree-sitter/tree-sitter-<language_name>"
)
HELP_KEEP_SUBMODULE = "Keep the git submodule (default: remove it)"

HELP_PROJECT_NAME = (
    "Override the project name used as qualified-name prefix for all nodes. "
    "Defaults to the repo directory name."
)
HELP_EXCLUDE_PATTERNS = (
    "Additional directories to exclude from indexing. Can be specified multiple times."
)
HELP_INTERACTIVE_SETUP = (
    "Show interactive prompt to select which detected directories to keep. "
    "Without this flag, all directories matching ignore patterns are automatically excluded."
)

HELP_ASK_AGENT = (
    "Run a single query in non-interactive mode and exit. "
    "Output is sent to stdout, useful for scripting."
)

HELP_QUERY_OUTPUT_FORMAT = (
    "Output format for --ask-agent: 'table' (default) prints the plain answer; "
    '\'json\' wraps it as {"query": ..., "response": ...} for scripting.'
)

HELP_MCP_TRANSPORT = "Transport mode: 'stdio' (default) or 'http'"
HELP_MCP_HTTP_HOST = (
    "Host to bind the HTTP server — only used when --transport http (default: 0.0.0.0)"
)
HELP_MCP_HTTP_PORT = (
    "Port to bind the HTTP server — only used when --transport http (default: 8080)"
)

HELP_DEADCODE_PROJECT_NAME = (
    "Project to scan (matches the Project node name). "
    "If omitted, the sole indexed project is used."
)
HELP_DEADCODE_ENTRY_POINT = (
    "Treat functions/methods whose qualified name ends with this value as "
    "reachable roots. Repeatable."
)
HELP_DEADCODE_DECORATOR_ROOT = (
    "Treat functions/methods carrying this decorator as reachable roots. "
    "Extends the built-in set (route, task, fixture, command, ...). Repeatable."
)
HELP_DEADCODE_EXCLUDE = (
    "Glob matched against a symbol's file path to exclude from the report "
    "(e.g. '*client/core*', '*.gen.*' for generated code). '*' spans '/'. "
    "Repeatable."
)
HELP_DEADCODE_INCLUDE_TESTS = (
    "Treat test code as reachable roots so production code it exercises is "
    "not reported. On by default."
)
HELP_DEADCODE_CLASSES = (
    "Also report unreachable classes. A class counts as used when it is "
    "instantiated or subclassed by a reachable class, so a base whose only "
    "subclass is itself unreachable is reported as part of the dead cluster. "
    "Off by default: classes referenced only via type annotations, isinstance, "
    "or dynamic lookups are not tracked and may be false positives."
)
HELP_DEADCODE_FORMAT = "Output format: 'table' (default) or 'json'."
HELP_DEADCODE_OUTPUT = "Write the report to this file instead of stdout."
HELP_DEADCODE_FAIL_ON_FOUND = (
    "Exit with code 1 when any candidate is found (useful in CI)."
)

HELP_DELETE_PROJECT_NAME = (
    "Name of the project to delete (matches the Project node name in the graph)."
)
HELP_DELETE_PROJECT_REPO_PATH = (
    "Optional path to the project's repo. If supplied, its hash cache is removed too."
)

CLI_COMMANDS: dict[CLICommandName, str] = {
    CLICommandName.START: CMD_START,
    CLICommandName.INDEX: CMD_INDEX,
    CLICommandName.EXPORT: CMD_EXPORT,
    CLICommandName.OPTIMIZE: CMD_OPTIMIZE,
    CLICommandName.MCP_SERVER: CMD_MCP_SERVER,
    CLICommandName.GRAPH_LOADER: CMD_GRAPH_LOADER,
    CLICommandName.LANGUAGE: CMD_LANGUAGE,
    CLICommandName.DOCTOR: CMD_DOCTOR,
    CLICommandName.STATS: CMD_STATS,
    CLICommandName.DEAD_CODE: CMD_DEAD_CODE,
    CLICommandName.DELETE_PROJECT: CMD_DELETE_PROJECT,
    CLICommandName.DAEMON: CMD_DAEMON,
    CLICommandName.WORKSPACE: CMD_WORKSPACE,
    CLICommandName.STOP: CMD_STOP,
    CLICommandName.STATUS: CMD_STATUS,
}
