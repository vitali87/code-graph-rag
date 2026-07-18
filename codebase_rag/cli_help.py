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
    HELP = "help"


APP_DESCRIPTION = (
    "Analyze source code with Tree-sitter, store its structure in a shared "
    "knowledge graph, and query it in natural language."
)
APP_EPILOG = "Run 'cgr help COMMAND' for details about a command."

PANEL_USE = "Query and improve code"
PANEL_GRAPH = "Build and inspect the graph"
PANEL_MANAGE = "Manage cgr"
PANEL_HELP = "Help"

CMD_START = "Open the code assistant for a repository or workspace"
CMD_INDEX = "Write an offline protobuf index for a repository"
CMD_EXPORT = "Export the shared graph database to JSON"
CMD_OPTIMIZE = "Run a language-focused code optimization session"
CMD_MCP_SERVER = "Serve cgr tools over stdio or HTTP"
CMD_GRAPH_LOADER = "Summarize an exported graph JSON file"
CMD_LANGUAGE = "Manage language grammars and parser metadata"
CMD_DOCTOR = "Check dependencies, services, and configuration"
CMD_STATS = "Show graph node and relationship counts"
CMD_DEAD_CODE = "Report code that appears unreachable from known entry points"
CMD_DELETE_PROJECT = "Delete one project without changing other indexed projects"
CMD_HELP = "Show help for a command"

CMD_LANGUAGE_GROUP = CMD_LANGUAGE
CMD_LANGUAGE_ADD = "Add and register a Tree-sitter grammar"
CMD_LANGUAGE_LIST = "List configured languages and their node mappings"
CMD_LANGUAGE_REMOVE = "Remove a language from cgr configuration"
CMD_LANGUAGE_CLEANUP = "Remove orphaned grammar entries under .git/modules"

CMD_DAEMON = "Manage the shared Memgraph and Qdrant stack"
CMD_DAEMON_GROUP = CMD_DAEMON
CMD_DAEMON_UP = "Start the shared stack and wait until it is healthy"
CMD_DAEMON_DOWN = "Stop the shared stack and preserve its data volumes"
CMD_DAEMON_STATUS = "Show stack state and service reachability"
CMD_DAEMON_LOGS = "Show Docker Compose logs for the shared stack"
CMD_DAEMON_RESTART = "Restart the shared stack and wait for health checks"

CMD_WORKSPACE = "Manage named groups of repositories"
CMD_WORKSPACE_GROUP = CMD_WORKSPACE
CMD_WORKSPACE_LIST = "List saved workspaces"
CMD_WORKSPACE_CREATE = "Create an empty workspace definition"
CMD_WORKSPACE_DELETE = "Delete a workspace definition but keep indexed graph data"
CMD_WORKSPACE_SHOW = "Show the repositories and project names in a workspace"
CMD_WORKSPACE_ADD_REPO = "Add a repository to a workspace"
CMD_WORKSPACE_REMOVE_REPO = "Remove a repository from a workspace by path"

CMD_STOP = "Stop the shared stack (alias for cgr daemon down)"
CMD_STATUS = "Show stack state and the last sync time for each project"

EXAMPLES_START = (
    "EXAMPLES\n\n"
    "  cgr start --repo-path ./my-repo\n\n"
    "  cgr start --workspace backend\n\n"
    '  cgr start --ask-agent "Where is authentication handled?"'
)
EXAMPLES_INDEX = "EXAMPLE\n\n  cgr index --repo-path ./my-repo -o ./index-out"
EXAMPLES_EXPORT = "EXAMPLE\n\n  cgr export -o graph.json"
EXAMPLES_OPTIMIZE = "EXAMPLE\n\n  cgr optimize python --repo-path ./my-repo"
EXAMPLES_MCP_SERVER = (
    "EXAMPLES\n\n  cgr mcp-server\n\n  cgr mcp-server --transport http --port 8080"
)
EXAMPLES_GRAPH_LOADER = "EXAMPLE\n\n  cgr graph-loader graph.json"
EXAMPLES_LANGUAGE_ADD = "EXAMPLE\n\n  cgr language add-grammar ruby"
EXAMPLES_LANGUAGE_REMOVE = "EXAMPLE\n\n  cgr language remove-language ruby"
EXAMPLES_DEAD_CODE = (
    "EXAMPLE\n\n  cgr dead-code --project-name my-project --format json"
)
EXAMPLES_DELETE_PROJECT = "EXAMPLE\n\n  cgr delete-project --name my-project"
EXAMPLES_HELP = "EXAMPLES\n\n  cgr help start\n\n  cgr help daemon logs"

EPILOG_LANGUAGE = "Run 'cgr help language COMMAND' for command-specific help."
EPILOG_DAEMON = "Run 'cgr help daemon COMMAND' for command-specific help."
EPILOG_WORKSPACE = "Run 'cgr help workspace COMMAND' for command-specific help."

HELP_WORKSPACE_DESCRIPTION = "Optional short description for the workspace."
HELP_WORKSPACE_FORCE = "Overwrite an existing workspace with the same name."
HELP_WORKSPACE_REPO_PROJECT_NAME = (
    "Project name to use for this repo. Defaults to the derived repo name."
)

MSG_NO_WORKSPACES = "(no workspaces; create one with 'cgr workspace create <name>')"

HELP_DAEMON_LOGS_FOLLOW = "Continue printing new log entries until interrupted."
HELP_DAEMON_LOGS_SERVICE = (
    "Show only SERVICE logs (memgraph, qdrant, or lab). By default, show all services."
)
HELP_NO_START_STACK = "Do not start the shared stack automatically."
HELP_NO_SYNC = "Do not synchronize the graph before starting the assistant."
HELP_NO_EMBEDDINGS = (
    "Do not generate semantic embeddings during sync. Graph nodes and relationships "
    "are still updated. Equivalent env: CGR_SKIP_EMBEDDINGS=1."
)
HELP_PROJECTS = (
    "Limit queries to comma-separated project names. Overrides --project-name; "
    "defaults to the selected repository or workspace."
)
HELP_WORKSPACE = "Query every project defined in workspace NAME."

HELP_BATCH_SIZE = "Flush to Memgraph after this many buffered nodes or relationships."
HELP_MEMGRAPH_HOST = "Memgraph host."
HELP_MEMGRAPH_PORT = "Memgraph port."
HELP_ORCHESTRATOR = (
    "Model for the planning assistant, in provider:model form "
    "(for example openai:gpt-4 or ollama:llama3.2)."
)
HELP_CYPHER_MODEL = "Model used to generate Cypher, in provider:model form."
HELP_NO_CONFIRM = "Skip edit confirmation prompts."
HELP_NO_INSTRUCTIONS = (
    "Do not load ~/.cgr.md or <repo>/.cgr.md into the session prompt."
)

HELP_REPO_PATH_RETRIEVAL = "Repository to open. Defaults to the current directory."
HELP_REPO_PATH_INDEX = "Repository to index. Defaults to the current directory."
HELP_REPO_PATH_OPTIMIZE = "Repository to optimize. Defaults to the current directory."
HELP_REPO_PATH_WATCH = "Repository to watch."
HELP_VERSION = "Show the version and exit."
HELP_QUIET = "Suppress progress, banners, and informational logs."

HELP_DEBOUNCE = "Debounce delay in seconds. Set to 0 to disable debouncing."
HELP_MAX_WAIT = (
    "Maximum wait time in seconds before forcing an update during continuous edits."
)

HELP_UPDATE_GRAPH = "Parse the repository and sync its graph before continuing."
HELP_CLEAN_DB = (
    "Delete every project from the shared graph and clear the selected repository's "
    "sync cache. With --update-graph, rebuild after deletion."
)
HELP_OUTPUT_GRAPH = "Write the updated graph to PATH as JSON. Requires --update-graph."
HELP_OUTPUT_PATH = "Write the exported graph to PATH."
HELP_OUTPUT_PROTO_DIR = "Write protobuf index files under DIRECTORY."
HELP_SPLIT_INDEX = "Write separate nodes.bin and relationships.bin files."
HELP_FORMAT_JSON = "Use JSON output. Other export formats are not supported."
HELP_LANGUAGE_ARG = "Language to optimize, such as python, java, javascript, or cpp."
HELP_REFERENCE_DOC = "Reference document to use during optimization."
HELP_GRAPH_FILE = "Exported graph JSON file to load."
HELP_EXPORTED_GRAPH_FILE = "Path to the exported_graph.json file."

HELP_GRAMMAR_URL = (
    "Tree-sitter grammar repository URL. Defaults to "
    "https://github.com/tree-sitter/tree-sitter-<language_name>."
)
HELP_KEEP_SUBMODULE = (
    "Keep the grammar git submodule when removing the language. By default, remove it."
)

HELP_PROJECT_NAME = (
    "Project name to store in the graph. Defaults to the repo directory name."
)
HELP_EXCLUDE_PATTERNS = (
    "Exclude paths matching PATTERN from indexing. Repeat the option to add patterns."
)
HELP_INTERACTIVE_SETUP = "Choose which detected directories remain included."
HELP_CAPTURE = (
    "Capture GROUP (structure, calls, types, imports, io), all/none, or a +TYPE/-TYPE "
    "override. Repeatable; later values override CGR_CAPTURE."
)

HELP_ASK_AGENT = "Ask one question, write the answer to stdout, and exit."

HELP_QUERY_OUTPUT_FORMAT = "Format --ask-agent output as table or json."

HELP_MCP_TRANSPORT = "Transport to serve: stdio or http."
HELP_MCP_HTTP_HOST = "HTTP bind host. Used only with --transport http."
HELP_MCP_HTTP_PORT = "HTTP bind port. Used only with --transport http."

HELP_DEADCODE_PROJECT_NAME = (
    "Project to scan. If omitted, cgr uses the only indexed project."
)
HELP_DEADCODE_ENTRY_POINT = (
    "Mark symbols ending with this qualified-name suffix as entry points. Repeatable."
)
HELP_DEADCODE_DECORATOR_ROOT = (
    "Mark symbols with this decorator as entry points. Extends the built-in set."
)
HELP_DEADCODE_EXCLUDE = (
    "Exclude symbols whose file path matches GLOB. '*' spans directories. Repeatable."
)
HELP_DEADCODE_INCLUDE_TESTS = (
    "Treat test code as reachable so exercised production code is not reported."
)
HELP_DEADCODE_CLASSES = (
    "Also report unreachable classes. This can include false positives for "
    "types used only by annotations or dynamic lookups."
)
HELP_DEADCODE_FORMAT = "Report format: table or json."
HELP_DEADCODE_OUTPUT = "Write the report to this file instead of stdout."
HELP_DEADCODE_FAIL_ON_FOUND = (
    "Exit with status 1 when any candidate is found. Useful in CI."
)

HELP_DELETE_PROJECT_NAME = "Project name to delete from the graph."
HELP_DELETE_PROJECT_REPO_PATH = (
    "Optional repo path. If set, the local hash cache is removed too."
)
HELP_COMMAND = "Command path to document, such as 'start' or 'daemon logs'."

CLI_COMMANDS: dict[CLICommandName, str] = {
    CLICommandName.START: CMD_START,
    CLICommandName.OPTIMIZE: CMD_OPTIMIZE,
    CLICommandName.MCP_SERVER: CMD_MCP_SERVER,
    CLICommandName.INDEX: CMD_INDEX,
    CLICommandName.EXPORT: CMD_EXPORT,
    CLICommandName.GRAPH_LOADER: CMD_GRAPH_LOADER,
    CLICommandName.STATS: CMD_STATS,
    CLICommandName.DEAD_CODE: CMD_DEAD_CODE,
    CLICommandName.DELETE_PROJECT: CMD_DELETE_PROJECT,
    CLICommandName.LANGUAGE: CMD_LANGUAGE,
    CLICommandName.DAEMON: CMD_DAEMON,
    CLICommandName.WORKSPACE: CMD_WORKSPACE,
    CLICommandName.STOP: CMD_STOP,
    CLICommandName.STATUS: CMD_STATUS,
    CLICommandName.DOCTOR: CMD_DOCTOR,
    CLICommandName.HELP: CMD_HELP,
}
