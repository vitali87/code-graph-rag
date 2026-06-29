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
CARGO_BIN = "cargo"
CARGO_RUN = "run"
CARGO_RELEASE = "--release"
CARGO_MANIFEST = "--manifest-path"
CARGO_QUIET = "-q"
CARGO_ARG_SEP = "--"
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
JAVA_SCORES_FILENAME = "java_scores.csv"
JAVA_DIFF_FILENAME = "java_diff.json"

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
