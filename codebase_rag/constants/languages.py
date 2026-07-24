# Supported languages, file extensions, metadata, and grammar modules.

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

# Source file extensions by language
EXT_PY = ".py"
EXT_JS = ".js"
EXT_JSX = ".jsx"
EXT_MJS = ".mjs"
EXT_CJS = ".cjs"
EXT_TS = ".ts"
EXT_MTS = ".mts"
EXT_CTS = ".cts"
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
EXT_CS = ".cs"
EXT_DART = ".dart"

# File extension tuples by language
PY_EXTENSIONS = (EXT_PY,)
JS_EXTENSIONS = (EXT_JS, EXT_JSX, EXT_MJS, EXT_CJS)
TS_EXTENSIONS = (EXT_TS, EXT_MTS, EXT_CTS)
TSX_EXTENSIONS = (EXT_TSX,)
JS_TS_ALL_EXTENSIONS = (
    EXT_JS,
    EXT_JSX,
    EXT_MJS,
    EXT_CJS,
    EXT_TS,
    EXT_MTS,
    EXT_CTS,
    EXT_TSX,
)
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
# Translation-unit sources: only these can define a linkable OS entry point;
# headers and C++ module interface files never are the entry unit.
C_CPP_SOURCE_EXTENSIONS = (EXT_C, EXT_CPP, EXT_CC, EXT_CXX)
PHP_EXTENSIONS = (EXT_PHP,)
LUA_EXTENSIONS = (EXT_LUA,)
CS_EXTENSIONS = (EXT_CS,)
DART_EXTENSIONS = (EXT_DART,)

# Package indicator files
PKG_INIT_PY = "__init__.py"
PKG_CARGO_TOML = "Cargo.toml"
PKG_CMAKE_LISTS = "CMakeLists.txt"
PKG_MAKEFILE = "Makefile"
PKG_VCXPROJ_GLOB = "*.vcxproj"
PKG_CONANFILE = "conanfile.txt"
PKG_PUBSPEC_YAML = "pubspec.yaml"


class CppFrontend(StrEnum):
    TREESITTER = "treesitter"
    LIBCLANG = "libclang"
    HYBRID = "hybrid"


class CSharpFrontend(StrEnum):
    # AUTO resolves at run time: HYBRID where a dotnet toolchain is on PATH,
    # TREESITTER otherwise (resolve_csharp_frontend). The parser fingerprint
    # records the RESOLVED mode, so dotnet and non-dotnet graphs never share
    # an identity.
    AUTO = "auto"
    TREESITTER = "treesitter"
    ROSLYN = "roslyn"
    HYBRID = "hybrid"


# JS/TS import specifier schemes naming genuinely external code (node
# builtins, registries, URLs). Any OTHER scheme (`ext:` deno aliases,
# bundler virtual modules) points at first-party code under a non-file-path
# name, so its unresolved calls defer to the trie.
JS_EXTERNAL_IMPORT_SCHEMES: frozenset[str] = frozenset(
    {"node", "npm", "jsr", "bun", "http", "https", "data", "file", "blob"}
)
# Module extensions stripped when turning a tsconfig `paths` target into a
# module qn (`src/util.ts` -> `src/util`), longest first so `.d.ts`-like
# suffixes match before the bare `.ts`.
JS_TS_MODULE_EXTENSIONS: tuple[str, ...] = (
    ".d.mts",
    ".d.cts",
    ".d.ts",
    ".tsx",
    ".mts",
    ".cts",
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
# When searching subdirectories for tsconfig files (monorepo `frontend/`,
# `packages/*`), skip dependency/build/VCS trees: their tsconfigs carry
# unrelated aliases and node_modules holds thousands of them.
TS_ALIAS_SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", "dist", "build", "out", ".git"}
)
JS_INDEX_STEM = "index"
# package.json fields read when mapping a workspace package name to the source
# file a specifier names (issue #945). `exports` is the modern declaration and
# may nest condition objects and `*` patterns; the entry keys are the legacy
# root-only fallbacks.
JS_PACKAGE_NAME_KEY = "name"
JS_PACKAGE_EXPORTS_KEY = "exports"
JS_PACKAGE_ENTRY_KEYS: tuple[str, ...] = ("main", "module", "types")
JS_EXPORTS_WILDCARD = "*"
# Order the conditions of an `exports` entry are tried in. The graph holds
# sources, so these usually lead back to one file; when they lead to
# different ones the first wins, and modern first-party code is ESM.
# Conditions an ESM import may select, in order. `require` is absent on
# purpose: the two module systems can name different modules, so a request
# never selects the other system's. `types` is absent too: it names the
# declaration module, which no runtime selects and which therefore is not
# what a call graph follows.
JS_EXPORT_CONDITION_ORDER: tuple[str, ...] = ("import", "module", "default")
# The same map read from the CommonJS side.
JS_REQUIRE_CONDITION_ORDER: tuple[str, ...] = ("require", "default")
# Every condition with a defined meaning here, so an unrecognised key (a
# runtime or bundler condition) is told apart from one deliberately skipped.
JS_KNOWN_EXPORT_CONDITIONS: frozenset[str] = frozenset(
    {"types", "import", "module", "default", "require"}
)
# A manifest points at the PUBLISHED build, which is never indexed; dropping
# one of these leading directories reaches the source it was built from
# (`./dist/src/a.js` -> `src/a.ts`).
JS_BUILD_OUTPUT_DIRS: frozenset[str] = frozenset({"dist", "build", "out", "lib"})
JS_SOURCE_DIR = "src"
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
    CSHARP = "c_sharp"
    DART = "dart"


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
    SupportedLanguage.CSHARP: LanguageMetadata(
        LanguageStatus.FULL,
        "Namespaces (block and file-scoped), classes/structs/records/interfaces/enums, generics, inheritance/interfaces/overrides, typed call resolution with overloads, using directives",
        "C#",
    ),
    SupportedLanguage.DART: LanguageMetadata(
        LanguageStatus.FULL,
        "Classes, mixins, extensions, enhanced enums, factory/named constructors, Flutter widgets, package/relative/dart: imports, part directives, pubspec dependencies",
        "Dart",
    ),
}

# Index file names
INDEX_INIT = "__init__"
INDEX_INDEX = "index"
INDEX_MOD = "mod"
INDEX_LUA_INIT = "init"

# File stems whose module is importable through the CONTAINING directory's
# name: pkg/__init__.py, shared/index.js, utils/mod.rs, storage/init.lua.
MODULE_INDEX_FILE_STEMS = frozenset(
    {INDEX_INIT, INDEX_INDEX, INDEX_MOD, INDEX_LUA_INIT}
)

# Parser loader paths and args
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
    CSHARP = "tree_sitter_c_sharp"
    DART = "tree_sitter_dart"


# Directory names with a context-dependent ignore: `bin` is build output
# everywhere EXCEPT Cargo's first-party src/bin/ binary layout.
DIR_BIN = "bin"
DIR_SRC = "src"

# Patterns detected at repo root and offered as exclude candidates (user picks which)
IGNORE_PATTERNS = frozenset(
    {
        ".cache",
        ".claude",
        # Android NDK per-ABI CMake build cache; ships compiler-probe
        # sources (CMakeCCompilerId.c) that index as project code.
        ".cxx",
        # Dart/Flutter tool cache (package_config, generated plugin code).
        ".dart_tool",
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

# pathspec style for .cgrignore / --exclude patterns (#495).
GITWILDMATCH_STYLE = "gitignore"
