# Embedded graph settings read outside the engine (config, the graph service,
# the wheel parity script), and the native tree bridge's names and probe; the
# engine's own constants live in embedded_graph/constants.py.

from enum import StrEnum

EG_DEFAULT_DB_FILENAME = "graph.sqlite"
# Settings whose presence, with GRAPH_BACKEND left unset, marks an install that
# ran on Memgraph when Memgraph was the default engine.
GRAPH_BACKEND_SETTING = "GRAPH_BACKEND"
MEMGRAPH_CONNECTION_SETTINGS = (
    "MEMGRAPH_HOST",
    "MEMGRAPH_PORT",
    "MEMGRAPH_USERNAME",
    "MEMGRAPH_PASSWORD",
)
EG_MEMORY_PATH = ":memory:"
# A writer waits this long for another process's write before failing.
EG_BUSY_TIMEOUT_MS = 60_000
EG_CACHE_SIZE_KIB = 262_144


# The tree walks the indexer hands it (parsers/native_trees.py), and what
# they raise for a node they cannot read exactly.
EG_RUST_TREE_BRIDGE = "TreeBridge"
EG_RUST_NOT_BORROWED = "NotBorrowed"
# A bridge method a build from before the scope walks lacks.
EG_RUST_SCOPE_FACTS = "scope_facts"


class TreeBridgeCheck(StrEnum):
    """What the probe compares through both runtimes before the bridge runs."""

    LAYOUT = "node layout"
    ANCHOR_HASH = "anchor hash"
    FINGERPRINT = "fingerprint"
    SCOPE_FACTS = "scope facts"
    INNER_SCOPE_NAMES = "inner scope names"


# The probe: definitions the two hashes walk, decorators, comments, f-strings,
# non-ASCII text (byte and column offsets part ways there), every binding
# form the scope walks read, and a syntax error (ERROR and MISSING nodes).
TREE_BRIDGE_PROBE = """\
import os  # comment
@decorator(arg=1)
class Café(Base, metaclass=Meta):
    \"\"\"Doc ✓.\"\"\"
    def method(self, x: int = 2, *args, **kw) -> list[int]:
        for i in range(x):
            if i % 2 and not kw:
                yield lambda y: y + i
            elif i:
                return [i, f"{x!r:>4} é"]
        with open(os.sep) as f, f as g:
            pass
        try:
            raise ValueError
        except (ValueError, TypeError) as e:
            del e
        return [n for n in args if n]

async def top(a, /, b, *, c):
    await a
    def inner():
        nonlocal b
        b += 1
    return inner

def scopes(p, *rest, key=None, **kw):
    global counter
    import os.path as osp, json
    from . import helpers
    try:
        from .pkg import mod as alias
    except ImportError:
        alias = None
    match p:
        case [first, *others] if first:
            pass
        case Point(x=_, y=yy) | _:
            pass
    total = [v for v in rest if (w := v)] + list({k: k for k in kw})
    fn = lambda a, b=1: a + b
    with open(p) as (h, i):
        for j, (k, *l) in enumerate(h):
            q: int = 1
            q += j
    class Inner:
        nonlocal key
        z = lambda: key
    return total, fn, Inner, w

def broken(x:
    return x +
""".encode()


# The Cypher AST enums the Rust parser in cgr_graph_rs mirrors. It imports
# them by this module's path (ENUM_MODULE in rust/cgr_graph_rs/src/cypher/
# python.rs), so they stay here; the engine reads them through its own
# constants module.
class EgDirection(StrEnum):
    OUT = "out"
    IN = "in"
    BOTH = "both"


class EgSetKind(StrEnum):
    PROP = "prop"
    MERGE = "merge"
    REPLACE = "replace"
    LABEL = "label"


class EgRemoveKind(StrEnum):
    PROP = EgSetKind.PROP.value
    LABEL = EgSetKind.LABEL.value


class EgQuantifier(StrEnum):
    ANY = "any"
    ALL = "all"
    NONE = "none"
    SINGLE = "single"


class EgSchemaKind(StrEnum):
    CREATE_INDEX = "create_index"
    CREATE_CONSTRAINT = "create_constraint"
    DROP_INDEX = "drop_index"
    DROP_CONSTRAINT = "drop_constraint"
    SHOW_CONSTRAINTS = "show_constraints"
    SHOW_INDEXES = "show_indexes"
    SHOW_STORAGE = "show_storage"


class EgBinary(StrEnum):
    OR = "or"
    XOR = "xor"
    AND = "and"
    EQ = "="
    NE = "<>"
    LT = "<"
    GT = ">"
    LE = "<="
    GE = ">="
    REGEX = "=~"
    IN = "in"
    STARTS_WITH = "starts with"
    ENDS_WITH = "ends with"
    CONTAINS = "contains"
    ADD = "+"
    SUB = "-"
    MUL = "*"
    DIV = "/"
    MOD = "%"
    POW = "^"


class EgUnary(StrEnum):
    NOT = "not"
    NEG = "-"
    POS = "+"
