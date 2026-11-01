import re
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
# Span (end_line) grading excludes Module: its end_line is the whole file,
# which the ast oracle records as 0, so it is not a meaningful def span.
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

# L2 dependency edges scored by name/path rather than node location:
# INHERITS by base simple name; IMPORTS by in-repo target file path
# (internal dependency graph only; external targets are DEPENDS_ON_EXTERNAL).
SCORED_NAME_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.INHERITS,
    cs.RelationshipType.IMPORTS,
)
INIT_STEM = "__init__"
# A bare access of an @property/@cached_property getter invokes it, which cgr
# models as a CALLS edge; the retrieval oracle counts these attribute reads as
# calls so property-as-call edges are not scored as false positives.
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

# Span grading: among nodes matched by (kind, file, start), how often cgr's
# end_line agrees with the oracle's. Its own category so a wrong span shows
# even when node identity is already 1.0.
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

# Go structure eval: cgr nodes graded against the go/ast oracle
# (evals/oracles/go_ast.go), joined on (kind, file, start_line).
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

# Multi-language retrieval (evals/retrieval_eval.py): file-level call
# localization per language, cgr's CALLS edges vs an independent parser's call
# sites over the same first-party name universe. The languages differ only in
# the spec each one registers; outputs and labels derive from its stem.
ORACLE_KEY_CALLS = "calls"
ORACLE_KEY_COVERED = "covered"
LANG_RETRIEVAL_DEFAULT_TARGET = "."
LANG_RETRIEVAL_SCORES_FILENAME = "{lang}_retrieval_scores.csv"
LANG_RETRIEVAL_DIFF_FILENAME = "{lang}_retrieval_diff.json"
LANG_RETRIEVAL_DIFF_PREFIX = "{lang}-retrieval:"
LANG_RETRIEVAL_TITLE = (
    "cgr multi-language retrieval: {language} CALLS vs {oracle} oracle"
)
CALL_EDGE_REPR = "{file} -> {name}"


class RetrievalLanguage(StrEnum):
    C = "c"
    CPP = "cpp"
    CSHARP = "csharp"
    GO = "go"
    JAVA = "java"
    JS = "js"
    LUA = "lua"
    PHP = "php"
    RUST = "rust"
    SCALA = "scala"
    TS = "ts"


# Display names for the retrieval titles and log lines.
C_LANGUAGE_NAME = "C"
CPP_LANGUAGE_NAME = "C++"
CSHARP_LANGUAGE_NAME = "C#"
GO_LANGUAGE_NAME = "Go"
JAVA_LANGUAGE_NAME = "Java"
JS_LANGUAGE_NAME = "JavaScript"
LUA_LANGUAGE_NAME = "Lua"
PHP_LANGUAGE_NAME = "PHP"
RUST_LANGUAGE_NAME = "Rust"
SCALA_LANGUAGE_NAME = "Scala"
TS_LANGUAGE_NAME = "TypeScript"
GO_AST_ORACLE_NAME = "go/ast"
JAVAC_ORACLE_NAME = "javac"
LIBCLANG_ORACLE_NAME = "libclang"
LUAPARSE_ORACLE_NAME = "luaparse"
PHP_PARSER_ORACLE_NAME = "php-parser"
ROSLYN_ORACLE_NAME = "Roslyn"
SCALAMETA_ORACLE_NAME = "scalameta"
SYN_ORACLE_NAME = "syn"
TSC_ORACLE_NAME = "tsc"

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

CPP_SOURCE_GLOBS: tuple[str, ...] = ("*.cc", "*.cpp", "*.cxx")
CPP_HEADER_GLOBS: tuple[str, ...] = ("*.h", "*.hpp", "*.hh", "*.hxx")
CLANG_CPP_STD = "-std=c++17"
CLANG_CPP_LANG_FLAG = "-x"
CLANG_CPP_LANG = "c++"
CLANG_DEFINE_FLAG = "-D"
# Apple ships a libclang matching the active macOS SDK's libc++, which the pip
# `libclang` wheel does not; C++ standard headers need that match to parse.
# Probed in order; first existing path wins, else the bundled default.
LIBCLANG_CANDIDATES: tuple[str, ...] = (
    "/Library/Developer/CommandLineTools/usr/lib/libclang.dylib",
)
# libc++ headers live under <sdk>/usr/include/c++/v1 and MUST precede the clang
# builtin resource headers, else libc++'s <cstddef> finds the C <stddef.h> first.
CLANG_LIBCXX_SUBPATH = "usr/include/c++/v1"

# Semantic-search relevance eval: does cgr's embedding ranking retrieve the
# right function for a natural-language query? Uses cgr's own embedder over
# function source from the captured graph; graded as recall@k on fixtures
# whose query->function relevance is unambiguous.
SEMANTIC_TOP_K = 3
SEMANTIC_SCORES_FILENAME = "semantic_scores.csv"
SEMANTIC_DIFF_FILENAME = "semantic_diff.json"
SEMANTIC_DIFF_PREFIX = "semantic:"
SEMANTIC_LABEL = "recall-at-k"
SEMANTIC_CASE_REPR = "{query} => {expected}"
SEMANTIC_TITLE = "cgr semantic-search eval: query->function recall@k"
SEMANTIC_RECALL_COLUMN = "recall"

# Query -> expected qualified name, over DEFAULT_TARGET (`codebase_rag`).
# Each query must map UNAMBIGUOUSLY to one function, or the case grades the
# fixture rather than the retriever. Kept here rather than in a test module so
# the CLI entry point and the tests score the same corpus; a benchmark whose
# cases live only in its test cannot be run.
SEMANTIC_CASES: tuple[tuple[str, str], ...] = (
    (
        "count edges between nodes in the result set for reranking",
        "codebase_rag.tools.graph_rerank.build_proximity_query",
    ),
    (
        "blend vector similarity with graph proximity to reorder hits",
        "codebase_rag.tools.graph_rerank.rerank_by_graph_proximity",
    ),
)

# Static CALLS eval: function-level call recall. The oracle resolves only
# unambiguous direct calls (a bare-name call to a function reachable via a
# first-party import or a same-module top-level def) to (caller_qn, callee_qn),
# using ast import resolution not cgr's trie. Method / attribute / dynamic
# calls need type inference and are out of scope, so only recall is graded:
# every statically-certain call must be in cgr's graph.
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
# Edge-payload keys: an oracle that grades containment edges emits a
# {nodes: [...], edges: [...]} object, each edge carrying rel + parent/child
# node refs joined against cgr on (kind, file, line).
ORACLE_KEY_NODES = "nodes"
ORACLE_KEY_EDGES = "edges"
ORACLE_KEY_REL = "rel"
ORACLE_KEY_PARENT = "parent"
ORACLE_KEY_CHILD = "child"
# Name-edge payload keys: an inheritance edge carries its source node ref and
# the base type's SIMPLE NAME (cgr resolves bases by simple name, not qn).
ORACLE_KEY_NAME_EDGES = "name_edges"
ORACLE_KEY_SOURCE = "source"
ORACLE_KEY_TARGET_NAME = "target_name"

# Inheritance edges graded by base simple name: INHERITS (extends/superclass
# and superinterface) and IMPLEMENTS (a class implementing an interface).
INHERITANCE_NAME_EDGE_TYPES: tuple[cs.RelationshipType, ...] = (
    cs.RelationshipType.INHERITS,
    cs.RelationshipType.IMPLEMENTS,
)
INHERITANCE_NAME_EDGE_TYPE_VALUES: frozenset[str] = frozenset(
    e.value for e in INHERITANCE_NAME_EDGE_TYPES
)

# Rust structure eval: cgr nodes graded against the syn oracle
# (evals/oracles/rs_oracle), joined on (kind, file, start_line).
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

# TypeScript structure eval: cgr nodes graded against the TS-compiler-API
# oracle (evals/oracles/ts_oracle), joined on (kind, file, start_line).
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
# Run instead of trusting `which node` (issue #1639). `@ruby/prism` is ESM-only,
# so a Node too old to `require()` an ES module satisfies a presence check and
# then fails the oracle with ERR_REQUIRE_ESM. Probing the interop boundary
# covers that break, a missing install and any future incompatibility, without
# hard-coding a version to keep up to date.
DOTNET_LIST_SDKS_FLAG = "--list-sdks"
DOTNET_SKIP_NO_BINARY = "{binary} is not on PATH"
DOTNET_SKIP_NO_SDKS = "dotnet --list-sdks failed: {stderr}"
DOTNET_SKIP_SDK_TOO_OLD = (
    "the C# oracle needs .NET SDK {needed} or newer to build its net{needed}.0 "
    "project; installed: {found}"
)
DOTNET_SKIP_BUILD_FAILED = "the C# oracle could not be built: {stderr}"
DOTNET_SKIP_BUILD_INCOMPLETE = (
    "the C# oracle build did not complete (another process held the build lock "
    "past the timeout)"
)
DOTNET_PROBE_TIMEOUT_S = 30
# The build restores packages, so it needs far longer than the version probe;
# bounded all the same, because an unreachable feed otherwise blocks forever.
DOTNET_BUILD_TIMEOUT_S = 600
DOTNET_SKIP_BUILD_TIMEOUT = (
    "the C# oracle build did not finish within {seconds}s (an unreachable "
    "package feed will do this); skipping rather than blocking the run"
)
DOTNET_SDK_LINE_SEP = " "
# Oracle.csproj targets net10.0, so an older SDK cannot build it (NETSDK1045).
# Kept beside the guard that reads it: if the csproj's TargetFramework moves,
# this moves with it.
CSHARP_ORACLE_MIN_SDK_MAJOR = 10
NODE_EVAL_FLAG = "-e"
# `require()` the oracle's own entry script, which is the call that breaks: a
# CommonJS script loading an ESM-only package (`@ruby/prism`) on a Node too old
# for it. A dynamic `import()` probe would pass on that Node and report the
# toolchain usable, which is worse than no guard at all.
NODE_REQUIRE_PROBE = 'require("{package}")'
NODE_PACKAGE_MANIFEST = "package.json"
NODE_PACKAGE_DEPS_KEY = "dependencies"
NODE_ORACLE_SCRIPT_GLOB = "*.js"
# Skip reasons name what actually went wrong. "node/npm toolchain not
# available" is FALSE on the machine that reported #1639: node is installed
# there, and the oracle dies on ERR_REQUIRE_ESM. The stderr is truncated
# because a full node stack trace buries the one line that explains the skip.
NODE_SKIP_NO_BINARY = "{binary} is not on PATH"
NODE_SKIP_INSTALL_FAILED = "npm install failed for this oracle: {stderr}"
NODE_SKIP_CANNOT_REQUIRE = "this node cannot require({package}): {stderr}"
SKIP_REASON_STDERR_CHARS = 400
# A child forked from a process that holds a live gRPC client (the Milvus Lite
# vector store opens one) inherits gRPC's fork handlers, which on macOS log
# absl lines such as `I1003 10:37:31.866642   75998 ev_poll_posix.cc:587] FD
# from fork parent still in poll list` to the child's stderr before it execs.
# That is the PARENT's noise, and a few lines of it fill the budget above
# before the child's own error.
# A line is inherited only when its source is one of gRPC's fork-time files,
# not merely when it has the absl format: the child may log in that format
# too, and those lines are its own. The sources, from grpc v1.84.0:
# - ev_poll_posix.cc: PollPoller::Work, "FD from fork parent still in poll
#   list" (line 587, as in the #2550 macOS CI log);
# - fork_posix.cc: grpc_prefork, "Other threads are currently calling into
#   gRPC, skipping fork() handlers" (line 71, also in that log);
# - ev_epoll1_linux.cc: Epoll1Poller::HandleForkInChild, "Post-fork grpc epoll
#   fd" (the Linux poller's counterpart, logged under GRPC_TRACE).
# util/fork.cc is left out: it logs nothing, and its name is not among the
# source files compiled into grpcio's cygrpc module.
GRPC_FORK_LOG_SOURCES: tuple[str, ...] = (
    "ev_poll_posix.cc",
    "fork_posix.cc",
    "ev_epoll1_linux.cc",
)
# The whole absl prefix (severity, MMDD, time to the microsecond, thread id,
# `file:line] `) so a node stack frame never matches.
INHERITED_LOG_LINE_PATTERN = re.compile(
    r"[IWEF][0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6} +[0-9]+ "
    rf"(?:{'|'.join(map(re.escape, GRPC_FORK_LOG_SOURCES))}):[0-9]+\] "
)
# The oracle's own `require("pkg")` calls: the authoritative statement of what
# it loads, and literally the call that fails on an incompatible Node. Builtins
# are excluded because they load everywhere and would make the probe vacuous.
NODE_REQUIRE_PATTERN = re.compile(r"""require\(\s*["']([^"']+)["']\s*\)""")
NODE_BUILTIN_MODULES: frozenset[str] = frozenset(
    {"fs", "path", "os", "util", "url", "process", "assert", "module", "buffer"}
)
NODE_PROBE_TIMEOUT_S = 30
NPM_BIN = "npm"
NPM_INSTALL = "install"
NPM_FLAGS: tuple[str, ...] = ("--no-audit", "--no-fund")
NODE_MODULES_DIRNAME = "node_modules"
# npm creates node_modules before populating it, so its existence is not proof
# of a completed install; the marker is written only after npm exits 0, and
# the lock directory serialises concurrent pytest-xdist installers.
NODE_DEPS_MARKER = ".node-deps-installed"
NODE_DEPS_LOCK = ".node-deps-lock"
NODE_DEPS_LOCK_TRIES = 600
NODE_DEPS_LOCK_POLL_SECONDS = 0.5
NODE_ORACLE_FAILED = "{script} exited {code}: {stderr}"
TS_DTS_SUFFIX = ".d.ts"
TS_SCORES_FILENAME = "ts_scores.csv"
TS_DIFF_FILENAME = "ts_diff.json"

# JavaScript structure eval: same TS-compiler-API oracle, run over .js/.jsx.
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

# Java structure eval: cgr nodes graded against the JDK Compiler Tree API
# oracle (evals/oracles/java_oracle/Oracle.java), joined on (kind, file, line).
JAVA_SUFFIX = ".java"
LOCATION_KEY_SEPARATOR = ":"
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

# C# structure eval: cgr nodes graded against a Roslyn syntax-tree oracle
# (Microsoft.CodeAnalysis.CSharp, the C# analogue of go/ast). class/struct/
# record map to Class, interface to Interface, enum to Enum, and members/
# local functions to Method/Function.
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
# The oracle reads this comma-separated dir set so its file walk (and its
# declared-type universe for splitting INHERITS/IMPLEMENTS) skips exactly the
# directories cgr's is_ignored/IGNORE_PATTERNS skips, not a smaller subset
# that would let an ignored folder's types pollute the classification.
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
JAVA_ORACLE_BUILD_LOCK = ".build-lock"
CSHARP_DEFAULT_TARGET = "."
CSHARP_SCORES_FILENAME = "csharp_scores.csv"
CSHARP_DIFF_FILENAME = "csharp_diff.json"
CSHARP_ORACLE_PARSE_FAILED = (
    "Failed to parse C# oracle JSON output: {error}\nstdout: {stdout}\nstderr: {stderr}"
)

# Lua structure eval: cgr nodes graded against a luaparse oracle. Lua has no
# classes, so every function (global/local/table/method/anonymous) is Function.
LUA_SUFFIX = ".lua"
LUA_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (cs.NodeLabel.FUNCTION,)
LUA_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in LUA_SCORED_NODE_KINDS
)
LUA_ORACLE_DIRNAME = "lua_oracle"
LUA_ORACLE_SCRIPT = "lua_ast.js"
LUA_SCORES_FILENAME = "lua_scores.csv"
LUA_DIFF_FILENAME = "lua_diff.json"

# Ruby structure eval (issue #1190): cgr nodes graded against Prism, Ruby's
# official parser. Ruby reaches the graph through the ast-grep structural tier.
#
# Ruby MODULES are excluded from the graded set rather than mapped onto Class.
# cgr has no Module label, so grading them would report a recall miss no
# implementation could fix; calling a module a Class instead would make ground
# truth assert something false (modules cannot be instantiated and join the
# ancestor chain by inclusion, not inheritance) and would score a future Module
# label as a regression. The exclusion keeps that gap visible and honest.
# Definitions nested inside a module are still graded — they are real nodes cgr
# emits.
RUBY_SUFFIX = ".rb"
RUBY_SCORED_NODE_KINDS: tuple[cs.NodeLabel, ...] = (
    cs.NodeLabel.FUNCTION,
    cs.NodeLabel.METHOD,
    cs.NodeLabel.CLASS,
)
RUBY_SCORED_NODE_KIND_VALUES: frozenset[str] = frozenset(
    k.value for k in RUBY_SCORED_NODE_KINDS
)
RUBY_ORACLE_DIRNAME = "ruby_oracle"
RUBY_ORACLE_SCRIPT = "ruby_ast.js"
RUBY_SCORES_FILENAME = "ruby_scores.csv"
RUBY_DIFF_FILENAME = "ruby_diff.json"

# PHP structure eval: cgr nodes graded against a php-parser oracle.
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

# C/C++ structure eval: cgr nodes graded against a libclang oracle driven by a
# compile_commands.json, so includes and macros resolve to the true AST that
# tree-sitter cannot. Joined on (kind, file, start_line).
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

# Retrieval benchmark: does graph-augmented retrieval find the code that calls
# a symbol better than grep? The unit is a file-level call edge (caller_file,
# callee_simple_name): "file F contains a call to symbol S". Mirrors the GitLab
# GKG "did it open the right file" signal; all conditions score against the
# same Python ast oracle over the same file and first-party symbol universe.


class GrepMode(StrEnum):
    # NAME matches the bare symbol token anywhere (a user's first grep); CALL
    # matches the symbol immediately followed by `(` (a call-tuned grep). Both
    # still over-match: NAME on imports/aliases/comments, CALL on def sites.
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
# --null separates the path from the match with a NUL byte instead of `:`, so
# a path containing a colon is parsed intact. -f - reads patterns from stdin
# (one per line), so the full symbol universe never lands in argv and cannot
# trip the OS per-argument length limit (128KB Linux, 32KB Windows). The
# pattern lines are ORed, equivalent to a single alternation.
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

# Incremental-update eval: index, apply a semantically neutral edit (a trailing
# comment that changes the file hash but not its AST), run an incremental
# update, then compare against a clean forced re-index of the same on-disk
# state. The clean re-index is the oracle; any divergence is a correctness bug.
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

# Import-resolution eval: classify each module's imports by top-level package
# as internal (first-party, resolves into the repo) or external (stdlib or
# third-party), against an ast + filesystem oracle. Surfaces internal/external
# misclassification (issue #498). Both sides reduce an import to its top-level
# package name, a unit each computes independently, so the oracle is clean.
IMPORTS_DEFAULT_TARGET = "codebase_rag"
IMPORTS_SCORES_FILENAME = "imports_scores.csv"
IMPORTS_DIFF_FILENAME = "imports_diff.json"
IMPORTS_DIFF_PREFIX = "imports:"
IMPORTS_ALL_LABEL = "imports-all"
IMPORTS_INTERNAL_LABEL = "imports-internal"
IMPORTS_EXTERNAL_LABEL = "imports-external"
IMPORT_DEP_REPR = "{file} -> {top} (external={external})"
IMPORTS_TITLE = "cgr import-resolution eval: internal vs external classification"
# `__future__` is a compiler directive, not a dependency; cgr ignores it, so
# the oracle excludes it to avoid false external-import misses.
IMPORTS_IGNORED_TOPS: frozenset[str] = frozenset({"__future__"})

# Inheritance eval: grade resolved INHERITS (subclass_qn -> base_qn) and
# OVERRIDES (subclass_qn, base_qn, method) against an ast oracle that resolves
# bases via same-module and from-import only, skipping ambiguous/attribute/
# external bases. Beyond L1, which checks INHERITS by base simple name.
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

# Dead-code eval: run cgr's reachability engine (codebase_rag.dead_code) over
# the captured graph and grade the reported unreachable set against fixtures
# whose dead functions are known by construction. Surfaces missing CALLS edges
# (a live function wrongly flagged dead). The engine is unit-tested on
# hand-built graphs, so a fixture mismatch points at cgr's graph, not the scorer.
DEAD_CODE_DEFAULT_TARGET = "codebase_rag"
DEAD_CODE_SCORES_FILENAME = "dead_code_scores.csv"
DEAD_CODE_DIFF_FILENAME = "dead_code_diff.json"
DEAD_CODE_DIFF_PREFIX = "dead-code:"
DEAD_CODE_LABEL = "dead-code"
DEAD_CODE_TITLE = "cgr dead-code eval: reachability over the captured graph"

# Duplicates eval: replay the duplicate-fingerprint Cypher fetches over the
# captured graph and run codebase_rag.duplicates on top, grading reported
# clone PAIRS against fixtures whose duplicates are known by construction.
# The engine is unit-tested on hand-built rows, so a fixture mismatch points
# at fingerprint ingest or the graph capture, not the grouping.
DUPLICATES_DEFAULT_TARGET = "codebase_rag"
DUPLICATES_REPORT_FILENAME = "duplicates_report.json"
DUPLICATES_DIFF_PREFIX = "duplicates:"
DUPLICATES_LABEL = "duplicate-pairs"
DUPLICATES_PAIR_SEP = "<->"

# Cross-project (monorepo) eval: does cgr resolve CALLS and IMPORTS across
# top-level package boundaries? The single-package corpora the other evals use
# never exercise this, yet monorepo RAG is cgr's headline. Graded on synthetic
# multi-package fixtures with known cross-package edges.
CROSS_PROJECT_DIFF_PREFIX = "cross-project:"
CROSS_CALLS_LABEL = "cross-package-calls"
CROSS_IMPORTS_LABEL = "cross-package-imports"
CROSS_EDGE_REPR = "{src} -> {dst}"

# Instantiation eval: file-level constructor localization. For each first-party
# class, which files instantiate it. cgr INSTANTIATES edges vs an ast oracle of
# calls whose callee simple name is a first-party class, isolating the
# INSTANTIATES signal the retrieval eval folds into CALLS.
INSTANTIATION_DEFAULT_TARGET = "codebase_rag"
INSTANTIATION_SCORES_FILENAME = "instantiation_scores.csv"
INSTANTIATION_DIFF_FILENAME = "instantiation_diff.json"
INSTANTIATION_DIFF_PREFIX = "instantiation:"
INSTANTIATES_LABEL = "instantiates"
INSTANTIATION_EDGE_REPR = "{file} -> {cls}"
INSTANTIATION_TITLE = "cgr instantiation eval: file-level INSTANTIATES vs ast oracle"

# Java inheritance grading (issue #1190). The oracle names supertypes by SIMPLE
# name, so the row is labelled to say what it measures rather than borrowing the
# Python row's label.
JAVA_SUPERTYPES_LABEL = "supertypes(simple-name)"
CPP_BASES_LABEL = "bases(simple-name)"
CSHARP_SUPERTYPES_LABEL = "supertypes(simple-name)"
TS_SUPERTYPES_LABEL = "supertypes(simple-name)"


class InheritanceLanguage(StrEnum):
    PYTHON = "python"
    JAVA = "java"
    CPP = "cpp"
    CSHARP = "csharp"
    TYPESCRIPT = "typescript"


INHERITANCE_LANGUAGE_HELP = (
    "Language to grade: python (ast oracle), java (javac oracle), "
    "cpp (libclang oracle), csharp (Roslyn oracle) or "
    "typescript (tsc oracle)."
)

INDEXING_DEFAULT_CORPUS = "django"
INDEXING_CORPUS_DIR = "evals/results/corpora"
INDEXING_RESULTS_FILE = "indexing_bench.json"

AGENTIC_DEFAULT_SAMPLE = 40
AGENTIC_QA_TIMEOUT_S = 300
AGENTIC_RESULTS_FILE = "agentic_qa{suffix}.json"
AGENTIC_RECORDS_FILE = "agentic_qa{suffix}_records.jsonl"

# First-tool eval (issue #2359).
FIRST_TOOL_TABLE_TITLE = "First tool call per structural question"
FIRST_TOOL_COL_QUESTION = "Question"
FIRST_TOOL_COL_TOOL = "First tool"
FIRST_TOOL_COL_GRAPH_FIRST = "Graph first"
FIRST_TOOL_NO_TOOL = "(none)"
FIRST_TOOL_PASS = "yes"
FIRST_TOOL_MISS = "no"

# Memgraph rejects a statement whose `$name` has no value; the graph double
# refuses it the same way so a caller that forgets one fails a unit test
# instead of every real sync (issue #2392).
CYPHER_PARAM_PATTERN = r"\$([A-Za-z_][A-Za-z0-9_]*)"
CYPHER_PARAMS_MISSING = "Parameter(s) {names} not provided for query:\n{query}"

# --- graph backend parity (issue #386) ---------------------------------------
# Every read query the package ships, run on two backends over one graph.
PARITY_QUERY_PREFIX = "CYPHER_"
PARITY_PACKAGE_PREFIX = "codebase_rag."
PARITY_SKIP_MODULE = ".tests"
PARITY_MAIN = "__main__"
PARITY_WRITE_PATTERN = re.compile(
    r"\b(MERGE|CREATE|SET|DELETE|REMOVE|FOREACH|DROP)\b", re.IGNORECASE
)
PARITY_PARAM_PATTERN = re.compile(r"\$([A-Za-z_]\w*)")
PARITY_ORDER_BY_PATTERN = re.compile(r"\bORDER\s+BY\b", re.IGNORECASE)
PARITY_LIMIT_PATTERN = re.compile(r"\bLIMIT\b", re.IGNORECASE)
# Columns holding engine-assigned ids, which no two engines share.
PARITY_ID_COLUMN_PATTERN = re.compile(r"(^|_)ids?$|^id\(|node_id|_id\b", re.IGNORECASE)
# Read queries the audit formats from pieces, not runnable on their own.
PARITY_FRAGMENTS = frozenset(
    {"CYPHER_AUDIT_IS_NULL", "CYPHER_AUDIT_MISSING_REQUIRED", "CYPHER_AUDIT_OR"}
)
PARITY_PARAM_FALLBACK = "x"
PARITY_FLOAT_DIGITS = 6
PARITY_TAG_NODE = "node"
PARITY_TAG_REL = "rel"
PARITY_ID_MASK = "<id>"
PARITY_MS_PER_S = 1000


class ParityEngine(StrEnum):
    MEMGRAPH = "memgraph"
    SQLITE = "sqlite"


PARITY_FORMAT_FIELD_PATTERN = re.compile(r"\{[a-z_]+\}")
PARITY_READ_PATTERN = re.compile(r"\bRETURN\b", re.IGNORECASE)
PARITY_DUMP_NODES = "MATCH (n) RETURN labels(n) AS labels, properties(n) AS props"
PARITY_DUMP_RELS = (
    "MATCH (a)-[r]->(b) RETURN labels(a)[0] AS from_label, properties(a) AS from_props, "
    "type(r) AS type, properties(r) AS props, labels(b)[0] AS to_label, "
    "properties(b) AS to_props"
)
PARITY_PROJECT_ROOT = "MATCH (p:Project {name: $name}) RETURN p.root_path AS root"
PARITY_TOP_CALLEES = (
    "MATCH (c)-[:CALLS]->(f) WHERE f.qualified_name STARTS WITH $prefix "
    "AND (f:Function OR f:Method) RETURN f.qualified_name AS qn, f.name AS name, "
    "f.start_line AS line, count(c) AS k ORDER BY k DESC, qn LIMIT 5"
)
PARITY_MODULES = (
    "MATCH (m:Module) WHERE m.qualified_name STARTS WITH $prefix "
    "RETURN m.qualified_name AS qn, m.name AS name ORDER BY qn LIMIT 5"
)
PARITY_FILES = (
    "MATCH (f:File) WHERE f.absolute_path STARTS WITH $root "
    "RETURN f.path AS path, f.absolute_path AS absolute_path ORDER BY path LIMIT 5"
)

# JARVIS micro-benchmark (arXiv 2305.05949): PyCG's programs plus JARVIS's own
# context-manager and control-flow categories, each with a ground-truth call
# graph, pinned to the commit the paper's artifact was published at.
JARVIS_DATASET_URL = "https://github.com/pythonJaRvis/pythonJaRvis.github.io.git"
JARVIS_DATASET_COMMIT = "8871b86494a1aec0095686ffa543994a7361af88"
JARVIS_PROGRAMS_DIR = "Jarvis/dataset/micro_benchmark"
JARVIS_TRUTH_DIR = "Jarvis/ground_truth/micro_benchmark"
JARVIS_TRUTH_FILE = "callgraph.json"
JARVIS_PROJECT = "bench"
JARVIS_CACHE_DIR = "evals/results/corpora/jarvis"
JARVIS_RESULTS_FILE = "evals/results/jarvis_micro.json"
JARVIS_TABLE_TITLE = "cgr on the JARVIS micro-benchmark ({count} programs)"
JARVIS_TOTAL_ROW = "TOTAL"
JARVIS_COLUMNS = ("category", "precision", "recall", "tp", "fp", "fn")
JARVIS_HELP_CACHE = "Checkout of the dataset."
JARVIS_HELP_OUT = "Where to write scores and diffs."
JARVIS_HELP_CATEGORY = "Score one category only."
