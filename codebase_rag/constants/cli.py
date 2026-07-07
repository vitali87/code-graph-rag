from enum import StrEnum


class ModelRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    CYPHER = "cypher"


class Provider(StrEnum):
    OLLAMA = "ollama"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    AZURE = "azure"
    LITELLM_PROXY = "litellm_proxy"


class Color(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    CYAN = "cyan"
    RED = "red"
    MAGENTA = "magenta"
    BLUE = "blue"


class PermissionMode(StrEnum):
    NORMAL = "normal"
    YOLO = "yolo"


class StyleModifier(StrEnum):
    BOLD = "bold"
    DIM = "dim"
    NONE = ""


class FileAction(StrEnum):
    READ = "read"
    EDIT = "edit"


DEFAULT_MODEL_ROLE = "model"

DEFAULT_REGION = "us-central1"
DEFAULT_MODEL = "llama3.2"
DEFAULT_API_KEY = "ollama"

ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_GOOGLE_API_KEY = "GOOGLE_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_AZURE_API_KEY = "AZURE_API_KEY"
ENV_AZURE_ENDPOINT = "AZURE_OPENAI_ENDPOINT"
ENV_AZURE_API_VERSION = "AZURE_API_VERSION"

HELP_ARG = "help"


class GoogleProviderType(StrEnum):
    GLA = "gla"
    VERTEX = "vertex"


# (H) CLI error and info messages
CLI_ERR_OUTPUT_REQUIRES_UPDATE = (
    "Error: --output/-o option requires --update-graph to be specified."
)
CLI_ERR_ONLY_JSON = "Error: Currently only JSON format is supported."
CLI_ERR_JSON_REQUIRES_ASK_AGENT = (
    "Error: --output-format json requires --ask-agent/-a; "
    "it only applies to single-query output."
)
CLI_ERR_PATH_NOT_EXISTS = "Error: --repo-path does not exist: {path}"
CLI_ERR_PATH_NOT_DIR = "Error: --repo-path is not a directory: {path}"
CLI_WARN_NOT_GIT_REPO = "Warning: --repo-path is not a Git repository: {path}"
CLI_ERR_STARTUP = "Startup Error: {error}"
CLI_ERR_CONFIG = "Configuration Error: {error}"
CLI_ERR_INDEXING = "An error occurred during indexing: {error}"
CLI_ERR_EXPORT_FAILED = "Failed to export graph: {error}"
CLI_ERR_LOAD_GRAPH = "Failed to load graph: {error}"
CLI_ERR_MCP_SERVER = "MCP Server Error: {error}"

CLI_MSG_UPDATING_GRAPH = "Updating knowledge graph for: {path}"
CLI_MSG_SYNCING_GRAPH = "Syncing knowledge graph for: {path} (use --no-sync to skip)"
CLI_MSG_WORKSPACE_SYNCING = "Syncing workspace '{name}' ({count} repos)..."
CLI_MSG_WORKSPACE_SYNC_REPO = (
    "[{idx}/{total}] Syncing {path} as project '{project_name}'"
)
CLI_MSG_WORKSPACE_EMPTY = (
    "Workspace '{name}' has no repos (use cgr workspace add-repo)."
)
MSG_SYNCING_KNOWLEDGE_GRAPH = (
    "[bold cyan]Syncing knowledge graph[/bold cyan] (incremental, --no-sync to skip)"
)
MSG_SYNCING_WORKSPACE = (
    "[bold cyan]Syncing workspace '{name}'[/bold cyan] ({count} repos)"
)
CLI_MSG_SYNC_SKIPPED = "Knowledge graph already in sync for '{project}' ({elapsed:.2f}s, no changes detected)."
CLI_MSG_SYNC_DONE = "Knowledge graph sync done for '{project}' in {elapsed:.2f}s."
CLI_MSG_CLEANING_DB = "Cleaning database..."
CLI_MSG_CLEANING_HASH_CACHE = "Removing hash cache: {path}"
CLI_MSG_CLEAN_DONE = "Clean completed successfully!"
CLI_MSG_DELETING_PROJECT = "Deleting project '{project_name}' from the graph..."
CLI_MSG_PROJECT_DELETED = "Project '{project_name}' deleted successfully."
CLI_ERR_PROJECT_NOT_FOUND = (
    "Project '{project_name}' not found. Available projects: {projects}"
)
CLI_ERR_PROJECT_NAME_REQUIRED = (
    "Error: --name is required and must be a non-empty project name."
)
CLI_ERR_DELETE_PROJECT_FAILED = "Failed to delete project '{project_name}': {error}"
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
CLI_MSG_VERSION = "{package} version {version}"
CLI_MSG_HINT_TARGET_REPO = (
    "\nHint: Make sure TARGET_REPO_PATH environment variable is set."
)
CLI_MSG_GRAPH_SUMMARY = "Graph Summary:"
CLI_MSG_CONNECTING_STATS = "Fetching graph statistics..."
CLI_STATS_NODE_TITLE = "Node Statistics"
CLI_STATS_REL_TITLE = "Relationship Statistics"
CLI_STATS_COL_NODE_TYPE = "Node Type"
CLI_STATS_COL_REL_TYPE = "Relationship Type"
CLI_STATS_COL_COUNT = "Count"
CLI_STATS_TOTAL_RELS = "Total Relationships"
CLI_STATS_UNKNOWN = "Unknown"
CLI_ERR_STATS_FAILED = "Failed to get graph statistics: {error}"

CLI_DEADCODE_CONNECTING = "Scanning for unreachable functions and methods..."
CLI_DEADCODE_TABLE_TITLE = "Dead Code Candidates ({project_name})"
CLI_DEADCODE_COL_KIND = "Kind"
CLI_DEADCODE_COL_QUALIFIED_NAME = "Qualified Name"
CLI_DEADCODE_COL_LINES = "Lines"
CLI_DEADCODE_LINE_RANGE = "{start}-{end}"
CLI_DEADCODE_SUMMARY = "{count} candidate(s) for review."
CLI_DEADCODE_NONE = "No unreachable functions or methods found."
CLI_DEADCODE_WRITTEN = "Wrote {count} candidate(s) to {path}"
CLI_ERR_DEADCODE_FAILED = "Failed to scan for dead code: {error}"
CLI_ERR_DEADCODE_NO_PROJECTS = (
    "No projects found in the graph. Index a repository first with 'cgr start'."
)
CLI_ERR_DEADCODE_AMBIGUOUS_PROJECT = (
    "Multiple projects found: {projects}. Specify which one with --project-name/-n."
)
CLI_MSG_AUTO_EXCLUDE = (
    "Auto-excluding common directories (venv, node_modules, .git, etc.). "
    "Use --interactive-setup to customize."
)

UI_DIFF_FILE_HEADER = "[bold cyan]File: {path}[/bold cyan]"
UI_NEW_FILE_HEADER = "[bold cyan]New file: {path}[/bold cyan]"
UI_SHELL_COMMAND_HEADER = "[bold cyan]Shell command:[/bold cyan]"
UI_TOOL_APPROVAL = "[bold yellow]⚠️  Tool '{tool_name}' requires approval:[/bold yellow]"
UI_FEEDBACK_PROMPT = "Feedback (why rejected, or press Enter to skip)"
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
UI_MODEL_SWITCHED = "[bold green]Model switched to: {model}[/bold green]"
UI_MODEL_CURRENT = "[bold cyan]Current model: {model}[/bold cyan]"
UI_MODEL_SWITCH_ERROR = "[bold red]Failed to switch model: {error}[/bold red]"
UI_MODEL_USAGE = "[bold yellow]Usage: /model <provider:model> (e.g., /model google:gemini-3.1-pro-preview)[/bold yellow]"
UI_HELP_COMMANDS = """[bold cyan]Available commands:[/bold cyan]
  /model <provider:model> - Switch to a different model
  /model                  - Show current model
  /help                   - Show this help
  exit, quit              - Exit the session"""
UI_TOOL_ARGS_FORMAT = "    Arguments: {args}"
UI_REFERENCE_DOC_INFO = " using the reference document: {reference_document}"
UI_INPUT_PROMPT_HTML = (
    "<ansigreen><b>{prompt}</b></ansigreen> <ansiyellow>{hint}</ansiyellow>: "
)
YES_ANSWER = "y"
YES_ANSWERS = frozenset({"y", "yes", ""})
NO_ANSWERS = frozenset({"n", "no"})
DIFF_GIT_HEADER = "diff --git "
DIFF_CONTINUATION_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "@@ ",
    "+",
    "-",
    " ",
    "\\ ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "rename from ",
    "rename to ",
    "similarity index ",
    "Binary files ",
)

# (H) CLI exit commands
EXIT_COMMANDS = frozenset({"exit", "quit"})

# (H) CLI commands
MODEL_COMMAND_PREFIX = "/model"
HELP_COMMAND = "/help"

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
MSG_TOOL_CALL_CANCELLED = "Tool call cancelled by user."
MSG_CHAT_INSTRUCTIONS = (
    "Ask questions about your codebase graph. Type 'exit' or 'quit' to end."
)

# (H) Default titles and prompts
DEFAULT_TABLE_TITLE = "Code-Graph-RAG Initializing..."
OPTIMIZATION_TABLE_TITLE = "Optimization Session Configuration"
PROMPT_ASK_QUESTION = "Ask a question"
PROMPT_YOUR_RESPONSE = "Your response"
MULTILINE_INPUT_HINT = (
    "(Press Ctrl+J or Ctrl+E to submit, Enter for new line, Shift+Tab to toggle mode)"
)
PERMISSION_MODE_NORMAL_LABEL = "● Normal mode (asks before destructive)"
PERMISSION_MODE_YOLO_LABEL = "● YOLO mode (auto-approve, allowlist off)"
PERMISSION_MODE_TOGGLED = "Permission mode: {label}"
STATUS_BAR_BRANCH_CLEAN_HTML = (
    '<style bg="ansigreen" fg="ansiblack"> ⎇ {branch} </style>'
)
STATUS_BAR_BRANCH_DIRTY_HTML = (
    '<style bg="ansiyellow" fg="ansiblack"> ⎇ {branch} ± </style>'
)
STATUS_BAR_BRANCH_CLEAN_PLAIN = " ⎇ {branch} "
STATUS_BAR_BRANCH_DIRTY_PLAIN = " ⎇ {branch} ± "
STATUS_BAR_BRANCH_RICH_TEXT = " ⎇ {branch}{marker} "
STATUS_BAR_CLEAN_STYLE = "black on green"
STATUS_BAR_DIRTY_STYLE = "black on yellow"
STATUS_BAR_DIRTY_MARKER = " ±"
STATUS_BAR_SPINNER = "dots"
STATUS_BAR_SEPARATOR_CHAR = "─"
STATUS_BAR_SEPARATOR_COLOR = "#666666"
STATUS_BAR_TOKEN_HTML = '  <style fg="{color}">{used} / {max_ctx} ({pct})</style>'
STATUS_BAR_CONFIG_COLOR = "#888888"
STATUS_BAR_CONFIG_LABEL_COLOR = "#5fafd7"
STATUS_BAR_CONFIG_SEPARATOR = "  │  "
STATUS_BAR_CONFIG_LABEL_O = "O"
STATUS_BAR_CONFIG_LABEL_C = "C"
STATUS_BAR_CONFIG_LABEL_EDIT = "edit"
STATUS_BAR_CONFIG_LABEL_INSTRUCTIONS = "instructions"
STATUS_BAR_CONFIG_LABEL_REPO = "repo"
STATUS_BAR_EDIT_ON = "on"
STATUS_BAR_EDIT_OFF = "off"
TOKEN_THRESHOLD_WARNING = 50
TOKEN_THRESHOLD_CRITICAL = 80
TOKEN_COLOR_OK = "green"
TOKEN_COLOR_WARNING = "yellow"
TOKEN_COLOR_CRITICAL = "red"

# (H) Interactive setup prompt - grouped view
INTERACTIVE_TITLE_GROUPED = "Detected Directories (will be excluded unless kept)"
INTERACTIVE_TITLE_NESTED = "Nested paths in '{pattern}'"
INTERACTIVE_COL_NUM = "#"
INTERACTIVE_COL_PATTERN = "Pattern"
INTERACTIVE_COL_NESTED = "Nested"
INTERACTIVE_COL_PATH = "Path"
INTERACTIVE_STYLE_DIM = "dim"
INTERACTIVE_STATUS_DETECTED = "auto-detected"
INTERACTIVE_STATUS_CLI = "--exclude"
INTERACTIVE_STATUS_CGRIGNORE = ".cgrignore"
INTERACTIVE_NESTED_SINGULAR = "{count} dir"
INTERACTIVE_NESTED_PLURAL = "{count} dirs"
INTERACTIVE_INSTRUCTIONS_GROUPED = (
    "These directories would normally be excluded. "
    "Options: 'all' (keep all), 'none' (keep none), "
    "numbers like '1,3' (keep groups), or '1e' to expand group 1"
)
INTERACTIVE_INSTRUCTIONS_NESTED = (
    "Select paths to keep from '{pattern}'. "
    "Options: 'all', 'none', or numbers like '1,3'"
)
INTERACTIVE_PROMPT_KEEP = "Keep"
INTERACTIVE_KEEP_ALL = "all"
INTERACTIVE_KEEP_NONE = "none"
INTERACTIVE_EXPAND_SUFFIX = "e"
INTERACTIVE_BFS_MAX_DEPTH = 10
INTERACTIVE_DEFAULT_GROUP = "."

REALTIME_LOGGER_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

WATCHER_SLEEP_INTERVAL = 1
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_ERROR = "ERROR"

# (H) Debounce settings for realtime watcher
DEFAULT_DEBOUNCE_SECONDS = 5
DEFAULT_MAX_WAIT_SECONDS = 30

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
SHELL_PIPE_OPERATORS = ("|", "&&", "||", ";")
SHELL_SUBSHELL_PATTERNS = ("$(", "`")
SHELL_REDIRECT_OPERATORS = frozenset({">", ">>", "<", "<<"})

# (H) Dangerous commands - absolutely blocked
SHELL_DANGEROUS_COMMANDS = frozenset(
    {
        "dd",
        "mkfs",
        "mkfs.ext4",
        "mkfs.ext3",
        "mkfs.xfs",
        "mkfs.btrfs",
        "mkfs.vfat",
        "fdisk",
        "parted",
        "shred",
        "wipefs",
        "mkswap",
        "swapon",
        "swapoff",
        "mount",
        "umount",
        "insmod",
        "rmmod",
        "modprobe",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
        "telinit",
        "systemctl",
        "service",
        "chroot",
        "nohup",
        "disown",
        "crontab",
        "at",
        "batch",
    }
)

# (H) Dangerous rm flags
SHELL_RM_DANGEROUS_FLAGS = frozenset({"-rf", "-fr"})
SHELL_RM_FORCE_FLAG = "-f"

# (H) System directories to protect from rm -rf
SHELL_SYSTEM_DIRECTORIES = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)

# (H) Dangerous patterns for full pipeline (cross-segment patterns with pipes/operators)
SHELL_DANGEROUS_PATTERNS_PIPELINE = (
    (r"(wget|curl)\s+.*\|\s*(sh|bash|zsh|ksh)", "remote script execution"),
    (r"(wget|curl)\s+.*>\s*.*\.sh\s*&&", "download and execute script"),
    (r"base64\s+-d.*\|", "base64 decode pipe execution"),
)

# (H) Build system directory regex pattern dynamically
_SYSTEM_DIRS_PATTERN = "|".join(sorted(SHELL_SYSTEM_DIRECTORIES))

# (H) Dangerous patterns for individual segments (per-command patterns)
SHELL_DANGEROUS_PATTERNS_SEGMENT = (
    (r"rm\s+.*-[rf]+\s+/($|\s)", "rm with root path"),
    (rf"rm\s+.*-[rf]+\s+/({_SYSTEM_DIRS_PATTERN})($|/|\s)", "rm with system directory"),
    (r"rm\s+.*-[rf]+\s+~($|\s)", "rm with home directory"),
    (r"rm\s+.*-[rf]+\s+\*", "rm with wildcard"),
    (r"rm\s+.*-[rf]+\s+\.\.", "rm with parent directory"),
    (r"dd\s+.*of=/dev/", "dd writing to device"),
    (r">\s*/dev/sd[a-z]", "redirect to disk device"),
    (r">\s*/dev/nvme", "redirect to nvme device"),
    (r">\s*/dev/null.*<", "null device manipulation"),
    (r"chmod\s+.*-R\s+777\s+/", "recursive 777 on root"),
    (r"chmod\s+.*777\s+/($|\s)", "777 on root"),
    (r"chown\s+.*-R\s+.*\s+/($|\s)", "recursive chown on root"),
    (r":\(\)\s*\{.*:\s*\|", "fork bomb pattern"),
    (r"mv\s+.*\s+/dev/null", "move to /dev/null"),
    (r"ln\s+-[sf]+\s+/dev/null", "symlink to /dev/null"),
    (r"cat\s+.*/dev/zero", "cat /dev/zero"),
    (r"cat\s+.*/dev/random", "cat /dev/random"),
    (r">\s*/etc/passwd", "overwrite passwd"),
    (r">\s*/etc/shadow", "overwrite shadow"),
    (r">\s*/etc/sudoers", "overwrite sudoers"),
    (r"echo\s+.*>\s*/etc/", "write to /etc"),
    (
        r"python.*-c.*(import\s+os|__import__\s*\(\s*['\"]os['\"]\s*\))",
        "python os import in command",
    ),
    (r"perl\s+-e", "perl one-liner"),
    (r"ruby\s+-e", "ruby one-liner"),
    (r"nc\s+-[el]", "netcat listener"),
    (r"ncat\s+-[el]", "ncat listener"),
    (r"/dev/tcp/", "bash tcp device"),
    (r"eval\s+", "eval command"),
    (r"exec\s+[0-9]+<>", "exec file descriptor manipulation"),
    (r"awk\s+.*system\s*\(", "awk system() call"),
    (r"awk\s+.*getline\s*[<|]", "awk getline file/pipe execution"),
    (r"sed\s+.*s(.).*?\1.*?\1[gip]*e[gip]*", "sed execute flag"),
    (r"xargs\s+.*(rm|chmod|chown|mv|dd|mkfs)", "xargs with destructive command"),
    (r"xargs\s+-I.*sh", "xargs shell execution"),
    (r"xargs\s+.*bash", "xargs bash execution"),
)

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

# (H) Document analyzer constants
MSG_DOC_NO_CANDIDATES = "No valid text found in response candidates."
MSG_DOC_NO_CONTENT = "No text content received from the API."

SHELL_CMD_WHERE = "where"
SHELL_CMD_WHICH = "which"
