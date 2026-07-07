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
        "Constructors, destructors, operator overloading, templates, lambdas, C++20 modules, namespaces",
        "C++",
    ),
    SupportedLanguage.LUA: LanguageMetadata(
        LanguageStatus.FULL,
        "Local/global functions, metatables, closures, coroutines",
        "Lua",
    ),
    SupportedLanguage.RUST: LanguageMetadata(
        LanguageStatus.FULL,
        "impl blocks, associated functions",
        "Rust",
    ),
    SupportedLanguage.JAVA: LanguageMetadata(
        LanguageStatus.FULL,
        "Generics, annotations, modern features (records/sealed classes), concurrency, reflection",
        "Java",
    ),
    SupportedLanguage.GO: LanguageMetadata(
        LanguageStatus.DEV,
        "Methods, type declarations",
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

# (H) Image file extensions for chat image handling
MULTIMODAL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
MIME_TYPE_PDF = "application/pdf"
MIME_TYPE_FALLBACK = "application/octet-stream"
LANG_PROMPT_EXTENSIONS = (
    "What file extensions should be associated with this language? (comma-separated)"
)
LANG_TABLE_COL_EXTENSIONS = "Extensions"


CPP_MODULE_EXTENSIONS = (".ixx", ".cppm", ".ccm", ".mxx")
