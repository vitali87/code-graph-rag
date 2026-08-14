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

TRACE_LANGUAGE_PYTHON = "python"
TRACE_TOOL_NAME = "cgr-trace"
TRACE_DEFAULT_OUTPUT = "cgr-trace.jsonl"

# Python runtime qualname markers.
TRACE_QUALNAME_LOCALS = "<locals>"
TRACE_QUALNAME_MODULE = "<module>"
TRACE_SYNTHETIC_PREFIX = "<"

# Installed-dependency code frequently lives under the repo root (virtualenvs,
# vendored packages); frames whose path contains any of these directory names
# are not project code and are skipped at capture time. Bare names, not
# separator-delimited fragments, so matching works with both POSIX and Windows
# separators in co_filename.
TRACE_EXCLUDED_DIR_NAMES = frozenset({"site-packages", ".venv", "node_modules"})

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


class TraceUnresolvedReason(StrEnum):
    """Why a traced frame could not be mapped to a graph node."""

    OUTSIDE_REPO = "outside_repo"
    SYNTHETIC = "synthetic"
    UNKNOWN_PATH = "unknown_path"
    NO_MATCH = "no_match"


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
