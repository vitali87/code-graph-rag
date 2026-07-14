from enum import StrEnum

from codebase_rag import constants as cs

PY_SUFFIX = ".py"
MODULE_START_LINE = 0

SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.MODULE,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
)
SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(k.value for k in SCORED_NODE_KINDS)
# (H) Span (end_line) grading excludes Module: a module's end_line is the whole
# (H) file, which the ast oracle records as 0, so it is not a meaningful def span.
SPANNED_NODE_KINDS_TUPLE: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.CLASS,
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
)
SPANNED_NODE_KINDS: frozenset[str] = frozenset(
    k.value for k in SPANNED_NODE_KINDS_TUPLE
)

SCORED_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.DEFINES,
    cs.RelationshipType.DEFINES_METHOD,
)
SCORED_EDGE_TYPE_VALUES: frozenset[str] = frozenset(e.value for e in SCORED_EDGE_TYPES)

# (H) L2 dependency edges scored by name/path rather than node location:
# (H) INHERITS by base simple name; IMPORTS by in-repo target file path (internal
# (H) module dependency graph only; external targets are DEPENDS_ON_EXTERNAL).
SCORED_NAME_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.INHERITS,
    cs.RelationshipType.IMPORTS,
)
INIT_STEM = "__init__"
# (H) A bare access of an @property/@cached_property getter invokes it, which cgr
# (H) models as a CALLS edge; the retrieval oracle counts these attribute reads as
# (H) calls so cgr's property-as-call edges are not scored as false positives.
PROPERTY_DECORATORS: frozenset[str] = frozenset({"property", "cached_property"})
SEP = cs.SEPARATOR_DOT
TRACE_CALL_EVENT = "call"
L3_DIFF_FILENAME = "calls_diff.json"
L3_WORKSPACE = "l3_workspace"
SCORED_NAME_EDGE_TYPE_VALUES: frozenset[str] = frozenset(
    e.value for e in SCORED_NAME_EDGE_TYPES
)
DIFF_NAME_EDGE_PREFIX = "name_edge:"
NAME_EDGE_REPR = "{rel} {sfile}:{sstart} -> {target}"

IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "site",
        "node_modules",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        ".ty_cache",
    }
)
EGG_INFO_SUFFIX = ".egg-info"


class Category(StrEnum):
    NODE = "node"
    EDGE = "edge"
    SPAN = "span"
    RETRIEVAL = "retrieval"


AGGREGATE_LABEL = "ALL"

# (H) Span grading: among nodes matched by (kind, file, start), how often cgr's
# (H) end_line agrees with the oracle's. Surfaced as its own category so a wrong
# (H) node span is visible even when node identity is already 1.0.
DIFF_SPAN_PREFIX = "span:"
SPAN_REPR = "{kind} {file}:{start}-{end}"

CSV_FIELDS: tuple[str, ...] = (
    "category",
    "label",
    "tp",
    "fp",
    "fn",
    "precision",
    "recall",
    "f1",
)
LEFT_COLUMNS: frozenset[str] = frozenset({"category", "label"})

DEFAULT_TARGET = "codebase_rag"
DEFAULT_OUT_DIR = "evals/results"
SCORES_FILENAME = "scores.csv"
DIFF_FILENAME = "diff.json"

NODE_REPR = "{kind} {file}:{start} {name}"
EDGE_REPR = "{rel} {pfile}:{pstart} -> {cfile}:{cstart}"
DIFF_NODE_PREFIX = "node:"
DIFF_EDGE_PREFIX = "edge:"

ROUND_DIGITS = 4

# (H) Go structure eval: cgr nodes graded against the go/ast oracle
# (H) (evals/oracles/go_ast.go), joined on (kind, file, start_line).
GO_SUFFIX = ".go"
GO_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.TYPE,
)
GO_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in GO_SCORED_NODE_KINDS
)
GO_ORACLE_DIRNAME = "oracles"
GO_ORACLE_GO_FILE = "go_ast.go"
GO_BIN = "go"
GO_RUN = "run"
GO_MODULE_ENV = "GO111MODULE"
GO_MODULE_OFF = "off"
GO_DEFAULT_TARGET = "."
GO_SCORES_FILENAME = "go_scores.csv"
GO_DIFF_FILENAME = "go_diff.json"

# (H) Multi-language retrieval (Go): file-level call localization for a second
# (H) language, cgr's Go CALLS vs go/ast call sites over the same first-party name
# (H) universe. The go/ast oracle is independent of cgr's tree-sitter parser.
ORACLE_KEY_CALLS = "calls"
ORACLE_KEY_COVERED = "covered"
GO_RETRIEVAL_SCORES_FILENAME = "go_retrieval_scores.csv"
GO_RETRIEVAL_DIFF_FILENAME = "go_retrieval_diff.json"
GO_RETRIEVAL_DIFF_PREFIX = "go-retrieval:"
GO_RETRIEVAL_LABEL = "graph"
GO_RETRIEVAL_TITLE = "cgr multi-language retrieval: Go CALLS vs go/ast oracle"
GO_CALL_EDGE_REPR = "{file} -> {name}"

RS_SUFFIX = ".rs"
RUST_DEFAULT_TARGET = "."
RUST_RETRIEVAL_SCORES_FILENAME = "rust_retrieval_scores.csv"
RUST_RETRIEVAL_DIFF_FILENAME = "rust_retrieval_diff.json"
RUST_RETRIEVAL_DIFF_PREFIX = "rust-retrieval:"
RUST_RETRIEVAL_LABEL = "graph"
RUST_RETRIEVAL_TITLE = "cgr multi-language retrieval: Rust CALLS vs syn oracle"
RUST_CALL_EDGE_REPR = "{file} -> {name}"

JAVA_DEFAULT_TARGET = "."
JAVA_RETRIEVAL_SCORES_FILENAME = "java_retrieval_scores.csv"
JAVA_RETRIEVAL_DIFF_FILENAME = "java_retrieval_diff.json"
JAVA_RETRIEVAL_DIFF_PREFIX = "java-retrieval:"
JAVA_RETRIEVAL_LABEL = "graph"
JAVA_RETRIEVAL_TITLE = "cgr multi-language retrieval: Java CALLS vs javac oracle"
JAVA_CALL_EDGE_REPR = "{file} -> {name}"

SCALA_DEFAULT_TARGET = "."
SCALA_RETRIEVAL_SCORES_FILENAME = "scala_retrieval_scores.csv"
SCALA_RETRIEVAL_DIFF_FILENAME = "scala_retrieval_diff.json"
SCALA_RETRIEVAL_DIFF_PREFIX = "scala-retrieval:"
SCALA_RETRIEVAL_LABEL = "graph"
SCALA_RETRIEVAL_TITLE = "cgr multi-language retrieval: Scala CALLS vs scalameta oracle"
SCALA_CALL_EDGE_REPR = "{file} -> {name}"

TS_DEFAULT_TARGET = "."
TS_RETRIEVAL_SCORES_FILENAME = "ts_retrieval_scores.csv"
TS_RETRIEVAL_DIFF_FILENAME = "ts_retrieval_diff.json"
TS_RETRIEVAL_DIFF_PREFIX = "ts-retrieval:"
TS_RETRIEVAL_LABEL = "graph"
TS_RETRIEVAL_TITLE = "cgr multi-language retrieval: TypeScript CALLS vs tsc oracle"
TS_CALL_EDGE_REPR = "{file} -> {name}"

JS_DEFAULT_TARGET = "."
JS_RETRIEVAL_SCORES_FILENAME = "js_retrieval_scores.csv"
JS_RETRIEVAL_DIFF_FILENAME = "js_retrieval_diff.json"
JS_RETRIEVAL_DIFF_PREFIX = "js-retrieval:"
JS_RETRIEVAL_LABEL = "graph"
JS_RETRIEVAL_TITLE = "cgr multi-language retrieval: JavaScript CALLS vs tsc oracle"
JS_CALL_EDGE_REPR = "{file} -> {name}"

PHP_DEFAULT_TARGET = "."
PHP_RETRIEVAL_SCORES_FILENAME = "php_retrieval_scores.csv"
PHP_RETRIEVAL_DIFF_FILENAME = "php_retrieval_diff.json"
PHP_RETRIEVAL_DIFF_PREFIX = "php-retrieval:"
PHP_RETRIEVAL_LABEL = "graph"
PHP_RETRIEVAL_TITLE = "cgr multi-language retrieval: PHP CALLS vs php-parser oracle"
PHP_CALL_EDGE_REPR = "{file} -> {name}"

LUA_DEFAULT_TARGET = "."
LUA_RETRIEVAL_SCORES_FILENAME = "lua_retrieval_scores.csv"
LUA_RETRIEVAL_DIFF_FILENAME = "lua_retrieval_diff.json"
LUA_RETRIEVAL_DIFF_PREFIX = "lua-retrieval:"
LUA_RETRIEVAL_LABEL = "graph"
LUA_RETRIEVAL_TITLE = "cgr multi-language retrieval: Lua CALLS vs luaparse oracle"
LUA_CALL_EDGE_REPR = "{file} -> {name}"

C_DEFAULT_TARGET = "."
C_SOURCE_GLOB = "*.c"
C_HEADER_GLOB = "*.h"
C_SUFFIXES: tuple[str, ...] = (".c", ".h")
CLANG_INCLUDE_FLAG = "-I"
CLANG_C_STD = "-std=c11"
CLANG_ISYSROOT_FLAG = "-isysroot"
CLANG_ISYSTEM_FLAG = "-isystem"
CLANG_INCLUDE_DIR = "include"
CLANG_SEVERITY_ERROR = 3
XCRUN_SDK_PATH_CMD: tuple[str, ...] = ("xcrun", "--show-sdk-path")
CLANG_RESOURCE_DIR_CMD: tuple[str, ...] = ("clang", "-print-resource-dir")
C_RETRIEVAL_SCORES_FILENAME = "c_retrieval_scores.csv"
C_RETRIEVAL_DIFF_FILENAME = "c_retrieval_diff.json"
C_RETRIEVAL_DIFF_PREFIX = "c-retrieval:"
C_RETRIEVAL_LABEL = "graph"
C_RETRIEVAL_TITLE = "cgr multi-language retrieval: C CALLS vs libclang oracle"
C_CALL_EDGE_REPR = "{file} -> {name}"

CPP_SOURCE_GLOBS: tuple[str, ...] = ("*.cc", "*.cpp", "*.cxx")
CPP_HEADER_GLOBS: tuple[str, ...] = ("*.h", "*.hpp", "*.hh", "*.hxx")
CPP_SUFFIXES: tuple[str, ...] = (".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx")
CLANG_CPP_STD = "-std=c++17"
CLANG_CPP_LANG_FLAG = "-x"
CLANG_CPP_LANG = "c++"
CLANG_DEFINE_FLAG = "-D"
# (H) Apple ships a libclang whose version matches the active macOS SDK's libc++,
# (H) which the pip `libclang` wheel does not; C++ standard headers need that match
# (H) to parse. Probed in order; first existing path wins, else the bundled default.
LIBCLANG_CANDIDATES: tuple[str, ...] = (
    "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
)
# (H) libc++ headers live under <sdk>/usr/include/c++/v1 and MUST precede the clang
# (H) builtin resource headers, else libc++'s <cstddef> finds the C <stddef.h> first.
CLANG_LIBCXX_SUBPATH = "usr/include/c++/v1"
CPP_RETRIEVAL_SCORES_FILENAME = "cpp_retrieval_scores.csv"
CPP_RETRIEVAL_DIFF_FILENAME = "cpp_retrieval_diff.json"
CPP_RETRIEVAL_DIFF_PREFIX = "cpp-retrieval:"
CPP_RETRIEVAL_LABEL = "graph"
CPP_RETRIEVAL_TITLE = "cgr multi-language retrieval: C++ CALLS vs libclang oracle"
CPP_CALL_EDGE_REPR = "{file} -> {name}"

# (H) Semantic-search relevance eval: does cgr's embedding ranking retrieve the
# (H) right function for a natural-language query? Uses cgr's own embedder over
# (H) function source extracted from the captured graph; graded as recall@k on
# (H) controlled fixtures whose query->function relevance is unambiguous.
SEMANTIC_TOP_K = 3
SEMANTIC_SCORES_FILENAME = "semantic_scores.csv"
SEMANTIC_DIFF_FILENAME = "semantic_diff.json"
SEMANTIC_DIFF_PREFIX = "semantic:"
SEMANTIC_LABEL = "recall-at-k"
SEMANTIC_CASE_REPR = "{query} => {expected}"
SEMANTIC_TITLE = "cgr semantic-search eval: query->function recall@k"

# (H) Static CALLS eval: function-level call recall. The oracle resolves only the
# (H) unambiguous direct calls (a bare-name call to a function reachable via a
# (H) first-party import or a same-module top-level def) to (caller_qn, callee_qn),
# (H) using ast import resolution rather than cgr's trie. Method / attribute /
# (H) dynamic calls need type inference and are out of the oracle's scope, so only
# (H) recall is graded: every statically-certain call must be in cgr's graph.
STATIC_CALLS_DEFAULT_TARGET = "codebase_rag"
STATIC_CALLS_SCORES_FILENAME = "static_calls_scores.csv"
STATIC_CALLS_DIFF_FILENAME = "static_calls_diff.json"
STATIC_CALLS_DIFF_PREFIX = "static-calls:"
STATIC_CALLS_LABEL = "direct-call-recall"
STATIC_CALL_EDGE_REPR = "{caller} -> {callee}"
STATIC_CALLS_TITLE = "cgr static-calls eval: function-level direct-call recall"
ORACLE_KEY_KIND = "kind"
ORACLE_KEY_FILE = "file"
ORACLE_KEY_LINE = "line"
ORACLE_KEY_END_LINE = "end_line"
ORACLE_KEY_NAME = "name"
# (H) Edge-payload keys: an oracle that grades containment edges emits a
# (H) {nodes: [...], edges: [...]} object, each edge carrying rel + parent/child
# (H) node references joined against cgr on (kind, file, line).
ORACLE_KEY_NODES = "nodes"
ORACLE_KEY_EDGES = "edges"
ORACLE_KEY_REL = "rel"
ORACLE_KEY_PARENT = "parent"
ORACLE_KEY_CHILD = "child"
# (H) Name-edge payload keys: an inheritance edge carries its source node ref and
# (H) the base type's SIMPLE NAME (cgr resolves bases by simple name, not qn).
ORACLE_KEY_NAME_EDGES = "name_edges"
ORACLE_KEY_SOURCE = "source"
ORACLE_KEY_TARGET_NAME = "target_name"

# (H) Inheritance edges graded by base simple name: INHERITS (extends/superclass
# (H) and superinterface) and IMPLEMENTS (a class implementing an interface).
INHERITANCE_NAME_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.INHERITS,
    cs.RelationshipType.IMPLEMENTS,
)
INHERITANCE_NAME_EDGE_TYPE_VALUES: frozenset[str] = frozenset(
    e.value for e in INHERITANCE_NAME_EDGE_TYPES
)

# (H) Rust structure eval: cgr nodes graded against the syn oracle
# (H) (evals/oracles/rs_oracle), joined on (kind, file, start_line).
RS_SUFFIX = ".rs"
RS_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.ENUM,
    cs.NodeLabel.UNION,
    cs.NodeLabel.TYPE,
)
RS_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in RS_SCORED_NODE_KINDS
)
RS_ORACLE_DIRNAME = "rs_oracle"
RS_ORACLE_BIN = "rs_oracle"
RS_ORACLE_BUILD_LOCK = ".build.lock"
EXE_SUFFIX_WINDOWS = ".exe"
OS_NT = "nt"
CARGO_BIN = "cargo"
CARGO_RUN = "run"
CARGO_BUILD = "build"
CARGO_METADATA = "metadata"
CARGO_FORMAT_VERSION = "--format-version"
CARGO_FORMAT_VERSION_1 = "1"
CARGO_NO_DEPS = "--no-deps"
CARGO_META_TARGET_DIR_KEY = "target_directory"
CARGO_RELEASE = "--release"
CARGO_RELEASE_DIRNAME = "release"
CARGO_MANIFEST = "--manifest-path"
CARGO_QUIET = "-q"
CARGO_ARG_SEP = "--"
CARGO_SRC_DIRNAME = "src"
CARGO_RS_GLOB = "**/*.rs"
RS_SCORES_FILENAME = "rs_scores.csv"
RS_DIFF_FILENAME = "rs_diff.json"

# (H) TypeScript structure eval: cgr nodes graded against the TS-compiler-API
# (H) oracle (evals/oracles/ts_oracle), joined on (kind, file, start_line).
TS_SUFFIXES: tuple[str, ...] = (".ts", ".tsx")
TS_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.ENUM,
    cs.NodeLabel.TYPE,
)
TS_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in TS_SCORED_NODE_KINDS
)
TS_ORACLE_DIRNAME = "ts_oracle"
TS_ORACLE_SCRIPT = "ts_ast.js"
NODE_BIN = "node"
NPM_BIN = "npm"
NPM_INSTALL = "install"
NPM_FLAGS: tuple[str, ...] = ("--no-audit", "--no-fund")
NODE_MODULES_DIRNAME = "node_modules"
# (H) npm creates node_modules before populating it, so its existence is not
# (H) proof of a completed install; the marker is written only after npm exits 0,
# (H) and the lock directory serializes concurrent pytest-xdist installers.
NODE_DEPS_MARKER = ".node-deps-installed"
NODE_DEPS_LOCK = ".node-deps-lock"
NODE_DEPS_LOCK_TRIES = 600
NODE_DEPS_LOCK_POLL_SECONDS = 0.5
NODE_ORACLE_FAILED = "{script} exited {code}: {stderr}"
TS_DTS_SUFFIX = ".d.ts"
TS_SCORES_FILENAME = "ts_scores.csv"
TS_DIFF_FILENAME = "ts_diff.json"

# (H) JavaScript structure eval: same TS-compiler-API oracle, run over .js/.jsx.
JS_SUFFIXES: tuple[str, ...] = (".js", ".jsx")
JS_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)
JS_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in JS_SCORED_NODE_KINDS
)
JS_SCORES_FILENAME = "js_scores.csv"
JS_DIFF_FILENAME = "js_diff.json"

# (H) Java structure eval: cgr nodes graded against the JDK Compiler Tree API
# (H) oracle (evals/oracles/java_oracle/Oracle.java), joined on (kind, file, line).
JAVA_SUFFIX = ".java"
JAVA_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.ENUM,
)
JAVA_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in JAVA_SCORED_NODE_KINDS
)
JAVA_ORACLE_DIRNAME = "java_oracle"
JAVA_ORACLE_SOURCE = "Oracle.java"
JAVA_ORACLE_CLASS = "Oracle"
JAVAC_BIN = "javac"
JAVA_BIN = "java"
JAVA_CP_FLAG = "-cp"
SCALA_SUFFIXES = (".scala", ".sc")
SCALA_ORACLE_DIRNAME = "scala_oracle"
SCALA_ORACLE_SOURCE = "Oracle.scala"
SCALA_CLI_BIN = "scala-cli"
SCALA_CLI_RUN = "run"
SCALA_CLI_ARG_SEP = "--"
JAVA_SCORES_FILENAME = "java_scores.csv"
JAVA_DIFF_FILENAME = "java_diff.json"

# (H) C# structure eval: cgr nodes graded against a Roslyn syntax-tree oracle
# (H) (Microsoft.CodeAnalysis.CSharp, the C# analog of Go's go/ast). class/struct/
# (H) record map to Class, interface to Interface, enum to Enum (cgr's
# (H) determine_node_type), and members/local functions to Method/Function.
CS_SUFFIX = ".cs"
CSHARP_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.ENUM,
)
CSHARP_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in CSHARP_SCORED_NODE_KINDS
)
CSHARP_ORACLE_DIRNAME = "csharp_oracle"
CSHARP_ORACLE_PROJECT = "Oracle.csproj"
DOTNET_BIN = "dotnet"
DOTNET_TELEMETRY_ENV = "DOTNET_CLI_TELEMETRY_OPTOUT"
DOTNET_NOLOGO_ENV = "DOTNET_NOLOGO"
# (H) The oracle reads this comma-separated dir set so its file walk (and thus its
# (H) declared-type universe used to split INHERITS/IMPLEMENTS) skips exactly the
# (H) directories cgr's is_ignored/IGNORE_PATTERNS skips, not a smaller hardcoded
# (H) subset that would let an ignored folder's types pollute the classification.
CGR_IGNORE_DIRS_ENV = "CGR_IGNORE_DIRS"
DOTNET_BUILD = "build"
DOTNET_CONFIG_FLAG = "-c"
DOTNET_CONFIG_RELEASE = "Release"
DOTNET_OUTPUT_FLAG = "-o"
DOTNET_VERBOSITY_FLAG = "--verbosity"
DOTNET_VERBOSITY_QUIET = "quiet"
CSHARP_ORACLE_SOURCE = "Program.cs"
CSHARP_ORACLE_BUILD_DIRNAME = "bin/oracle"
CSHARP_ORACLE_DLL = "Oracle.dll"
CSHARP_ORACLE_BUILD_LOCK = ".build-lock"
CSHARP_DEFAULT_TARGET = "."
CSHARP_SCORES_FILENAME = "csharp_scores.csv"
CSHARP_DIFF_FILENAME = "csharp_diff.json"
CSHARP_ORACLE_PARSE_FAILED = (
    "Failed to parse C# oracle JSON output: {error}\nstdout: {stdout}\nstderr: {stderr}"
)

# (H) Lua structure eval: cgr nodes graded against a luaparse oracle. Lua has no
# (H) classes, so every function (global/local/table/method/anonymous) is Function.
LUA_SUFFIX = ".lua"
LUA_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (cs.NodeLabel.FUNCTION,)
LUA_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in LUA_SCORED_NODE_KINDS
)
LUA_ORACLE_DIRNAME = "lua_oracle"
LUA_ORACLE_SCRIPT = "lua_ast.js"
LUA_SCORES_FILENAME = "lua_scores.csv"
LUA_DIFF_FILENAME = "lua_diff.json"

# (H) PHP structure eval: cgr nodes graded against a php-parser oracle.
PHP_SUFFIX = ".php"
PHP_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
    cs.NodeLabel.INTERFACE,
    cs.NodeLabel.ENUM,
)
PHP_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in PHP_SCORED_NODE_KINDS
)
PHP_ORACLE_DIRNAME = "php_oracle"
PHP_ORACLE_SCRIPT = "php_ast.js"
PHP_SCORES_FILENAME = "php_scores.csv"
PHP_DIFF_FILENAME = "php_diff.json"

# (H) C/C++ structure eval: cgr nodes graded against a libclang oracle driven by a
# (H) compile_commands.json, so includes and macros resolve to the true AST (which
# (H) tree-sitter cannot do). Joined on (kind, file, start_line).
CPP_SUFFIXES: tuple[str, ...] = (
    ".cpp",
    ".cc",
    ".cxx",
    ".c",
    ".hpp",
    ".hh",
    ".hxx",
    ".h",
)
CPP_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)
CPP_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in CPP_SCORED_NODE_KINDS
)
CPP_COMPDB_FILENAME = "compile_commands.json"
CPP_SCORES_FILENAME = "cpp_scores.csv"
CPP_DIFF_FILENAME = "cpp_diff.json"
CPP_DEFAULT_TARGET = "."

# (H) Retrieval benchmark: does graph-augmented retrieval find the code that
# (H) calls a symbol better than grep? The unit is a file-level call edge
# (H) (caller_file, callee_simple_name): "file F contains a call to symbol S".
# (H) This mirrors the GitLab GKG "did it open the right file" localization
# (H) signal, and all conditions are scored against the same Python ast oracle
# (H) over the same file and first-party symbol universe.


class GrepMode(StrEnum):
    # (H) NAME matches the bare symbol token anywhere (a user's first grep); CALL
    # (H) matches the symbol immediately followed by `(` (a call-tuned grep). Both
    # (H) still over-match: NAME on imports/aliases/comments, CALL on def sites.
    NAME = "name"
    CALL = "call"


class RetrievalCondition(StrEnum):
    GRAPH = "graph"
    GREP_NAME = "grep_name"
    GREP_CALL = "grep_call"


RG_BIN = "rg"
RG_ONLY_MATCHING = "-o"
RG_WITH_FILENAME = "-H"
RG_NO_LINE_NUMBER = "--no-line-number"
RG_NO_HEADING = "--no-heading"
# (H) --null separates the path from the match with a NUL byte instead of `:`, so
# (H) a path containing a colon is parsed intact. -f - reads the patterns from
# (H) stdin (one per line), so the full symbol universe never lands in argv and
# (H) cannot trip the OS per-argument length limit (128KB on Linux, 32KB on
# (H) Windows). The pattern lines are ORed, equivalent to a single alternation.
RG_NULL = "--null"
RG_PATTERN_FILE_FLAG = "-f"
RG_STDIN = "-"
RG_GLOB_FLAG = "-g"
RG_PY_GLOB = "*.py"
RG_SEARCH_PATH = "."
RG_NULL_SEP = "\x00"
RG_OK_RETURNCODES: frozenset[int] = frozenset({0, 1})

PATTERN_SEP = "\n"
GREP_NAME_TEMPLATE = r"\b{name}\b"
GREP_CALL_TEMPLATE = r"\b{name}\s*\("
IDENTIFIER_PATTERN = r"[A-Za-z_][A-Za-z0-9_]*"

RETRIEVAL_DEFAULT_TARGET = "codebase_rag"
RETRIEVAL_SCORES_FILENAME = "retrieval_scores.csv"
RETRIEVAL_DIFF_FILENAME = "retrieval_diff.json"
RETRIEVAL_DIFF_PREFIX = "retrieval:"
RETRIEVAL_TITLE = "cgr retrieval eval: graph vs grep (file-level call localization)"

# (H) Incremental-update eval: index, apply a semantically neutral edit (a
# (H) trailing comment that changes the file hash but not its AST), run an
# (H) incremental update, then compare against a clean forced re-index of the
# (H) same on-disk state. The clean re-index is the oracle; any divergence is an
# (H) incremental-update correctness bug.
INCREMENTAL_DEFAULT_TARGET = "codebase_rag"
INCREMENTAL_SCORES_FILENAME = "incremental_scores.csv"
INCREMENTAL_DIFF_FILENAME = "incremental_diff.json"
INCREMENTAL_NODE_DIFF_PREFIX = "incremental-node:"
INCREMENTAL_EDGE_DIFF_PREFIX = "incremental-edge:"
INCREMENTAL_TITLE = "cgr incremental-update eval: incremental vs clean re-index"
INCREMENTAL_WORK_DIRNAME = "repo"
INCREMENTAL_TMP_PREFIX = "cgr-incremental-eval-"
NEUTRAL_EDIT_COMMENT = "\n# cgr-incremental-eval neutral edit\n"
INCREMENTAL_MTIME_BUMP = 10.0
INCREMENTAL_DEFAULT_SAMPLE = 25
INCREMENTAL_DIFF_SAMPLE_CAP = 50
STATE_NODE_REPR = "{label} {uid}"
STATE_EDGE_REPR = "{rel} {fl}:{fv} -> {tl}:{tv}"

# (H) Import-resolution eval: classify each module's imports by top-level package
# (H) as internal (first-party, resolves into the repo) or external (stdlib or
# (H) third-party), against an ast + filesystem oracle. Surfaces internal/external
# (H) misclassification (issue #498). Both sides reduce an import to its top-level
# (H) package name, a unit each computes independently, so the oracle is clean.
IMPORTS_DEFAULT_TARGET = "codebase_rag"
IMPORTS_SCORES_FILENAME = "imports_scores.csv"
IMPORTS_DIFF_FILENAME = "imports_diff.json"
IMPORTS_DIFF_PREFIX = "imports:"
IMPORTS_ALL_LABEL = "imports-all"
IMPORTS_INTERNAL_LABEL = "imports-internal"
IMPORTS_EXTERNAL_LABEL = "imports-external"
IMPORT_DEP_REPR = "{file} -> {top} (external={external})"
IMPORTS_TITLE = "cgr import-resolution eval: internal vs external classification"
# (H) `__future__` is a compiler directive, not a dependency; cgr ignores it, so
# (H) the oracle excludes it to avoid false external-import misses.
IMPORTS_IGNORED_TOPS: frozenset[str] = frozenset({"__future__"})

# (H) Inheritance eval: grade resolved INHERITS (subclass_qn -> base_qn) and
# (H) OVERRIDES (subclass_qn, base_qn, method) against an ast oracle that resolves
# (H) bases via same-module and from-import only, skipping ambiguous/attribute/
# (H) external bases. Goes beyond L1, which checks INHERITS by base simple name.
INHERITANCE_DEFAULT_TARGET = "codebase_rag"
INHERITANCE_SCORES_FILENAME = "inheritance_scores.csv"
INHERITANCE_DIFF_FILENAME = "inheritance_diff.json"
INHERITANCE_DIFF_PREFIX = "inheritance:"
INHERITS_LABEL = "inherits-resolved"
OVERRIDES_LABEL = "overrides"
INHERITS_EDGE_REPR = "{sub} -> {base}"
OVERRIDES_EDGE_REPR = "{sub} -> {base} .{method}"
INHERITANCE_TITLE = "cgr inheritance eval: resolved INHERITS and OVERRIDES"
STAR_IMPORT = "*"
SEP_NUL = "\x00"

# (H) Dead-code eval: run cgr's reachability engine (codebase_rag.dead_code) over
# (H) the captured graph and grade the reported unreachable set against controlled
# (H) fixtures whose dead functions are known by construction. Surfaces missing
# (H) CALLS edges (a live function wrongly flagged dead). The engine is
# (H) unit-tested on hand-built graphs, so a fixture mismatch points at cgr's
# (H) graph, not the scorer.
DEAD_CODE_DEFAULT_TARGET = "codebase_rag"
DEAD_CODE_SCORES_FILENAME = "dead_code_scores.csv"
DEAD_CODE_DIFF_FILENAME = "dead_code_diff.json"
DEAD_CODE_DIFF_PREFIX = "dead-code:"
DEAD_CODE_LABEL = "dead-code"
DEAD_CODE_TITLE = "cgr dead-code eval: reachability over the captured graph"

# (H) Cross-project (monorepo) eval: does cgr resolve CALLS and IMPORTS across
# (H) top-level package boundaries? The single-package corpora the other evals use
# (H) never exercise this; cgr's headline is monorepo RAG. Graded on synthetic
# (H) multi-package fixtures with known cross-package edges.
CROSS_PROJECT_DIFF_PREFIX = "cross-project:"
CROSS_CALLS_LABEL = "cross-package-calls"
CROSS_IMPORTS_LABEL = "cross-package-imports"
CROSS_EDGE_REPR = "{src} -> {dst}"

# (H) Instantiation eval: file-level constructor localization. For each first-party
# (H) class, which files instantiate it. cgr INSTANTIATES edges vs an ast oracle of
# (H) calls whose callee simple name is a first-party class. Isolates the
# (H) INSTANTIATES signal the retrieval eval folds into CALLS.
INSTANTIATION_DEFAULT_TARGET = "codebase_rag"
INSTANTIATION_SCORES_FILENAME = "instantiation_scores.csv"
INSTANTIATION_DIFF_FILENAME = "instantiation_diff.json"
INSTANTIATION_DIFF_PREFIX = "instantiation:"
INSTANTIATES_LABEL = "instantiates"
INSTANTIATION_EDGE_REPR = "{file} -> {cls}"
INSTANTIATION_TITLE = "cgr instantiation eval: file-level INSTANTIATES vs ast oracle"
