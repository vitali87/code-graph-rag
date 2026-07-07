from enum import StrEnum
from typing import NamedTuple

from .languages import SupportedLanguage


class PyInstallerPackage(NamedTuple):
    name: str
    collect_all: bool = False
    collect_data: bool = False
    hidden_import: str | None = None


class KeyBinding(StrEnum):
    CTRL_J = "c-j"
    CTRL_E = "c-e"
    ENTER = "enter"
    CTRL_C = "c-c"
    SHIFT_TAB = "s-tab"


# (H) Package indicator files
PKG_INIT_PY = "__init__.py"
PKG_CARGO_TOML = "Cargo.toml"
PKG_CMAKE_LISTS = "CMakeLists.txt"
PKG_MAKEFILE = "Makefile"
PKG_VCXPROJ_GLOB = "*.vcxproj"
PKG_CONANFILE = "conanfile.txt"


class CppFrontend(StrEnum):
    TREESITTER = "treesitter"
    LIBCLANG = "libclang"


# (H) Provider endpoints
OPENAI_DEFAULT_ENDPOINT = "https://api.openai.com/v1"
OLLAMA_HEALTH_PATH = "/api/tags"
GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
V1_PATH = "/v1"

# (H) HTTP status codes
HTTP_OK = 200

UNIXCODER_MODEL = "microsoft/unixcoder-base"
EMBEDDING_DEFAULT_BATCH_SIZE = 64
EMBEDDING_CACHE_FILENAME = ".embedding_cache.json"

ERR_SUBSTR_ALREADY_EXISTS = "already exists"
ERR_SUBSTR_CONSTRAINT = "constraint"

# (H) File names
INIT_PY = "__init__.py"

# (H) Encoding
ENCODING_UTF8 = "utf-8"

# (H) Protobuf file names
PROTOBUF_INDEX_FILE = "index.bin"
PROTOBUF_NODES_FILE = "nodes.bin"
PROTOBUF_RELS_FILE = "relationships.bin"
PACKAGE_NAME = "code-graph-rag"

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


class DeadCodeFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


class QueryFormat(StrEnum):
    TABLE = "table"
    JSON = "json"


EXCLUDED_DEPENDENCY_NAMES = frozenset({"python", "php"})

# (H) Byte size constants
BYTES_PER_MB = 1024 * 1024

# (H) Method signature formatting
EMPTY_PARENS = "()"
DOCSTRING_STRIP_CHARS = "'\" \n"

# (H) Inline module path prefix
INLINE_MODULE_PATH_PREFIX = "inline_module_"

# (H) Dependency files
DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "composer.json",
    }
)
CSPROJ_SUFFIX = ".csproj"

_CYPHER_EMBEDDING_BASE = """
MATCH (m:Module)-[:DEFINES]->(n)
WHERE (n:Function OR n:Method)
  AND m.qualified_name STARTS WITH ($project_name + '.')
"""


# (H) Tree-sitter AST node type constants
FUNCTION_NODES_BASIC = ("function_declaration", "function_definition")
FUNCTION_NODES_LAMBDA = (
    "lambda_expression",
    "arrow_function",
    "anonymous_function",
    "closure_expression",
)
FUNCTION_NODES_METHOD = (
    "method_declaration",
    "constructor_declaration",
    "destructor_declaration",
)
FUNCTION_NODES_TEMPLATE = (
    "template_declaration",
    "function_signature_item",
    "function_signature",
)
FUNCTION_NODES_GENERATOR = ("generator_function_declaration", "function_expression")

CLASS_NODES_BASIC = ("class_declaration", "class_definition")
CLASS_NODES_STRUCT = ("struct_declaration", "struct_specifier", "struct_item")
CLASS_NODES_INTERFACE = ("interface_declaration", "trait_declaration", "trait_item")
CLASS_NODES_ENUM = ("enum_declaration", "enum_item", "enum_specifier")
CLASS_NODES_TYPE_ALIAS = ("type_alias_declaration", "type_item")
CLASS_NODES_UNION = ("union_specifier", "union_item")

CALL_NODES_BASIC = ("call_expression", "function_call")
CALL_NODES_METHOD = (
    "method_invocation",
    "member_call_expression",
    "field_expression",
)
CALL_NODES_OPERATOR = ("binary_expression", "unary_expression", "update_expression")
CALL_NODES_SPECIAL = ("new_expression", "delete_expression", "macro_invocation")

IMPORT_NODES_STANDARD = ("import_declaration", "import_statement")
IMPORT_NODES_FROM = ("import_from_statement",)
IMPORT_NODES_MODULE = ("lexical_declaration", "export_statement")
IMPORT_NODES_INCLUDE = ("preproc_include",)
JS_TS_LANGUAGES = frozenset(
    {SupportedLanguage.JS, SupportedLanguage.TS, SupportedLanguage.TSX}
)
SHIFT_TAB_ESCAPE = b"\x1b[Z"
MARKDOWN_FENCE = "```"
MARKDOWN_FENCE_DIFF = "```diff"

ANTHROPIC_COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_HEADER_API_KEY = "x-api-key"
ANTHROPIC_HEADER_VERSION = "anthropic-version"
HEADER_CONTENT_TYPE = "content-type"
CONTENT_TYPE_JSON = "application/json"
ANTHROPIC_COUNT_TIMEOUT_S = 10.0

DEFAULT_CONTEXT_WINDOW = 200_000
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-0": 200_000,
    "claude-sonnet-4-6": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-0": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-haiku-4-0": 200_000,
}

# (H) JSON formatting
JSON_INDENT = 2

# (H) Patterns to detect at repo root and offer as exclude candidates (user selects which to exclude)
IGNORE_PATTERNS = frozenset(
    {
        ".cache",
        ".claude",
        ".eclipse",
        ".eggs",
        ".env",
        ".git",
        ".gradle",
        ".hg",
        ".idea",
        ".maven",
        ".mypy_cache",
        ".nox",
        ".npm",
        ".nyc_output",
        ".pnpm-store",
        ".pytest_cache",
        ".qdrant_code_embeddings",
        ".ruff_cache",
        ".svn",
        ".tmp",
        ".tox",
        ".venv",
        ".vs",
        ".vscode",
        ".yarn",
        "__pycache__",
        "bin",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "env",
        "htmlcov",
        "node_modules",
        "obj",
        "out",
        "Pods",
        "site-packages",
        "target",
        "temp",
        "tmp",
        "vendor",
        "venv",
    }
)
IGNORE_SUFFIXES = frozenset(
    {".tmp", "~", ".pyc", ".pyo", ".o", ".a", ".so", ".dll", ".class"}
)

# (H) pathspec style for .cgrignore / --exclude patterns (#495).
GITWILDMATCH_STYLE = "gitignore"


class EventType(StrEnum):
    MODIFIED = "modified"
    CREATED = "created"
    DELETED = "deleted"


class Architecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"
    ARM64 = "arm64"
    AMD64 = "amd64"


BINARY_NAME_TEMPLATE = "code-graph-rag-{system}-{machine}"
BINARY_FILE_PERMISSION = 0o755
DIST_DIR = "dist"
BYTES_PER_MB_FLOAT = 1024 * 1024

PYPROJECT_PATH = "pyproject.toml"
PYPROJECT_KEY_TOOL = "tool"
PYPROJECT_KEY_SETUPTOOLS = "setuptools"
PYPROJECT_KEY_PACKAGE_DIR = "package-dir"
TREESITTER_EXTRA_KEY = "treesitter-full"
TREESITTER_PKG_PREFIX = "tree-sitter-"

# (H) PyInstaller CLI constants
PYINSTALLER_CMD = "pyinstaller"
PYINSTALLER_ARG_NAME = "--name"
PYINSTALLER_ARG_ONEFILE = "--onefile"
PYINSTALLER_ARG_NOCONFIRM = "--noconfirm"
PYINSTALLER_ARG_CLEAN = "--clean"
PYINSTALLER_ARG_COLLECT_ALL = "--collect-all"
PYINSTALLER_ARG_COLLECT_DATA = "--collect-data"
PYINSTALLER_ARG_HIDDEN_IMPORT = "--hidden-import"
PYINSTALLER_ARG_EXCLUDE_MODULE = "--exclude-module"
PYINSTALLER_ENTRY_POINT = "main.py"

PYINSTALLER_EXCLUDED_MODULES = ["logfire"]

# (H) TOML parsing constants
TOML_KEY_PROJECT = "project"
TOML_KEY_OPTIONAL_DEPS = "optional-dependencies"

# (H) Version string parsing
VERSION_SPLIT_GTE = ">="
VERSION_SPLIT_EQ = "=="
VERSION_SPLIT_LT = "<"
CHAR_HYPHEN = "-"
CHAR_UNDERSCORE = "_"

PYINSTALLER_PACKAGES: list["PyInstallerPackage"] = [
    PyInstallerPackage(
        name="pydantic_ai",
        collect_all=True,
        collect_data=True,
        hidden_import="pydantic_ai_slim",
    ),
    PyInstallerPackage(name="rich", collect_all=True),
    PyInstallerPackage(name="typer", collect_all=True),
    PyInstallerPackage(name="loguru", collect_all=True),
    PyInstallerPackage(name="toml", collect_all=True),
    PyInstallerPackage(name="protobuf", collect_all=True),
    PyInstallerPackage(name="genai_prices", collect_all=True),
]

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
CHAR_UNDERSCORE = "_"
CHAR_SPACE = " "
SEPARATOR_COMMA_SPACE = ", "
PUNCTUATION_TYPES = (CHAR_PAREN_OPEN, CHAR_PAREN_CLOSE, CHAR_COMMA)

REGEX_METHOD_CHAIN_SUFFIX = r"\)\.[^)]*$"
REGEX_FINAL_METHOD_CAPTURE = r"\.([^.()]+)$"

DEFAULT_NAME = "Unknown"
TEXT_UNKNOWN = "unknown"

MODULE_TORCH = "torch"
MODULE_TRANSFORMERS = "transformers"
MODULE_QDRANT_CLIENT = "qdrant_client"

SEMANTIC_DEPENDENCIES = (MODULE_QDRANT_CLIENT, MODULE_TORCH, MODULE_TRANSFORMERS)
ML_DEPENDENCIES = (MODULE_TORCH, MODULE_TRANSFORMERS)


class UniXcoderMode(StrEnum):
    ENCODER_ONLY = "<encoder-only>"
    DECODER_ONLY = "<decoder-only>"
    ENCODER_DECODER = "<encoder-decoder>"


UNIXCODER_MASK_TOKEN = "<mask0>"
UNIXCODER_BUFFER_BIAS = "bias"
UNIXCODER_MAX_CONTEXT = 1024

DICT_KEY_RESULTS = "results"
TIKTOKEN_ENCODING = "cl100k_base"

# (H) File editor constants
TMP_EXTENSION = ".tmp"
SEMANTIC_BATCH_SIZE = 100
SEMANTIC_TYPE_UNKNOWN = "Unknown"
MIME_TYPE_DEFAULT = "application/octet-stream"
DOC_PROMPT_PREFIX = (
    "Based on the document provided, please answer the following question: {question}"
)

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

# (H) JavaScript built-in types
JS_BUILTIN_TYPES: frozenset[str] = frozenset(
    {
        "Array",
        "Object",
        "String",
        "Number",
        "Date",
        "RegExp",
        "Function",
        "Map",
        "Set",
        "Promise",
        "Error",
        "Boolean",
    }
)

# (H) JavaScript built-in function patterns
JS_BUILTIN_PATTERNS: frozenset[str] = frozenset(
    {
        "Object.create",
        "Object.keys",
        "Object.values",
        "Object.entries",
        "Object.assign",
        "Object.freeze",
        "Object.seal",
        "Object.defineProperty",
        "Object.getPrototypeOf",
        "Object.setPrototypeOf",
        "Array.from",
        "Array.of",
        "Array.isArray",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "encodeURIComponent",
        "decodeURIComponent",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        "console.log",
        "console.error",
        "console.warn",
        "console.info",
        "console.debug",
        "JSON.parse",
        "JSON.stringify",
        "Math.random",
        "Math.floor",
        "Math.ceil",
        "Math.round",
        "Math.abs",
        "Math.max",
        "Math.min",
        "Date.now",
        "Date.parse",
    }
)

JS_METHOD_BIND = "bind"
JS_METHOD_CALL = "call"
JS_METHOD_APPLY = "apply"
JS_SUFFIX_BIND = ".bind"
JS_SUFFIX_CALL = ".call"
JS_SUFFIX_APPLY = ".apply"
JS_FUNCTION_PROTOTYPE_SUFFIXES: dict[str, str] = {
    JS_SUFFIX_BIND: JS_METHOD_BIND,
    JS_SUFFIX_CALL: JS_METHOD_CALL,
    JS_SUFFIX_APPLY: JS_METHOD_APPLY,
}
# (H) `fn.bind(ctx)` / `fn.call(...)` / `fn.apply(...)` all use `fn`; when such a
# (H) call sits in a value position (`onError: handleError.bind(toast)`) the `.bind`
# (H) resolves to the Function.prototype builtin, so `fn` itself must be referenced
# (H) separately or it reports as dead.
JS_FUNCTION_PROTOTYPE_METHODS = frozenset(
    {JS_METHOD_BIND, JS_METHOD_CALL, JS_METHOD_APPLY}
)

# (H) C++ operator mappings
CPP_OPERATORS: dict[str, str] = {
    "operator_plus": "builtin.cpp.operator_plus",
    "operator_minus": "builtin.cpp.operator_minus",
    "operator_multiply": "builtin.cpp.operator_multiply",
    "operator_divide": "builtin.cpp.operator_divide",
    "operator_modulo": "builtin.cpp.operator_modulo",
    "operator_equal": "builtin.cpp.operator_equal",
    "operator_not_equal": "builtin.cpp.operator_not_equal",
    "operator_less": "builtin.cpp.operator_less",
    "operator_greater": "builtin.cpp.operator_greater",
    "operator_less_equal": "builtin.cpp.operator_less_equal",
    "operator_greater_equal": "builtin.cpp.operator_greater_equal",
    "operator_assign": "builtin.cpp.operator_assign",
    "operator_plus_assign": "builtin.cpp.operator_plus_assign",
    "operator_minus_assign": "builtin.cpp.operator_minus_assign",
    "operator_multiply_assign": "builtin.cpp.operator_multiply_assign",
    "operator_divide_assign": "builtin.cpp.operator_divide_assign",
    "operator_modulo_assign": "builtin.cpp.operator_modulo_assign",
    "operator_increment": "builtin.cpp.operator_increment",
    "operator_decrement": "builtin.cpp.operator_decrement",
    "operator_left_shift": "builtin.cpp.operator_left_shift",
    "operator_right_shift": "builtin.cpp.operator_right_shift",
    "operator_bitwise_and": "builtin.cpp.operator_bitwise_and",
    "operator_bitwise_or": "builtin.cpp.operator_bitwise_or",
    "operator_bitwise_xor": "builtin.cpp.operator_bitwise_xor",
    "operator_bitwise_not": "builtin.cpp.operator_bitwise_not",
    "operator_logical_and": "builtin.cpp.operator_logical_and",
    "operator_logical_or": "builtin.cpp.operator_logical_or",
    "operator_logical_not": "builtin.cpp.operator_logical_not",
    "operator_subscript": "builtin.cpp.operator_subscript",
    "operator_call": "builtin.cpp.operator_call",
}

# (H) Language CLI paths and patterns
LANG_GRAMMARS_DIR = "grammars"
LANG_CONFIG_FILE = "codebase_rag/language_spec.py"
LANG_TREE_SITTER_JSON = "tree-sitter.json"
LANG_NODE_TYPES_JSON = "node-types.json"
LANG_SRC_DIR = "src"
LANG_GIT_MODULES_PATH = ".git/modules/{path}"
LANG_DEFAULT_GRAMMAR_URL = "https://github.com/tree-sitter/tree-sitter-{name}"
LANG_TREE_SITTER_URL_MARKER = "github.com/tree-sitter/tree-sitter"
LANG_FALLBACK_METHOD_NODE = "method_declaration"

# (H) Language CLI node type detection keywords
LANG_FUNCTION_KEYWORDS = frozenset(
    {
        "function",
        "method",
        "constructor",
        "destructor",
        "lambda",
        "arrow_function",
        "anonymous_function",
        "closure",
    }
)
LANG_CLASS_KEYWORDS = frozenset(
    {
        "class",
        "interface",
        "struct",
        "enum",
        "trait",
        "object",
        "type",
        "impl",
        "union",
    }
)
LANG_CALL_KEYWORDS = frozenset({"call", "invoke", "invocation"})
LANG_MODULE_KEYWORDS = frozenset(
    {"program", "source_file", "compilation_unit", "module", "chunk"}
)
LANG_EXCLUSION_KEYWORDS = frozenset({"access", "call"})

# (H) Language CLI messages
LANG_MSG_USING_DEFAULT_URL = "Using default tree-sitter URL: {url}"
LANG_MSG_CUSTOM_URL_WARNING = (
    "WARNING: You are adding a grammar from a custom URL. "
    "This may execute code from the repository. Only proceed if you trust the source."
)
LANG_MSG_ADDING_SUBMODULE = "Adding submodule from {url}..."
LANG_MSG_SUBMODULE_SUCCESS = "Successfully added submodule at {path}"
LANG_MSG_SUBMODULE_EXISTS = (
    "Submodule already exists at {path}. Forcing re-installation..."
)
LANG_MSG_REMOVING_ENTRY = "   -> Removing existing submodule entry..."
LANG_MSG_READDING_SUBMODULE = "   -> Re-adding submodule..."
LANG_MSG_REINSTALL_SUCCESS = "Successfully re-installed submodule at {path}"
LANG_MSG_AUTO_DETECTED_LANG = "Auto-detected language: {name}"
LANG_MSG_USING_LANG_NAME = "Using language name: {name}"
LANG_MSG_AUTO_DETECTED_EXT = "Auto-detected file extensions: {extensions}"
LANG_MSG_FOUND_NODE_TYPES = "Found {count} total node types in grammar"
LANG_MSG_SEMANTIC_CATEGORIES = "Tree-sitter semantic categories:"
LANG_MSG_CATEGORY_FORMAT = "  {category}: {subtypes} ({count} total)"
LANG_MSG_MAPPED_CATEGORIES = "\nMapped to our categories:"
LANG_MSG_FUNCTIONS = "Functions: {nodes}"
LANG_MSG_CLASSES = "Classes: {nodes}"
LANG_MSG_MODULES = "Modules: {nodes}"
LANG_MSG_CALLS = "Calls: {nodes}"
LANG_MSG_LANG_ADDED = "\nLanguage '{name}' has been added to the configuration!"
LANG_MSG_UPDATED_CONFIG = "Updated {path}"
LANG_MSG_REVIEW_PROMPT = "Please review the detected node types:"
LANG_MSG_REVIEW_HINT = "   The auto-detection is good but may need manual adjustments."
LANG_MSG_EDIT_HINT = "   Edit the configuration in: {path}"
LANG_MSG_COMMON_ISSUES = "Look for these common issues:"
LANG_MSG_ISSUE_MISCLASSIFIED = (
    "   - Remove misclassified types (e.g., table_constructor in functions)"
)
LANG_MSG_ISSUE_MISSING = "   - Add missing types that should be included"
LANG_MSG_ISSUE_CLASS_TYPES = (
    "   - Verify class_node_types includes all relevant class-like constructs"
)
LANG_MSG_ISSUE_CALL_TYPES = (
    "   - Check call_node_types covers all function call patterns"
)
LANG_MSG_LIST_HINT = (
    "You can run 'cgr language list-languages' to see the current config."
)
LANG_MSG_LANG_NOT_FOUND = "Language '{name}' not found."
LANG_MSG_AVAILABLE_LANGS = "Available languages: {langs}"
LANG_MSG_REMOVED_FROM_CONFIG = "Removed language '{name}' from configuration file."
LANG_MSG_REMOVING_SUBMODULE = "Removing git submodule '{path}'..."
LANG_MSG_CLEANED_MODULES = "Cleaned up git modules directory: {path}"
LANG_MSG_SUBMODULE_REMOVED = "Successfully removed submodule '{path}'"
LANG_MSG_NO_SUBMODULE = "No submodule found at '{path}'"
LANG_MSG_KEEPING_SUBMODULE = "Keeping submodule (--keep-submodule flag used)"
LANG_MSG_LANG_REMOVED = "Language '{name}' has been removed successfully!"
LANG_MSG_NO_MODULES_DIR = "No grammars modules directory found."
LANG_MSG_NO_GITMODULES = "No .gitmodules file found."
LANG_MSG_NO_ORPHANS = "No orphaned modules found!"
LANG_MSG_FOUND_ORPHANS = "Found {count} orphaned module(s): {modules}"
LANG_MSG_REMOVED_ORPHAN = "Removed orphaned module: {module}"
LANG_MSG_CLEANUP_COMPLETE = "Cleanup complete!"
LANG_MSG_CLEANUP_CANCELLED = "Cleanup cancelled."

# (H) Language CLI error messages
LANG_ERR_MISSING_ARGS = "Error: Either language_name or --grammar-url must be provided"
LANG_ERR_REINSTALL_FAILED = "Failed to reinstall submodule: {error}"
LANG_ERR_MANUAL_REMOVE_HINT = "You may need to remove it manually and try again:"
LANG_ERR_REPO_NOT_FOUND = "Error: Repository not found at {url}"
LANG_ERR_CUSTOM_URL_HINT = "Try using a custom URL with: --grammar-url <your-repo-url>"
LANG_ERR_GIT = "Git error: {error}"
LANG_ERR_NODE_TYPES_WARNING = (
    "Warning: node-types.json not found in any expected location for {name}"
)
LANG_ERR_TREE_SITTER_JSON_WARNING = "Warning: tree-sitter.json not found in {path}"
LANG_ERR_NO_GRAMMARS_WARNING = "Warning: No grammars found in tree-sitter.json"
LANG_ERR_PARSE_NODE_TYPES = "Error parsing node-types.json: {error}"
LANG_ERR_UPDATE_CONFIG = "Error updating config file: {error}"
LANG_ERR_CONFIG_NOT_FOUND = "Could not find LANGUAGE_SPECS dictionary end"
LANG_ERR_REMOVE_CONFIG = "Failed to update config file: {error}"
LANG_ERR_REMOVE_SUBMODULE = "Failed to remove submodule: {error}"

# (H) Language CLI prompts
LANG_PROMPT_LANGUAGE_NAME = "Language name (e.g., 'c-sharp', 'python')"
LANG_PROMPT_COMMON_NAME = "What is the common name for this language?"
LANG_PROMPT_FUNCTIONS = "Select nodes representing FUNCTIONS (comma-separated)"
LANG_PROMPT_CLASSES = "Select nodes representing CLASSES (comma-separated)"
LANG_PROMPT_MODULES = "Select nodes representing MODULES (comma-separated)"
LANG_PROMPT_CALLS = "Select nodes representing FUNCTION CALLS (comma-separated)"
LANG_PROMPT_CONTINUE = "Do you want to continue?"
LANG_PROMPT_REMOVE_ORPHANS = "Do you want to remove these orphaned modules?"

# (H) Language CLI fallback manual add message
LANG_FALLBACK_MANUAL_ADD = (
    "FALLBACK: Please manually add the following entry to "
    "'LANGUAGE_SPECS' in 'codebase_rag/language_spec.py':"
)

# (H) Language CLI table configuration
LANG_TABLE_TITLE = "Configured Languages"
LANG_TABLE_COL_LANGUAGE = "Language"
LANG_TABLE_COL_FUNCTION_TYPES = "Function Types"
LANG_TABLE_COL_CLASS_TYPES = "Class Types"
LANG_TABLE_COL_CALL_TYPES = "Call Types"
LANG_TABLE_PLACEHOLDER = "—"
LANG_ELLIPSIS = "..."
LANG_GIT_SUFFIX = ".git"
LANG_GITMODULES_FILE = ".gitmodules"
LANG_CALL_KEYWORD_EXCLUDE = "call"

# (H) Git submodule regex
LANG_GITMODULES_REGEX = r"path = (grammars/tree-sitter-[^\\n]+)"


class CppNodeType(StrEnum):
    TRANSLATION_UNIT = "translation_unit"
    NAMESPACE_DEFINITION = "namespace_definition"
    NAMESPACE_IDENTIFIER = "namespace_identifier"
    IDENTIFIER = "identifier"
    EXPORT = "export"
    EXPORT_KEYWORD = "export_keyword"
    PRIMITIVE_TYPE = "primitive_type"
    DECLARATION = "declaration"
    FUNCTION_DEFINITION = "function_definition"
    TEMPLATE_DECLARATION = "template_declaration"
    CLASS_SPECIFIER = "class_specifier"
    FUNCTION_DECLARATOR = "function_declarator"
    POINTER_DECLARATOR = "pointer_declarator"
    REFERENCE_DECLARATOR = "reference_declarator"
    FIELD_DECLARATION = "field_declaration"
    FIELD_IDENTIFIER = "field_identifier"
    QUALIFIED_IDENTIFIER = "qualified_identifier"
    OPERATOR_NAME = "operator_name"
    DESTRUCTOR_NAME = "destructor_name"
    CONSTRUCTOR_OR_DESTRUCTOR_DEFINITION = "constructor_or_destructor_definition"
    CONSTRUCTOR_OR_DESTRUCTOR_DECLARATION = "constructor_or_destructor_declaration"
    INLINE_METHOD_DEFINITION = "inline_method_definition"
    OPERATOR_CAST_DEFINITION = "operator_cast_definition"
    TYPE_IDENTIFIER = "type_identifier"
    PARAMETER_LIST = "parameter_list"
    PARAMETER_DECLARATION = "parameter_declaration"
    OPTIONAL_PARAMETER_DECLARATION = "optional_parameter_declaration"
    INIT_DECLARATOR = "init_declarator"
    TEMPLATE_TYPE = "template_type"
    FIELD_EXPRESSION = "field_expression"
    COMPOUND_STATEMENT = "compound_statement"
    THIS = "this"
    TYPE_DEFINITION = "type_definition"
    ALIAS_DECLARATION = "alias_declaration"
    TYPE_DESCRIPTOR = "type_descriptor"


CPP_MODULE_PATH_MARKERS = frozenset({"interfaces", "modules"})

# (H) C++ module declaration prefixes
CPP_EXPORT_MODULE_PREFIX = "export module "
CPP_MODULE_PREFIX = "module "
CPP_MODULE_PRIVATE_PREFIX = "module ;"
CPP_IMPL_SUFFIX = "_impl"

# (H) C++ module type values
CPP_MODULE_TYPE_INTERFACE = "interface"
CPP_MODULE_TYPE_IMPLEMENTATION = "implementation"

# (H) C++ export prefixes for class detection
CPP_EXPORT_CLASS_PREFIX = "export class "
CPP_EXPORT_STRUCT_PREFIX = "export struct "
CPP_EXPORT_UNION_PREFIX = "export union "
CPP_EXPORT_TEMPLATE_PREFIX = "export template"
CPP_EXPORT_PREFIXES = (
    CPP_EXPORT_CLASS_PREFIX,
    CPP_EXPORT_STRUCT_PREFIX,
    CPP_EXPORT_UNION_PREFIX,
    CPP_EXPORT_TEMPLATE_PREFIX,
)

# (H) C++ keywords for class detection
CPP_KEYWORD_CLASS = "class"
CPP_KEYWORD_STRUCT = "struct"
CPP_EXPORTED_CLASS_KEYWORDS = frozenset({CPP_KEYWORD_CLASS, CPP_KEYWORD_STRUCT})

# (H) A C/C++ class/struct/union tag with no body is a forward declaration
# (H) (`class Widget;`); it must not become its own node, or it collides with the
# (H) real definition's qn and fragments one class into several same-named nodes.
CPP_TYPE_SPECIFIER_NODE_TYPES = frozenset(
    {"class_specifier", "struct_specifier", "union_specifier"}
)

CPP_FALLBACK_OPERATOR = "operator_unknown"
CPP_FALLBACK_DESTRUCTOR = "~destructor"
CPP_OPERATOR_TEXT_PREFIX = "operator"
CPP_DESTRUCTOR_PREFIX = "~"

CPP_OPERATOR_SYMBOL_MAP: dict[str, str] = {
    "+": "operator_plus",
    "-": "operator_minus",
    "*": "operator_multiply",
    "/": "operator_divide",
    "%": "operator_modulo",
    "=": "operator_assign",
    "==": "operator_equal",
    "!=": "operator_not_equal",
    "<": "operator_less",
    ">": "operator_greater",
    "<=": "operator_less_equal",
    ">=": "operator_greater_equal",
    "&&": "operator_logical_and",
    "||": "operator_logical_or",
    "&": "operator_bitwise_and",
    "|": "operator_bitwise_or",
    "^": "operator_bitwise_xor",
    "~": "operator_bitwise_not",
    "!": "operator_not",
    "<<": "operator_left_shift",
    ">>": "operator_right_shift",
    "++": "operator_increment",
    "--": "operator_decrement",
    "+=": "operator_plus_assign",
    "-=": "operator_minus_assign",
    "*=": "operator_multiply_assign",
    "/=": "operator_divide_assign",
    "%=": "operator_modulo_assign",
    "&=": "operator_and_assign",
    "|=": "operator_or_assign",
    "^=": "operator_xor_assign",
    "<<=": "operator_left_shift_assign",
    ">>=": "operator_right_shift_assign",
    "[]": "operator_subscript",
    "()": "operator_call",
}

# (H) Dependency parser TOML/JSON keys
DEP_KEY_TOOL = "tool"
DEP_KEY_POETRY = "poetry"
DEP_KEY_DEPENDENCIES = "dependencies"
DEP_KEY_DEV_DEPENDENCIES = "dev-dependencies"
DEP_KEY_PROJECT = "project"
DEP_KEY_OPTIONAL_DEPS = "optional-dependencies"
DEP_KEY_DEV_DEPS_JSON = "devDependencies"
DEP_KEY_PEER_DEPS = "peerDependencies"
DEP_KEY_REQUIRE = "require"
DEP_KEY_REQUIRE_DEV = "require-dev"
DEP_KEY_VERSION = "version"
DEP_KEY_GROUP = "group"

# (H) Dependency parser XML attributes
DEP_ATTR_INCLUDE = "Include"
DEP_ATTR_VERSION = "Version"
DEP_XML_PACKAGE_REF = "PackageReference"

# (H) Dependency parser language exclusions
DEP_EXCLUDE_PYTHON = "python"
DEP_EXCLUDE_PHP = "php"

# (H) Dependency file names (lowercase)
DEP_FILE_PYPROJECT = "pyproject.toml"
DEP_FILE_REQUIREMENTS = "requirements.txt"
DEP_FILE_PACKAGE_JSON = "package.json"
DEP_FILE_CARGO = "cargo.toml"
DEP_FILE_GOMOD = "go.mod"
# (H) The go.mod directive naming the module path that prefixes every import of
# (H) the module's packages; a same-line comment (incl. the official
# (H) `// Deprecated:` form) may trail it.
GO_KEYWORD_MODULE = "module"
GO_MOD_COMMENT_PREFIX = "//"
DEP_FILE_GEMFILE = "gemfile"
DEP_FILE_COMPOSER = "composer.json"

# (H) Go.mod parsing patterns
GOMOD_REQUIRE_BLOCK_START = "require ("
GOMOD_BLOCK_END = ")"
GOMOD_REQUIRE_LINE_PREFIX = "require "
GOMOD_COMMENT_PREFIX = "//"

# (H) Gemfile parsing patterns
GEMFILE_GEM_PREFIX = "gem "

# (H) Incremental update hash cache
HASH_CACHE_FILENAME = ".cgr-hash-cache.json"
DIR_MTIMES_FILENAME = ".cgr-dir-mtimes.json"
GIT_DIR_NAME = ".git"
ROOT_DIR_KEY = "."
JSON_EMPTY_OBJECT = "{}"

# (H) Import processor cache config
IMPORT_CACHE_TTL = 3600
IMPORT_CACHE_DIR = ".cache/codebase_rag"
IMPORT_CACHE_FILE = "stdlib_cache.json"
IMPORT_CACHE_KEY = "cache"
IMPORT_TIMESTAMPS_KEY = "timestamps"

# (H) Tree-sitter Python import node types
TS_IMPORT_STATEMENT = "import_statement"
TS_IMPORT_FROM_STATEMENT = "import_from_statement"
TS_DOTTED_NAME = "dotted_name"
TS_ALIASED_IMPORT = "aliased_import"
TS_RELATIVE_IMPORT = "relative_import"
TS_IMPORT_PREFIX = "import_prefix"
TS_WILDCARD_IMPORT = "wildcard_import"

# (H) Tree-sitter JS/TS import node types
TS_STRING = "string"
TS_IMPORT_CLAUSE = "import_clause"
TS_LEXICAL_DECLARATION = "lexical_declaration"
TS_EXPORT_STATEMENT = "export_statement"
TS_NAMED_IMPORTS = "named_imports"
TS_IMPORT_SPECIFIER = "import_specifier"
TS_NAMESPACE_IMPORT = "namespace_import"
TS_IDENTIFIER = "identifier"
TS_VARIABLE_DECLARATOR = "variable_declarator"
TS_CALL_EXPRESSION = "call_expression"
TS_EXPORT_CLAUSE = "export_clause"
TS_EXPORT_SPECIFIER = "export_specifier"
TS_EXPORT_DEFAULT = "default"
TS_ACCESSIBILITY_MODIFIER = "accessibility_modifier"
TS_PRIVATE = "private"
TS_PRIVATE_PROPERTY_IDENTIFIER = "private_property_identifier"

# (H) Tree-sitter Java import node types
TS_IMPORT_DECLARATION = "import_declaration"
TS_STATIC = "static"
TS_SCOPED_IDENTIFIER = "scoped_identifier"
TS_ASTERISK = "asterisk"

# (H) Tree-sitter Rust import node types
TS_USE_DECLARATION = "use_declaration"

# (H) Tree-sitter Go import node types
TS_IMPORT_SPEC = "import_spec"
TS_IMPORT_SPEC_LIST = "import_spec_list"
TS_PACKAGE_IDENTIFIER = "package_identifier"
TS_INTERPRETED_STRING_LITERAL = "interpreted_string_literal"

# (H) Tree-sitter C++ import node types
TS_PREPROC_INCLUDE = "preproc_include"
TS_TEMPLATE_FUNCTION = "template_function"
TS_DECLARATION = "declaration"
TS_STRING_LITERAL = "string_literal"
TS_SYSTEM_LIB_STRING = "system_lib_string"
TS_TEMPLATE_ARGUMENT_LIST = "template_argument_list"
TS_TYPE_DESCRIPTOR = "type_descriptor"
TS_TYPE_IDENTIFIER = "type_identifier"
LUA_STRING_TYPES = (TS_STRING, TS_STRING_LITERAL)

# (H) Tree-sitter Lua node types
TS_DOT_INDEX_EXPRESSION = "dot_index_expression"
TS_LUA_VARIABLE_DECLARATION = "variable_declaration"
TS_LUA_ASSIGNMENT_STATEMENT = "assignment_statement"
TS_LUA_VARIABLE_LIST = "variable_list"
TS_LUA_EXPRESSION_LIST = "expression_list"
TS_LUA_FUNCTION_CALL = "function_call"
TS_LUA_METHOD_INDEX_EXPRESSION = "method_index_expression"
TS_LUA_IDENTIFIER = "identifier"
TS_LUA_LOCAL_STATEMENT = "local_statement"
LUA_STATEMENT_SUFFIX = "statement"
LUA_DEFAULT_VAR_TYPES = (TS_LUA_IDENTIFIER,)

# (H) Lua method separator
LUA_METHOD_SEPARATOR = ":"

# (H) Fallback display value
STR_NONE = "None"

# (H) Tree-sitter JS/TS utility node types
TS_RETURN_STATEMENT = "return_statement"
TS_RETURN = "return"
TS_NEW_EXPRESSION = "new_expression"

# (H) Tree-sitter class/module node types for class_ingest
TS_MODULE_DECLARATION = "module_declaration"
TS_IMPL_ITEM = "impl_item"
TS_INTERFACE_DECLARATION = "interface_declaration"
TS_ENUM_DECLARATION = "enum_declaration"
TS_ENUM_SPECIFIER = "enum_specifier"
TS_ENUM_CLASS_SPECIFIER = "enum_class_specifier"
TS_TYPE_ALIAS_DECLARATION = "type_alias_declaration"
TS_STRUCT_SPECIFIER = "struct_specifier"
TS_UNION_SPECIFIER = "union_specifier"
TS_CLASS_DECLARATION = "class_declaration"
TS_NAMESPACE_DEFINITION = "namespace_definition"
TS_ABSTRACT_CLASS_DECLARATION = "abstract_class_declaration"
TS_INTERNAL_MODULE = "internal_module"

# (H) Tree-sitter Go node types
TS_GO_TYPE_DECLARATION = "type_declaration"
TS_GO_TYPE_SPEC = "type_spec"
TS_GO_TYPE_ALIAS = "type_alias"
TS_GO_STRUCT_TYPE = "struct_type"
TS_GO_SELECTOR_EXPRESSION = "selector_expression"
TS_GO_FIELD_DECLARATION_LIST = "field_declaration_list"
TS_GO_FIELD_DECLARATION = "field_declaration"
TS_GO_FIELD_IDENTIFIER = "field_identifier"
TS_GO_INTERFACE_TYPE = "interface_type"
TS_GO_PARAMETER_DECLARATION = "parameter_declaration"
TS_GO_FUNC_LITERAL = "func_literal"
TS_GO_SOURCE_FILE = "source_file"
TS_GO_FUNCTION_DECLARATION = "function_declaration"
TS_GO_METHOD_DECLARATION = "method_declaration"
TS_GO_CALL_EXPRESSION = "call_expression"
TS_GO_IMPORT_DECLARATION = "import_declaration"
TS_GO_PARAMETER_LIST = "parameter_list"
TS_GO_VAR_DECLARATION = "var_declaration"
TS_GO_VAR_SPEC = "var_spec"
TS_GO_SHORT_VAR_DECLARATION = "short_var_declaration"
TS_GO_ASSIGNMENT_STATEMENT = "assignment_statement"
TS_GO_EXPRESSION_LIST = "expression_list"
TS_GO_COMPOSITE_LITERAL = "composite_literal"
TS_GO_LITERAL_VALUE = "literal_value"
TS_GO_KEYED_ELEMENT = "keyed_element"
TS_GO_LITERAL_ELEMENT = "literal_element"
TS_GO_UNARY_EXPRESSION = "unary_expression"
TS_GO_POINTER_TYPE = "pointer_type"
# (H) Go composite types a method may return; a chained call lands on the CONTAINER,
# (H) not its element, so return-type inference must not unwrap these to an element
# (H) name (a `[]Command` return must not resolve `.Run()` to `Command.Run`).
TS_GO_CONTAINER_TYPES: frozenset[str] = frozenset(
    {"slice_type", "array_type", "map_type", "channel_type", "function_type"}
)

# (H) Tree-sitter Scala node types
TS_SCALA_CLASS_DEFINITION = "class_definition"
TS_SCALA_OBJECT_DEFINITION = "object_definition"
TS_SCALA_TRAIT_DEFINITION = "trait_definition"
TS_SCALA_COMPILATION_UNIT = "compilation_unit"
TS_SCALA_FUNCTION_DEFINITION = "function_definition"
TS_SCALA_FUNCTION_DECLARATION = "function_declaration"
TS_SCALA_CALL_EXPRESSION = "call_expression"
# (H) Shared tree-sitter node type: a call with explicit type args, e.g. Rust
# (H) turbofish `f::<T>()` and Scala `f[T]()`. Its `function` field holds the
# (H) actual callee (identifier or scoped_identifier).
TS_GENERIC_FUNCTION = "generic_function"
TS_SCALA_GENERIC_FUNCTION = TS_GENERIC_FUNCTION
TS_SCALA_FIELD_EXPRESSION = "field_expression"
TS_SCALA_INFIX_EXPRESSION = "infix_expression"
TS_SCALA_IMPORT_DECLARATION = "import_declaration"

# (H) Tree-sitter PHP node types
TS_PHP_FUNCTION_DEFINITION = "function_definition"
TS_PHP_METHOD_DECLARATION = "method_declaration"
TS_PHP_TRAIT_DECLARATION = "trait_declaration"
# (H) PHP inheritance clauses: `extends ...` (base_clause, for class AND
# (H) interface) and `implements ...` (class_interface_clause); each lists `name`
# (H) nodes naming the base types.
TS_PHP_BASE_CLAUSE = "base_clause"
TS_PHP_CLASS_INTERFACE_CLAUSE = "class_interface_clause"
TS_PHP_NAME = "name"
# (H) PHP fully-qualified base (`\Exception`, `\App\Base`); its trailing `name`
# (H) child is the simple name cgr resolves against.
TS_PHP_QUALIFIED_NAME = "qualified_name"
TS_PHP_FUNCTION_STATIC_DECLARATION = "function_static_declaration"
TS_PHP_ANONYMOUS_FUNCTION = "anonymous_function"
TS_PHP_ARROW_FUNCTION = "arrow_function"
TS_PHP_MEMBER_CALL_EXPRESSION = "member_call_expression"
TS_PHP_SCOPED_CALL_EXPRESSION = "scoped_call_expression"
TS_PHP_FUNCTION_CALL_EXPRESSION = "function_call_expression"
TS_PHP_NULLSAFE_MEMBER_CALL_EXPRESSION = "nullsafe_member_call_expression"
TS_PHP_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
TS_PHP_NAMESPACE_DEFINITION = "namespace_definition"
TS_PHP_NAMESPACE_USE_DECLARATION = "namespace_use_declaration"
TS_PHP_NAMESPACE_USE_CLAUSE = "namespace_use_clause"
TS_PHP_FUNCTION = "function"
TS_PHP_INCLUDE_EXPRESSION = "include_expression"
TS_PHP_INCLUDE_ONCE_EXPRESSION = "include_once_expression"
TS_PHP_REQUIRE_EXPRESSION = "require_expression"
TS_PHP_REQUIRE_ONCE_EXPRESSION = "require_once_expression"
TS_PHP_ATTRIBUTE_LIST = "attribute_list"
TS_PHP_ATTRIBUTE = "attribute"
TS_PHP_ATTRIBUTE_GROUP = "attribute_group"
TS_PHP_VISIBILITY_MODIFIER = "visibility_modifier"
TS_PHP_USE_DECLARATION = "use_declaration"
TS_PHP_QUALIFIED_NAME = "qualified_name"

# (H) Tree-sitter Lua node types for language_spec
TS_LUA_CHUNK = "chunk"
TS_LUA_FUNCTION_DECLARATION = "function_declaration"
TS_LUA_FUNCTION_DEFINITION = "function_definition"
TS_LUA_FUNCTION_CALL = "function_call"

# (H) Tree-sitter C++ node types for language_spec
TS_CPP_FUNCTION_DEFINITION = "function_definition"
TS_CPP_DECLARATION = "declaration"
TS_CPP_FIELD_DECLARATION = "field_declaration"
TS_CPP_TEMPLATE_DECLARATION = "template_declaration"
TS_CPP_TEMPLATE_PARAMETER_LIST = "template_parameter_list"
# (H) The template TYPE-parameter declaration node types. A value/non-type param
# (H) (`parameter_declaration`, e.g. `int N` / `MyEnum E`) and a template-template param
# (H) are deliberately excluded: their type name is a concrete type, not a stand-in that
# (H) a call receiver could be instantiated as, so it must not enter the template-param set.
CPP_TYPE_PARAMETER_DECL_TYPES = frozenset(
    {
        "type_parameter_declaration",
        "optional_type_parameter_declaration",
        "variadic_type_parameter_declaration",
    }
)
TS_CPP_LAMBDA_EXPRESSION = "lambda_expression"
TS_CPP_TRANSLATION_UNIT = "translation_unit"
TS_CPP_LINKAGE_SPECIFICATION = "linkage_specification"
TS_CPP_CALL_EXPRESSION = "call_expression"
TS_CPP_FIELD_EXPRESSION = "field_expression"
TS_CPP_SUBSCRIPT_EXPRESSION = "subscript_expression"
TS_CPP_NEW_EXPRESSION = "new_expression"
TS_CPP_DELETE_EXPRESSION = "delete_expression"
TS_CPP_BINARY_EXPRESSION = "binary_expression"
TS_CPP_UNARY_EXPRESSION = "unary_expression"
TS_CPP_UPDATE_EXPRESSION = "update_expression"
TS_CPP_FUNCTION_DECLARATOR = "function_declarator"
# (H) Substring shared by C++ declarator node types (pointer_declarator,
# (H) reference_declarator, parenthesized_declarator, ...), used to unwrap a
# (H) parameter declarator down to its bound identifier.
CPP_DECLARATOR_SUFFIX = "declarator"

# (H) Tree-sitter Java node types for language_spec
TS_JAVA_METHOD_INVOCATION = "method_invocation"
TS_JAVA_ANNOTATION_TYPE_DECLARATION = "annotation_type_declaration"

TS_BASE_CLASS_CLAUSE = "base_class_clause"
TS_TEMPLATE_TYPE = "template_type"
TS_ACCESS_SPECIFIER = "access_specifier"
TS_VIRTUAL = "virtual"
TS_TYPE_LIST = "type_list"
TS_CLASS_HERITAGE = "class_heritage"
# (H) TS class `implements I, J` clause (a child of class_heritage).
TS_IMPLEMENTS_CLAUSE = "implements_clause"
TS_EXTENDS_CLAUSE = "extends_clause"
TS_MEMBER_EXPRESSION = "member_expression"
TS_SELECTOR_EXPRESSION = "selector_expression"
TS_EXTENDS = "extends"
TS_ARGUMENTS = "arguments"
TS_EXTENDS_TYPE_CLAUSE = "extends_type_clause"
# (H) Java interface `extends A, B` clause (tree-sitter-java); holds a type_list.
TS_JAVA_EXTENDS_INTERFACES = "extends_interfaces"
TS_METHOD_DEFINITION = "method_definition"
TS_DECORATOR = "decorator"
TS_ERROR = "ERROR"
TS_EXPRESSION_STATEMENT = "expression_statement"
TS_STATEMENT_BLOCK = "statement_block"
TS_PARENTHESIZED_EXPRESSION = "parenthesized_expression"
TS_ATTRIBUTE = "attribute"

# (H) Derived node type tuples for class ingestion
CPP_CLASS_TYPES = (CppNodeType.CLASS_SPECIFIER, TS_STRUCT_SPECIFIER)
CPP_COMPOUND_TYPES = (*CPP_CLASS_TYPES, TS_UNION_SPECIFIER, TS_ENUM_SPECIFIER)
# (H) Node types that open their own variable scope; C++ local-variable inference must
# (H) not descend into them, or a name declared inside a lambda / nested function /
# (H) local class body would be attributed to the enclosing function's scope.
CPP_NESTED_SCOPE_NODE_TYPES = frozenset(
    (
        TS_CPP_FUNCTION_DEFINITION,
        TS_CPP_LAMBDA_EXPRESSION,
        *CPP_COMPOUND_TYPES,
    )
)
JS_TS_PARENT_REF_TYPES = (TS_IDENTIFIER, TS_MEMBER_EXPRESSION)
# (H) JSX element nodes that carry a component name (javascript and tsx
# (H) grammars share these); the closing element repeats the name and must not
# (H) double-emit.
TS_JSX_SELF_CLOSING_ELEMENT = "jsx_self_closing_element"
TS_JSX_OPENING_ELEMENT = "jsx_opening_element"
# (H) The `{...}` wrapper around an expression in a JSX attribute value or child
# (H) (`onClick={handleLogout}`, `onClick={() => x()}`); its inner expression can
# (H) hand a function to the element as a prop.
TS_JSX_EXPRESSION = "jsx_expression"

# (H) Import processor function names
IMPORT_REQUIRE = "require"
IMPORT_PCALL = "pcall"
IMPORT_IMPORT = "import"

# (H) Lua stdlib module names
LUA_STDLIB_MODULES = frozenset(
    {
        "string",
        "math",
        "table",
        "os",
        "io",
        "debug",
        "package",
        "coroutine",
        "utf8",
        "bit32",
    }
)

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

# (H) C++ stdlib namespace and type inference prefixes
CPP_STD_NAMESPACE = "std"
CPP_PREFIX_IS = "is_"
CPP_PREFIX_HAS = "has_"

# (H) JSON keys for stdlib introspection subprocess responses
JSON_KEY_HAS_ENTITY = "hasEntity"
JSON_KEY_ENTITY_TYPE = "entityType"

# (H) C++ stdlib entity names for heuristic detection
CPP_STDLIB_ENTITIES = frozenset(
    {
        "vector",
        "string",
        "map",
        "set",
        "list",
        "deque",
        "unique_ptr",
        "shared_ptr",
        "weak_ptr",
        "thread",
        "mutex",
        "condition_variable",
        "future",
        "promise",
        "sort",
        "find",
        "copy",
        "transform",
        "accumulate",
    }
)

# (H) Java stdlib package prefixes for static stdlib detection
JAVA_STDLIB_PREFIXES = (
    "java.",
    "javax.",
    "jdk.",
    "com.sun.",
    "sun.",
    "org.w3c.",
    "org.xml.",
    "org.ietf.",
    "org.omg.",
    "netscape.",
)

# (H) Java common class names for heuristic detection
JAVA_STDLIB_CLASSES = frozenset(
    {
        "String",
        "Object",
        "Integer",
        "Double",
        "Boolean",
        "ArrayList",
        "HashMap",
        "HashSet",
        "LinkedList",
        "File",
        "URL",
        "Pattern",
        "LocalDateTime",
        "BigDecimal",
    }
)

# (H) Import processor misc
IMPORT_DEFAULT_SUFFIX = ".default"
IMPORT_STD_PREFIX = "std."
CPP_STD_PREFIX = "std"
IMPORT_MODULE_LABEL = "Module"
IMPORT_QUALIFIED_NAME = "qualified_name"
IMPORT_RELATIONSHIP = "IMPORTS"

# (H) Java type inference constants
JAVA_LANG_PREFIX = "java.lang."
JAVA_ARRAY_SUFFIX = "[]"
JAVA_SUFFIX_EXCEPTION = "Exception"
JAVA_SUFFIX_ERROR = "Error"
JAVA_SUFFIX_INTERFACE = "Interface"
JAVA_SUFFIX_BUILDER = "Builder"
JAVA_PRIMITIVE_TYPES = frozenset(
    {
        "int",
        "long",
        "double",
        "float",
        "boolean",
        "char",
        "byte",
        "short",
    }
)
JAVA_WRAPPER_TYPES = frozenset(
    {
        "String",
        "Object",
        "Integer",
        "Long",
        "Double",
        "Boolean",
    }
)

# (H) Java tree-sitter node types
TS_FORMAL_PARAMETER = "formal_parameter"
TS_SPREAD_PARAMETER = "spread_parameter"
TS_LOCAL_VARIABLE_DECLARATION = "local_variable_declaration"
TS_FIELD_DECLARATION = "field_declaration"
TS_ASSIGNMENT_EXPRESSION = "assignment_expression"
# (H) TS "cast" wrappers that are transparent for reference resolution: `x as T`,
# (H) `x satisfies T`, and the non-null assertion `x!`. Their first named child is
# (H) the wrapped value, so unwrapping reaches the real referenced expression
# (H) (`export const persist = persistImpl as unknown as Persist`).
TS_AS_EXPRESSION = "as_expression"
TS_SATISFIES_EXPRESSION = "satisfies_expression"
TS_NON_NULL_EXPRESSION = "non_null_expression"
TS_CAST_WRAPPER_TYPES = frozenset(
    {TS_AS_EXPRESSION, TS_SATISFIES_EXPRESSION, TS_NON_NULL_EXPRESSION}
)
TS_OBJECT_CREATION_EXPRESSION = "object_creation_expression"
TS_METHOD_INVOCATION = "method_invocation"
TS_FIELD_ACCESS = "field_access"
TS_INTEGER_LITERAL = "integer_literal"
TS_DECIMAL_FLOATING_POINT_LITERAL = "decimal_floating_point_literal"
TS_ARRAY_CREATION_EXPRESSION = "array_creation_expression"
TS_METHOD_DECLARATION = "method_declaration"
TS_ENHANCED_FOR_STATEMENT = "enhanced_for_statement"
TS_RECORD_DECLARATION = "record_declaration"
TS_TRUE = "true"
TS_FALSE = "false"

# (H) Tree-sitter field names for child_by_field_name
TS_FIELD_NAME = "name"
TS_FIELD_TYPE = "type"
TS_SCOPED_TYPE_IDENTIFIER = "scoped_type_identifier"
TS_FIELD_SUPERCLASS = "superclass"
TS_FIELD_INTERFACES = "interfaces"
TS_FIELD_TYPE_PARAMETERS = "type_parameters"
TS_FIELD_PARAMETERS = "parameters"
TS_FIELD_DECLARATOR = "declarator"
TS_FIELD_OBJECT = "object"
TS_FIELD_ARGUMENTS = "arguments"
TS_FIELD_FUNCTION = "function"
TS_FIELD_BODY = "body"
TS_FIELD_LEFT = "left"
TS_FIELD_RIGHT = "right"

# (H) Java type inference keywords
JAVA_KEYWORD_THIS = "this"
JAVA_KEYWORD_SUPER = "super"

# (H) Java array type suffix
JAVA_ARRAY_SUFFIX = "[]"

# (H) Java heuristic patterns
JAVA_GETTER_PATTERN = "get"
JAVA_NAME_PATTERN = "name"
JAVA_ID_PATTERN = "id"
JAVA_SIZE_PATTERN = "size"
JAVA_LENGTH_PATTERN = "length"
JAVA_CREATE_PATTERN = "create"
JAVA_NEW_PATTERN = "new"
JAVA_IS_PATTERN = "is"
JAVA_HAS_PATTERN = "has"
JAVA_USER_PATTERN = "user"
JAVA_ORDER_PATTERN = "order"

# (H) Java entity type names
ENTITY_CONSTRUCTOR = "Constructor"

# (H) Java callable entity types for method resolution
JAVA_CALLABLE_ENTITY_TYPES = frozenset({ENTITY_METHOD, ENTITY_CONSTRUCTOR})

# (H) Java primitive type names
JAVA_TYPE_STRING = "String"
JAVA_TYPE_INT = "int"
JAVA_TYPE_DOUBLE = "double"
JAVA_TYPE_BOOLEAN = "boolean"
JAVA_TYPE_LONG = "java.lang.Long"
JAVA_TYPE_STRING_FQN = "java.lang.String"
JAVA_TYPE_OBJECT = "Object"

# (H) Java heuristic return type names
JAVA_HEURISTIC_USER = "User"
JAVA_HEURISTIC_ORDER = "Order"

# (H) Java tree-sitter node types for java_utils
TS_PACKAGE_DECLARATION = "package_declaration"
TS_ANNOTATION_TYPE_DECLARATION = "annotation_type_declaration"
TS_CONSTRUCTOR_DECLARATION = "constructor_declaration"
TS_ANNOTATION = "annotation"
TS_MARKER_ANNOTATION = "marker_annotation"
TS_GENERIC_TYPE = "generic_type"
TS_TYPE_PARAMETER = "type_parameter"
TS_MODIFIERS = "modifiers"
TS_VOID_TYPE = "void_type"
TS_PROGRAM = "program"
TS_THIS = "this"
TS_SUPER = "super"

# (H) Java modifier node types
JAVA_MODIFIER_PUBLIC = "public"
JAVA_MODIFIER_PRIVATE = "private"
JAVA_MODIFIER_PROTECTED = "protected"
JAVA_MODIFIER_STATIC = "static"
JAVA_MODIFIER_FINAL = "final"
JAVA_MODIFIER_ABSTRACT = "abstract"
JAVA_MODIFIER_SYNCHRONIZED = "synchronized"
JAVA_MODIFIER_TRANSIENT = "transient"
JAVA_MODIFIER_VOLATILE = "volatile"

JAVA_CLASS_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_ABSTRACT,
    }
)

JAVA_METHOD_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_ABSTRACT,
        JAVA_MODIFIER_SYNCHRONIZED,
    }
)

JAVA_FIELD_MODIFIERS = frozenset(
    {
        JAVA_MODIFIER_PUBLIC,
        JAVA_MODIFIER_PRIVATE,
        JAVA_MODIFIER_PROTECTED,
        JAVA_MODIFIER_STATIC,
        JAVA_MODIFIER_FINAL,
        JAVA_MODIFIER_TRANSIENT,
        JAVA_MODIFIER_VOLATILE,
    }
)

# (H) Java visibility values
JAVA_VISIBILITY_PUBLIC = "public"
JAVA_VISIBILITY_PROTECTED = "protected"
JAVA_VISIBILITY_PRIVATE = "private"
JAVA_VISIBILITY_PACKAGE = "package"

# (H) Java class type suffixes and names
JAVA_DECLARATION_SUFFIX = "_declaration"
JAVA_TYPE_METHOD = "method"
JAVA_TYPE_CONSTRUCTOR = "constructor"

# (H) Java class node types for matching
JAVA_CLASS_NODE_TYPES = frozenset(
    {
        TS_CLASS_DECLARATION,
        TS_INTERFACE_DECLARATION,
        TS_ENUM_DECLARATION,
        TS_ANNOTATION_TYPE_DECLARATION,
        TS_RECORD_DECLARATION,
    }
)

# (H) Java method node types
JAVA_METHOD_NODE_TYPES = frozenset(
    {
        TS_METHOD_DECLARATION,
        TS_CONSTRUCTOR_DECLARATION,
    }
)

# (H) Java main method constants
JAVA_MAIN_METHOD_NAME = "main"
JAVA_MAIN_PARAM_ARRAY = "String[]"
JAVA_MAIN_PARAM_VARARGS = "String..."
JAVA_MAIN_PARAM_TYPE = "String"

# (H) Java path parsing constants
JAVA_PATH_JAVA = "java"
JAVA_PATH_KOTLIN = "kotlin"
JAVA_PATH_SCALA = "scala"
JAVA_PATH_SRC = "src"
JAVA_PATH_MAIN = "main"
JAVA_PATH_TEST = "test"

JAVA_JVM_LANGUAGES = frozenset(
    {
        JAVA_PATH_JAVA,
        JAVA_PATH_KOTLIN,
        JAVA_PATH_SCALA,
    }
)

JAVA_SRC_FOLDERS = frozenset(
    {
        JAVA_PATH_MAIN,
        JAVA_PATH_TEST,
    }
)

# (H) Delimiter tokens for argument parsing
DELIMITER_TOKENS = frozenset({"(", ")", ","})

# (H) Python tree-sitter node types for type inference
TS_PY_IDENTIFIER = "identifier"
TS_PY_TYPED_PARAMETER = "typed_parameter"
TS_PY_TYPED_DEFAULT_PARAMETER = "typed_default_parameter"
TS_PY_ATTRIBUTE = "attribute"
TS_PY_CALL = "call"
TS_PY_LIST = "list"
TS_PY_DICTIONARY = "dictionary"
TS_PY_PAIR = "pair"
TS_PY_SET = "set"
TS_PY_TUPLE = "tuple"
TS_PY_LIST_COMPREHENSION = "list_comprehension"
TS_PY_FOR_STATEMENT = "for_statement"
TS_PY_FOR_IN_CLAUSE = "for_in_clause"
TS_PY_ASSIGNMENT = "assignment"
PY_ASSIGNMENT_QUERY = "(assignment) @assignment"
PY_RETURN_QUERY = "(return_statement) @return_stmt"
TS_PY_CLASS_DEFINITION = "class_definition"
TS_PY_BLOCK = "block"
TS_PY_FUNCTION_DEFINITION = "function_definition"
TS_PY_LAMBDA = "lambda"
TS_PY_RETURN_STATEMENT = "return_statement"
TS_PY_RETURN = "return"
TS_PY_KEYWORD = "keyword"
TS_PY_MODULE = "module"
TS_PY_IMPORT_STATEMENT = "import_statement"
TS_PY_IMPORT_FROM_STATEMENT = "import_from_statement"
TS_PY_WITH_STATEMENT = "with_statement"
TS_PY_EXPRESSION_STATEMENT = "expression_statement"
TS_PY_STRING = "string"
TS_PY_DECORATED_DEFINITION = "decorated_definition"
TS_PY_DECORATOR = "decorator"
TS_PY_KEYWORD_ARGUMENT = "keyword_argument"
TS_PY_DEFAULT_PARAMETER = "default_parameter"
TS_PY_LIST_SPLAT_PATTERN = "list_splat_pattern"
TS_PY_DICTIONARY_SPLAT_PATTERN = "dictionary_splat_pattern"
TS_PY_SUBSCRIPT = "subscript"
TS_PY_COMPARISON_OPERATOR = "comparison_operator"
TS_FIELD_OPERATORS = "operators"
TS_PY_IF_STATEMENT = "if_statement"
TS_PY_WHILE_STATEMENT = "while_statement"
TS_PY_ELIF_CLAUSE = "elif_clause"
TS_PY_CONDITIONAL_EXPRESSION = "conditional_expression"
TS_PY_BOOLEAN_OPERATOR = "boolean_operator"
TS_PY_NOT_OPERATOR = "not_operator"
TS_FIELD_CONDITION = "condition"
TS_FIELD_ARGUMENT = "argument"

# (H) Python operator syntax dispatches to dunder methods at runtime; these names
# (H) let the call extractor synthesize the implied <operand>.__dunder__ call.
PY_OP_IN = "in"
PY_BUILTIN_LEN = "len"
PY_BUILTIN_GETATTR = "getattr"
TS_PY_STRING_CONTENT = "string_content"
PY_DUNDER_GETITEM = "__getitem__"
PY_DUNDER_SETITEM = "__setitem__"
PY_DUNDER_CONTAINS = "__contains__"
PY_DUNDER_LEN = "__len__"
PY_DUNDER_BOOL = "__bool__"
# (H) Operands with these characters are not simple attribute/name chains (calls,
# (H) nested subscripts, whitespace), so the operator-dispatch synthesizer skips them.
PY_OPERAND_REJECT_CHARS = "()[]{}\n\t "
# (H) Optional annotation handling: X | None names a single concrete class.
PY_UNION_SEPARATOR = "|"
PY_NONE = "None"

# (H) Python keyword identifiers
PY_KEYWORD_SELF = "self"
PY_KEYWORD_CLS = "cls"
# (H) Visibility by naming convention: a leading underscore marks a private
# (H) symbol, while a dunder (__x__) is public API invoked by the runtime.
PY_NAME_UNDERSCORE = "_"
PY_NAME_DUNDER = "__"
# (H) typing.Protocol base name and the conventional XxxProtocol class suffix
# (H) used to map a Protocol to its concrete implementer.
PY_PROTOCOL = "Protocol"
PY_METHOD_INIT = "__init__"
DECORATOR_AT = "@"
PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property"})
ABSTRACT_DECORATORS: frozenset[str] = frozenset({"abstractmethod", "abstractproperty"})

# (H) Eager builtins that invoke a callable argument synchronously within the
# (H) caller's own stack frame; a function passed to one is invoked there, so the
# (H) trace attributes the call to the enclosing function (no Python frame exists
# (H) for the builtin). Lazy higher-order builtins (map/filter) are excluded:
# (H) they defer invocation until the result is consumed, which may be elsewhere.
HIGHER_ORDER_BUILTINS: frozenset[str] = frozenset({"sorted", "min", "max", "reduce"})

# (H) Python attribute prefixes
PY_SELF_PREFIX = "self."
PY_CLS_PREFIX = "cls."

# (H) Python type inference patterns
PY_VAR_PATTERN_ALL = "all_"
PY_VAR_SUFFIX_PLURAL = "s"
PY_CLASS_REPOSITORY = "Repository"
PY_MODELS_BASE_PATH = ".models.base."
PY_METHOD_CREATE = "create"

# (H) Type inference scoring
PY_SCORE_EXACT_MATCH = 100
PY_SCORE_SUFFIX_MATCH = 90
PY_SCORE_CONTAINS_BASE = 80

# (H) Type inference defaults
TYPE_INFERENCE_LIST = "list"
TYPE_INFERENCE_BASE_MODEL = "BaseModel"

# (H) Recursion guard attributes
ATTR_TYPE_INFERENCE_IN_PROGRESS = "_type_inference_in_progress"
GUARD_INHERITED_METHOD = "_inherited_method_guard"

# (H) JS/TS ingest node types
TS_PAIR = "pair"
TS_OBJECT = "object"
TS_ARRAY = "array"

# (H) When a variable_declarator's value is one of these, the variable binds the
# (H) call/construction RESULT, not a function -- so an arrow found inside its
# (H) arguments (`const m = useMutation({fn: () => {}})`) must not inherit the
# (H) variable's name. Object-literal / arrow values are not here, so arrows nested
# (H) directly under an object bound to a const still take the object's name.
JS_CALL_RESULT_VALUE_TYPES = frozenset({TS_CALL_EXPRESSION, TS_NEW_EXPRESSION})
TS_FUNCTION_EXPRESSION = "function_expression"
TS_ARROW_FUNCTION = "arrow_function"
TS_REQUIRED_PARAMETER = "required_parameter"
TS_OPTIONAL_PARAMETER = "optional_parameter"
TS_FIELD_PATTERN = "pattern"
TS_FIELD_PARAMETER = "parameter"
TS_MODULE = "module"
TS_CLASS_BODY = "class_body"
TS_STATIC = "static"
TS_PROPERTY_IDENTIFIER = "property_identifier"
TS_VARIABLE_DECLARATOR = "variable_declarator"

# (H) JS prototype property keywords
JS_PROTOTYPE_KEYWORD = "prototype"
JS_OBJECT_NAME = "Object"
JS_CREATE_METHOD = "create"

# (H) JS prototype inheritance query
JS_PROTOTYPE_INHERITANCE_QUERY = """
(assignment_expression
  left: (member_expression
    object: (identifier) @child_class
    property: (property_identifier) @prototype (#eq? @prototype "prototype"))
  right: (call_expression
    function: (member_expression
      object: (identifier) @object_name (#eq? @object_name "Object")
      property: (property_identifier) @create_method (#eq? @create_method "create"))
    arguments: (arguments
      (member_expression
        object: (identifier) @parent_class
        property: (property_identifier) @parent_prototype (#eq? @parent_prototype "prototype")))))
"""

# (H) JS prototype method assignment query
JS_PROTOTYPE_METHOD_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @constructor_name
      property: (property_identifier) @prototype_keyword (#eq? @prototype_keyword "prototype"))
    property: (property_identifier) @method_name)
  right: (function_expression) @method_function)
"""

# (H) JS object method query
JS_OBJECT_METHOD_QUERY = """
(pair
  key: (property_identifier) @method_name
  value: (function_expression) @method_function)
"""

# (H) JS method definition query
JS_METHOD_DEF_QUERY = """
(object
  (method_definition
    name: (property_identifier) @method_name) @method_function)
"""

# (H) JS object arrow function query
JS_OBJECT_ARROW_QUERY = """
(object
  (pair
    (property_identifier) @method_name
    (arrow_function) @arrow_function))
"""

# (H) JS assignment arrow function query
JS_ASSIGNMENT_ARROW_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (arrow_function) @arrow_function)
"""

# (H) JS assignment function expression query
JS_ASSIGNMENT_FUNCTION_QUERY = """
(assignment_expression
  (member_expression) @member_expr
  (function_expression) @function_expr)
"""

# (H) JS/TS module system node types
TS_OBJECT_PATTERN = "object_pattern"
TS_SHORTHAND_PROPERTY_IDENTIFIER_PATTERN = "shorthand_property_identifier_pattern"
TS_PAIR_PATTERN = "pair_pattern"
TS_FUNCTION_DECLARATION = "function_declaration"
TS_GENERATOR_FUNCTION_DECLARATION = "generator_function_declaration"

# (H) JS/TS module system keywords
JS_REQUIRE_KEYWORD = "require"
JS_EXPORTS_KEYWORD = "exports"
JS_MODULE_KEYWORD = "module"

# (H) JS/TS export type descriptions
JS_EXPORT_TYPE_COMMONJS = "CommonJS Export"
JS_EXPORT_TYPE_COMMONJS_MODULE = "CommonJS Module Export"
JS_EXPORT_TYPE_ES6_FUNCTION = "ES6 Export Function"
JS_EXPORT_TYPE_ES6_FUNCTION_DECL = "ES6 Export Function Declaration"

# (H) JS/TS CommonJS destructure query
JS_COMMONJS_DESTRUCTURE_QUERY = """
(lexical_declaration
  (variable_declarator
    name: (object_pattern)
    value: (call_expression
      function: (identifier) @func (#eq? @func "require")
    )
  ) @variable_declarator
)
"""

# (H) JS/TS CommonJS exports function query
JS_COMMONJS_EXPORTS_FUNCTION_QUERY = """
(assignment_expression
  left: (member_expression
    object: (identifier) @exports_obj
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# (H) JS/TS CommonJS module.exports query
JS_COMMONJS_MODULE_EXPORTS_QUERY = """
(assignment_expression
  left: (member_expression
    object: (member_expression
      object: (identifier) @module_obj
      property: (property_identifier) @exports_prop)
    property: (property_identifier) @export_name)
  right: [(function_expression) (arrow_function)] @export_function)
"""

# (H) JS/TS ES6 export const query
JS_ES6_EXPORT_CONST_QUERY = """
(export_statement
  (lexical_declaration
    (variable_declarator
      name: (identifier) @export_name
      value: [(function_expression) (arrow_function)] @export_function)))
"""

# (H) JS/TS ES6 export function query
JS_ES6_EXPORT_FUNCTION_QUERY = """
(export_statement
  [(function_declaration) (generator_function_declaration)] @export_function)
"""

# (H) Tree-sitter Rust node types
TS_RS_SCOPED_TYPE_IDENTIFIER = "scoped_type_identifier"
TS_RS_PRIMITIVE_TYPE = "primitive_type"
TS_RS_USE_AS_CLAUSE = "use_as_clause"
TS_RS_USE_WILDCARD = "use_wildcard"
TS_RS_USE_LIST = "use_list"
TS_RS_SCOPED_USE_LIST = "scoped_use_list"
TS_RS_SOURCE_FILE = "source_file"
TS_RS_MOD_ITEM = "mod_item"
TS_RS_CRATE = "crate"
TS_RS_KEYWORD_AS = "as"
TS_RS_STRUCT_ITEM = "struct_item"
TS_RS_ENUM_ITEM = "enum_item"
TS_RS_TRAIT_ITEM = "trait_item"
TS_RS_TYPE_ITEM = "type_item"
TS_RS_FUNCTION_ITEM = "function_item"
TS_RS_IMPL_ITEM = "impl_item"
TS_RS_FUNCTION_SIGNATURE_ITEM = "function_signature_item"
TS_RS_CLOSURE_EXPRESSION = "closure_expression"
TS_RS_UNION_ITEM = "union_item"
TS_RS_USE_DECLARATION = "use_declaration"
TS_RS_EXTERN_CRATE_DECLARATION = "extern_crate_declaration"
TS_RS_CALL_EXPRESSION = "call_expression"
TS_RS_MACRO_INVOCATION = "macro_invocation"
TS_RS_ATTRIBUTE_ITEM = "attribute_item"
TS_RS_INNER_ATTRIBUTE_ITEM = "inner_attribute_item"

# (H) Rust node types for local-variable type inference (receiver-dispatch)
TS_RS_LET_DECLARATION = "let_declaration"
TS_RS_PARAMETER = "parameter"
TS_RS_SELF_PARAMETER = "self_parameter"
TS_RS_STRUCT_EXPRESSION = "struct_expression"
TS_RS_FIELD_DECLARATION_LIST = "field_declaration_list"
TS_RS_FIELD_DECLARATION = "field_declaration"
TS_RS_FIELD_IDENTIFIER = "field_identifier"
TS_RS_MATCH_EXPRESSION = "match_expression"
TS_RS_MATCH_ARM = "match_arm"
# (H) A Rust call node whose callee is descended for chain flattening: a plain call
# (H) or a turbofish generic_function (`f::<T>()`).
RS_CALL_OR_GENERIC_FN = (TS_RS_CALL_EXPRESSION, TS_GENERIC_FUNCTION)
TS_RS_TUPLE_STRUCT_PATTERN = "tuple_struct_pattern"
TS_RS_TYPE_ARGUMENTS = "type_arguments"
TS_RS_TRY_EXPRESSION = "try_expression"
TS_RS_FIELD_EXPRESSION = "field_expression"
TS_RS_FIELD_PATH = "path"
TS_RS_TOKEN_DOT = "."
# (H) Nodes that can be a receiver token preceding `.method` in a macro token
# (H) stream: a plain identifier or the `self` keyword.
# (H) A receiver/chain base that is a plain identifier or the `self` keyword (used
# (H) both for macro-token receiver reconstruction and value-chain base flattening).
RS_IDENT_OR_SELF = (TS_IDENTIFIER, KEYWORD_SELF)
RS_MACRO_RECEIVER_TYPES = RS_IDENT_OR_SELF
# (H) Rust `Self` return type resolves to the enclosing impl target.
RS_SELF_TYPE = "Self"
# (H) Transparent smart pointers that auto-deref (Rust deref coercion) to their
# (H) inner type: a method call on the pointer dispatches to the inner type's method,
# (H) so strip them from any type name (receiver OR return) to reach the real type.
RS_DEREF_WRAPPERS = frozenset({"Arc", "Rc", "Box", "Pin"})
# (H) Guard containers that do NOT deref-coerce: the inner value is only reachable
# (H) through a lock/borrow guard accessor. Stripped to the inner type ONLY in field
# (H) extraction (where the field is virtually always accessed via a lock chain, e.g.
# (H) `self.shared.state.lock().unwrap()`); a bare local/param/return of a guard type
# (H) is preserved so a direct wrapper-method call (`m.is_poisoned()`) is not
# (H) mis-resolved to an inner-type method.
RS_GUARD_WRAPPERS = frozenset({"Mutex", "RwLock", "RefCell", "Cell"})
# (H) Result<T>/Option<T>: stripped to their inner T only for a RETURN type (the
# (H) value a `?`/`.unwrap()` yields). NOT stripped for a receiver type, where a
# (H) method call `opt.map(..)` dispatches to Option itself.
RS_RESULT_WRAPPERS = frozenset({"Result", "Option"})
# (H) Full strip set for return types (deref pointers + Result/Option unwrap).
RS_RETURN_STRIP_WRAPPERS = RS_DEREF_WRAPPERS | RS_RESULT_WRAPPERS
TS_RS_REFERENCE_TYPE = "reference_type"
TS_RS_POINTER_TYPE = "pointer_type"
# (H) Node types that can stand for a Rust return/field type. Reference/pointer
# (H) wrappers (`&Frame`, `*const T`) are included so a generic inner argument
# (H) (`Result<&Frame>`) and a bare `-> &Frame` return descend to the referent.
RS_RETURN_TYPE_NODE_TYPES = (
    TS_TYPE_IDENTIFIER,
    TS_RS_PRIMITIVE_TYPE,
    TS_GENERIC_TYPE,
    TS_RS_SCOPED_TYPE_IDENTIFIER,
    TS_RS_REFERENCE_TYPE,
    TS_RS_POINTER_TYPE,
)
# (H) Wrapper-passthrough methods: they return the receiver's own (inner) type, so
# (H) a call-bound local keeps its type across them (`Type::mk().unwrap().m()`).
RS_IDENTITY_METHODS = frozenset(
    {
        "unwrap",
        "expect",
        "clone",
        "unwrap_or_default",
        "to_owned",
        "borrow",
        "borrow_mut",
        "as_ref",
        "as_mut",
        "as_deref",
        "as_deref_mut",
    }
)
# (H) Guard accessors: called on a guard container (Mutex/RwLock/RefCell) to obtain a
# (H) guard that derefs to the inner type. In a receiver chain, one of these
# (H) immediately after a guard-wrapped field unwraps the wrapper to its inner type
# (H) (recorded in class_field_guard_inner) -- the only sound unwrap point, since
# (H) guard containers do not deref-coerce.
RS_GUARD_ACCESSORS = frozenset(
    {"lock", "read", "write", "try_lock", "borrow", "borrow_mut"}
)

# (H) Rust identifier tuples
RS_IDENTIFIER_TYPES = (TS_IDENTIFIER, TS_TYPE_IDENTIFIER)
RS_SCOPED_TYPES = (TS_SCOPED_IDENTIFIER, TS_RS_SCOPED_TYPE_IDENTIFIER)
RS_PATH_KEYWORDS = (TS_RS_CRATE, KEYWORD_SUPER, KEYWORD_SELF)

# (H) Delimiter tokens for Rust use lists
RS_USE_LIST_DELIMITERS = frozenset({"{", "}", ","})

# (H) Rust encoding
RS_ENCODING_UTF8 = "utf8"

# (H) Rust wildcard prefix
RS_WILDCARD_PREFIX = "*"

# (H) Rust field names
RS_FIELD_ARGUMENT = "argument"


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

# (H) TS-specific node types
TS_FUNCTION_SIGNATURE = "function_signature"

# (H) FQN node type tuples for Python
FQN_PY_SCOPE_TYPES = (
    TS_PY_CLASS_DEFINITION,
    TS_PY_MODULE,
    TS_PY_FUNCTION_DEFINITION,
)
FQN_PY_FUNCTION_TYPES = (TS_PY_FUNCTION_DEFINITION,)

# (H) FQN node type tuples for JS
FQN_JS_SCOPE_TYPES = (
    TS_CLASS_DECLARATION,
    TS_PROGRAM,
    TS_FUNCTION_DECLARATION,
    TS_FUNCTION_EXPRESSION,
    TS_ARROW_FUNCTION,
    TS_METHOD_DEFINITION,
)
FQN_JS_FUNCTION_TYPES = (
    TS_FUNCTION_DECLARATION,
    TS_METHOD_DEFINITION,
    TS_ARROW_FUNCTION,
    TS_FUNCTION_EXPRESSION,
)

# (H) FQN node type tuples for TS. The grammar emits `internal_module` for a
# (H) `namespace`/`module` block; without it a class declared inside a namespace
# (H) loses the namespace from its qn and collides with a top-level same name.
FQN_TS_SCOPE_TYPES = (
    TS_CLASS_DECLARATION,
    TS_INTERFACE_DECLARATION,
    TS_NAMESPACE_DEFINITION,
    TS_INTERNAL_MODULE,
    TS_PROGRAM,
    TS_FUNCTION_DECLARATION,
    TS_FUNCTION_EXPRESSION,
    TS_ARROW_FUNCTION,
    TS_METHOD_DEFINITION,
)
FQN_TS_FUNCTION_TYPES = (
    TS_FUNCTION_DECLARATION,
    TS_METHOD_DEFINITION,
    TS_ARROW_FUNCTION,
    TS_FUNCTION_EXPRESSION,
    TS_FUNCTION_SIGNATURE,
)

# (H) When climbing a nameless arrow's ancestors to find its binding declarator,
# (H) crossing one of these means the arrow lives INSIDE another function's body
# (H) (a JSX event handler, a `.map()` callback), not directly bound to the outer
# (H) const -- so it must not inherit that const's name. Without this stop the inner
# (H) arrow becomes `Component.Component`/`utils.getInitials.getInitials`, a
# (H) double-segment phantom with no incoming edge (dead-code false positive) that
# (H) also steals the enclosing scope from the real inline handler nodes.
JS_ARROW_NAME_CLIMB_BOUNDARY = frozenset(
    {
        TS_STATEMENT_BLOCK,
        TS_ARROW_FUNCTION,
        TS_FUNCTION_DECLARATION,
        TS_FUNCTION_EXPRESSION,
        TS_METHOD_DEFINITION,
        TS_GENERATOR_FUNCTION_DECLARATION,
        TS_CLASS_BODY,
    }
)

# (H) FQN node type tuples for Rust
FQN_RS_SCOPE_TYPES = (
    TS_RS_STRUCT_ITEM,
    TS_RS_ENUM_ITEM,
    TS_RS_TRAIT_ITEM,
    TS_RS_IMPL_ITEM,
    TS_RS_MOD_ITEM,
    TS_RS_SOURCE_FILE,
)
FQN_RS_FUNCTION_TYPES = (
    TS_RS_FUNCTION_ITEM,
    TS_RS_FUNCTION_SIGNATURE_ITEM,
    TS_RS_CLOSURE_EXPRESSION,
)

# (H) FQN node type tuples for Java
FQN_JAVA_SCOPE_TYPES = (
    TS_CLASS_DECLARATION,
    TS_INTERFACE_DECLARATION,
    TS_ENUM_DECLARATION,
    TS_PROGRAM,
)
FQN_JAVA_FUNCTION_TYPES = (
    TS_METHOD_DECLARATION,
    TS_CONSTRUCTOR_DECLARATION,
)

# (H) FQN node type tuples for C++
FQN_CPP_SCOPE_TYPES = (
    CppNodeType.CLASS_SPECIFIER,
    TS_STRUCT_SPECIFIER,
    TS_NAMESPACE_DEFINITION,
    TS_CPP_TRANSLATION_UNIT,
)
FQN_CPP_FUNCTION_TYPES = (
    TS_CPP_FUNCTION_DEFINITION,
    TS_CPP_DECLARATION,
    TS_CPP_FIELD_DECLARATION,
    TS_CPP_TEMPLATE_DECLARATION,
    TS_CPP_LAMBDA_EXPRESSION,
)

# (H) FQN node type tuples for Lua
FQN_LUA_SCOPE_TYPES = (TS_LUA_CHUNK,)
FQN_LUA_FUNCTION_TYPES = (
    TS_LUA_FUNCTION_DECLARATION,
    TS_LUA_FUNCTION_DEFINITION,
)

# (H) FQN node type tuples for Go
FQN_GO_SCOPE_TYPES = (
    TS_GO_TYPE_DECLARATION,
    TS_GO_SOURCE_FILE,
)
FQN_GO_FUNCTION_TYPES = (
    TS_GO_FUNCTION_DECLARATION,
    TS_GO_METHOD_DECLARATION,
)

# (H) FQN node type tuples for Scala
FQN_SCALA_SCOPE_TYPES = (
    TS_SCALA_CLASS_DEFINITION,
    TS_SCALA_OBJECT_DEFINITION,
    TS_SCALA_TRAIT_DEFINITION,
    TS_SCALA_COMPILATION_UNIT,
)
FQN_SCALA_FUNCTION_TYPES = (
    TS_SCALA_FUNCTION_DEFINITION,
    TS_SCALA_FUNCTION_DECLARATION,
)

# (H) FQN node type tuples for PHP
FQN_PHP_SCOPE_TYPES = (
    TS_CLASS_DECLARATION,
    TS_INTERFACE_DECLARATION,
    TS_PHP_TRAIT_DECLARATION,
    TS_PHP_NAMESPACE_DEFINITION,
    TS_PROGRAM,
)
FQN_PHP_FUNCTION_TYPES = (
    TS_PHP_FUNCTION_DEFINITION,
    TS_PHP_METHOD_DECLARATION,
    TS_PHP_ANONYMOUS_FUNCTION,
    TS_PHP_ARROW_FUNCTION,
)

# (H) LANGUAGE_SPECS node type tuples for Python
SPEC_PY_FUNCTION_TYPES = (TS_PY_FUNCTION_DEFINITION,)
SPEC_PY_CLASS_TYPES = (TS_PY_CLASS_DEFINITION,)
SPEC_PY_MODULE_TYPES = (TS_PY_MODULE,)
SPEC_PY_CALL_TYPES = (TS_PY_CALL, TS_PY_WITH_STATEMENT)
SPEC_PY_IMPORT_TYPES = (TS_PY_IMPORT_STATEMENT,)
SPEC_PY_IMPORT_FROM_TYPES = (TS_PY_IMPORT_FROM_STATEMENT,)
SPEC_PY_PACKAGE_INDICATORS = (PKG_INIT_PY,)

# (H) LANGUAGE_SPECS node type tuples for JS/TS
SPEC_JS_MODULE_TYPES = (TS_PROGRAM,)
SPEC_JS_CALL_TYPES = (TS_CALL_EXPRESSION,)

# (H) Derived node types for _js_get_name
JS_NAME_NODE_TYPES = (
    TS_FUNCTION_DECLARATION,
    TS_CLASS_DECLARATION,
    TS_METHOD_DEFINITION,
    # (H) TS `namespace`/`module` block; its `name` field scopes nested classes.
    TS_INTERNAL_MODULE,
)

# (H) Derived node types for _rust_get_name
RS_TYPE_NODE_TYPES = (
    TS_RS_STRUCT_ITEM,
    TS_RS_ENUM_ITEM,
    TS_RS_TRAIT_ITEM,
    TS_RS_TYPE_ITEM,
)
RS_IDENT_NODE_TYPES = (TS_RS_FUNCTION_ITEM, TS_RS_MOD_ITEM)

# (H) Derived node types for _cpp_get_name
CPP_NAME_NODE_TYPES = (
    CppNodeType.CLASS_SPECIFIER,
    TS_STRUCT_SPECIFIER,
    TS_ENUM_SPECIFIER,
)

# (H) Derived node types for _c_get_name
C_NAME_NODE_TYPES = (
    TS_STRUCT_SPECIFIER,
    TS_UNION_SPECIFIER,
    TS_ENUM_SPECIFIER,
)

# (H) LANGUAGE_SPECS node type tuples for Rust
SPEC_RS_FUNCTION_TYPES = (
    TS_RS_FUNCTION_ITEM,
    TS_RS_FUNCTION_SIGNATURE_ITEM,
    TS_RS_CLOSURE_EXPRESSION,
)
SPEC_RS_CLASS_TYPES = (
    TS_RS_STRUCT_ITEM,
    TS_RS_ENUM_ITEM,
    TS_RS_UNION_ITEM,
    TS_RS_TRAIT_ITEM,
    TS_RS_IMPL_ITEM,
    TS_RS_TYPE_ITEM,
)
SPEC_RS_MODULE_TYPES = (TS_RS_SOURCE_FILE, TS_RS_MOD_ITEM)
SPEC_RS_CALL_TYPES = (TS_RS_CALL_EXPRESSION, TS_RS_MACRO_INVOCATION)
SPEC_RS_IMPORT_TYPES = (TS_RS_USE_DECLARATION, TS_RS_EXTERN_CRATE_DECLARATION)
SPEC_RS_IMPORT_FROM_TYPES = (TS_RS_USE_DECLARATION,)
SPEC_RS_PACKAGE_INDICATORS = (PKG_CARGO_TOML,)

# (H) LANGUAGE_SPECS node type tuples for Go
SPEC_GO_FUNCTION_TYPES = (TS_GO_FUNCTION_DECLARATION, TS_GO_METHOD_DECLARATION)
SPEC_GO_CLASS_TYPES = (TS_GO_TYPE_SPEC, TS_GO_TYPE_ALIAS)
SPEC_GO_MODULE_TYPES = (TS_GO_SOURCE_FILE,)
SPEC_GO_CALL_TYPES = (TS_GO_CALL_EXPRESSION,)
SPEC_GO_IMPORT_TYPES = (TS_GO_IMPORT_DECLARATION,)

# (H) LANGUAGE_SPECS node type tuples for Scala
SPEC_SCALA_FUNCTION_TYPES = (
    TS_SCALA_FUNCTION_DEFINITION,
    TS_SCALA_FUNCTION_DECLARATION,
)
SPEC_SCALA_CLASS_TYPES = (
    TS_SCALA_CLASS_DEFINITION,
    TS_SCALA_OBJECT_DEFINITION,
    TS_SCALA_TRAIT_DEFINITION,
)
SPEC_SCALA_MODULE_TYPES = (TS_SCALA_COMPILATION_UNIT,)
SPEC_SCALA_CALL_TYPES = (
    TS_SCALA_CALL_EXPRESSION,
    TS_SCALA_GENERIC_FUNCTION,
    TS_SCALA_FIELD_EXPRESSION,
    TS_SCALA_INFIX_EXPRESSION,
)
SPEC_SCALA_IMPORT_TYPES = (TS_SCALA_IMPORT_DECLARATION,)

# (H) LANGUAGE_SPECS node type tuples for Java
SPEC_JAVA_FUNCTION_TYPES = (TS_METHOD_DECLARATION, TS_CONSTRUCTOR_DECLARATION)
SPEC_JAVA_CLASS_TYPES = (
    TS_CLASS_DECLARATION,
    TS_INTERFACE_DECLARATION,
    TS_ENUM_DECLARATION,
    TS_JAVA_ANNOTATION_TYPE_DECLARATION,
    TS_RECORD_DECLARATION,
)
SPEC_JAVA_MODULE_TYPES = (TS_PROGRAM,)
SPEC_JAVA_CALL_TYPES = (TS_JAVA_METHOD_INVOCATION,)
SPEC_JAVA_IMPORT_TYPES = (TS_IMPORT_DECLARATION,)

# (H) LANGUAGE_SPECS node type tuples for C++
SPEC_CPP_FUNCTION_TYPES = (
    TS_CPP_FUNCTION_DEFINITION,
    TS_CPP_DECLARATION,
    TS_CPP_FIELD_DECLARATION,
    TS_CPP_TEMPLATE_DECLARATION,
    TS_CPP_LAMBDA_EXPRESSION,
)
SPEC_CPP_CLASS_TYPES = (
    CppNodeType.CLASS_SPECIFIER,
    TS_STRUCT_SPECIFIER,
    TS_UNION_SPECIFIER,
    TS_ENUM_SPECIFIER,
)
SPEC_CPP_MODULE_TYPES = (
    TS_CPP_TRANSLATION_UNIT,
    TS_NAMESPACE_DEFINITION,
    TS_CPP_LINKAGE_SPECIFICATION,
    TS_CPP_DECLARATION,
)
SPEC_CPP_CALL_TYPES = (
    TS_CPP_CALL_EXPRESSION,
    TS_CPP_FIELD_EXPRESSION,
    TS_CPP_SUBSCRIPT_EXPRESSION,
    TS_CPP_NEW_EXPRESSION,
    TS_CPP_DELETE_EXPRESSION,
    TS_CPP_BINARY_EXPRESSION,
    TS_CPP_UNARY_EXPRESSION,
    TS_CPP_UPDATE_EXPRESSION,
)
SPEC_CPP_PACKAGE_INDICATORS = (
    PKG_CMAKE_LISTS,
    PKG_MAKEFILE,
    PKG_VCXPROJ_GLOB,
    PKG_CONANFILE,
)

# (H) FQN node type tuples for C
FQN_C_SCOPE_TYPES = (
    TS_CPP_TRANSLATION_UNIT,
    TS_STRUCT_SPECIFIER,
    TS_UNION_SPECIFIER,
    TS_ENUM_SPECIFIER,
)
FQN_C_FUNCTION_TYPES = (TS_CPP_FUNCTION_DEFINITION,)

# (H) LANGUAGE_SPECS node type tuples for C
SPEC_C_FUNCTION_TYPES = (TS_CPP_FUNCTION_DEFINITION,)
SPEC_C_CLASS_TYPES = (
    TS_STRUCT_SPECIFIER,
    TS_UNION_SPECIFIER,
    TS_ENUM_SPECIFIER,
)
SPEC_C_MODULE_TYPES = (TS_CPP_TRANSLATION_UNIT,)
SPEC_C_CALL_TYPES = (TS_CPP_CALL_EXPRESSION,)
SPEC_C_PACKAGE_INDICATORS = (PKG_CMAKE_LISTS, PKG_MAKEFILE)

# (H) LANGUAGE_SPECS node type tuples for PHP
SPEC_PHP_FUNCTION_TYPES = (
    TS_PHP_FUNCTION_DEFINITION,
    TS_PHP_METHOD_DECLARATION,
    TS_PHP_ANONYMOUS_FUNCTION,
    TS_PHP_ARROW_FUNCTION,
)
SPEC_PHP_CLASS_TYPES = (
    TS_CLASS_DECLARATION,
    TS_INTERFACE_DECLARATION,
    TS_PHP_TRAIT_DECLARATION,
    TS_ENUM_DECLARATION,
)
SPEC_PHP_MODULE_TYPES = (TS_PROGRAM,)
SPEC_PHP_CALL_TYPES = (
    TS_PHP_FUNCTION_CALL_EXPRESSION,
    TS_PHP_MEMBER_CALL_EXPRESSION,
    TS_PHP_SCOPED_CALL_EXPRESSION,
    TS_PHP_NULLSAFE_MEMBER_CALL_EXPRESSION,
    TS_PHP_OBJECT_CREATION_EXPRESSION,
)
SPEC_PHP_IMPORT_TYPES = (TS_PHP_NAMESPACE_USE_DECLARATION,)
SPEC_PHP_IMPORT_FROM_TYPES = (
    TS_PHP_INCLUDE_EXPRESSION,
    TS_PHP_INCLUDE_ONCE_EXPRESSION,
    TS_PHP_REQUIRE_EXPRESSION,
    TS_PHP_REQUIRE_ONCE_EXPRESSION,
)

# (H) LANGUAGE_SPECS node type tuples for Lua
SPEC_LUA_FUNCTION_TYPES = (TS_LUA_FUNCTION_DECLARATION, TS_LUA_FUNCTION_DEFINITION)
SPEC_LUA_CLASS_TYPES: tuple[str, ...] = ()
SPEC_LUA_MODULE_TYPES = (TS_LUA_CHUNK,)
SPEC_LUA_CALL_TYPES = (TS_LUA_FUNCTION_CALL,)
SPEC_LUA_IMPORT_TYPES = (TS_LUA_FUNCTION_CALL,)

HEALTH_CHECK_DOCKER_RUNNING = "Docker daemon is running"
HEALTH_CHECK_DOCKER_NOT_RUNNING = "Docker daemon is not running"
HEALTH_CHECK_DOCKER_RUNNING_MSG = "Running (version {version})"
HEALTH_CHECK_DOCKER_NOT_RESPONDING_MSG = "Not responding"
HEALTH_CHECK_DOCKER_NOT_INSTALLED_MSG = "Not installed"
HEALTH_CHECK_DOCKER_NOT_IN_PATH = "docker command not found in PATH"
HEALTH_CHECK_DOCKER_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_DOCKER_TIMEOUT_ERROR = (
    "The 'docker info' command took more than 5 seconds to respond."
)
HEALTH_CHECK_DOCKER_FAILED_MSG = "Check failed"
HEALTH_CHECK_DOCKER_EXIT_CODE = "Non-zero exit code"

HEALTH_CHECK_MEMGRAPH_SUCCESSFUL = "Memgraph connection successful"
HEALTH_CHECK_MEMGRAPH_FAILED = "Memgraph connection failed"
HEALTH_CHECK_MEMGRAPH_CONNECTED_MSG = "Connected and responsive at {host}:{port}"
HEALTH_CHECK_MEMGRAPH_CONNECTION_FAILED_MSG = "Connection or query failed"
HEALTH_CHECK_MEMGRAPH_UNEXPECTED_FAILURE_MSG = "Unexpected failure"
HEALTH_CHECK_MEMGRAPH_ERROR = "Memgraph error: {error}"
HEALTH_CHECK_MEMGRAPH_QUERY = "RETURN 1 AS test;"

HEALTH_CHECK_API_KEY_SET = "{display_name} API key is set"
HEALTH_CHECK_API_KEY_NOT_SET = "{display_name} API key is not set"
HEALTH_CHECK_API_KEY_CONFIGURED = "Configured"
HEALTH_CHECK_API_KEY_NOT_CONFIGURED = "Not set"
HEALTH_CHECK_API_KEY_MISSING_MSG = (
    "Set the {env_name} environment variable or configure it in your settings."
)

HEALTH_CHECK_TOOL_INSTALLED = "{tool_name} is installed"
HEALTH_CHECK_TOOL_NOT_INSTALLED = "{tool_name} is not installed"
HEALTH_CHECK_TOOL_INSTALLED_MSG = "Installed ({path})"
HEALTH_CHECK_TOOL_NOT_IN_PATH_MSG = "'{cmd}' not found in PATH"
HEALTH_CHECK_TOOL_TIMEOUT_MSG = "Check timed out"
HEALTH_CHECK_TOOL_TIMEOUT_ERROR = (
    "The command to find '{cmd}' took more than 4 seconds to respond."
)
HEALTH_CHECK_TOOL_FAILED_MSG = "Check failed"

HEALTH_CHECK_TOOLS = [
    ("GEMINI_API_KEY", "Gemini"),
    ("OPENAI_API_KEY", "OpenAI"),
    ("ORCHESTRATOR_API_KEY", "Orchestrator"),
    ("CYPHER_API_KEY", "Cypher"),
]

HEALTH_CHECK_EXTERNAL_TOOLS = [
    ("ripgrep", "rg"),
    ("cmake", "cmake"),
]
