"""Constants for dynamic runtime call-trace capture and ingestion."""

from enum import StrEnum

# Interchange format (one JSON object per line; first line is the header).
TRACE_FORMAT_VERSION = 1
TRACE_KIND_HEADER = "header"
TRACE_KIND_CALL = "call"

TRACE_KEY_KIND = "kind"
TRACE_KEY_VERSION = "version"
TRACE_KEY_LANGUAGE = "language"
TRACE_KEY_REPO_ROOT = "repo_root"
TRACE_KEY_TRACER = "tracer"
TRACE_KEY_CALLER = "caller"
TRACE_KEY_CALLEE = "callee"
TRACE_KEY_PATH = "path"
TRACE_KEY_QUALNAME = "qualname"
TRACE_KEY_LINE = "line"
TRACE_KEY_COUNT = "count"
TRACE_KEY_WORKLOADS = "workloads"
TRACE_KEY_RECEIVER_TYPES = "receiver_types"
# Header flag: True when the tracer sampled the call stack (pprof, V8 cpuprofile,
# dotnet-trace, Dart CPU samples) so parent/child adjacency and counts are
# approximate, False when it instrumented every call (Python sys.monitoring, the
# JVM agent, Xdebug, the C shim, the Lua hook) so edges and counts are exact.
# Absent in pre-flag trace files, which read back as exact.
TRACE_KEY_SAMPLED = "sampled"

TRACE_LANGUAGE_PYTHON = "python"
TRACE_LANGUAGE_JVM = "jvm"
TRACE_LANGUAGE_JS = "javascript"
TRACE_LANGUAGE_DOTNET = "dotnet"
TRACE_LANGUAGE_PHP = "php"
TRACE_LANGUAGE_LUA = "lua"
TRACE_LANGUAGE_DART = "dart"
TRACE_LANGUAGE_GO = "go"
TRACE_LANGUAGE_CPP = "cpp"
TRACE_LANGUAGE_RUST = "rust"
TRACE_TOOL_NAME = "cgr-trace"
TRACE_TOOL_NAME_JVM = "cgr-trace-jvm"
TRACE_TOOL_NAME_CPUPROFILE = "cgr-trace-cpuprofile"
TRACE_TOOL_NAME_SPEEDSCOPE = "cgr-trace-speedscope"
TRACE_TOOL_NAME_XDEBUG = "cgr-trace-xdebug"
TRACE_TOOL_NAME_PPROF = "cgr-trace-pprof"
TRACE_TOOL_NAME_RUST_PPROF = "cgr-trace-rust-pprof"
TRACE_TOOL_NAME_INSTRUMENTED = "cgr-trace-instrumented"
TRACE_TOOL_NAME_EBPF = "cgr-trace-ebpf"
TRACE_DEFAULT_OUTPUT = "cgr-trace.jsonl"

# Python runtime qualname markers.
TRACE_QUALNAME_LOCALS = "<locals>"
TRACE_QUALNAME_MODULE = "<module>"
TRACE_SYNTHETIC_PREFIX = "<"

# V8 cpuprofile markers.
TRACE_QUALNAME_ANONYMOUS = "<anonymous>"
TRACE_JS_FILE_URL_PREFIX = "file://"
TRACE_JS_LOCAL_FILE_HOST = "localhost"
TRACE_JS_UNC_PATH = "//{host}{path}"

TRACE_ERR_BAD_CPUPROFILE = "{path} is not a V8 .cpuprofile (missing node tree)."

# dotnet-trace speedscope markers.
TRACE_ERR_BAD_SPEEDSCOPE = (
    "{path} is not a speedscope profile (missing frames or sampled profiles)."
)
TRACE_DOTNET_ASSEMBLY_SEPARATOR = "!"
TRACE_DOTNET_CTOR = "..ctor"
TRACE_DOTNET_CCTOR = "..cctor"
TRACE_DOTNET_NESTED_MARKER = "+"

# Xdebug computerized-trace markers (trace_format=1, file format 4).
TRACE_ERR_BAD_PPROF = "{path} is not a pprof CPU profile."
# A run shorter than one sampling period writes a well-formed profile with no
# samples; calling it malformed sent users after a format problem (#2887).
TRACE_ERR_PPROF_NO_SAMPLES = (
    "{path} is a valid pprof profile but has no samples: the workload ended "
    "before the profiler's first sample, one sampling period in (10 ms at Go's "
    "default 100 Hz). Profile a longer run, or raise the sampling rate."
)
# A gzipped pprof is inflated in bounded chunks: a few MB of compressed zeros
# would otherwise expand to gigabytes in memory (#2263). The cap matches the
# `cgr trace pull` download cap, which counts compressed bytes only.
TRACE_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
TRACE_GZIP_MAGIC = b"\x1f\x8b"
TRACE_GZIP_WBITS = 16 + 15
# Compressed input is fed in slices of this size, and at most this many gzip
# members are read: a profile of thousands of empty members would otherwise
# stay under both byte caps while each member re-read the whole remainder.
TRACE_GZIP_INPUT_CHUNK_BYTES = 1024 * 1024
TRACE_MAX_GZIP_MEMBERS = 64
TRACE_ERR_PPROF_TOO_LARGE = (
    "{path} decompresses to more than {limit} bytes; refusing to load it."
)
TRACE_ERR_BAD_ADDRS = "{path} is not a cgr instrumented address trace."
TRACE_ERR_NO_SYMBOLIZER = "Neither atos nor addr2line is available to symbolise."
TRACE_ERR_ADDRS_DROPPED = (
    "{path} lost call edges (the shim's edge table filled, calls nested deeper "
    "than its stack, or a signal handler called in while the shim was busy), "
    "so the trace is incomplete and cannot honour exact invocation counts."
)
TRACE_ERR_ADDRS_UNWOUND = (
    "{path} left functions without their exit hook (longjmp, or a C++ exception "
    "under clang++), so some calls may name the wrong caller and the trace "
    "cannot honour exact invocation counts."
)
TRACE_MSG_ADDRS_UNRESOLVED = (
    "{count} of {total} instrumented addresses did not symbolise to a source "
    "position (missing debug info or stripped symbols); their edges were dropped."
)
TRACE_ERR_BAD_XDEBUG = (
    "{path} is not an Xdebug computerized trace (expected 'File format: 4')."
)
TRACE_XDEBUG_FORMAT_LINE = "File format: 4"
TRACE_XDEBUG_MAIN = "{main}"
TRACE_XDEBUG_GLUE_FUNCTIONS = frozenset(
    {"require", "require_once", "include", "include_once", "eval"}
)
TRACE_PHP_INSTANCE_SEPARATOR = "->"
TRACE_PHP_STATIC_SEPARATOR = "::"
TRACE_PHP_NAMESPACE_SEPARATOR = "\\"
TRACE_PHP_CLOSURE_PREFIX = "{closure:"

# JVM runtime name markers.
TRACE_JVM_CONSTRUCTOR = "<init>"
TRACE_JVM_STATIC_INITIALIZER = "<clinit>"
TRACE_JVM_LAMBDA_PREFIX = "lambda$"
TRACE_JVM_ANONFUN_PREFIX = "$anonfun$"
TRACE_JVM_NESTED_MARKER = "$"

# Installed-dependency code frequently lives under the repo root (virtualenvs,
# vendored packages); frames whose path contains any of these directory names
# are not project code and are skipped at capture time. Bare names, not
# separator-delimited fragments, so matching works with both POSIX and Windows
# separators in co_filename.
TRACE_EXCLUDED_DIR_NAMES = frozenset({"site-packages", ".venv", "node_modules"})
# Installed code never counts as evidence of where another machine's checkout
# lives, even when it is a copy of the indexed package. Debian's system Python
# installs into dist-packages rather than site-packages.
TRACE_INSTALLED_DIR_NAMES = TRACE_EXCLUDED_DIR_NAMES | {"dist-packages"}

# Frames recorded on Windows name files with this separator; tracebacks are
# pasted across OSes, so it is read as a path separator wherever cgr runs.
TRACE_WINDOWS_PATH_SEPARATOR = "\\"

# Names whose first parameter marks a bound receiver worth sampling.
TRACE_RECEIVER_PARAMS = ("self", "cls")
# Receiver types are sampled only for the first N observations of a pair to
# bound the cost of materialising frame locals on hot paths.
TRACE_RECEIVER_SAMPLE_LIMIT = 8

# Caps applied when writing edge properties so graph rows stay bounded.
TRACE_MAX_WORKLOADS_PER_EDGE = 20
TRACE_MAX_RECEIVER_TYPES_PER_EDGE = 10

# Properties stored on CALLS edges by trace ingestion. A `dynamic` edge that
# `static_missed` is a relationship the static passes could not see (dynamic
# dispatch, reflection, registries); one without the flag confirms a static
# edge at runtime.
TRACE_PROP_DYNAMIC = "dynamic"
TRACE_PROP_CALL_COUNT = "dynamic_call_count"
TRACE_PROP_WORKLOADS = "dynamic_workloads"
TRACE_PROP_WORKLOAD_COUNT = "dynamic_workload_count"
TRACE_PROP_RECEIVER_TYPES = "dynamic_receiver_types"
TRACE_PROP_STATIC_MISSED = "static_missed"
# True when the edge came from a sampling profiler, so its presence and
# dynamic_call_count are approximate (a sampled edge that was never sampled is
# not evidence of dead code); False when the tracer observed every call.
TRACE_PROP_SAMPLED = "dynamic_sampled"
# True once an incremental sync re-parsed an endpoint whose definition changed
# after the trace was ingested (issue #2429): the observation is kept, but it
# describes code that no longer exists as traced. Only a new ingest clears it.
TRACE_PROP_STALE = "dynamic_stale"
# What a re-parse carries of a trace-derived edge: the runtime observation
# itself. Resolution, `static_missed` and the dispatch-literal site are
# re-derived against the re-parsed static graph instead, as an ingest would.
TRACE_CARRIED_PROPS = (
    TRACE_PROP_DYNAMIC,
    TRACE_PROP_CALL_COUNT,
    TRACE_PROP_WORKLOADS,
    TRACE_PROP_WORKLOAD_COUNT,
    TRACE_PROP_RECEIVER_TYPES,
    TRACE_PROP_SAMPLED,
)


class TraceUnresolvedReason(StrEnum):
    """Why a traced frame could not be mapped to a graph node."""

    OUTSIDE_REPO = "outside_repo"
    SYNTHETIC = "synthetic"
    UNKNOWN_PATH = "unknown_path"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"


# Said instead of handing back an empty result that reads as "the graph does
# not know this code" when only the paths failed to line up (issue #2587).
TRACEBACK_NOTE_NOTHING_RESOLVED = (
    "0 of {total} frames resolved: {outside} lie outside the indexed checkout "
    "{root}, and no checkout root they share matches a file the graph indexes. "
    "If the traceback comes from another machine, a container or a CI runner, "
    "pass {param} to map its checkout root onto the repository, "
    'e.g. {{"/app": "."}}.'
)

TRACE_ERR_BAD_HEADER = "Trace file {path} does not start with a valid cgr trace header."
TRACE_ERR_VERSION = (
    "Trace file {path} has format version {found}; this build reads version {expected}."
)
TRACE_ERR_BAD_RECORD = "Trace file {path} line {line}: malformed record."

TRACE_MSG_INGEST_SUMMARY = (
    "records={records} edges={edges} confirmed_static={confirmed} "
    "static_missed={missed} unresolved={unresolved}"
)
TRACE_MSG_UNRESOLVED_DETAIL = "  unresolved[{reason}]={count}"

TRACE_MSG_SOURCEMAP_RESOLUTION = (
    "source-map resolution: {resolved}/{total} project frames resolved to "
    "source ({rate})"
)
TRACE_MSG_SOURCEMAP_DETAIL = "  source_map[{outcome}]={count}"

TRACE_ERR_EBPF_LANGUAGE = (
    "Unsupported eBPF profile language {language!r}; supported: {supported}."
)
TRACE_MSG_EBPF_UNSYMBOLIZED = (
    "eBPF symbolisation: {count} unsymbolised locations across {mappings} mappings"
)
TRACE_MSG_EBPF_MAPPING_DETAIL = "  unsymbolised[{name}]={count}"
TRACE_MSG_EBPF_UNMAPPED = (
    "{count} frames at {paths} unmapped build paths (add --path-map to re-anchor)"
)
