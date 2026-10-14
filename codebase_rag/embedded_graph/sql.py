"""The SQL the embedded graph engine emits, in one place.

This is the engine's query-text module, as `cypher_queries.py` is the rest of
cgr's: every SQL keyword, table and column name the compilers and the
executor put into a statement is spelled here, and they assemble statements
only through these names and builders. Values never pass through here:
whatever a query supplies reaches SQLite as a bound parameter.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..constants.embedded_graph import (
    EG_META_GRAPH_VERSION,
    EG_NODE_ID,
    EG_NODE_LABELS,
    EG_NODE_PROPERTIES,
    EG_REL_END,
    EG_REL_START,
    EG_REL_TYPE,
    EgDirection,
    EgSqlFn,
)

# ------------------------------------------------------------------ schema

NODES = "cgr_nodes"
EDGES = "cgr_edges"
ID = "id"
LABEL = "label"
KEY = "key"
PROPS = "props"
SRC = "src"
DST = "dst"
TYPE = "type"
ROWID = "rowid"
# json_each's columns.
VALUE = "value"
JSON_TYPE = "type"

# ------------------------------------------------------- generated names

# Row aliases: the input binding table, an OPTIONAL MATCH's matched rows,
# a walk's seed node, one edge and one queue row of a recursive walk.
INPUT = "p"
OPTIONAL = "o"
SEED = "s"
EDGE = "e"
QUEUE = "v"
# Prefixes the compiler numbers fresh names from.
STAGE_PREFIX = "eg_s"
COLUMN_PREFIX = "c"
NODE_PREFIX = "n"
REL_PREFIX = "r"
WALK_PREFIX = "eg_vl"
WALK_ROW_PREFIX = "v"
EACH_PREFIX = "je"
PARAM_PREFIX = "p"
PARAM_SIGIL = ":"
TEMP_PREFIX = "eg_t"
TEMP_SCHEMA = "temp"
# A recursive walk's columns: where it started, where it is, how far it
# went, and the ids of the edges it took, comma-delimited.
WALK_SEED = "seed"
WALK_NODE = "node"
WALK_DEPTH = "depth"
WALK_PATH = "path"
WALK_PATH_SEP = "','"

NOTHING = ""
ONE = "1"
ZERO = "0"
NULL = "NULL"
ALL_ROWS = "-1"
NO_ROWS_SKIPPED = "0"
# The placeholder column of a binding table that binds no variables.
ONE_COLUMN_NAME = "c_one"
ONE_COLUMN = f"1 AS {ONE_COLUMN_NAME}"
COMMA = ", "
STAR = "*"
UNION_PREFIX = "u"


# --------------------------------------------------------------- fragments


def column(alias: str, name: str) -> str:
    return f"{alias}.{name}"


def aliased(expr: str, name: str) -> str:
    return f"{expr} AS {name}"


def table(name: str, alias: str) -> str:
    return f"{name} AS {alias}"


def qualified(schema: str, name: str) -> str:
    return f"{schema}.{name}"


def commas(parts: Iterable[str]) -> str:
    return COMMA.join(parts)


def paren(sql: str) -> str:
    return f"({sql})"


def all_of(conds: Iterable[str]) -> str:
    return " AND ".join(f"({c})" for c in conds)


def where(conds: list[str]) -> str:
    return f" WHERE {all_of(conds)}" if conds else NOTHING


def eq(left: str, right: str) -> str:
    return f"{left} = {right}"


def ne(left: str, right: str) -> str:
    return f"{left} <> {right}"


def at_least(left: str, right: str) -> str:
    return f"{left} >= {right}"


def below(left: str, right: str) -> str:
    return f"{left} < {right}"


def is_same(left: str, right: str) -> str:
    """Equality where NULL equals NULL, for re-joining a row to itself."""
    return f"{left} IS {right}"


def sql_str(value: str) -> str:
    """A SQL string literal for text the compiler itself produced."""
    return "'" + value.replace("'", "''") + "'"


def name_list(names: tuple[str, ...]) -> str:
    return paren(commas(sql_str(n) for n in names))


def is_in(expr: str, names: tuple[str, ...]) -> str:
    return f"{expr} IN {name_list(names)}"


def json_path(key: str) -> str:
    return sql_str('$."' + key.replace('"', '\\"') + '"')


def json_extract(doc: str, path: str) -> str:
    return f"json_extract({doc}, {path})"


def prop_of(alias: str, key: str) -> str:
    """Property `key` of the joined node or relationship row `alias`."""
    return json_extract(column(alias, PROPS), json_path(key))


def json_each(doc: str, alias: str) -> str:
    return f"json_each({doc}) AS {alias}"


# ------------------------------------------------------------------ select


def select(
    columns: Iterable[str],
    source: str,
    tail: str = NOTHING,
    *,
    distinct: bool = False,
) -> str:
    listed = commas(columns) or ONE_COLUMN
    return f"SELECT {'DISTINCT ' if distinct else ''}{listed} FROM {source}{tail}"


def select_constant() -> str:
    return f"SELECT {ONE_COLUMN}"


def select_value(expr: str, name: str) -> str:
    return f"SELECT {aliased(expr, name)}"


def scalar(column_name: str, source: str) -> str:
    """A single-row stage's value, read where a later stage needs it."""
    return paren(select([column_name], source))


def group_by(keys: list[str]) -> str:
    return f" GROUP BY {commas(keys)}"


# Grouping keys over no rows give no groups, unlike a bare aggregate.
NONEMPTY_GROUPS = " HAVING count(*) > 0"


def order_by(keys: list[str]) -> str:
    return f" ORDER BY {commas(keys)}" if keys else NOTHING


def ascending(key: str) -> str:
    return f"{key} ASC NULLS LAST"


def descending(key: str) -> str:
    return f"{key} DESC NULLS FIRST"


def limit(count: str, skip: str) -> str:
    return f" LIMIT {count} OFFSET {skip}"


# A no-op LIMIT: SQLite never flattens a subquery that has one into the
# query that reads it.
UNFLATTENED = " LIMIT -1"


def left_join(left: str, right: str, on: str) -> str:
    return f"{left} LEFT JOIN {right} ON {on}"


def cte(name: str, body: str) -> str:
    return f"{name} AS ({body})"


def with_ctes(ctes: Iterable[str]) -> str:
    listed = ",\n".join(ctes)
    return f"WITH RECURSIVE {listed}\n" if listed else NOTHING


# ------------------------------------------------------------- relationships


def outgoing(alias: str, left: str, right: str) -> str:
    return f"{alias}.{SRC} = {left} AND {alias}.{DST} = {right}"


def either_way(alias: str, left: str, right: str) -> str:
    # A self-loop matches once, not once in each direction.
    return (
        f"(({alias}.{SRC} = {left} AND {alias}.{DST} = {right}) OR "
        f"({alias}.{DST} = {left} AND {alias}.{SRC} = {right} "
        f"AND {alias}.{SRC} <> {alias}.{DST}))"
    )


# --------------------------------------------------------- variable length


def walk_columns(name: str) -> str:
    return f"{name}({WALK_SEED}, {WALK_NODE}, {WALK_DEPTH}, {WALK_PATH})"


def walk_far_end(direction_out: bool | None) -> str:
    """The node an edge leads to from the queue row: by direction, or either."""
    if direction_out is True:
        return column(EDGE, DST)
    if direction_out is False:
        return column(EDGE, SRC)
    return (
        f"(CASE WHEN {column(EDGE, SRC)} = {column(QUEUE, WALK_NODE)} "
        f"THEN {column(EDGE, DST)} ELSE {column(EDGE, SRC)} END)"
    )


def walk_join(
    direction_out: bool | None, types: tuple[str, ...], edge_conds: list[str]
) -> str:
    here = column(QUEUE, WALK_NODE)
    if direction_out is True:
        join = eq(column(EDGE, SRC), here)
    elif direction_out is False:
        join = eq(column(EDGE, DST), here)
    else:
        join = f"({eq(column(EDGE, SRC), here)} OR {eq(column(EDGE, DST), here)})"
    if types:
        join += f" AND {is_in(column(EDGE, TYPE), types)}"
    for cond in edge_conds:
        join += f" AND {cond}"
    return join


def walk(
    name: str,
    seeds: str,
    direction_out: bool | None,
    types: tuple[str, ...],
    high: int | None,
    edge_conds: list[str],
) -> str:
    """A recursive CTE of every path from `seeds`, one row per path.

    `edge_conds` read the stepped edge as `EDGE`; every edge of a path must
    pass them. Each row keeps the ids of the edges it took, so no edge
    repeats in a path, as Cypher requires. The queue row is CROSS JOINed first: SQLite
    then looks up the edges leaving that one node instead of scanning every
    edge of the type for it, a plan it otherwise picked without statistics.
    """
    edge_id = column(EDGE, ID)
    depth = column(QUEUE, WALK_DEPTH)
    path = column(QUEUE, WALK_PATH)
    bound = f"{below(depth, str(high))} AND " if high is not None else ""
    return (
        f"SELECT {ID}, {ID}, 0, {WALK_PATH_SEP} FROM ({seeds}) "
        f"UNION ALL SELECT {column(QUEUE, WALK_SEED)}, {walk_far_end(direction_out)}, "
        f"{depth} + 1, {path} || {edge_id} || {WALK_PATH_SEP} "
        f"FROM {table(name, QUEUE)} CROSS JOIN {table(EDGES, EDGE)} "
        f"ON {walk_join(direction_out, types, edge_conds)} "
        f"WHERE {bound}{path_avoids(path, edge_id)}"
    )


def native_walk(
    name: str,
    seeds: str,
    direction_out: bool | None,
    types: tuple[str, ...],
    high: int | None,
) -> str:
    """`walk`'s rows, path included, from the Rust extension (native.py).

    The seeds go in as one JSON array with the graph version this statement
    reads, and the rows come back as JSON `[[seed, node, depth, path], ...]`.
    They stay a recursive CTE whose step adds none: SQLite then plans the
    MATCH as it plans `walk`'s, taking the rows for a large table and
    indexing them for the join; a plain `json_each` reads as 25 rows and was
    scanned once per outer row (7 s for one project's ownership walk).
    """
    if direction_out is None:
        direction = EgDirection.BOTH
    else:
        direction = EgDirection.OUT if direction_out else EgDirection.IN
    seed_ids = paren(select([call(JSON_GROUP_ARRAY, ID)], paren(seeds)))
    args = [
        seed_ids,
        paren(GRAPH_VERSION),
        sql_str(direction),
        NULL if high is None else str(high),
        *(sql_str(t) for t in types),
    ]
    rows = json_each(call(EgSqlFn.WALK, *args), EACH_PREFIX)
    element = column(EACH_PREFIX, VALUE)
    picked = [json_extract(element, sql_str(f"$[{i}]")) for i in range(4)]
    columns = [WALK_SEED, WALK_NODE, WALK_DEPTH, WALK_PATH]
    return f"{select(picked, rows)} UNION ALL {select(columns, name)} WHERE {ZERO}"


def path_avoids(path: str, edge_id: str) -> str:
    """Whether a walk's comma-delimited edge list leaves out `edge_id`."""
    return f"instr({path}, {WALK_PATH_SEP} || {edge_id} || {WALK_PATH_SEP}) = 0"


def paths_disjoint(left: str, right: str) -> str:
    return call(EgSqlFn.PATHS_DISJOINT, left, right)


def all_node_ids() -> str:
    return select([ID], NODES)


def seed_ids(conds: list[str]) -> str:
    return select([aliased(column(SEED, ID), ID)], table(NODES, SEED), where(conds))


def input_seed(expr: str) -> str:
    return select_value(expr, ID)


def input_seeds(col: str, stage: str) -> str:
    return select([aliased(col, ID)], stage, distinct=True)


# ---------------------------------------------------------- JSON conversions


def json_of_scalar(value: str) -> str:
    """A native value as JSON text; SQL NULL stays NULL, not `'null'`."""
    return f"nullif(json_quote({value}), 'null')"


def json_of_bool(value: str) -> str:
    """A 0/1 boolean as JSON text, which json_quote would leave a number."""
    return (
        f"(CASE WHEN {value} IS NULL THEN NULL WHEN {value} THEN 'true' "
        f"ELSE 'false' END)"
    )


def union(selects: list[str], all_rows: tuple[bool, ...]) -> str:
    sql = selects[0]
    for keep_all, select_sql in zip(all_rows, selects[1:]):
        sql += f" UNION {'ALL ' if keep_all else ''}{select_sql}"
    return sql


# ------------------------------------------------------------- transactions

# IMMEDIATE takes the write lock up front, so two writers queue on the busy
# timeout instead of one failing to upgrade a read lock.
BEGIN_WRITE = "BEGIN IMMEDIATE"
BEGIN_READ = "BEGIN"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"

# A counter every transaction that changes the graph bumps once, here and
# in the Rust extension's writes; the extension reads only where it equals
# what the calling statement's own transaction reads (native.py).
GRAPH_VERSION = (
    f"SELECT CAST(value AS TEXT) FROM cgr_meta WHERE name = '{EG_META_GRAPH_VERSION}'"
)
BUMP_GRAPH_VERSION = (
    f"INSERT INTO cgr_meta(name, value) VALUES ('{EG_META_GRAPH_VERSION}', '1') "
    "ON CONFLICT(name) DO UPDATE SET value = CAST(value AS INTEGER) + 1"
)

# ------------------------------------------------------------ temp tables


def temp_table(name: str) -> str:
    return qualified(TEMP_SCHEMA, name)


def is_temp_table(name: str) -> bool:
    return name.startswith(temp_table(NOTHING))


def create_temp_as(name: str, query: str) -> str:
    return f"CREATE TEMP TABLE {name} AS {query}"


def create_temp(name: str, columns: list[str]) -> str:
    return f"CREATE TEMP TABLE {name} ({commas(columns)})"


def drop_temp(name: str) -> str:
    return f"DROP TABLE IF EXISTS {temp_table(name)}"


def insert_row(name: str, width: int) -> str:
    return f"INSERT INTO {name} VALUES ({commas('?' for _ in range(width))})"


def insert_constant(name: str) -> str:
    return f"INSERT INTO {name} VALUES ({ONE})"


def count_rows(source: str) -> str:
    return select(["count(*)"], source)


def in_row_order(columns: list[str], source: str, alias: str | None = None) -> str:
    """Rows of a binding table in insertion order, which the writes rely on
    to line each row up with the values evaluated for it."""
    if alias is None:
        return select(columns, source, order_by([ROWID]))
    return select(columns, table(source, alias), order_by([column(alias, ROWID)]))


def filtered_copy(name: str, source: str, cond: str) -> str:
    return create_temp_as(name, select([STAR], table(source, INPUT), where([cond])))


# --------------------------------------------------------- fixed statements
# Bound positionally; the write path, SHOW commands and procedures run them.

COUNT_NODES = count_rows(NODES)
COUNT_EDGES = count_rows(EDGES)
LIST_NODE_INDEXES = (
    f"SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = '{NODES}'"
)
INSERT_NODE = f"INSERT INTO {NODES}({LABEL}, {KEY}, {PROPS}) VALUES (?, ?, ?)"
INSERT_EDGE = f"INSERT INTO {EDGES}({SRC}, {DST}, {TYPE}, {PROPS}) VALUES (?, ?, ?, ?)"
NODE_LABEL = f"SELECT {LABEL} FROM {NODES} WHERE {ID} = ?"
DELETE_NODE = f"DELETE FROM {NODES} WHERE {ID} = ?"
DELETE_EDGE = f"DELETE FROM {EDGES} WHERE {ID} = ?"
DETACH_NODE = f"DELETE FROM {EDGES} WHERE {SRC} = ? OR {DST} = ?"
NODE_HAS_EDGES = f"SELECT 1 FROM {EDGES} WHERE {SRC} = ? OR {DST} = ? LIMIT 1"
FIND_NODE_BY_KEY = f"SELECT {ID} FROM {NODES} WHERE {LABEL} = ? AND {KEY} = ?"
# What the procedures read; ordered by id so every run, Python or Rust,
# visits nodes and edges in the same order and gives the same floats.
NODE_IDS_IN_ORDER = f"SELECT {ID} FROM {NODES} ORDER BY {ID}"
EDGES_IN_ORDER = f"SELECT {ID}, {SRC}, {DST} FROM {EDGES} ORDER BY {ID}"
EDGE_ENDS = f"SELECT {SRC}, {DST} FROM {EDGES}"
SOURCES_OF = f"SELECT {SRC} FROM {EDGES} WHERE {DST} = ?"
TARGETS_OF = f"SELECT {DST} FROM {EDGES} WHERE {SRC} = ?"


def refresh_key(constraints: dict[str, str]) -> str:
    """Re-derive a node's `key` column after its properties changed."""
    cases = " ".join(
        f"WHEN {sql_str(label)} THEN {json_extract(PROPS, json_path(prop))}"
        for label, prop in constraints.items()
    )
    return (
        f"UPDATE {NODES} SET {KEY} = CASE {LABEL} {cases} ELSE {KEY} END WHERE {ID} = ?"
    )


def remove_prop(target: str, key: str) -> str:
    return f"UPDATE {target} SET {PROPS} = json_remove({PROPS}, {json_path(key)}) WHERE {ID} = ?"


def set_prop(target: str, key: str) -> str:
    return f"UPDATE {target} SET {PROPS} = json_set({PROPS}, {json_path(key)}, json(?)) WHERE {ID} = ?"


def merge_props(target: str) -> str:
    return f"UPDATE {target} SET {PROPS} = json_patch({PROPS}, ?) WHERE {ID} = ?"


def replace_props(target: str) -> str:
    # json_patch onto an empty object keeps only the keys whose new value
    # is not null, as `SET n = {...}` does.
    return f"UPDATE {target} SET {PROPS} = json_patch('{{}}', coalesce(?, '{{}}')) WHERE {ID} = ?"


def find_node(keys: list[str], structured: list[bool]) -> str:
    """The first node of a label whose properties equal the bound values.

    A list or map value is compared as JSON (`json(?)`), so key order and
    spacing in the stored text do not matter.
    """
    conds = [
        f"json(json_extract({PROPS}, {json_path(k)})) = json(?)"
        if is_structured
        else f"json_extract({PROPS}, {json_path(k)}) = ?"
        for k, is_structured in zip(keys, structured)
    ]
    extra = "".join(f" AND {c}" for c in conds)
    return f"SELECT {ID} FROM {NODES} WHERE {LABEL} = ?{extra} ORDER BY {ID} LIMIT 1"


def find_edge(keys: list[str], either_way: bool) -> str:
    """The first relationship between two nodes with these property values.

    `IS ?` so a property the pattern sets to null matches its absence.
    """
    conds = "".join(f" AND json_extract({PROPS}, {json_path(k)}) IS ?" for k in keys)
    ends = (
        f"(({SRC} = ? AND {DST} = ?) OR ({SRC} = ? AND {DST} = ?))"
        if either_way
        else f"{SRC} = ? AND {DST} = ?"
    )
    return f"SELECT {ID} FROM {EDGES} WHERE {ends} AND {TYPE} = ?{conds} ORDER BY {ID} LIMIT 1"


# ------------------------------------------------------------- expressions
# The fragments ExprCompiler composes. Each takes SQL already compiled for
# its operands and returns SQL; NULL in means NULL out wherever Cypher says
# so, which is why several wrap their test in a CASE on the operand.

TRUE = ONE
FALSE = ZERO
COUNT_ALL = "count(*)"
EMPTY_JSON_OBJECT = "json_object()"
EMPTY_JSON_ARRAY = "json_array()"
JSON_ROOT = "'$'"
JSON_FIRST = "'$[0]'"
JSON_LAST = "'$[#-1]'"
JSON_GROUP_ARRAY = "json_group_array"
# A uniform float in [0, 1) from SQLite's 64-bit signed random().
RANDOM_UNIT = "((random() + 9223372036854775808.0) / 18446744073709551616.0)"


def call(fn: str, *args: str) -> str:
    return f"{fn}({commas(args)})"


def binary(left: str, op: str, right: str) -> str:
    return f"({left} {op} {right})"


def concat(left: str, right: str) -> str:
    return f"({left} || {right})"


def negate(value: str) -> str:
    return f"(NOT {value})"


def minus(value: str) -> str:
    return f"(-{value})"


def is_null(value: str, negated: bool = False) -> str:
    return f"({value} IS {'NOT ' if negated else ''}NULL)"


def null_if_null(subject: str, value: str) -> str:
    """`value`, or NULL where `subject` is NULL."""
    return f"(CASE WHEN {subject} IS NULL THEN NULL ELSE {value} END)"


def case(subject: str | None, whens: list[tuple[str, str]], default: str) -> str:
    head = f"CASE {subject} " if subject is not None else "CASE "
    arms = " ".join(f"WHEN {cond} THEN {value}" for cond, value in whens)
    return f"({head}{arms} ELSE {default} END)"


def xor(left: str, right: str) -> str:
    return f"(({left} OR {right}) AND NOT ({left} AND {right}))"


def json_value(doc: str) -> str:
    """The SQL value of a JSON document: a scalar, or JSON text."""
    return json_extract(doc, JSON_ROOT)


def json_text(value: str) -> str:
    """JSON text that json_array/json_object embed as JSON, not a string."""
    return f"json({value})"


def json_array(items: Iterable[str]) -> str:
    return call("json_array", *items)


def json_object(parts: Iterable[str]) -> str:
    return call("json_object", *parts)


def json_at(doc: str, path: str, arrow: bool) -> str:
    """The JSON text at `path`, exactly: `true` stays a boolean, a string
    stays quoted, so the value round-trips with its type."""
    if arrow:
        return f"({doc} -> {path})"
    # SQLite before 3.38 has no `->`: rebuild the JSON text from the type.
    return (
        f"(CASE json_type({doc}, {path}) WHEN 'true' THEN 'true' "
        f"WHEN 'false' THEN 'false' WHEN 'null' THEN NULL "
        f"WHEN 'object' THEN json_extract({doc}, {path}) "
        f"WHEN 'array' THEN json_extract({doc}, {path}) "
        f"ELSE json_quote(json_extract({doc}, {path})) END)"
    )


def dynamic_key_path(key: str) -> str:
    return f"""('$."' || {key} || '"')"""


def dynamic_index_path(index: str) -> str:
    # A negative index counts from the end, which JSON paths spell `#-n`.
    return (
        f"(CASE WHEN {index} < 0 THEN '$[#' || {index} || ']' "
        f"ELSE '$[' || {index} || ']' END)"
    )


def lookup(source: str, name: str, row_id: str) -> str:
    """Column `name` of the node or relationship whose id is `row_id`."""
    return paren(select([name], source, where([eq(ID, row_id)])))


def each_element(alias: str) -> str:
    """A `json_each` row's element as JSON text, keeping its exact type:
    arrays and objects arrive as JSON already, scalars need quoting back."""
    value = column(alias, VALUE)
    kind = column(alias, JSON_TYPE)
    return (
        f"(CASE {kind} WHEN 'object' THEN {value} WHEN 'array' THEN {value} "
        f"WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' WHEN 'null' THEN NULL "
        f"ELSE json_quote({value}) END)"
    )


def exists(source: str, cond: str) -> str:
    return f"EXISTS (SELECT 1 FROM {source} WHERE {cond})"


def count_where(source: str, cond: str) -> str:
    return paren(select([COUNT_ALL], source, where([cond])))


def list_comprehension(
    source: str, element: str, each: str, alias: str, filters: list[str]
) -> str:
    """`[x IN source WHERE ... | element]`, elements in list order."""
    rows = select([STAR], each, where(filters) + order_by([column(alias, KEY)]))
    collected = call(
        EgSqlFn.COALESCE, call(JSON_GROUP_ARRAY, element), EMPTY_JSON_ARRAY
    )
    return paren(
        select(
            [f"CASE WHEN {source} IS NULL THEN NULL ELSE {collected} END"],
            table(paren(rows), alias),
        )
    )


def in_constant_list(value: str, items: str, alias: str) -> str:
    """`value IN list` for a list that is the same on every row, as an
    uncorrelated IN: SQLite builds one ephemeral index instead of scanning
    the list per row. Lists and maps never equal a scalar."""
    each = f"json_each({items}) {alias}"
    elements = select(
        [column(alias, VALUE)],
        each,
        f" WHERE {column(alias, JSON_TYPE)} NOT IN ('null', 'object', 'array')",
    )
    return (
        f"(CASE WHEN {value} IS NULL THEN NULL WHEN {items} IS NULL THEN NULL "
        f"WHEN {value} IN ({elements}) THEN 1 ELSE 0 END)"
    )


def in_list(value: str, items: str, alias: str) -> str:
    """`value IN list` for a list that varies by row."""
    each = f"json_each({items}) {alias}"
    cond = (
        f"{column(alias, VALUE)} = {value} "
        f"AND {column(alias, JSON_TYPE)} NOT IN ('object', 'array')"
    )
    return (
        f"(CASE WHEN {value} IS NULL THEN NULL WHEN {items} IS NULL THEN NULL "
        f"WHEN {exists(each, cond)} THEN 1 ELSE 0 END)"
    )


def _when_both_text(left: str, right: str, test: str) -> str:
    return (
        f"(CASE WHEN typeof({left}) <> 'text' OR typeof({right}) <> 'text' "
        f"THEN NULL ELSE {test} END)"
    )


def starts_with(value: str, prefix: str) -> str:
    return _when_both_text(
        value, prefix, f"substr({value}, 1, length({prefix})) = {prefix}"
    )


def ends_with(value: str, suffix: str) -> str:
    return _when_both_text(
        value,
        suffix,
        f"(length({suffix}) = 0 OR substr({value}, -length({suffix})) = {suffix})",
    )


def contains(value: str, part: str) -> str:
    return _when_both_text(value, part, f"instr({value}, {part}) > 0")


def is_key_column(value: str) -> bool:
    return value.endswith(column(NOTHING, KEY))


def prefix_range(value: str, low: str, high: str, exact: str) -> str:
    """A key range the (label, key) index serves, ANDed with the exact test."""
    return f"({value} >= {low} AND {value} < {high} AND {exact})"


def aggregate(fn: str, value: str, distinct: bool) -> str:
    return f"{fn}({'DISTINCT ' if distinct else NOTHING}{value})"


def sum_or_zero(value: str, distinct: bool) -> str:
    # Cypher's sum() over no rows is 0, SQLite's is NULL.
    return call(EgSqlFn.COALESCE, aggregate("sum", value, distinct), ZERO)


def collect(value: str, present: str, distinct: bool) -> str:
    """collect(): every non-null value, as a JSON array (empty, not NULL)."""
    grouped = aggregate(JSON_GROUP_ARRAY, value, distinct)
    return (
        f"coalesce({grouped} FILTER (WHERE {present} IS NOT NULL "
        f"AND {present} <> 'null'), {EMPTY_JSON_ARRAY})"
    )


def json_type(doc: str) -> str:
    return call("json_type", doc)


def keys_of(doc: str) -> str:
    """keys(): a map's keys in their stored order."""
    keys = select([KEY], f"json_each({doc})", order_by([ID]))
    collected = call(
        EgSqlFn.COALESCE, call(JSON_GROUP_ARRAY, column("k", KEY)), EMPTY_JSON_ARRAY
    )
    return paren(
        select(
            [f"CASE WHEN {doc} IS NULL THEN NULL ELSE {collected} END"],
            f"{paren(keys)} k",
        )
    )


def labels_of(node: str, label: str) -> str:
    return null_if_null(node, json_array([label]))


def substring(text: str, start: str, length: str | None) -> str:
    # Cypher counts from 0, substr from 1.
    first = f"({start}) + 1"
    if length is None:
        return call(EgSqlFn.SUBSTR, text, first)
    return call(EgSqlFn.SUBSTR, text, first, length)


def left_part(text: str, length: str) -> str:
    return call(EgSqlFn.SUBSTR, text, ONE, length)


def right_part(text: str, length: str) -> str:
    # substr(s, -0) is the whole string, so a zero length is spelled out.
    return f"(CASE WHEN {length} = 0 THEN '' ELSE substr({text}, -({length})) END)"


def entity_object(row_id: str, rel: bool) -> str:
    """A node or relationship as the JSON object the executor decodes."""
    row = "x"
    if rel:
        fields = [
            sql_str(EG_NODE_ID),
            column(row, ID),
            sql_str(EG_REL_TYPE),
            column(row, TYPE),
            sql_str(EG_REL_START),
            column(row, SRC),
            sql_str(EG_REL_END),
            column(row, DST),
            sql_str(EG_NODE_PROPERTIES),
            json_text(column(row, PROPS)),
        ]
    else:
        fields = [
            sql_str(EG_NODE_ID),
            column(row, ID),
            sql_str(EG_NODE_LABELS),
            json_array([column(row, LABEL)]),
            sql_str(EG_NODE_PROPERTIES),
            json_text(column(row, PROPS)),
        ]
    source = f"{EDGES if rel else NODES} {row}"
    return paren(
        select([json_object(fields)], source, where([eq(column(row, ID), row_id)]))
    )


def map_list(items: str, alias: str, element: str) -> str:
    """A JSON array with `element` (SQL over `alias.value`) for each item,
    in list order."""
    return paren(
        select(
            [call(JSON_GROUP_ARRAY, json_text(element))],
            f"json_each({items}) {alias}",
        )
    )
