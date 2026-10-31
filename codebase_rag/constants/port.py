# Cross-language parity (`cgr port check`, epic #3413): how two
# implementations of the same code are paired and judged.

from enum import StrEnum

from .graph import EdgeResolution, NodeLabel


class ParityVerdict(StrEnum):
    """A dimension's or a pair's verdict. `UNKNOWN` is what the graph cannot
    settle (an edge it only guessed, a signature it did not record), and is
    never reported as `MATCH`."""

    MATCH = "MATCH"
    DIFFERS = "DIFFERS"
    UNKNOWN = "UNKNOWN"


# A pair's verdict is the worst of its dimensions'.
PARITY_VERDICT_RANK: dict[ParityVerdict, int] = {
    ParityVerdict.MATCH: 0,
    ParityVerdict.UNKNOWN: 1,
    ParityVerdict.DIFFERS: 2,
}


class PairedBy(StrEnum):
    """How a pair was found, strongest first: a `--pair` the user gave; the
    same normalised name at the same module path relative to each scope (and
    in the same class, for a method); the same name in the same container
    (the class, or the module file's stem) when the layouts differ; or the
    same name alone with the containers differing, which is the pairing to
    verify."""

    EXPLICIT = "explicit"
    MODULE = "module"
    CONTAINER = "container"
    NAME = "name"


# The automatic tiers, tried in this order on what the previous one left.
PORT_PAIRING_TIERS = (PairedBy.MODULE, PairedBy.CONTAINER, PairedBy.NAME)


PORT_CALLABLE_LABELS = frozenset({NodeLabel.FUNCTION.value, NodeLabel.METHOD.value})

# Edges the resolver only guessed: a difference resting on them alone is
# UNKNOWN, since the other side may simply have bound the same call exactly.
PORT_UNCERTAIN_RESOLUTIONS = frozenset(
    {EdgeResolution.HEURISTIC.value, EdgeResolution.OVERLOAD.value}
)

# Folded before names are compared, so `_read_number`, `readNumber` and
# `ReadNumber` agree. The boundary pattern splits camelCase and the end of an
# acronym (`HTTPServer` -> `HTTP_Server`).
PORT_NAME_STRIP_CHARS = "_"
PORT_CAMEL_BOUNDARY_PATTERN = r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
PORT_NAME_JOIN = "_"

# File stems that name a package rather than its contents (`__init__.py`,
# Rust `mod.rs` and `lib.rs`, `index.ts`): a function's container is then the
# directory holding the file.
PORT_PACKAGE_ENTRY_STEMS = frozenset({"__init__", "mod", "lib", "main", "index"})

PORT_PAIR_SEPARATOR = "="
PORT_SCOPE_ROOTS = frozenset({"", "."})

# Overlapping scopes and a malformed or repeated `--pair` are usage errors,
# reported under click's own status for one; a scope or pair naming nothing
# in the graph exits with GRAPH_EXIT_UNKNOWN_TARGET like `cgr graph`.
PORT_EXIT_USAGE = 2
PORT_EXIT_DIFFERS = 1

PORT_ERR_EMPTY_SCOPE = (
    "No function or method of project '{project}' lies under '{scope}'. "
    "A scope is a path relative to the repository root, as `cgr graph "
    "definition` prints it."
)
PORT_ERR_OVERLAPPING_SCOPES = (
    "The scopes '{old}' and '{new}' overlap in project '{project}': each "
    "definition must belong to one side."
)
PORT_ERR_PAIR_FORMAT = "--pair takes OLD=NEW, got '{value}'."
PORT_ERR_PAIR_TARGET = (
    "--pair: '{target}' names {count} definitions under '{scope}'; give a "
    "qualified name, or a dotted suffix of one, that names exactly one."
)
PORT_ERR_PAIR_TWICE = "--pair: '{qualified_name}' is paired more than once."
