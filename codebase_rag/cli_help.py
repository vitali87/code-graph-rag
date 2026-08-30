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
    DUPLICATES = "duplicates"
    DELETE_PROJECT = "delete-project"
    DAEMON = "daemon"
    WORKSPACE = "workspace"
    TRACE = "trace"
    EDITS = "edits"
    GRAPH = "graph"
    CHECK = "check"
    STOP = "stop"
    STATUS = "status"
    HELP = "help"


APP_DESCRIPTION = (
    "Analyse source code with Tree-sitter, store its structure in a shared "
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
CMD_OPTIMIZE = "Run a language-focused code optimisation session"
CMD_MCP_SERVER = "Serve cgr tools over stdio or HTTP"
CMD_GRAPH_LOADER = "Summarise an exported graph JSON file"
CMD_LANGUAGE = "Manage language grammars and parser metadata"
CMD_DOCTOR = "Check dependencies, services, and configuration"
CMD_STATS = "Show graph node and relationship counts"
CMD_DEAD_CODE = "Report code that appears unreachable from known entry points"
CMD_DUPLICATES = "Report structurally duplicated functions and methods"
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

CMD_TRACE = "Ingest runtime call traces as dynamic CALLS edges"
CMD_TRACE_GROUP = CMD_TRACE
CMD_EDITS = (
    "Show or undo recorded edit transactions (multi-file edits applied through cgr)."
)
CMD_EDITS_GROUP = CMD_EDITS
CMD_EDITS_SHOW = (
    "List the last N recorded edit transactions, newest first, with their diffs."
)
CMD_EDITS_UNDO = "Reverse the last N recorded edit transactions, newest first; stops at the first file that changed since."
EPILOG_EDITS = "Run 'cgr help edits COMMAND' for command-specific help."
EXAMPLES_EDITS_SHOW = (
    "Examples:\n  cgr edits show\n  cgr edits show -n 5 --repo-path ~/proj"
)
EXAMPLES_EDITS_UNDO = (
    "Examples:\n  cgr edits undo\n  cgr edits undo -n 2 --repo-path ~/proj"
)
HELP_EDITS_COUNT = "How many transactions to show or undo."
HELP_EDITS_REPO_PATH = (
    "Repository root holding .cgr-edit-history.json (default: current directory)."
)
HELP_EDITS_DIFF = "Print each transaction's unified diff."
CMD_GRAPH = "Deterministic graph queries (resolve, definition, callers, callees, implementors, overrides, importers, tests-reaching) as JSON, no LLM."
CMD_GRAPH_GROUP = CMD_GRAPH
CMD_GRAPH_RESOLVE = "Resolve a name, dotted suffix, or path:line to qualified names."
CMD_GRAPH_DEFINITION = "File, span, docstring and source of one definition."
CMD_GRAPH_CALLERS = "Call sites that invoke a qualified name, one row per site."
CMD_GRAPH_CALLEES = "Call sites inside a qualified name, one row per site."
CMD_GRAPH_IMPLEMENTORS = "Types that inherit from or implement a type."
CMD_GRAPH_OVERRIDES = "Methods overriding a method, and the method it overrides."
CMD_GRAPH_IMPORTERS = (
    "Modules importing a module, with each import statement's location."
)
CMD_GRAPH_TESTS_REACHING = (
    "Tests from which a qualified name is reachable, with distance."
)
EPILOG_GRAPH = "Run 'cgr help graph COMMAND' for command-specific help."
HELP_GRAPH_PROJECT = "Project name in the graph (default: derived from --repo-path)."
HELP_GRAPH_REPO_PATH = (
    "Repository root the project name derives from and source is read from."
)
HELP_GRAPH_DEPTH = "How many hops to follow (1 to 5)."
CMD_CHECK = (
    "Report the structural delta of the working tree against a git ref: "
    "dangling callers, arity findings, new duplicates, new import cycles, "
    "tests reaching the edited symbols."
)
EXAMPLES_CHECK = (
    "Examples:\n  cgr check --base HEAD\n  cgr check --base origin/main --fail-on-found"
)
HELP_CHECK_BASE = (
    "Git ref the graph was indexed at; files differing from it are re-ingested."
)
HELP_CHECK_FAIL_ON_FOUND = (
    "Exit with status 1 when the delta reports dangling callers, arity "
    "findings, new duplicates or new import cycles."
)
CMD_TRACE_INGEST = "Resolve a trace file against a project and write dynamic edges"
CMD_TRACE_CONVERT = "Convert a V8 .cpuprofile (node --cpu-prof) to a trace file"

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
EXAMPLES_DUPLICATES = (
    "EXAMPLES\n\n"
    "  cgr duplicates --project-name my-project\n\n"
    "  cgr duplicates --threshold 0.9 --format json --fail-on-found"
)
EXAMPLES_DELETE_PROJECT = "EXAMPLE\n\n  cgr delete-project --name my-project"
EXAMPLES_HELP = "EXAMPLES\n\n  cgr help start\n\n  cgr help daemon logs"

EPILOG_LANGUAGE = "Run 'cgr help language COMMAND' for command-specific help."
EPILOG_DAEMON = "Run 'cgr help daemon COMMAND' for command-specific help."
EPILOG_WORKSPACE = "Run 'cgr help workspace COMMAND' for command-specific help."
EPILOG_TRACE = "Run 'cgr help trace COMMAND' for command-specific help."

HELP_TRACE_REPO_PATH = (
    "Repository the trace was recorded against. Used to derive the project "
    "name and re-anchor traced file paths."
)
HELP_TRACE_PROJECT_NAME = (
    "Project name to ingest into. Defaults to the name derived from --repo-path."
)
EXAMPLES_TRACE_INGEST = (
    "EXAMPLE\n\n  pytest --cgr-trace\n\n"
    "  cgr trace ingest cgr-trace.jsonl --repo-path ./my-repo"
)
HELP_TRACE_OUTPUT = "Where to write the converted trace file."
HELP_TRACE_WORKLOAD = "Workload label to attach to every converted record."
HELP_TRACE_INCLUDE = (
    "Comma-separated namespace prefixes to keep (dotnet-trace speedscope "
    "profiles carry no file paths, so scoping is by name)."
)
HELP_TRACE_LANGUAGE = (
    "Override the auto-detected source language. Rust pprof profiles share Go's "
    "gzipped-protobuf format, so pass 'rust' to demangle them as Rust. For an "
    "eBPF profile of a managed runtime, pass '--format ebpf --language python' "
    "(or '--language jvm') together to resolve the in-kernel-unwound source "
    "frames against that language's graph ('python' and 'jvm' are eBPF-only "
    "overrides)."
)
HELP_TRACE_FORMAT = (
    "Force a profile format instead of auto-detecting it. Use 'ebpf' for pprof "
    "profiles from an eBPF continuous profiler (Parca, Pyroscope, OTel, perf)."
)
HELP_TRACE_PATH_MAP = (
    "Re-anchor a production build path to the repo, as BUILD_PREFIX=REPO_PREFIX. "
    "Repeatable; applied before the in-repo check (ebpf format)."
)
HELP_TRACE_BUILD_ID = (
    "Keep only frames from the mapping with this build id or binary filename "
    "(ebpf format); other binaries are seen through."
)
HELP_TRACE_SERVICE = (
    "Keep only samples whose label matches, as KEY=VALUE (ebpf format)."
)
HELP_TRACE_LABEL = (
    "Sample label whose value becomes each edge's workload (ebpf format)."
)
HELP_TRACE_COMMIT = (
    "The commit the profiled binary was built from; warns if it differs from "
    "the repository HEAD at convert time (ebpf format)."
)
CMD_TRACE_PULL = (
    "Download a pprof profile from an eBPF continuous profiler over HTTP and "
    "convert it (Parca download, Pyroscope render?format=pprof, or any URL)."
)
HELP_TRACE_PULL_SAVE = (
    "Keep the downloaded pprof at this path; by default it is removed after conversion."
)
HELP_TRACE_PULL_HEADER = (
    "Extra HTTP request header as NAME=VALUE (e.g. Authorization=Bearer TOKEN); "
    "repeatable."
)
HELP_TRACE_PULL_TIMEOUT = "HTTP request timeout in seconds (default 60)."
ERR_TRACE_PULL_BAD_URL = "Unsupported URL {url}; expected an http:// or https:// URL."
# The header value can be a bearer token, so it is never echoed back.
ERR_TRACE_PULL_BAD_HEADER = (
    "Invalid --header; expected NAME=VALUE (for example Authorization=Bearer TOKEN)."
)
ERR_TRACE_PULL_FAILED = "Could not download {url}: {error}."
ERR_TRACE_PULL_TOO_LARGE = (
    "Profile at {url} exceeds the maximum download size (256 MB)."
)
ERR_TRACE_PULL_SAVE_EQUALS_OUTPUT = (
    "--save and --output must be different paths (--output holds the converted "
    "trace, --save the downloaded profile)."
)
EXAMPLES_TRACE_PULL = (
    "EXAMPLE\n\n"
    "  cgr trace pull https://parca.example/api/... \\\n"
    "      --repo-path ./my-repo --build-id 8f3a \\\n"
    "      --path-map /build/src/=./my-repo/src/ \\\n"
    "      --header Authorization=Bearer $TOKEN\n\n"
    "  cgr trace ingest cgr-trace.jsonl --repo-path ./my-repo"
)
ERR_TRACE_CONVERT_BAD_FORMAT = "Unknown --format '{format}'; supported: ebpf."
ERR_TRACE_CONVERT_BAD_PATH_MAP = (
    "Invalid --path-map '{value}'; expected BUILD_PREFIX=REPO_PREFIX."
)
ERR_TRACE_CONVERT_BAD_SERVICE = "Invalid --service '{value}'; expected KEY=VALUE."
MSG_TRACE_COMMIT_MISMATCH = (
    "Profiled commit {profiled} differs from repository HEAD {head}; edges may "
    "resolve against moved or renamed code."
)
ERR_TRACE_CONVERT_BAD_LANGUAGE = (
    "Unknown --language '{language}'; supported override values: {supported}."
)
ERR_TRACE_CONVERT_NEEDS_REPO = "V8 cpuprofiles need --repo-path to scope frames."
ERR_TRACE_CONVERT_NEEDS_INCLUDE = (
    "speedscope profiles need --include namespace prefixes to scope frames."
)
EXAMPLES_TRACE_CONVERT = (
    "EXAMPLE\n\n  node --cpu-prof --cpu-prof-name=run.cpuprofile app.js\n\n"
    "  cgr trace convert run.cpuprofile --repo-path ./my-repo\n\n"
    "  cgr trace ingest cgr-trace.jsonl --repo-path ./my-repo"
)

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
HELP_NO_SYNC = "Do not synchronise the graph before starting the assistant."
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
    "(for example openai:gpt-5.6-terra or ollama:qwen2.5-coder)."
)
HELP_CYPHER_MODEL = "Model used to generate Cypher, in provider:model form."
HELP_NO_CONFIRM = "Skip edit confirmation prompts."
HELP_NO_INSTRUCTIONS = (
    "Do not load ~/.cgr.md or <repo>/.cgr.md into the session prompt."
)

HELP_REPO_PATH_RETRIEVAL = "Repository to open. Defaults to the current directory."
HELP_REPO_PATH_INDEX = "Repository to index. Defaults to the current directory."
HELP_REPO_PATH_OPTIMIZE = "Repository to optimise. Defaults to the current directory."
HELP_REPO_PATH_WATCH = "Repository to watch."
HELP_VERSION = "Show the version and exit."
HELP_QUIET = "Suppress progress, banners, and informational logs."

HELP_DEBOUNCE = "Debounce delay in seconds. Set to 0 to disable debouncing."
HELP_MAX_WAIT = (
    "Maximum wait time in seconds before forcing an update during continuous edits."
)

HELP_UPDATE_GRAPH = "Parse the repository and sync its graph before continuing."
HELP_CLEAN_DB = (
    "DESTRUCTIVE: Delete every project from the shared graph and clear the selected "
    "repository's sync cache. With --update-graph, rebuild after deletion. Asks for "
    "confirmation when other projects would be destroyed; use --yes to skip the prompt."
)
HELP_ASSUME_YES = (
    "Answer yes to destructive confirmations, such as the one --clean asks before "
    "deleting other projects from the shared graph."
)
HELP_OUTPUT_GRAPH = "Write the updated graph to PATH as JSON. Requires --update-graph."
HELP_OUTPUT_PATH = "Write the exported graph to PATH."
HELP_OUTPUT_PROTO_DIR = "Write protobuf index files under DIRECTORY."
HELP_SPLIT_INDEX = "Write separate nodes.bin and relationships.bin files."
HELP_FORMAT_JSON = "Use JSON output. Other export formats are not supported."
HELP_LANGUAGE_ARG = "Language to optimise, such as python, java, javascript, or cpp."
HELP_REFERENCE_DOC = "Reference document to use during optimisation."
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
    "Exclude symbols whose file path matches GLOB. The glob must cover the "
    "whole repo-relative path ('*' spans directories) and be quoted "
    "('tests/*' for a root-level tests directory, '*/tests/*' for nested "
    "ones) so the shell cannot expand it first. Repeatable."
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
HELP_DEADCODE_MIN_RESOLUTION = (
    "Ignore call edges below this confidence when deciding liveness: "
    "heuristic < overload < exact < trace_confirmed (dynamic counts as confirmed)."
)
HELP_DEADCODE_FAIL_ON_FOUND = (
    "Exit with status 1 when any candidate is found. Useful in CI."
)

HELP_DUPLICATES_PROJECT_NAME = (
    "Project to scan. If omitted, cgr uses the only indexed project."
)
HELP_DUPLICATES_THRESHOLD = (
    "Minimum branch-overlap similarity for a near-duplicate pair, 0-1."
)
HELP_DUPLICATES_MIN_SIZE = (
    "Minimum skeleton size (tree nodes) for a function to be considered. "
    "Filters trivial getters and one-liners."
)
HELP_DUPLICATES_EXACT_ONLY = (
    "Report only identical-fingerprint clone groups; skip similarity scoring."
)
HELP_DUPLICATES_EXCLUDE = (
    "Exclude symbols whose file path matches GLOB. The glob must cover the "
    "whole repo-relative path ('*' spans directories) and be quoted "
    "('tests/*' for a root-level tests directory, '*/tests/*' for nested "
    "ones) so the shell cannot expand it first. Repeatable."
)
HELP_DUPLICATES_FORMAT = "Report format: table or json."
HELP_DUPLICATES_OUTPUT = "Write the report to this file instead of stdout."
HELP_DUPLICATES_FAIL_ON_FOUND = (
    "Exit with status 1 when any duplicate is found. Useful in CI."
)
HELP_DUPLICATES_OPEN = (
    "Open group N's first two members side by side in your editor "
    "(CGR_EDITOR picks the editor; CGR_DIFF_COMMAND overrides the command)."
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
    CLICommandName.DUPLICATES: CMD_DUPLICATES,
    CLICommandName.DELETE_PROJECT: CMD_DELETE_PROJECT,
    CLICommandName.LANGUAGE: CMD_LANGUAGE,
    CLICommandName.DAEMON: CMD_DAEMON,
    CLICommandName.TRACE: CMD_TRACE,
    CLICommandName.EDITS: CMD_EDITS,
    CLICommandName.GRAPH: CMD_GRAPH,
    CLICommandName.CHECK: CMD_CHECK,
    CLICommandName.WORKSPACE: CMD_WORKSPACE,
    CLICommandName.STOP: CMD_STOP,
    CLICommandName.STATUS: CMD_STATUS,
    CLICommandName.DOCTOR: CMD_DOCTOR,
    CLICommandName.HELP: CMD_HELP,
}
CMD_VERIFY_INDEX = "Verify a protobuf index against its provenance manifest"
HELP_VERIFY_INDEX_DIR = "Directory holding the index artifacts and manifest.json."
HELP_TRUSTED_MANIFEST_SHA = (
    "Externally trusted sha256 of manifest.json (e.g. from an attestation); "
    "anchors verification beyond local self-consistency."
)
CMD_DIFF_INDEX = "Structural diff between two protobuf index snapshots"
HELP_DIFF_OLD = "Directory holding the OLD snapshot artifacts."
HELP_DIFF_NEW = "Directory holding the NEW snapshot artifacts."
HELP_DIFF_JSON_OUT = "Write the JSON delta to FILE instead of stdout."
