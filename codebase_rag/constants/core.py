# Cross-cutting kernel constants: separators, chars, paths, misc keys.

from enum import StrEnum

INIT_PY = "__init__.py"

ENCODING_UTF8 = "utf-8"

ARG_TARGET_CODE = "target_code"
ARG_REPLACEMENT_CODE = "replacement_code"
ARG_FILE_PATH = "file_path"
ARG_CONTENT = "content"
ARG_COMMAND = "command"
ARG_PATTERN = "pattern"
ARG_REWRITE = "rewrite"
ARG_LANGUAGE = "language"
ARG_DRY_RUN = "dry_run"

SEPARATOR_DOT = "."
SEPARATOR_SLASH = "/"
# Splits "provider:model" both in user-supplied settings and in the model
# names pydantic-ai enumerates.
MODEL_STRING_SEPARATOR = ":"
# `derive_project_name` builds "<name>__<8-hex-digest>", so this marker is
# what distinguishes a project-qualified name from free text that merely
# contains dots (a docstring, a file path).
PROJECT_NAME_DIGEST_MARKER = "__"
# Hex digits after the marker. Shared so `derive_project_name` and the
# scoping filter that recognises its output cannot drift apart.
PROJECT_NAME_DIGEST_LEN = 8
# Disambiguates definitions that share one qualified name (if/else import
# fallbacks, typing.overload, try/except fallbacks): "<qn>@<start_line>".
DUP_QN_MARKER = "@"
# Joined after the line when a same-named definition already holds that line,
# so same-line twins stay distinct (issue #1071). Every consumer splits on
# DUP_QN_MARKER and keeps the base, so nothing reads the suffix back.
DUP_QN_COLUMN_MARKER = "_"

PATH_CURRENT_DIR = "."
PATH_PARENT_DIR = ".."
GLOB_ALL = "*"

TRIE_TYPE_KEY = "__type__"
TRIE_QN_KEY = "__qn__"
TRIE_INTERNAL_PREFIX = "__"

BYTES_PER_MB = 1024 * 1024

EMPTY_PARENS = "()"
DOCSTRING_STRIP_CHARS = "'\" \n"

INLINE_MODULE_PATH_PREFIX = "inline_module_"

# Method name constants for getattr/hasattr
METHOD_FIND_WITH_PREFIX = "find_with_prefix"
METHOD_ITEMS = "items"

JSON_INDENT = 2


class EventType(StrEnum):
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"


REALTIME_LOGGER_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)

WATCHER_SLEEP_INTERVAL = 1
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_ERROR = "ERROR"

# Debounce settings for realtime watcher
DEFAULT_DEBOUNCE_SECONDS = 5
DEFAULT_MAX_WAIT_SECONDS = 30

CHAR_HYPHEN = "-"
CHAR_UNDERSCORE = "_"

CHAR_SEMICOLON = ";"
CHAR_COMMA = ","
CHAR_COLON = ":"
CHAR_ANGLE_OPEN = "<"
CHAR_ANGLE_CLOSE = ">"
CHAR_PAREN_OPEN = "("
CHAR_PAREN_CLOSE = ")"
CHAR_QUESTION_MARK = "?"

CHAR_SPACE = " "
SEPARATOR_COMMA_SPACE = ", "
PUNCTUATION_TYPES = (CHAR_PAREN_OPEN, CHAR_PAREN_CLOSE, CHAR_COMMA)

REGEX_METHOD_CHAIN_SUFFIX = r"\)\.[^)]*$"
REGEX_FINAL_METHOD_CAPTURE = r"\.([^.()]+)$"

DEFAULT_NAME = "Unknown"
TEXT_UNKNOWN = "unknown"

TMP_EXTENSION = ".tmp"

MOD_RS = "mod.rs"
LIB_RS = "lib.rs"
MAIN_RS = "main.rs"
SEPARATOR_DOUBLE_COLON = "::"
SEPARATOR_PROTOTYPE = ".prototype."
RUST_CRATE_KEYWORD = "crate"
# A Rust crate path whose module is backed by a file the qn scheme cannot key
# (an unrepresentable `#[path]` target: absolute, Windows-separated, or a climb
# above the repository root) has no referent in the graph. The resolvers return
# this qn so the path binds nothing and callers treat it as a decided drop
# rather than falling back to a name-derived shadow file (issue #1082). The NUL
# byte keeps it distinct from every real qn while remaining an ordinary str.
RUST_UNRESOLVABLE_QN = "\x00unrepresentable"
BUILTIN_PREFIX = "builtin"
IIFE_FUNC_PREFIX = "iife_func_"
IIFE_ARROW_PREFIX = "iife_arrow_"
OPERATOR_PREFIX = "operator"
KEYWORD_SUPER = "super"
KEYWORD_SELF = "self"
KEYWORD_CONSTRUCTOR = "constructor"
# Receivers that name the enclosing type rather than an ordinary value, so
# `self.Inner()` and `cls.Inner()` are real nested-class constructions.
# Membership is not sufficient on its own: `self` is also a legal Go receiver
# name, and a spelling test alone accepted `self.Error()` for a module-level
# `Error`. The caller pairs this with a nesting check against the enclosing
# type, which is what actually distinguishes the two (issue #1641).
SELF_RECEIVER_KEYWORDS = frozenset({"self", "cls", "this"})

# Incremental update hash cache
HASH_CACHE_FILENAME = ".cgr-hash-cache.json"
DIR_MTIMES_FILENAME = ".cgr-dir-mtimes.json"
PARSER_FINGERPRINT_FILENAME = ".cgr-parser-fingerprint"
DELOMBOK_STATE_FILENAME = ".cgr-delombok-state.json"
# The exclusion set the last run indexed under, covering both the excludes and
# the unignores (which come from `!` lines in .cgrignore/.gitignore and from
# interactive setup; there is no --unignore flag). Nothing on disk changes when
# only the CLI --exclude flags do, so without this the sync check cannot tell
# that the eligible set moved (issue #1606).
EXCLUSION_STATE_FILENAME = ".cgr-exclusion-state.json"
# Recorded edit transactions for `cgr edits show|undo` (issue #1528).
EDIT_HISTORY_FILENAME = ".cgr-edit-history.json"
EDIT_LOCK_FILENAME = ".cgr-edit-lock"
PLATFORM_WINDOWS = "win32"
# Permission bits copied onto a replacement file (rwx for u/g/o, setuid etc.).
EDIT_MODE_MASK = 0o7777
# Mode of the exclusively created temp sibling before the target's mode is
# copied onto it: owner-only, so nothing reads staged bytes mid-write.
EDIT_TEMP_FILE_MODE = 0o600
CGR_STATE_FILENAMES: frozenset[str] = frozenset(
    {
        HASH_CACHE_FILENAME,
        DIR_MTIMES_FILENAME,
        PARSER_FINGERPRINT_FILENAME,
        DELOMBOK_STATE_FILENAME,
        EXCLUSION_STATE_FILENAME,
        EDIT_HISTORY_FILENAME,
        EDIT_LOCK_FILENAME,
    }
)
# Edit transactions (issue #1528).
EDIT_HISTORY_LIMIT = 50
EDIT_TRANSACTION_ID_LENGTH = 12
EDIT_STAGING_PREFIX = "cgr-edit-"
DIFF_DEV_NULL = "/dev/null"
EDIT_KEY_ID = "id"
EDIT_KEY_AT = "at"
EDIT_KEY_FILES = "files"
EDIT_KEY_BEFORE = "before"
EDIT_KEY_AFTER = "after"
EDIT_KEY_VERIFICATION = "verification"
EDIT_KEY_OK = "ok"
EDIT_KEY_MESSAGE = "message"
EDIT_KEY_MODE = "mode"

# Inputs to the parser fingerprint: everything that changes how source files
# become graph nodes and edges, plus the installed grammar wheels. Paths are
# relative to the codebase_rag package root.
PARSER_FINGERPRINT_SOURCE_DIRS: tuple[str, ...] = ("parsers", "constants")
PARSER_FINGERPRINT_SOURCE_FILES: tuple[str, ...] = (
    "graph_updater.py",
    "function_registry.py",
    "ast_cache.py",
    "language_spec.py",
    "parser_loader.py",
)
PY_SOURCE_GLOB = "*.py"
# The bundled Roslyn C# frontend tool is parser code too, though .cs/.csproj
# rather than Python: an edit changes the semantic edges it produces, so its
# sources are folded into the parser fingerprint.
PARSER_FINGERPRINT_TOOL_DIR = "parsers/csharp_frontend/roslyn"
PARSER_FINGERPRINT_TOOL_GLOBS: tuple[str, ...] = ("*.cs", "*.csproj")
# Bundled semantic-frontend tool sources (per (dir, globs)): parser code that
# is not Python, so an edit must still trip the staleness fingerprint.
PARSER_FINGERPRINT_TOOL_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("parsers/csharp_frontend/roslyn", ("*.cs", "*.csproj")),
    ("parsers/go_frontend/gotypes", ("*.go", "*.mod", "*.sum")),
    ("parsers/java_frontend/javac", ("**/*.java",)),
)
GRAMMAR_DIST_PREFIX = "tree-sitter"
GRAMMAR_VERSION_FMT = "{name}=={version}"
GIT_DIR_NAME = ".git"
ROOT_DIR_KEY = "."
JSON_EMPTY_OBJECT = "{}"

STR_NONE = "None"

ENTITY_CLASS = "Class"
ENTITY_FUNCTION = "Function"
ENTITY_METHOD = "Method"

PREFIX_LAMBDA = "lambda_"
PREFIX_ANONYMOUS = "anonymous_"
PREFIX_IIFE = "iife_"
PREFIX_IIFE_DIRECT = "iife_direct_"
PREFIX_ARROW = "arrow"
PREFIX_FUNC = "func"

# JSON keys for stdlib introspection subprocess responses
JSON_KEY_HAS_ENTITY = "hasEntity"
JSON_KEY_ENTITY_TYPE = "entityType"

IMPORT_DEFAULT_SUFFIX = ".default"
IMPORT_STD_PREFIX = "std."
CPP_STD_PREFIX = "std"
IMPORT_MODULE_LABEL = "Module"
IMPORT_QUALIFIED_NAME = "qualified_name"
IMPORT_RELATIONSHIP = "IMPORTS"

# Delimiter tokens for argument parsing
DELIMITER_TOKENS = frozenset({"(", ")", ","})
