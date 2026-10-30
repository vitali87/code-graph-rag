from enum import StrEnum
from pathlib import Path

TRUNCATED_VARINT = "SCIP index ends inside a varint"
TRUNCATED_FIELD = "SCIP index ends inside field {field}"
UNKNOWN_WIRE = "SCIP index has unsupported wire type {wire} on field {field}"
EXPECTED_BYTES = "SCIP field expected a length-delimited value"
EXPECTED_VARINT = "SCIP field expected a varint"
UNTERMINATED_ESCAPE = "unterminated backtick in SCIP symbol"
BAD_DESCRIPTOR = "malformed SCIP descriptor: {text}"


class ScipLanguage(StrEnum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    RUST = "rust"
    GO = "go"
    JAVA = "java"
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"


# Symbol grammar (scip.proto).
LOCAL_PREFIX = "local "
NAMESPACE = "/"
TYPE = "#"
TERM = "."
META = ":"
MACRO = "!"
METHOD = "()."
TYPE_PARAMETER = "[]"
PARAMETER = "()"
SIMPLE_SUFFIXES: frozenset[str] = frozenset({NAMESPACE, TYPE, TERM, META, MACRO})
METHOD_SUFFIX = "()."
SYMBOL_CACHE_SIZE = 1 << 16

# SymbolRole bit flags.
ROLE_DEFINITION = 0x1
ROLE_IMPORT = 0x2
# SymbolInformation.Kind values that name something callable.
KIND_CONSTRUCTOR = 9
KIND_FUNCTION = 17
KIND_METHOD = 26
CALLABLE_KINDS: frozenset[int] = frozenset(
    {KIND_CONSTRUCTOR, KIND_FUNCTION, KIND_METHOD}
)

LOCAL_KEY_SEP = "\x00"
MODULE_LINE = -1
CALL_GAP_CHARS = " \t"
OPTIONAL_CALL = "?."
# `<...>` type arguments before a call's parenthesis, nested one level, and
# Rust's turbofish `::<...>`.
TYPE_ARGS_PATTERN = r"(?:::)?<(?:[^<>()]|<[^<>()]*>)*>\s*"
# `= (a, b) =>`, `= x =>`, `= async (...) =>`, `= function`, with an
# optional TS annotation on the binding (`: Handler =`) or an object
# property's colon (`name: (...) =>`).
ARROW_INIT_PATTERN = (
    r"\s*(?::[^=;\n]*)?[=:]\s*(?:async\s+)?"
    r"(?:function\b[^(]*\([^)]*\)[^{]*|\([^)]*\)\s*(?::[^=]*)?=>|[A-Za-z_$][\w$]*\s*=>)"
)

# `function name` / `async function* name`, ending right at the name.
FUNCTION_KEYWORD_PATTERN = r"\bfunction\s*\*?\s*$"
NAMED_FUNCTION_PATTERN = r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("
UNINDEXED_KEY = "function {line}:{col}"
SCIP_PYTHON = "scip-python"
SCIP_TYPESCRIPT = "scip-typescript"
RUST_ANALYZER = "rust-analyzer"
SCIP_GO = "scip-go"
SCIP_JAVA = "scip-java"
SCIP_CLANG = "scip-clang"
SCIP_DOTNET = "scip-dotnet"
PROJECT_VERSION = "0"
CARGO_TOML = "Cargo.toml"
GO_MOD = "go.mod"
COMPILE_COMMANDS = "compile_commands.json"
CLANG_COMPDB_ARG = "--compdb-path=compile_commands.json"
CLANG_OUTPUT_ARG = "--index-output-path={output}"
TSCONFIG = "tsconfig.json"
# Without a tsconfig scip-typescript indexes nothing, and its own inferred
# config skips .js files whenever .ts files sit beside them.
TS_TSCONFIG = (
    '{"compilerOptions": {"noEmit": true, "allowJs": false, "skipLibCheck": true},'
    ' "include": ["**/*.ts", "**/*.tsx"]}\n'
)
JS_TSCONFIG = (
    '{"compilerOptions": {"noEmit": true, "allowJs": true, "checkJs": false,'
    ' "skipLibCheck": true}, "include": ["**/*.js", "**/*.jsx"]}\n'
)
NODE_DIR = Path(__file__).resolve().parent / "node"
NPM_BIN_DIR = ".bin"
NODE_OPTIONS_ENV = "NODE_OPTIONS"
NODE_HEAP_FLAG = "--max-old-space-size"
NODE_HEAP_OPTION = f"{NODE_HEAP_FLAG}=8192"
STAGE_IGNORE: tuple[str, ...] = ("target", "index.scip")
WORKSPACE_PREFIX = "cgr-scip-"
INDEX_FILENAME = "index.scip"
INDEX_TIMEOUT_S = 3600
DECODE_ERRORS = "replace"
STDERR_TAIL = 4000

# Shared with the indexing benchmark's pinned checkouts (gitignored).
CORPORA_DIRNAME = "corpora"
CATEGORY = "scip_calls"
SCORES_FILENAME = "scip_calls_{language}_scores.csv"
DIFF_FILENAME = "scip_calls_{language}_diff.json"
TABLE_TITLE = "cgr {language} CALLS vs {tool} {version} (SCIP call-site oracle)"
STRICT_SUFFIX = ""
DISPATCH_SUFFIX = ":dispatch"
EXACT_SUFFIX = ":exact"
UNMATCHED_SCIP_KEY = "unmatched:scip_callables"
UNMATCHED_CGR_KEY = "unmatched:cgr_callables"
DIFF_SAMPLE = 300
DEFINITION_REPR = "{path}:{line} {name}"
MODULE_REPR = "{path} <module>"
EDGE_REPR = "{caller} -> {callee}"
