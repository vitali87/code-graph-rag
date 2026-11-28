# What the Python engine hands from one stage of an index to the next, written
# out for the Rust rewrite's parity checks (docs/reports/PHASE1_SEAMS.md,
# section 3). Both engines write these files; their format is the contract.

from enum import StrEnum

HANDOFF_HEADER = "# cgr handoff {file} {version}"


class HandoffFile(StrEnum):
    # Hand-off A, written once discovery and change detection are done.
    FILES = "files.txt"
    # Hand-off B, written when the deferred definitions are resolved.
    INDEX = "index.txt"


# The format version each file's first line names. Each engine writes the
# version it implements, and a comparison of two versions fails naming both.
# The project index is at 2 since it holds Go's own sections
# (docs/reports/PHASE3_GO.md, section 6); the file plan is as phase 1 left it.
HANDOFF_FORMAT_VERSIONS: dict[HandoffFile, int] = {
    HandoffFile.FILES: 1,
    HandoffFile.INDEX: 2,
}


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
    # Go's own tables, read where Python keeps them: each module's package
    # clause, each method's signature, each interface's embeds, each
    # implementation pair, each free function's first result and each file's
    # build constraint.
    GO_PACKAGE = "go_package"
    GO_METHOD = "go_method"
    GO_INTERFACE = "go_interface"
    GO_IMPLEMENTS = "go_implements"
    GO_RETURN_TYPE = "go_return_type"
    GO_BUILD = "go_build"


# Who paired a Go type with an interface: the go/types helper, or the method
# sets' match by name and arity.
class GoPairSource(StrEnum):
    PROVEN = "proven"
    GUESSED = "guessed"


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
HANDOFF_KEY_CLAUSE = "clause"
HANDOFF_KEY_FILE = "file"
HANDOFF_KEY_PACKAGE = "package"
HANDOFF_KEY_PARAMS = "params"
HANDOFF_KEY_RESULTS = "results"
HANDOFF_KEY_EMBEDS = "embeds"
HANDOFF_KEY_INTERFACE = "interface"
HANDOFF_KEY_SOURCE = "source"
HANDOFF_KEY_BUILDS = "builds"
HANDOFF_KEY_TYPE = "type"
HANDOFF_KEY_CONSTRAINT = "constraint"
