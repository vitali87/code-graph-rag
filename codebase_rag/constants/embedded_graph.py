# Embedded graph engine: an openCypher subset compiled to SQLite (issue #386).
#
# One table of nodes and one of relationships, each with a JSON property map.
# A node's label-unique key (NODE_UNIQUE_CONSTRAINTS) is lifted into its own
# column so MERGE and every `{qualified_name: $qn}` lookup are index seeks.

from enum import StrEnum

EG_ENGINE_DISPLAY_NAME = "embedded SQLite"
EG_DEFAULT_DB_FILENAME = "graph.sqlite"
EG_MEMORY_PATH = ":memory:"
# A writer waits this long for another process's write before failing.
EG_BUSY_TIMEOUT_MS = 60_000
EG_CACHE_SIZE_KIB = 262_144
EG_STATEMENT_CACHE_SIZE = 512
EG_COMPILE_CACHE_SIZE = 2048


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
    PROP = "prop"
    LABEL = "label"


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

EG_FUNC_COUNT = "count"
EG_FUNC_COLLECT = "collect"
EG_AGGREGATES: frozenset[str] = frozenset(
    {"count", "collect", "sum", "min", "max", "avg"}
)

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

# ----------------------------------------------------------------- storage

EG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cgr_nodes(
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    key TEXT,
    props TEXT NOT NULL DEFAULT '{}'
);
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
EG_PRAGMAS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=OFF",
    "PRAGMA temp_store=MEMORY",
)
EG_PRAGMA_BUSY = "PRAGMA busy_timeout={ms}"
EG_PRAGMA_CACHE = "PRAGMA cache_size=-{kib}"
# The planner's statistics are pinned, not measured. Measured on a graph
# that is still being written, they describe it at its smallest: a sync's
# first connection recorded a near-empty graph, after which every batch of
# relationship MERGEs scanned the batch once per node pair (14 s for 352
# rows). These are the statistics ANALYZE measured on a 38k-node,
# 166k-relationship cgr graph, the plans behind its 154-query parity run;
# the shape (unique keys, a few edges per node and type) holds at any size.
EG_PLANNER_STATISTICS: tuple[tuple[str, str, str], ...] = (
    ("cgr_nodes", "cgr_nodes_label", "38183 3819"),
    ("cgr_nodes", "cgr_nodes_key", "38183 1"),
    ("cgr_nodes", "cgr_nodes_label_key", "38183 3819 1"),
    ("cgr_edges", "cgr_edges_type", "166107 11074"),
    ("cgr_edges", "cgr_edges_src", "166107 6 4"),
    ("cgr_edges", "cgr_edges_dst", "166107 5 3"),
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
EG_SCHEMA_VERSION = "1"
EG_META_UPSERT = (
    "INSERT INTO cgr_meta(name, value) VALUES (?, ?) "
    "ON CONFLICT(name) DO UPDATE SET value = excluded.value"
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
EG_NODE_PROPERTIES = "properties"
EG_REL_TYPE = "type"
EG_REL_START = "start"
EG_REL_END = "end"
EG_PATH_NODES = "nodes"
EG_PATH_RELATIONSHIPS = "relationships"

# ----------------------------------------------------------------- errors

EG_ERR_SYNTAX = "Cypher syntax error at offset {pos}: {message}"
EG_ERR_UNEXPECTED = "unexpected {found!r}, expected {expected}"
EG_ERR_UNTERMINATED_STRING = "unterminated string literal"
EG_ERR_UNTERMINATED_COMMENT = "unterminated block comment"
EG_ERR_UNTERMINATED_IDENT = "unterminated backtick identifier"
EG_ERR_UNSUPPORTED = "unsupported in the embedded graph engine: {what}"
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
EG_ERR_VARLEN_VAR = "a variable-length relationship cannot be bound to a variable"
EG_ERR_LABEL_COUNT = "a node holds exactly one label; `{labels}` is not supported"
EG_ERR_YIELD = "procedure {name} has no output column {column}"
