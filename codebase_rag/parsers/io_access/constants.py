from __future__ import annotations

from enum import StrEnum

from ... import constants as cs

# Python nested-scope boundaries. The per-caller IO/flow DFS must not descend
# into these: a nested def/class is its own caller and is walked separately, so a
# read/write/flow is attributed to its immediate scope only (matching how CALLS is
# attributed). Single source of truth for io_access + flow_access.
PY_SCOPE_BOUNDARIES = (
    cs.TS_PY_FUNCTION_DEFINITION,
    cs.TS_PY_CLASS_DEFINITION,
    cs.TS_PY_DECORATED_DEFINITION,
)


class ResourceKind(StrEnum):
    FILE = "FILE"
    NETWORK = "NETWORK"
    DATABASE = "DATABASE"
    STDIN = "STDIN"
    STDOUT = "STDOUT"
    STDERR = "STDERR"
    ENV = "ENV"
    SOCKET = "SOCKET"
    ENDPOINT = "ENDPOINT"
    # A codegen contract operation (issue #912): client stubs and server
    # implementations of the same contract meet at one shared node keyed
    # `<ServiceStem>.<Method>`, no URL literal involved.
    RPC = "RPC"


class IODirection(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


# Synthetic qualified name for a Resource node: resource::<kind>::<identity>.
RESOURCE_QN_FORMAT = "resource::{kind}::{identity}"

# Identity used when the accessed target is not a static string literal.
DYNAMIC_TARGET = "<dynamic>"

KEY_KIND = "kind"

# Python open()-style mode characters that imply writing / read-write.
MODE_WRITE_CHARS = ("w", "a", "x")
MODE_READ_CHAR = "r"
MODE_UPDATE_CHAR = "+"

# fetch-style options objects carry the HTTP verb under this key; the verb
# refines the sink's declared direction (an unlisted verb is unknown).
HTTP_METHOD_OPTION_KEY = "method"
HTTP_READ_VERBS = frozenset({"GET", "HEAD", "OPTIONS"})
HTTP_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# SQL leading keywords used to refine a DB handle execute() into read vs write.
# An unlisted first keyword falls back to the method's declared direction
# (execute -> READ_WRITE), so only add keywords whose direction is unambiguous from
# the first token. WITH/PRAGMA are intentionally omitted (a CTE or pragma can be
# either), keeping the fallback rather than guessing.
SQL_READ_KEYWORDS = ("SELECT", "EXPLAIN", "VALUES")
SQL_WRITE_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "CREATE",
    "DROP",
    "ALTER",
    "REPLACE",
    "TRUNCATE",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT",
    "RELEASE",
    "BEGIN",
    "VACUUM",
    "REINDEX",
    "ATTACH",
    "DETACH",
)
