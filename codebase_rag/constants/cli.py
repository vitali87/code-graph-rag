# CLI/TUI messages, styles, prompts, and interactive display constants.

from enum import StrEnum


class Color(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    CYAN = "cyan"
    RED = "red"
    MAGENTA = "magenta"
    BLUE = "blue"


class KeyBinding(StrEnum):
    CTRL_J = "c-j"
    CTRL_E = "c-e"
    ENTER = "enter"
    CTRL_C = "c-c"
    SHIFT_TAB = "s-tab"
    UP = "up"
    DOWN = "down"


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


HELP_ARG = "help"

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
CLI_WARN_CLEAN_OTHER_PROJECTS = (
    "--clean deletes EVERY project from the shared graph, not just "
    "'{project_name}'.\n{count} other project(s) would be destroyed: {projects}"
)
CLI_PROMPT_CLEAN_CONFIRM = "Delete all {count} project(s) from the shared graph?"
CLI_ERR_CLEAN_NEEDS_CONFIRMATION = (
    "Refusing to run --clean without a terminal to confirm on. "
    "Re-run with --yes to delete every project in the shared graph, or drop "
    "--clean to update '{project_name}' in place."
)
CLI_ERR_CLEAN_UNKNOWN_PROJECTS = (
    "Refusing to run --clean: the existing projects could not be listed, so "
    "there is no way to show what the wipe would destroy. Fix the graph "
    "connection, or pass --yes to delete everything regardless."
)
CLI_MSG_CLEAN_ABORTED = "Aborted: the graph was left untouched."
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
CLI_MSG_MANIFEST_WRITTEN = "Provenance manifest written to {path}"
CLI_MSG_VERIFY_PROBLEM = "VERIFY FAILED: {problem}"
CLI_MSG_VERIFY_OK = "Index verified against its manifest: {path}"
CLI_MSG_DIFF_WRITTEN = "Graph delta written to {path}"
CLI_MSG_DIFF_EMPTY = "No structural delta: the snapshots are equivalent"
CLI_MSG_CONNECTING_MEMGRAPH = "Connecting to Memgraph to export graph..."
CLI_MSG_EXPORTING_DATA = "Exporting graph data..."
CLI_MSG_OPTIMIZATION_TERMINATED = "\nOptimization session terminated by user."
CLI_MSG_MCP_TERMINATED = "\nMCP server terminated by user."
PACKAGE_NAME = "code-graph-rag"
# How a test spawns the CLI as a subprocess. Shared so the deadline gate in
# test_cli_smoke can assert no other test file spawns it outside that gate
# (issue #1655). Both spellings pay the same startup cost, so both need the
# same deadline: `-m codebase_rag.cli`, and the console scripts in
# [project.scripts].
CLI_MODULE_INVOCATION = "codebase_rag.cli"
CLI_ENTRY_POINT_NAMES: frozenset[str] = frozenset({"cgr", PACKAGE_NAME})
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
CLI_STATS_TOTAL_NODES = "Total Nodes"
CLI_STATS_TOTAL_RELS = "Total Relationships"
CLI_STATS_UNKNOWN = "Unknown"
CLI_ERR_STATS_FAILED = "Failed to get graph statistics: {error}"
# `cgr check` (issue #1525).
CHECK_GIT_FAILED = "Cannot diff the working tree against {base}: {error}"
CHECK_NOT_INDEXED = (
    "Project {project} is not indexed; run 'cgr start --update-graph' at the "
    "base ref first."
)

CLI_DEADCODE_CONNECTING = "Scanning for unreachable functions and methods..."
CLI_DEADCODE_TABLE_TITLE = "Dead Code Candidates ({project_name})"
CLI_DEADCODE_COL_KIND = "Kind"
CLI_DEADCODE_COL_QUALIFIED_NAME = "Qualified Name"
CLI_DEADCODE_COL_LINES = "Lines"
CLI_DEADCODE_LINE_RANGE = "{start}-{end}"
CLI_DEADCODE_SUMMARY = "{count} candidate(s) for review."
CLI_DEADCODE_NONE = "No unreachable functions or methods found."
CLI_DEADCODE_WRITTEN = "Wrote {count} candidate(s) to {path}"
CLI_DEADCODE_STRUCTURAL_TIER_SKIPPED = (
    "{count} symbol(s) in structural-tier languages were not analyzed "
    "(no call graph for these languages)."
)
CLI_ERR_DEADCODE_FAILED = "Failed to scan for dead code: {error}"
CLI_ERR_DEADCODE_NO_PROJECTS = (
    "No projects found in the graph. Index a repository first with 'cgr start'."
)
CLI_ERR_DEADCODE_AMBIGUOUS_PROJECT = (
    "Multiple projects found: {projects}. Specify which one with --project-name/-n."
)
CLI_ERR_DEADCODE_UNKNOWN_PROJECT = (
    "Project '{project}' is not indexed. Indexed projects: {projects}."
)

CLI_DUPLICATES_CONNECTING = "Scanning for structurally duplicated functions..."
CLI_DUPLICATES_TABLE_TITLE = "Duplicate Code Groups ({project_name})"
CLI_DUPLICATES_COL_GROUP = "Group"
CLI_DUPLICATES_COL_SIMILARITY = "Similarity"
CLI_DUPLICATES_COL_KIND = "Kind"
CLI_DUPLICATES_COL_MEMBER = "Member"
CLI_DUPLICATES_COL_LOCATION = "Location"
CLI_DUPLICATES_LOCATION = "{path}:{start}-{end}"
CLI_DUPLICATES_SIMILARITY_EXACT = "100%"
CLI_DUPLICATES_SIMILARITY_PCT = "{pct:.0f}%"
CLI_DUPLICATES_SUMMARY = "{groups} duplicate group(s) covering {members} function(s)."
CLI_DUPLICATES_NONE = "No duplicated functions or methods found."
CLI_DUPLICATES_WRITTEN = "Wrote {count} group(s) to {path}"
CLI_DUPLICATES_STRUCTURAL_TIER_SKIPPED = (
    "{count} symbol(s) were not analyzed (no structural fingerprint: "
    "pattern-tier language or bodiless declaration)."
)
CLI_DUPLICATES_STALE_GRAPH = (
    "None of the {count} function(s)/method(s) in this project carry a "
    "structural fingerprint; the graph predates fingerprint stamping. "
    "Re-index the repository (cgr start --update-graph) and rerun."
)
CLI_DUPLICATES_TRUNCATED_NOTICE = (
    "Similar-group enumeration reached its cap; some qualifying groups may "
    "be missing. Raise --threshold or --min-size to narrow the scan."
)
CLI_ERR_DUPLICATES_FAILED = "Failed to scan for duplicates: {error}"
CLI_ERR_DUPLICATES_UNKNOWN_PROJECT = (
    "Project '{project}' is not indexed. Indexed projects: {projects}."
)

# Clickable report locations (OSC 8 hyperlinks) and `duplicates --open`.
# A template receives {path} (absolute, URL-quoted for URLs) and {line};
# diff-command templates receive {left} and {right}.
EDITOR_AUTO = "auto"
EDITOR_NONE = "none"
EDITOR_VSCODE = "vscode"
EDITOR_URL_TEMPLATES: dict[str, str] = {
    "vscode": "vscode://file/{path}:{line}",
    "cursor": "cursor://file/{path}:{line}",
    "windsurf": "windsurf://file/{path}:{line}",
    "zed": "zed://file/{path}:{line}",
    "idea": "idea://open?file={path}&line={line}",
    "textmate": "txmt://open?url=file://{path}&line={line}",
}
# Two-file deep link for a group's side-by-side view. No single-file URL
# scheme can carry a pair, so this is its own scheme; terminals that
# understand it (Croft) open both members, others refuse it like any
# custom URI. Values are fully percent-encoded `path:line`.
DIFF_LINK_TEMPLATE = "diff://open?left={left}&right={right}"
EDITOR_DIFF_COMMANDS: dict[str, str] = {
    "vscode": "code --diff {left} {right}",
    "cursor": "cursor --diff {left} {right}",
    "windsurf": "windsurf --diff {left} {right}",
}
ENV_TERM_PROGRAM = "TERM_PROGRAM"
TERM_PROGRAM_VSCODE = "vscode"
ENV_CF_BUNDLE_ID = "__CFBundleIdentifier"
# Substring of the hosting app's macOS bundle identifier -> editor name.
EDITOR_BUNDLE_MARKERS: tuple[tuple[str, str], ...] = (
    ("cursor", "cursor"),
    ("windsurf", "windsurf"),
    ("zed", "zed"),
)
STYLE_LINK = "link {url}"
TEMPLATE_KEY_PATH = "path"
TEMPLATE_KEY_LINE = "line"
TEMPLATE_KEY_LEFT = "left"
TEMPLATE_KEY_RIGHT = "right"

CLI_ERR_DUPLICATES_OPEN_UNKNOWN_GROUP = (
    "Group {number} does not exist; the report has {count} group(s)."
)
CLI_ERR_DUPLICATES_OPEN_NO_ROOT = (
    "The graph records no root path for '{project}'; re-index with "
    "'cgr start --update-graph' to enable --open and clickable locations."
)
CLI_ERR_DUPLICATES_OPEN_NO_TOOL = (
    "No side-by-side diff command available for editor '{editor}'. Set "
    'CGR_DIFF_COMMAND (e.g. "code --diff {{left}} {{right}}").'
)
CLI_DUPLICATES_OPENED_DIFF = "Opened {left} and {right} side by side."
DUPLICATES_OPEN_PAIR_SIZE = 2
CLI_DUPLICATES_URL_TEMPLATE_INVALID = (
    "CGR_EDITOR_URL_TEMPLATE {template!r} is invalid ({error}); locations "
    "are shown as plain text. The template may use only {{path}} and "
    "{{line}}."
)
CLI_ERR_DUPLICATES_DIFF_TEMPLATE_INVALID = (
    "CGR_DIFF_COMMAND {template!r} is invalid ({error}). The command may "
    "use only {{left}} and {{right}}."
)
EDITOR_ERR_EMPTY_COMMAND = "command is empty"
EDITOR_ERR_UNSUPPORTED_FIELD = "unsupported placeholder {{{field}}}"
CLI_DUPLICATES_OPEN_EXTRA_MEMBERS = (
    "Group {number} has {count} members; opened the first two side by side."
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
# Per-turn token consumption and USD cost line (issue #80). The cost segment is
# appended only for proprietary models with a known price.
UI_TURN_USAGE_TOKENS = "tokens · turn {ti:,}→{to:,} · session {si:,}→{so:,}"
UI_TURN_USAGE_COST = " · ${tc:.4f} turn · ${sc:.4f} session"
# When an earlier turn had no known price (e.g. a local model), the running
# session total understates the true spend, so it is shown as a partial floor.
UI_TURN_USAGE_COST_PARTIAL = " · ${tc:.4f} turn · ${sc:.4f}+ session (partial)"
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


class DeadCodeFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


class DuplicatesFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


class QueryFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


# Image file extensions for chat image handling
MULTIMODAL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
MIME_TYPE_PDF = "application/pdf"
MIME_TYPE_FALLBACK = "application/octet-stream"
YES_ANSWER = "y"
YES_ANSWERS = frozenset({"y", "yes", ""})
NO_ANSWERS = frozenset({"n", "no"})
SHIFT_TAB_ESCAPE = b"\x1b[Z"
DIFF_GIT_HEADER = "diff --git "
MARKDOWN_FENCE = "```"
MARKDOWN_FENCE_DIFF = "```diff"
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

EXIT_COMMANDS = frozenset({"exit", "quit"})

MODEL_COMMAND_PREFIX = "/model"
HELP_COMMAND = "/help"

HORIZONTAL_SEPARATOR = "─" * 60

SESSION_LOG_HEADER = "=== CODE-GRAPH RAG SESSION LOG ===\n\n"

LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}"

TMP_DIR = ".tmp"
SESSION_LOG_PREFIX = "session_"
SESSION_LOG_EXT = ".log"

SESSION_PREFIX_USER = "USER: "
SESSION_PREFIX_ASSISTANT = "ASSISTANT: "

SESSION_CONTEXT_START = (
    "\n\n[SESSION CONTEXT - Previous conversation in this session]:\n"
)
SESSION_CONTEXT_END = "\n[END SESSION CONTEXT]\n\n"

CONFIRM_ENABLED = "Enabled"
CONFIRM_DISABLED = "Disabled (YOLO Mode)"

DIFF_LABEL_BEFORE = "before"
DIFF_LABEL_AFTER = "after"
DIFF_FALLBACK_PATH = "file"


class DiffMarker:
    ADD = "+"
    DEL = "-"
    HUNK = "@"
    HEADER_ADD = "+++"
    HEADER_DEL = "---"


TABLE_COL_CONFIGURATION = "Configuration"
TABLE_COL_VALUE = "Value"

TABLE_ROW_TARGET_LANGUAGE = "Target Language"
TABLE_ROW_ORCHESTRATOR_MODEL = "Orchestrator Model"
TABLE_ROW_CYPHER_MODEL = "Cypher Model"
TABLE_ROW_OLLAMA_ENDPOINT = "Ollama Endpoint"
TABLE_ROW_OLLAMA_ORCHESTRATOR = "Ollama Endpoint (Orchestrator)"
TABLE_ROW_OLLAMA_CYPHER = "Ollama Endpoint (Cypher)"
TABLE_ROW_EDIT_CONFIRMATION = "Edit Confirmation"
TABLE_ROW_TARGET_REPOSITORY = "Target Repository"

MSG_CONNECTED_MEMGRAPH = "Successfully connected to Memgraph."
MSG_THINKING_CANCELLED = "Thinking cancelled."
MSG_TIMEOUT_FORMAT = "Operation timed out after {timeout} seconds."
MSG_TOOL_CALL_CANCELLED = "Tool call cancelled by user."
MSG_CHAT_INSTRUCTIONS = (
    "Ask questions about your codebase graph. Type 'exit' or 'quit' to end."
)

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

MSG_SURGICAL_SUCCESS = "Successfully applied surgical code replacement in: {path}"
# Span-preserving patchers (issue #1529).
PATCH_BAD_POSITION = "No such position: line {line}, column {col}"
PATCH_BAD_OFFSET = "Byte offset {offset} is outside the file"
PATCH_BAD_SPAN = "Span ({start}, {end}) is outside the file or reversed"
PATCH_OVERLAP = "Edit ({start}, {end}) overlaps an earlier edit in the same file"
PATCH_OUTSIDE_ROOT = "Path is outside the repository: {path}"
PATCH_NO_FILE = "No such file to patch: {path}"
PATCH_IDENTIFIER_MISMATCH = (
    "{path}:{line}:{col} holds {found!r}, not the identifier {expected!r}"
)
PATCH_NOT_AN_IDENTIFIER = "{path}:{line}:{col} is not a whole identifier"
PATCH_PARSE_FAILED = "{path} no longer parses after the patch"
PATCH_FORMAT_DRIFT = "{path} applied, but {tool} would reformat it"
PATCH_OK = "{path}: {count} edit(s) applied"
# `parses is None` means no grammar was available, so the patch was checked
# by nothing. Reported apart from PATCH_OK because reporting both as OK is
# what turned an unverifiable write into an apparently verified one; the
# write itself is still allowed, since refusing would make edits a silent
# no-op on a base install where Rust and Go grammars are absent (#1580).
# The one wording for "checked by nothing", shared so the two messages that
# carry it cannot drift apart and a caller can match on either.
PATCH_UNVERIFIED_FRAGMENT = "unverified (no parser for it)"
PATCH_UNVERIFIED = "{path}: {count} edit(s) applied, " + PATCH_UNVERIFIED_FRAGMENT
# Both facts, because either alone is misleading here: drift on its own reads
# as "parsed fine, just reformat it", and a language with no grammar is
# exactly the case that also has a formatter installed (Rust, Go).
PATCH_UNVERIFIED_DRIFT = (
    "{path}: {count} edit(s) applied, "
    + PATCH_UNVERIFIED_FRAGMENT
    + "; {tool} would also reformat it"
)
# Edit transactions (issue #1528).
EDIT_NOT_A_FILE = "Staged path is not a regular file: {path}"
EDIT_RESERVED_PATH = "Path is cgr state, not part of the tree: {path}"
EDIT_TRANSACTION_FINISHED = "This transaction has already been committed or rolled back"
EDIT_CONFLICT = "File changed since it was staged; transaction refused: {path}"
EDIT_NOTHING_STAGED = "Nothing staged; the working tree is untouched"
EDIT_VERIFICATION_FAILED = (
    "Verification failed; the working tree is untouched: {reason}"
)
EDIT_VERIFIER_RAISED = "verifier raised {error!r}"
EDIT_VERIFIER_FALSE = "verifier returned False"
EDIT_APPLIED = "Applied {count} file(s)"
EDIT_UNDO_NONE = "No recorded edit transactions to undo"
EDIT_UNDO_DONE = "Undid transaction {tx} ({count} file(s))"
EDIT_UNDO_STOPPED = "Stopped at transaction {tx}: {reason}"
EDIT_SHOW_NONE = "No recorded edit transactions"
EDIT_SHOW_HEADER = "{tx}  {at}  {count} file(s)  verification={ok}"
MSG_SURGICAL_FAILED = (
    "Failed to apply surgical replacement in {path}. "
    "Target code not found or patches failed."
)

GREP_SUGGESTION = " Use 'rg' instead of 'grep' for text searching."

QUERY_NOT_AVAILABLE = "N/A"
DICT_KEY_RESULTS = "results"
DICT_KEY_ERROR = "error"
DICT_KEY_QUERY_USED = "query_used"
TIKTOKEN_ENCODING = "cl100k_base"
QUERY_SUMMARY_SUCCESS = "Successfully retrieved {count} item(s) from the graph."
QUERY_SUMMARY_TRUNCATED = (
    "Results truncated: showing {kept} of {total} items (~{tokens} tokens, limit {max_tokens}). "
    "Refine your query for more specific results."
)
QUERY_SUMMARY_TRANSLATION_FAILED = (
    "I couldn't translate your request into a database query. Error: {error}"
)
QUERY_SUMMARY_DB_ERROR = "There was an error querying the database: {error}"
# Refused rather than answered unscoped: rows with no qualified name cannot
# be attributed to a project, so the requested scope cannot be honoured.
QUERY_SUMMARY_UNSCOPEABLE = (
    "This query cannot be scoped to project {project!r}: it returns no "
    "qualified name, so results cannot be attributed to a project. Ask for "
    "the qualified name in the query."
)
QUERY_SUMMARY_TIMEOUT = (
    "Query exceeded the {timeout:.1f}s timeout and was cancelled. "
    "Avoid unbounded traversals; add depth bounds or use a graph-algorithm procedure."
)
QUERY_RESULTS_PANEL_TITLE = "[bold blue]Cypher Query Results[/bold blue]"

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

MSG_DOC_NO_CANDIDATES = "No valid text found in response candidates."
MSG_DOC_NO_CONTENT = "No text content received from the API."
# Newer typer generations vendor click here for click 8.3+ compatibility;
# commands and exceptions then descend from these classes instead of the
# real click's.
TYPER_VENDORED_CLICK_EXCEPTIONS_MODULE = "typer._click.exceptions"
MIME_TYPE_DEFAULT = "application/octet-stream"
DOC_PROMPT_PREFIX = (
    "Based on the document provided, please answer the following question: {question}"
)
