# (H) Supported languages, file extensions, metadata, and grammar modules.

from enum import StrEnum
from typing import NamedTuple

BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".tiff",
        ".webp",
    }
)

# (H) Source file extensions by language
EXT_PY = ".py"
EXT_JS = ".js"
EXT_JSX = ".jsx"
EXT_TS = ".ts"
EXT_TSX = ".tsx"
EXT_RS = ".rs"
EXT_GO = ".go"
EXT_SCALA = ".scala"
EXT_SC = ".sc"
EXT_JAVA = ".java"
EXT_CLASS = ".class"
EXT_CPP = ".cpp"
EXT_H = ".h"
EXT_HPP = ".hpp"
EXT_CC = ".cc"
EXT_CXX = ".cxx"
EXT_HXX = ".hxx"
EXT_HH = ".hh"
EXT_IXX = ".ixx"
EXT_CPPM = ".cppm"
EXT_CCM = ".ccm"
EXT_C = ".c"
EXT_PHP = ".php"
EXT_LUA = ".lua"

# (H) File extension tuples by language
PY_EXTENSIONS = (EXT_PY,)
JS_EXTENSIONS = (EXT_JS, EXT_JSX)
TS_EXTENSIONS = (EXT_TS,)
TSX_EXTENSIONS = (EXT_TSX,)
JS_TS_ALL_EXTENSIONS = (EXT_JS, EXT_JSX, EXT_TS, EXT_TSX)
RS_EXTENSIONS = (EXT_RS,)
GO_EXTENSIONS = (EXT_GO,)
SCALA_EXTENSIONS = (EXT_SCALA, EXT_SC)
JAVA_EXTENSIONS = (EXT_JAVA,)
C_EXTENSIONS = (EXT_C,)
CPP_EXTENSIONS = (
    EXT_CPP,
    EXT_H,
    EXT_HPP,
    EXT_CC,
    EXT_CXX,
    EXT_HXX,
    EXT_HH,
    EXT_IXX,
    EXT_CPPM,
    EXT_CCM,
)
PHP_EXTENSIONS = (EXT_PHP,)
LUA_EXTENSIONS = (EXT_LUA,)

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


# (H) JS/TS import specifier schemes that name genuinely external code (node
# (H) builtins, package registries, URLs). A specifier with any OTHER scheme
# (H) (`ext:` deno-runtime aliases, bundler virtual modules) points at first-party
# (H) code under a non-file-path name, so its unresolved calls defer to the trie.
JS_EXTERNAL_IMPORT_SCHEMES: frozenset[str] = frozenset(
    {"node", "npm", "jsr", "bun", "http", "https", "data", "file", "blob"}
)
# (H) Module file extensions stripped when turning a tsconfig `paths` target into a
# (H) module qn (`src/util.ts` -> `src/util`), longest first so `.d.ts`-like
# (H) compound suffixes are handled before the bare `.ts`.
JS_TS_MODULE_EXTENSIONS: tuple[str, ...] = (
    ".d.ts",
    ".tsx",
    ".ts",
    ".jsx",
    ".mjs",
    ".cjs",
    ".js",
)
TSCONFIG_FILENAMES: tuple[str, ...] = (
    "tsconfig.json",
    "tsconfig.base.json",
    "jsconfig.json",
)
# (H) When searching subdirectories for tsconfig files (monorepo `frontend/`,
# (H) `packages/*`), skip dependency/build/VCS trees: their tsconfigs carry
# (H) unrelated aliases and there can be thousands of them under node_modules.
TS_ALIAS_SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", "dist", "build", "out", ".git"}
)
JS_INDEX_STEM = "index"
TS_COMPILER_OPTIONS_KEY = "compilerOptions"
TS_PATHS_KEY = "paths"
TS_BASE_URL_KEY = "baseUrl"
PATH_RELATIVE_PREFIX = "./"
PATH_PARENT_PREFIX = "../"
CPP_IMPORT_PARTITION_PREFIX = "import :"
CPP_PARTITION_PREFIX = "partition_"


class SupportedLanguage(StrEnum):
    PYTHON = "python"
    JS = "javascript"
    TS = "typescript"
    TSX = "tsx"
    RUST = "rust"
    GO = "go"
    SCALA = "scala"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    PHP = "php"
    LUA = "lua"


class LanguageStatus(StrEnum):
    FULL = "Fully Supported"
    DEV = "In Development"


class LanguageMetadata(NamedTuple):
    status: LanguageStatus
    additional_features: str
    display_name: str


LANGUAGE_METADATA: dict[SupportedLanguage, LanguageMetadata] = {
    SupportedLanguage.PYTHON: LanguageMetadata(
        LanguageStatus.FULL,
        "Type inference, decorators, nested functions",
        "Python",
    ),
    SupportedLanguage.JS: LanguageMetadata(
        LanguageStatus.FULL,
        "ES6 modules, CommonJS, prototype methods, object methods, arrow functions",
        "JavaScript",
    ),
    SupportedLanguage.TS: LanguageMetadata(
        LanguageStatus.FULL,
        "Interfaces, type aliases, enums, namespaces, ES6/CommonJS modules",
        "TypeScript",
    ),
    SupportedLanguage.TSX: LanguageMetadata(
        LanguageStatus.FULL,
        "All TypeScript features plus JSX elements and components",
        "TypeScript (TSX)",
    ),
    SupportedLanguage.C: LanguageMetadata(
        LanguageStatus.FULL,
        "Functions, structs, unions, enums, preprocessor includes",
        "C",
    ),
    SupportedLanguage.CPP: LanguageMetadata(
        LanguageStatus.FULL,
        "Constructors, destructors, operator overloading, templates, lambdas, C++20 modules, namespaces, preprocessor macros",
        "C++",
    ),
    SupportedLanguage.LUA: LanguageMetadata(
        LanguageStatus.FULL,
        "Local/global functions, metatables, closures, coroutines",
        "Lua",
    ),
    SupportedLanguage.RUST: LanguageMetadata(
        LanguageStatus.FULL,
        "impl blocks, associated functions, macro_rules! macros",
        "Rust",
    ),
    SupportedLanguage.JAVA: LanguageMetadata(
        LanguageStatus.FULL,
        "Generics, annotations, modern features (records/sealed classes), concurrency, reflection",
        "Java",
    ),
    SupportedLanguage.GO: LanguageMetadata(
        LanguageStatus.FULL,
        "Receiver methods with cross-file binding, structs, interfaces, type declarations, function-local types",
        "Go",
    ),
    SupportedLanguage.SCALA: LanguageMetadata(
        LanguageStatus.DEV,
        "Case classes, objects",
        "Scala",
    ),
    SupportedLanguage.PHP: LanguageMetadata(
        LanguageStatus.FULL,
        "Classes, interfaces, traits, enums, namespaces, PHP 8 attributes",
        "PHP",
    ),
}

# (H) Index file names
INDEX_INIT = "__init__"
INDEX_INDEX = "index"
INDEX_MOD = "mod"
INDEX_LUA_INIT = "init"

# (H) File stems whose module is importable through the CONTAINING directory's
# (H) name: pkg/__init__.py, shared/index.js, utils/mod.rs, storage/init.lua.
MODULE_INDEX_FILE_STEMS = frozenset(
    {INDEX_INIT, INDEX_INDEX, INDEX_MOD, INDEX_LUA_INIT}
)

# (H) Parser loader paths and args
GRAMMARS_DIR = "grammars"
TREE_SITTER_PREFIX = "tree-sitter-"
TREE_SITTER_MODULE_PREFIX = "tree_sitter_"
BINDINGS_DIR = "bindings"
SETUP_PY = "setup.py"
BUILD_EXT_CMD = "build_ext"
INPLACE_FLAG = "--inplace"
LANG_ATTR_PREFIX = "language_"
LANG_ATTR_TYPESCRIPT = "language_typescript"
LANG_ATTR_TSX = "language_tsx"
LANG_ATTR_PHP = "language_php"


class TreeSitterModule(StrEnum):
    PYTHON = "tree_sitter_python"
    JS = "tree_sitter_javascript"
    TS = "tree_sitter_typescript"
    RUST = "tree_sitter_rust"
    GO = "tree_sitter_go"
    SCALA = "tree_sitter_scala"
    JAVA = "tree_sitter_java"
    C = "tree_sitter_c"
    CPP = "tree_sitter_cpp"
    LUA = "tree_sitter_lua"
    PHP = "tree_sitter_php"


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
