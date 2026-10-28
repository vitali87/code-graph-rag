# Embedded graph engine: an openCypher subset compiled to SQLite (issue #386).
#
# One table of nodes and one of relationships, each with a JSON property map.
# A node's label-unique key (NODE_UNIQUE_CONSTRAINTS) is lifted into its own
# column so MERGE and every `{qualified_name: $qn}` lookup are index seeks.

from enum import Enum, StrEnum

EG_ENGINE_DISPLAY_NAME = "embedded SQLite"
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
EG_STATEMENT_CACHE_SIZE = 512
EG_COMPILE_CACHE_SIZE = 2048
# SQLite virtual-machine steps between checks of a statement's deadline:
# frequent enough to stop within milliseconds, rare enough to cost nothing.
EG_PROGRESS_STEPS = 10_000
EG_DEADLINE_VAR = "eg_deadline"
# The shortest wait while a timed-out regex lets the deadline's clock catch up.
EG_CLOCK_POLL_S = 0.001
EG_BYTES_PER_MB = 1024 * 1024
# The parameter the ingestor's `UNWIND $batch AS row` writes bind their rows to.
EG_BATCH_PARAM = "batch"
# In a live-variable set: every variable is live (`WITH *` / `RETURN *`).
EG_ALL_LIVE = "*"


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


class EgTok(StrEnum):
    IDENT = "ident"
    QUOTED_IDENT = "quoted_ident"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    PARAM = "param"
    PUNCT = "punct"
    EOF = "eof"


class EgKind(Enum):
    """The static types the compiler tracks a value by."""

    NODE = "node"
    REL = "rel"
    LIST = "list"
    MAP = "map"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    ANY = "any"
    NULL = "null"


class EgDecode(StrEnum):
    """How the executor turns one result column's SQL value into Python."""

    RAW = "raw"
    BOOL = "bool"
    JSON = "json"
    NULL = "null"


class EgParamMode(StrEnum):
    """How a parameter is bound: as-is, as JSON text, or as the exclusive
    upper bound of a STARTS WITH prefix range."""

    NATIVE = "native"
    JSON = "json"
    UPPER = "upper"


class EgForm(StrEnum):
    """The SQL form the write path evaluates an expression into."""

    RAW = "raw"
    NATIVE = "native"
    JSON = "json"


class EgKw(StrEnum):
    """Cypher keywords, in the upper case the parser compares them in."""

    MATCH = "MATCH"
    OPTIONAL = "OPTIONAL"
    WHERE = "WHERE"
    WITH = "WITH"
    RETURN = "RETURN"
    UNWIND = "UNWIND"
    UNION = "UNION"
    ALL = "ALL"
    CREATE = "CREATE"
    MERGE = "MERGE"
    SET = "SET"
    REMOVE = "REMOVE"
    DELETE = "DELETE"
    DETACH = "DETACH"
    FOREACH = "FOREACH"
    CALL = "CALL"
    YIELD = "YIELD"
    ORDER = "ORDER"
    BY = "BY"
    SKIP = "SKIP"
    LIMIT = "LIMIT"
    ON = "ON"
    AS = "AS"
    DISTINCT = "DISTINCT"
    ASC = "ASC"
    ASCENDING = "ASCENDING"
    DESC = "DESC"
    DESCENDING = "DESCENDING"
    EXPLAIN = "EXPLAIN"
    PROFILE = "PROFILE"
    SHOW = "SHOW"
    DROP = "DROP"
    INDEX = "INDEX"
    CONSTRAINT = "CONSTRAINT"
    CONSTRAINTS = "CONSTRAINTS"
    STORAGE = "STORAGE"
    IN = "IN"
    STARTS = "STARTS"
    ENDS = "ENDS"
    CONTAINS = "CONTAINS"
    IS = "IS"
    NOT = "NOT"
    NULL = "NULL"
    OR = "OR"
    XOR = "XOR"
    AND = "AND"
    TRUE = "TRUE"
    FALSE = "FALSE"
    CASE = "CASE"
    WHEN = "WHEN"
    THEN = "THEN"
    ELSE = "ELSE"
    END = "END"
    ANY = "ANY"
    NONE = "NONE"
    SINGLE = "SINGLE"
    SHORTESTPATH = "SHORTESTPATH"
    ALLSHORTESTPATHS = "ALLSHORTESTPATHS"


class EgPunct(StrEnum):
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    LBRACE = "{"
    RBRACE = "}"
    COMMA = ","
    DOT = "."
    COLON = ":"
    PIPE = "|"
    SEMICOLON = ";"
    EQ = "="
    LT = "<"
    GT = ">"
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    PERCENT = "%"
    CARET = "^"
    LE = "<="
    GE = ">="
    NE = "<>"
    BANG_EQ = "!="
    REGEX = "=~"
    PLUS_EQ = "+="
    RANGE = ".."


EG_PUNCTUATION_2: tuple[str, ...] = tuple(p.value for p in EgPunct if len(p.value) == 2)
EG_PUNCTUATION_1 = "".join(p.value for p in EgPunct if len(p.value) == 1)
# A `.` after one of these continues a value (`f(x).y`, `l[0].y`), so it is
# an accessor, never the start of a number such as `.5`.
EG_VALUE_CLOSERS: frozenset[str] = frozenset(
    {EgPunct.RPAREN, EgPunct.RBRACKET, EgPunct.RBRACE}
)
EG_LINE_COMMENT = "//"
EG_BLOCK_COMMENT_OPEN = "/*"
EG_BLOCK_COMMENT_CLOSE = "*/"
EG_NEWLINE = "\n"
EG_EMPTY = ""
EG_IDENT_EXTRA = "_"
EG_BACKTICK = "`"
EG_QUOTES = "'\""
EG_BACKSLASH = "\\"
EG_UNICODE_ESCAPE = "u"
EG_UNICODE_ESCAPE_LEN = 4
EG_HEX_BASE = 16
EG_PARAM_SIGIL = "$"
EG_EXPONENT = "eE"
EG_SIGNS = "+-"
EG_STRING_ESCAPES: dict[str, str] = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "\\": "\\",
    "'": "'",
    '"': '"',
}

# Reserved words: a clause boundary or operator, never a bare variable.
# Labels, relationship types, property keys and aliases may still use them
# (`(n:Union)`, `n.end`, `AS count`), so the parser only consults this set
# where an expression or clause is expected.
EG_CLAUSE_KEYWORDS: frozenset[str] = frozenset(
    {
        EgKw.MATCH,
        EgKw.OPTIONAL,
        EgKw.WHERE,
        EgKw.WITH,
        EgKw.RETURN,
        EgKw.UNWIND,
        EgKw.UNION,
        EgKw.CREATE,
        EgKw.MERGE,
        EgKw.SET,
        EgKw.REMOVE,
        EgKw.DELETE,
        EgKw.DETACH,
        EgKw.FOREACH,
        EgKw.CALL,
        EgKw.YIELD,
        EgKw.ORDER,
        EgKw.SKIP,
        EgKw.LIMIT,
        EgKw.ON,
    }
)
EG_EXPRESSION_STOP_WORDS: frozenset[str] = EG_CLAUSE_KEYWORDS | frozenset(
    {
        EgKw.AS,
        EgKw.THEN,
        EgKw.ELSE,
        EgKw.END,
        EgKw.WHEN,
        EgKw.ASC,
        EgKw.ASCENDING,
        EgKw.DESC,
        EgKw.DESCENDING,
        EgKw.BY,
    }
)
# What the parser says it expected, in its syntax errors.
EG_EXPECT_EXPRESSION = "an expression"
EG_EXPECT_NAME = "a name"
EG_EXPECT_INT = "an integer"
EG_EXPECT_CLAUSE = "a clause"
EG_EXPECT_PATTERN = "a pattern"
EG_EXPECT_SET_ITEM = "a SET item"
EG_EXPECT_SCHEMA = "an index or constraint statement"
EG_END_OF_QUERY = "end of query"


class EgFn(StrEnum):
    """Cypher functions the engine compiles, by the lower-cased name the
    parser records (Cypher function names are case-insensitive)."""

    ID = "id"
    ELEMENT_ID = "elementid"
    LABELS = "labels"
    TYPE = "type"
    PROPERTIES = "properties"
    KEYS = "keys"
    START_NODE = "startnode"
    END_NODE = "endnode"
    COALESCE = "coalesce"
    TO_LOWER = "tolower"
    TO_UPPER = "toupper"
    LOWER = "lower"
    UPPER = "upper"
    TRIM = "trim"
    LTRIM = "ltrim"
    RTRIM = "rtrim"
    REPLACE = "replace"
    SUBSTRING = "substring"
    LEFT = "left"
    RIGHT = "right"
    SIZE = "size"
    LENGTH = "length"
    HEAD = "head"
    LAST = "last"
    TAIL = "tail"
    RANGE = "range"
    SPLIT = "split"
    REVERSE = "reverse"
    TO_STRING = "tostring"
    TO_INTEGER = "tointeger"
    TO_INT = "toint"
    TO_FLOAT = "tofloat"
    TO_BOOLEAN = "toboolean"
    ABS = "abs"
    CEIL = "ceil"
    FLOOR = "floor"
    ROUND = "round"
    SQRT = "sqrt"
    SIGN = "sign"
    LOG = "log"
    EXP = "exp"
    EXISTS = "exists"
    TIMESTAMP = "timestamp"
    RAND = "rand"
    NODES = "nodes"
    RELATIONSHIPS = "relationships"
    COUNT = "count"
    COLLECT = "collect"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    AVG = "avg"


EG_FUNC_COUNT = EgFn.COUNT
EG_FUNC_COLLECT = EgFn.COLLECT
EG_AGGREGATES: frozenset[str] = frozenset(
    {EgFn.COUNT, EgFn.COLLECT, EgFn.SUM, EgFn.MIN, EgFn.MAX, EgFn.AVG}
)
# Functions whose value differs between calls, so never hoisted as constant.
EG_VOLATILE_FUNCTIONS: frozenset[str] = frozenset({EgFn.RAND, EgFn.TIMESTAMP})


class EgSqlFn(StrEnum):
    """SQL functions compiled Cypher calls: SQLite builtins, and the `eg_`
    functions `embedded_graph.functions` registers on every connection."""

    LOWER = "lower"
    UPPER = EgFn.UPPER.value
    TRIM = "trim"
    LTRIM = "ltrim"
    RTRIM = "rtrim"
    REPLACE = EgFn.REPLACE.value
    ABS = "abs"
    COALESCE = "coalesce"
    SUBSTR = "substr"
    LENGTH = "length"
    JSON_ARRAY_LENGTH = "json_array_length"
    REGEX = "eg_regex"
    LIST_ADD = "eg_list_add"
    CONCAT = "eg_concat"
    ADD = "eg_add"
    POW = "eg_pow"
    MOD = "eg_mod"
    SIZE = "eg_size"
    SLICE = "eg_slice"
    STR_SLICE = "eg_str_slice"
    RANGE = "eg_range"
    SPLIT = "eg_split"
    REVERSE_STR = "eg_reverse_str"
    REVERSE_LIST = "eg_reverse_list"
    TO_STRING = "eg_to_string"
    TO_INTEGER = "eg_to_integer"
    TO_FLOAT = "eg_to_float"
    TO_BOOLEAN = "eg_to_boolean"
    CEIL = "eg_ceil"
    FLOOR = "eg_floor"
    ROUND = "eg_round"
    SQRT = "eg_sqrt"
    LOG = "eg_log"
    EXP = "eg_exp"
    SIGN = "eg_sign"
    TIMESTAMP = "eg_timestamp"
    PATHS_DISJOINT = "eg_paths_disjoint"
    # Registered by native.py, on a file's connections, where the Rust
    # extension walks variable-length relationships.
    WALK = "eg_walk"


# JSON's spelling of the booleans, which `eg_` functions read and write.
EG_JSON_TRUE = "true"
EG_JSON_FALSE = "false"
# Separates the edge ids in a variable-length walk's path, as `,1,7,`.
EG_PATH_SEPARATOR = ","
# Compact JSON, as SQLite's own json functions write it.
EG_JSON_SEPARATORS = (",", ":")


class EgProcCol(StrEnum):
    """Output columns of the procedures, named as MAGE names them."""

    NODE = "node"
    RANK = "rank"
    DEGREE = "degree"
    COMPONENT_ID = "component_id"
    COMPONENTS = "components"
    CYCLES = "cycles"
    CYCLE = "cycle"
    NODES = "nodes"
    PATH = "path"
    BETWEENNESS = "betweenness_centrality"
    ANCESTORS = "ancestors"
    DESCENDANTS = "descendants"


# degree_centrality.get's default mode, which counts both directions.
EG_DEGREE_UNDIRECTED = "undirected"
# The optional Rust extension and the one function the engine calls in it.
EG_RUST_MODULE = "cgr_graph_rs"
EG_RUST_PARSE_CYPHER = "parse_cypher"
# Its parts native.py uses: the long-lived reader and writer of a graph file,
# and the result-row decoder.
EG_RUST_READER = "Reader"
EG_RUST_WRITER = "Writer"
EG_RUST_DECODE_ROWS = "decode_rows"
# The tree walks the indexer hands it (parsers/native_trees.py), and what
# they raise for a node they cannot read exactly.
EG_RUST_TREE_BRIDGE = "TreeBridge"
EG_RUST_NOT_BORROWED = "NotBorrowed"
# Bridge methods a build from before the scope walks, or from before the
# interface fingerprint, lacks.
EG_RUST_SCOPE_FACTS = "scope_facts"
EG_RUST_INTERFACE_FINGERPRINT = "interface_fingerprint"


class TreeBridgeCheck(StrEnum):
    """What the probe compares through both runtimes before the bridge runs."""

    LAYOUT = "node layout"
    ANCHOR_HASH = "anchor hash"
    FINGERPRINT = "fingerprint"
    SCOPE_FACTS = "scope facts"
    INNER_SCOPE_NAMES = "inner scope names"
    INTERFACE_FINGERPRINT = "interface fingerprint"


# The probe: definitions the two hashes walk, decorators, comments, f-strings,
# non-ASCII text (byte and column offsets part ways there), every binding
# form the scope walks read, bodies the interface fingerprint keeps whole and
# one it summarises by its calls and returns (`relay`), and a syntax error
# (ERROR and MISSING nodes).
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

def relay(r, cb):
    handler = make()
    r.register(handler, key=cb.run)
    Spec(handler=helper, retries=3)
    def later():
        return 1
    if r:
        return r.build()
    return [r.done]

def broken(x:
    return x +
""".encode()
# Property types as the node and relationship schemas declare them.
EG_SCHEMA_STRING = EgKind.STRING.value
EG_SCHEMA_INT = EgKind.INT.value
EG_SCHEMA_BOOLEAN = "boolean"
EG_SCHEMA_FLOAT = EgKind.FLOAT.value
EG_SCHEMA_LIST_OPEN = "list["
EG_SCHEMA_LIST_CLOSE = "]"
# How SQLite's version prints, for the version error.
EG_VERSION_SEPARATOR = "."

# Procedures the engine serves to generated queries. Named after the MAGE
# procedures the Memgraph prompt already teaches, so a model's habits carry
# over; each family prefix is on CYPHER_ALLOWED_PROCEDURE_PREFIXES.
EG_PROC_PAGERANK = "pagerank.get"
EG_PROC_DEGREE = "degree_centrality.get"
EG_PROC_WCC = "weakly_connected_components.get"
EG_PROC_SCC = "nxalg.strongly_connected_components"
EG_PROC_CYCLES = "nxalg.simple_cycles"
EG_PROC_FIND_CYCLE = "nxalg.find_cycle"
EG_PROC_TOPOSORT = "nxalg.topological_sort"
EG_PROC_SHORTEST_PATH = "nxalg.shortest_path"
EG_PROC_BETWEENNESS = "betweenness_centrality.get"
EG_PROC_ANCESTORS = "graph_util.ancestors"
EG_PROC_DESCENDANTS = "graph_util.descendants"

# Plan operator names reported to the read-only guard. Every read is named
# after an operator already on CYPHER_PLAN_READ_OPERATORS; every write after
# one that is not, so `check_plan` refuses it without a second allowlist.
EG_PLAN_SCAN = "ScanAll"
EG_PLAN_EXPAND = "Expand"
EG_PLAN_EXPAND_VARIABLE = "ExpandVariable"
EG_PLAN_OPTIONAL = "Optional"
EG_PLAN_UNWIND = "Unwind"
EG_PLAN_PRODUCE = "Produce"
EG_PLAN_FILTER = "Filter"
EG_PLAN_UNION = "Union"
EG_PLAN_CALL = "CallProcedure"
EG_PLAN_CREATE = "CreateNode"
EG_PLAN_MERGE = "Merge"
EG_PLAN_SET = "SetProperties"
EG_PLAN_REMOVE = "RemoveProperties"
EG_PLAN_DELETE = "Delete"
EG_PLAN_FOREACH = "Foreach"
EG_PLAN_SCHEMA = "SchemaChange"
# One EXPLAIN row, as Memgraph prints an operator.
EG_PLAN_ROW = " * {op}"
EG_PLAN_CALL_OF = EG_PLAN_CALL + "<{procedure}>"
# SHOW STORAGE INFO rows the health check reads, in Memgraph's names.
EG_STORAGE_VERTEX_COUNT = "vertex_count"
EG_STORAGE_EDGE_COUNT = "edge_count"
# Appended to a STARTS WITH prefix, the exclusive upper bound of the range
# of strings that start with it: no code point sorts after U+10FFFF.
EG_PREFIX_UPPER_BOUND = "\U0010ffff"

# ----------------------------------------------------------------- storage

# AUTOINCREMENT, so a deleted node's id is never handed out again, as
# Memgraph never reuses one. Vector stores key embeddings by node id, and an
# incremental run without embeddings leaves a deleted function's vector in
# place: a reused id would make that vector find whatever node took the id.
EG_NODES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    key TEXT,
    props TEXT NOT NULL DEFAULT '{{}}'
);"""
EG_SCHEMA_SQL = (
    EG_NODES_TABLE_SQL.format(table="cgr_nodes")
    + """
CREATE UNIQUE INDEX IF NOT EXISTS cgr_nodes_label_key ON cgr_nodes(label, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS cgr_nodes_key ON cgr_nodes(key);
CREATE INDEX IF NOT EXISTS cgr_nodes_label ON cgr_nodes(label);
CREATE TABLE IF NOT EXISTS cgr_edges(
    id INTEGER PRIMARY KEY,
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    type TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS cgr_edges_src ON cgr_edges(src, type);
CREATE INDEX IF NOT EXISTS cgr_edges_dst ON cgr_edges(dst, type);
CREATE INDEX IF NOT EXISTS cgr_edges_type ON cgr_edges(type);
CREATE TABLE IF NOT EXISTS cgr_meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
)
# Its node and relationship tables, as sqlite_stat1 rows name them.
EG_NODES_TABLE = "cgr_nodes"
EG_EDGES_TABLE = "cgr_edges"
EG_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=OFF",
    "PRAGMA temp_store=MEMORY",
)
EG_PRAGMA_BUSY = "PRAGMA busy_timeout={ms}"
EG_PRAGMA_CACHE = "PRAGMA cache_size=-{kib}"
# Process-wide: SQLite fails an allocation past it with SQLITE_NOMEM rather
# than letting one runaway query take the machine's memory.
EG_PRAGMA_HEAP_LIMIT = "PRAGMA hard_heap_limit={bytes}"
# Peak bytes per item while range() builds and serializes its list: 47
# measured for small ints, rounded up for large ones and their longer text.
EG_RANGE_ITEM_BYTES = 64
# The planner's statistics are pinned, not measured. Measured on a graph
# that is still being written, they describe it at its smallest: a sync's
# first connection recorded a near-empty graph, after which every batch of
# relationship MERGEs scanned the batch once per node pair (14 s for 352
# rows). These are the statistics ANALYZE measured on a 38k-node,
# 166k-relationship cgr graph, the plans behind its 154-query parity run;
# the shape (unique keys, a few edges per node and type) holds at any size.
EG_PLANNER_STATISTICS: tuple[tuple[str, str, str], ...] = (
    (EG_NODES_TABLE, "cgr_nodes_label", "38183 3819"),
    (EG_NODES_TABLE, "cgr_nodes_key", "38183 1"),
    (EG_NODES_TABLE, "cgr_nodes_label_key", "38183 3819 1"),
    (EG_EDGES_TABLE, "cgr_edges_type", "166107 11074"),
    (EG_EDGES_TABLE, "cgr_edges_src", "166107 6 4"),
    (EG_EDGES_TABLE, "cgr_edges_dst", "166107 5 3"),
)
# ANALYZE of an empty table creates sqlite_stat1 and writes nothing to it.
EG_SQL_CREATE_STATS = "ANALYZE cgr_meta"
EG_SQL_CLEAR_STATS = "DELETE FROM sqlite_stat1 WHERE tbl IN ('cgr_nodes', 'cgr_edges')"
EG_SQL_INSERT_STAT = "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?, ?, ?)"
# Makes this connection's planner read sqlite_stat1 again.
EG_SQL_RELOAD_STATS = "ANALYZE sqlite_schema"
EG_SQL_JSON_PROBE = "SELECT json_valid('[]')"
EG_URI_READ_ONLY = "?mode=ro"
EG_PROP_INDEX_NAME = "cgr_nodes_prop_{prop}"
EG_PROP_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS {name} ON cgr_nodes(label, json_extract(props, {path}))"
)
EG_META_SCHEMA_VERSION = "schema_version"
EG_SCHEMA_VERSION = "2"
# Rebuilding a node table created before AUTOINCREMENT (schema 1): the new
# table is filled under a scratch name, then takes the old one's name and
# indexes. Copying the ids keeps every edge and stored vector pointing where
# it did; SQLite then starts the sequence after the largest copied id.
EG_AUTOINCREMENT = "AUTOINCREMENT"
EG_NODES_REBUILD_TABLE = "cgr_nodes_rebuild"
EG_SQL_TABLE_DDL = "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?"
EG_SQL_TABLE_INDEXES = (
    "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? "
    "AND sql IS NOT NULL"
)
EG_SQL_COPY_NODES = (
    "INSERT INTO {dst}(id, label, key, props) SELECT id, label, key, props FROM {src}"
)
EG_SQL_DROP_TABLE = "DROP TABLE {table}"
EG_SQL_RENAME_TABLE = "ALTER TABLE {src} RENAME TO {dst}"
EG_META_UPSERT = (
    "INSERT INTO cgr_meta(name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value"
)
# A counter every transaction that changes the graph bumps once (the
# executor's and the Rust extension's writes; sql.BUMP_GRAPH_VERSION). The
# extension reads on its own connection, which another process's commit can
# leave on a newer graph than the calling statement's; it reads only where
# the counter equals the one the statement's own transaction sees (native.py).
# The seed spells the row's name out; a test pins it to this one.
EG_META_GRAPH_VERSION = "graph_version"
EG_SQL_SEED_GRAPH_VERSION = (
    "INSERT OR IGNORE INTO cgr_meta(name, value) VALUES ('graph_version', '0')"
)
EG_ALIAS_SPACE_TEMP = "temp"
EG_SQLITE_TEMP_DATABASE = "temp"
EG_SQLITE_MAIN_DATABASE = "main"

# Plan/introspection column names, as Memgraph reports them.
EG_COL_QUERY_PLAN = "QUERY PLAN"
EG_COL_LABEL = "label"
EG_COL_PROPERTIES = "properties"
EG_COL_INDEX_TYPE = "index type"
EG_COL_PROPERTY = "property"
EG_COL_COUNT = "count"
EG_COL_STORAGE_NAME = "storage info"
EG_COL_STORAGE_VALUE = "value"

# Value shapes a node or relationship takes in a result row.
EG_NODE_ID = "id"
EG_NODE_LABELS = "labels"
EG_NODE_PROPERTIES = EgFn.PROPERTIES.value
EG_REL_TYPE = "type"
EG_REL_START = "start"
EG_REL_END = "end"
EG_PATH_NODES = EgFn.NODES.value
EG_PATH_RELATIONSHIPS = "relationships"

# ----------------------------------------------------------------- errors

EG_ERR_SYNTAX = "Cypher syntax error at offset {pos}: {message}"
EG_ERR_RANGE_TOO_LARGE = "range() of {count} items exceeds the query memory limit"
EG_ERR_UNEXPECTED = "unexpected {found!r}, expected {expected}"
EG_ERR_UNTERMINATED_STRING = "unterminated string literal"
EG_ERR_UNTERMINATED_COMMENT = "unterminated block comment"
EG_ERR_UNTERMINATED_IDENT = "unterminated backtick identifier"
EG_ERR_UNSUPPORTED = "unsupported in the embedded graph engine: {what}"
EG_WHAT_PATH_VARIABLES = "path variables"
EG_WHAT_PARAM_PATTERN_PROPS = "parameter property maps in patterns"
EG_WHAT_VARLEN_ROW_PROPS = (
    "variable-length relationship properties that read a variable"
)
EG_WHAT_READ_WITHOUT_RETURN = "a read query without RETURN"
EG_WHAT_UNION_UPDATES = "UNION with updating clauses"
EG_WHAT_REMOVE_LABEL = "REMOVE of a label"
EG_WHAT_MERGE_PATH = "MERGE of a path with unbound nodes"
EG_WHAT_MERGE_REL = "MERGE of this relationship"
EG_WHAT_CREATE_REL = "CREATE of this relationship"
EG_WHAT_PATH_FUNCTIONS = "path functions"
EG_ERR_UNBOUND = "variable `{name}` is not defined"
EG_ERR_MISSING_PARAM = "parameter ${name} was not supplied"
EG_ERR_UNKNOWN_FUNCTION = "unknown function {name}()"
EG_ERR_UNKNOWN_PROCEDURE = "unknown procedure {name}"
EG_ERR_DELETE_ATTACHED = (
    "cannot delete node {id}: it still has relationships; use DETACH DELETE"
)
EG_ERR_MERGE_NULL = "cannot MERGE a pattern whose property {key} is null"
EG_ERR_READ_ONLY = "the read-only connection refused a write"
EG_ERR_SQLITE_VERSION = (
    "the embedded graph needs SQLite {need} with JSON support; this Python links "
    "SQLite {have}. Use a Python built with a newer SQLite (uv-managed Pythons are), "
    "or set GRAPH_BACKEND=memgraph."
)
EG_ERR_PARAMS_MAPPING = (
    "embedded graph parameters must be a mapping, not {kind}; batched writes "
    "pass {{'batch': rows}}"
)
EG_ERR_NOT_ENTITY = "expected a node or relationship id, got {value!r}"
EG_ERR_NOT_JSON = "expected a JSON value, got {value!r}"
EG_ERR_CLOSED = "the embedded graph connection is closed"
EG_ERR_NOT_NODE = "expected a node for `{name}`"
EG_ERR_NOT_REL = "expected a relationship for `{name}`"
EG_ERR_TIMEOUT = "the query ran past its time limit and was stopped"
EG_ERR_VARLEN_VAR = "a variable-length relationship cannot be bound to a variable"
EG_ERR_LABEL_COUNT = "a node holds exactly one label; `{labels}` is not supported"
EG_ERR_YIELD = "procedure {name} has no output column {column}"
