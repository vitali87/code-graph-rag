# What the Python engine hands from one stage of an index to the next, written
# out for the Rust rewrite's parity checks (docs/reports/PHASE1_SEAMS.md,
# section 3). Both engines write these files; their format is the contract.

from enum import StrEnum

# The format version each file's first line names. Each engine writes the
# version it implements, and a comparison of two versions fails naming both.
HANDOFF_FORMAT_VERSION = 1
HANDOFF_HEADER = "# cgr handoff {file} {version}"


class HandoffFile(StrEnum):
    # Hand-off A, written once discovery and change detection are done.
    FILES = "files.txt"
    # Hand-off B, written when the deferred definitions are resolved.
    INDEX = "index.txt"


# The first field of a line: what its key names.
class HandoffKind(StrEnum):
    FILE = "file"
    REPARSE = "reparse"
    DELETED = "deleted"
    MODULE = "module"
    DEFINITION = "definition"
    SIMPLE_NAME = "simple_name"
    IMPORTS = "imports"
    INHERITS = "inherits"


# Why an incremental run re-parses a file: it is new, its bytes changed, or a
# change elsewhere moves what it binds to.
class ReparseReason(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    DEPENDENT = "dependent"


# The keys of a line's JSON value.
HANDOFF_KEY_LANGUAGE = "language"
HANDOFF_KEY_ROUTE = "route"
HANDOFF_KEY_MD5 = "md5"
HANDOFF_KEY_REASON = "reason"
HANDOFF_KEY_LABEL = "label"
HANDOFF_KEY_PATH = "path"
