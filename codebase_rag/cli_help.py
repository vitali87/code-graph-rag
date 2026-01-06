from enum import StrEnum


class CLICommandName(StrEnum):
    START = "start"
    INDEX = "index"
    EXPORT = "export"
    OPTIMIZE = "optimize"
    MCP_SERVER = "mcp-server"
    GRAPH_LOADER = "graph-loader"
    LANGUAGE = "language"


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

CMD_LANGUAGE_GROUP = "CLI for managing language grammars"
CMD_LANGUAGE_ADD = "Add a new language grammar to the project."
CMD_LANGUAGE_LIST = "List all currently configured languages."
CMD_LANGUAGE_REMOVE = "Remove a language from the project."
CMD_LANGUAGE_CLEANUP = "Clean up orphaned git modules that weren't properly removed."

HELP_BATCH_SIZE = "Number of buffered nodes/relationships before flushing to Memgraph"
HELP_MEMGRAPH_HOST = "Memgraph host"
HELP_MEMGRAPH_PORT = "Memgraph port"
HELP_ORCHESTRATOR = (
    "Specify orchestrator as provider:model "
    "(e.g., ollama:llama3.2, openai:gpt-4, google:gemini-2.5-pro)"
)
HELP_CYPHER_MODEL = (
    "Specify cypher model as provider:model "
    "(e.g., ollama:codellama, google:gemini-2.5-flash)"
)
HELP_NO_CONFIRM = "Disable confirmation prompts for edit operations (YOLO mode)"

HELP_REPO_PATH_RETRIEVAL = "Path to the target repository for code retrieval"
HELP_REPO_PATH_INDEX = "Path to the target repository to index."
HELP_REPO_PATH_OPTIMIZE = "Path to the repository to optimize"
HELP_REPO_PATH_WATCH = "Path to the repository to watch."

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

HELP_EXCLUDE_PATTERNS = (
    "Additional directories to exclude from indexing. Can be specified multiple times."
)
HELP_INTERACTIVE_SETUP = (
    "Show interactive prompt to select which detected directories to keep. "
    "Without this flag, all directories matching ignore patterns are automatically excluded."
)

CLI_COMMANDS: dict[CLICommandName, str] = {
    CLICommandName.START: CMD_START,
    CLICommandName.INDEX: CMD_INDEX,
    CLICommandName.EXPORT: CMD_EXPORT,
    CLICommandName.OPTIMIZE: CMD_OPTIMIZE,
    CLICommandName.MCP_SERVER: CMD_MCP_SERVER,
    CLICommandName.GRAPH_LOADER: CMD_GRAPH_LOADER,
    CLICommandName.LANGUAGE: CMD_LANGUAGE,
}
