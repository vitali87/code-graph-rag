# (H) Cross-cutting kernel constants: separators, chars, paths, misc keys.

from enum import StrEnum

# (H) File names
INIT_PY = "__init__.py"

# (H) Encoding
ENCODING_UTF8 = "utf-8"

# (H) Tool argument field names
ARG_TARGET_CODE = "target_code"
ARG_REPLACEMENT_CODE = "replacement_code"
ARG_FILE_PATH = "file_path"
ARG_CONTENT = "content"
ARG_COMMAND = "command"

# (H) Qualified name separators
SEPARATOR_DOT = "."
SEPARATOR_SLASH = "/"
# (H) Disambiguates definitions that share one qualified name (if/else import
# (H) fallbacks, typing.overload, try/except fallbacks): "<qn>@<start_line>".
DUP_QN_MARKER = "@"

# (H) Path navigation
PATH_CURRENT_DIR = "."
PATH_PARENT_DIR = ".."
GLOB_ALL = "*"

# (H) Trie internal keys
TRIE_TYPE_KEY = "__type__"
TRIE_QN_KEY = "__qn__"
TRIE_INTERNAL_PREFIX = "__"

SEPARATOR_COMMA = ","

# (H) Byte size constants
BYTES_PER_MB = 1024 * 1024

# (H) Method signature formatting
EMPTY_PARENS = "()"
DOCSTRING_STRIP_CHARS = "'\" \n"

# (H) Inline module path prefix
INLINE_MODULE_PATH_PREFIX = "inline_module_"

# (H) Method name constants for getattr/hasattr
METHOD_FIND_WITH_PREFIX = "find_with_prefix"
METHOD_ITEMS = "items"

# (H) JSON formatting
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

# (H) Debounce settings for realtime watcher
DEFAULT_DEBOUNCE_SECONDS = 5
DEFAULT_MAX_WAIT_SECONDS = 30

CHAR_HYPHEN = "-"
CHAR_UNDERSCORE = "_"

ALLOWED_COMMENT_MARKERS = frozenset(
    {"(H)", "type:", "noqa", "pyright", "ty:", "@@protoc", "nosec"}
)
QUOTE_CHARS = frozenset({'"', "'"})
TRIPLE_QUOTES = ('"""', "'''")
COMMENT_CHAR = "#"
ESCAPE_CHAR = "\\"
CHAR_SEMICOLON = ";"
CHAR_COMMA = ","
CHAR_COLON = ":"
CHAR_ANGLE_OPEN = "<"
CHAR_ANGLE_CLOSE = ">"
CHAR_PAREN_OPEN = "("
CHAR_PAREN_CLOSE = ")"

CHAR_SPACE = " "
SEPARATOR_COMMA_SPACE = ", "
PUNCTUATION_TYPES = (CHAR_PAREN_OPEN, CHAR_PAREN_CLOSE, CHAR_COMMA)

REGEX_METHOD_CHAIN_SUFFIX = r"\)\.[^)]*$"
REGEX_FINAL_METHOD_CAPTURE = r"\.([^.()]+)$"

DEFAULT_NAME = "Unknown"
TEXT_UNKNOWN = "unknown"

# (H) File editor constants
TMP_EXTENSION = ".tmp"

# (H) Call processor constants
MOD_RS = "mod.rs"
SEPARATOR_DOUBLE_COLON = "::"
SEPARATOR_COLON = ":"
SEPARATOR_PROTOTYPE = ".prototype."
RUST_CRATE_PREFIX = "crate::"
BUILTIN_PREFIX = "builtin"
IIFE_FUNC_PREFIX = "iife_func_"
IIFE_ARROW_PREFIX = "iife_arrow_"
OPERATOR_PREFIX = "operator"
KEYWORD_SUPER = "super"
KEYWORD_SELF = "self"
KEYWORD_CONSTRUCTOR = "constructor"

# (H) Incremental update hash cache
HASH_CACHE_FILENAME = ".cgr-hash-cache.json"
DIR_MTIMES_FILENAME = ".cgr-dir-mtimes.json"
PARSER_FINGERPRINT_FILENAME = ".cgr-parser-fingerprint"
CGR_STATE_FILENAMES: frozenset[str] = frozenset(
    {HASH_CACHE_FILENAME, DIR_MTIMES_FILENAME, PARSER_FINGERPRINT_FILENAME}
)

# (H) Inputs to the parser fingerprint: everything that changes how source
# (H) files are turned into graph nodes and edges, plus the installed grammar
# (H) wheels. Paths are relative to the codebase_rag package root.
PARSER_FINGERPRINT_SOURCE_DIRS: tuple[str, ...] = ("parsers", "constants")
PARSER_FINGERPRINT_SOURCE_FILES: tuple[str, ...] = (
    "graph_updater.py",
    "language_spec.py",
    "parser_loader.py",
)
PY_SOURCE_GLOB = "*.py"
GRAMMAR_DIST_PREFIX = "tree-sitter"
GRAMMAR_VERSION_FMT = "{name}=={version}"
GIT_DIR_NAME = ".git"
ROOT_DIR_KEY = "."
JSON_EMPTY_OBJECT = "{}"

# (H) Fallback display value
STR_NONE = "None"

# (H) Entity type names
ENTITY_CLASS = "Class"
ENTITY_FUNCTION = "Function"
ENTITY_METHOD = "Method"

# (H) Anonymous function name prefixes
PREFIX_LAMBDA = "lambda_"
PREFIX_ANONYMOUS = "anonymous_"
PREFIX_IIFE = "iife_"
PREFIX_IIFE_DIRECT = "iife_direct_"
PREFIX_ARROW = "arrow"
PREFIX_FUNC = "func"

# (H) JSON keys for stdlib introspection subprocess responses
JSON_KEY_HAS_ENTITY = "hasEntity"
JSON_KEY_ENTITY_TYPE = "entityType"

# (H) Import processor misc
IMPORT_DEFAULT_SUFFIX = ".default"
IMPORT_STD_PREFIX = "std."
CPP_STD_PREFIX = "std"
IMPORT_MODULE_LABEL = "Module"
IMPORT_QUALIFIED_NAME = "qualified_name"
IMPORT_RELATIONSHIP = "IMPORTS"

# (H) Delimiter tokens for argument parsing
DELIMITER_TOKENS = frozenset({"(", ")", ","})
