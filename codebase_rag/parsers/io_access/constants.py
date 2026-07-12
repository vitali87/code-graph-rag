from __future__ import annotations

from enum import StrEnum

from ... import constants as cs

# (H) Python nested-scope boundaries. The per-caller IO/flow DFS must not descend
# (H) into these: a nested def/class is its own caller and is walked separately,
# (H) so a read/write/flow is attributed to its immediate scope only (matching
# (H) how CALLS is attributed). Single source of truth for io_access + flow_access.
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


class IODirection(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    READ_WRITE = "READ_WRITE"


# (H) Synthetic qualified name for a Resource node: resource::<kind>::<identity>.
RESOURCE_QN_FORMAT = "resource::{kind}::{identity}"

# (H) Identity used when the accessed target is not a static string literal.
DYNAMIC_TARGET = "<dynamic>"

KEY_KIND = "kind"

# (H) Python open()-style mode characters that imply writing / read-write.
MODE_WRITE_CHARS = ("w", "a", "x")
MODE_READ_CHAR = "r"
MODE_UPDATE_CHAR = "+"

# (H) SQL leading keywords used to refine a DB handle execute() into read vs write.
SQL_READ_KEYWORDS = ("SELECT",)
SQL_WRITE_KEYWORDS = ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER")
