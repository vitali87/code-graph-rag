"""Runs parsed Cypher against the SQLite store.

A read-only query is one compiled SQL statement, cached by query text and
parameter types. A query with updating clauses runs clause by clause: the
reads before a write are compiled and materialized into a temp table, the
write is applied from those rows (expressions evaluated in SQL, one pass per
clause), and the next reads continue from the result. CALL works the same
way, with the procedure producing the rows.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from ..constants.embedded_graph import (
    EG_COL_COUNT,
    EG_COL_LABEL,
    EG_COL_PROPERTIES,
    EG_COL_QUERY_PLAN,
    EG_COL_STORAGE_NAME,
    EG_COL_STORAGE_VALUE,
    EG_COMPILE_CACHE_SIZE,
    EG_ERR_DELETE_ATTACHED,
    EG_ERR_LABEL_COUNT,
    EG_ERR_MERGE_NULL,
    EG_ERR_MISSING_PARAM,
    EG_ERR_NOT_ENTITY,
    EG_ERR_NOT_JSON,
    EG_ERR_READ_ONLY,
    EG_ERR_UNSUPPORTED,
    EG_ERR_YIELD,
    EG_PLAN_CALL,
    EG_PLAN_CREATE,
    EG_PLAN_DELETE,
    EG_PLAN_EXPAND,
    EG_PLAN_EXPAND_VARIABLE,
    EG_PLAN_FILTER,
    EG_PLAN_FOREACH,
    EG_PLAN_MERGE,
    EG_PLAN_OPTIONAL,
    EG_PLAN_PRODUCE,
    EG_PLAN_REMOVE,
    EG_PLAN_SCAN,
    EG_PLAN_SCHEMA,
    EG_PLAN_SET,
    EG_PLAN_UNION,
    EG_PLAN_UNWIND,
    EG_PROP_INDEX_NAME,
    EG_PROP_INDEX_SQL,
    EgDirection,
    EgRemoveKind,
    EgSchemaKind,
    EgSetKind,
)
from ..constants.graph import NODE_UNIQUE_CONSTRAINTS
from ..types_defs import JsonValue, ResultValue
from . import cypher_ast as a
from . import types as ty
from .compiler import (
    DECODE_BOOL,
    DECODE_JSON,
    DECODE_NULL,
    DECODE_RAW,
    OutputColumn,
    Pipeline,
    QueryCompiler,
    Stage,
    VarInfo,
    decode_for,
    live_after,
)
from .expressions import CE, CompileError, Ctx, ParamSpec, json_path, sql_str
from .parser import parse
from .procedures import PROCEDURES, ProcValue, run_procedure
from .types import Kind, SqlValue, T


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple[ResultValue, ...]]


_READ_CLAUSES = (a.Match, a.Unwind)
_WRITE_CLAUSES = (a.Create, a.Merge, a.SetClause, a.Remove, a.Delete, a.Foreach, a.Call)

_KEY_CASE = (
    "CASE label "
    + " ".join(
        f"WHEN {sql_str(label)} THEN json_extract(props, {json_path(prop)})"
        for label, prop in NODE_UNIQUE_CONSTRAINTS.items()
    )
    + " ELSE key END"
)
_SQL_REFRESH_KEY = f"UPDATE cgr_nodes SET key = {_KEY_CASE} WHERE id = ?"
_SQL_INSERT_EDGE = "INSERT INTO cgr_edges(src, dst, type, props) VALUES (?, ?, ?, ?)"


# ---------------------------------------------------------------- parameters


def value_type(value: object) -> T:
    if value is None:
        return ty.NULL
    if isinstance(value, bool):
        return ty.BOOL
    if isinstance(value, int):
        return ty.INT
    if isinstance(value, float):
        return ty.FLOAT
    if isinstance(value, str):
        return ty.STRING
    if isinstance(value, Mapping):
        return ty.MAP
    if isinstance(value, (list, tuple)):
        return ty.list_of(ty.unify([value_type(v) for v in value]) if value else ty.ANY)
    return ty.ANY


def _json_default(value: object) -> JsonValue:
    if isinstance(value, (set, frozenset)):
        return list(value)
    return str(value)


def bind_value(spec: ParamSpec, params: Mapping[str, object]) -> SqlValue:
    """A parameter as SQLite binds it.

    Parameters arrive as whatever the caller holds -- the ingestor's
    TypedDict batches, enums, sets -- so they are read as plain objects and
    become a SQLite value here: JSON text for anything structured.
    """
    if spec.name not in params:
        raise CompileError(EG_ERR_MISSING_PARAM.format(name=spec.name))
    value = params[spec.name]
    if spec.mode == "upper":
        return None if value is None else f"{value}\U0010ffff"
    if value is None:
        return None
    if spec.mode == "json":
        return json.dumps(value, default=_json_default)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float | str):
        return value
    return json.dumps(value, default=_json_default)


def _entity_id(value: JsonValue) -> int:
    """A node or relationship id, as SQLite stores it: an integer."""
    if isinstance(value, int):
        return value
    raise CompileError(EG_ERR_NOT_ENTITY.format(value=value))


def _json(value: JsonValue) -> JsonValue:
    """A value the compiler emitted as JSON text, decoded."""
    if isinstance(value, str):
        return json.loads(value)
    raise CompileError(EG_ERR_NOT_JSON.format(value=value))


def _json_map(value: JsonValue) -> dict[str, JsonValue]:
    decoded = _json(value) if value else {}
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: JsonValue) -> list[JsonValue]:
    decoded = _json(value) if value else []
    return decoded if isinstance(decoded, list) else []


def _binds(ctx: Ctx, params: Mapping[str, object]) -> dict[str, SqlValue]:
    return {spec.placeholder: bind_value(spec, params) for spec in ctx.params}


def decode(kind: str, value: SqlValue) -> ResultValue:
    if value is None or kind == DECODE_RAW:
        return value
    if kind == DECODE_BOOL:
        return bool(value)
    if kind == DECODE_JSON:
        return json.loads(value) if isinstance(value, str) else value
    if kind == DECODE_NULL:
        return None
    return value


# ---------------------------------------------------------------- read plans


@dataclass(frozen=True)
class ReadPlan:
    sql: str
    params: tuple[ParamSpec, ...]
    columns: tuple[OutputColumn, ...]


def _has_updates(query: a.Query) -> bool:
    """Whether the query runs clause by clause (writes, or CALL)."""
    return any(
        isinstance(c, _WRITE_CLAUSES) for part in query.parts for c in part.clauses
    )


def _mutates(query: a.Query) -> bool:
    """Whether the query changes the graph; every registered procedure reads."""
    return any(
        isinstance(c, _WRITE_CLAUSES) and not isinstance(c, a.Call)
        for part in query.parts
        for c in part.clauses
    )


@lru_cache(maxsize=EG_COMPILE_CACHE_SIZE)
def compile_read(
    text: str, param_sig: tuple[tuple[str, T], ...], json_arrow: bool
) -> ReadPlan:
    stmt = parse(text)
    query = stmt.query if isinstance(stmt, a.Explain) else stmt
    assert isinstance(query, a.Query)
    ctx = Ctx(dict(param_sig), json_arrow)
    qc = QueryCompiler(ctx)
    pipeline = Pipeline()
    parts: list[tuple[str, list[OutputColumn], list[str]]] = []
    for part in query.parts:
        stage = qc.start_stage(pipeline)
        *body, last = part.clauses
        if not (isinstance(last, a.Projection) and last.is_return):
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what="a read query without RETURN")
            )
        live = live_after(part.clauses)
        for clause, after in zip(body, live):
            stage = qc.clause(pipeline, stage, clause, after)
        parts.append(qc.returning(stage, last))
    sql, columns = _union(parts, query.union_all)
    return ReadPlan(pipeline.with_clause() + sql, tuple(ctx.params), tuple(columns))


def _union(
    parts: list[tuple[str, list[OutputColumn], list[str]]], union_all: tuple[bool, ...]
) -> tuple[str, list[OutputColumn]]:
    if len(parts) == 1:
        return parts[0][0], parts[0][1]
    names = [c.name for c in parts[0][1]]
    kinds: dict[str, str] = {}
    for _, outs, _ in parts:
        for out in outs:
            prior = kinds.setdefault(out.name, out.decode)
            if prior != out.decode:
                kinds[out.name] = DECODE_JSON
    selects = []
    for sql, outs, cols in parts:
        by_name = {out.name: (out, col) for out, col in zip(outs, cols)}
        exprs = []
        for i, name in enumerate(names):
            out, col = by_name[name]
            value = col
            if kinds[name] == DECODE_JSON and out.decode == DECODE_RAW:
                value = f"nullif(json_quote({col}), 'null')"
            elif kinds[name] == DECODE_JSON and out.decode == DECODE_BOOL:
                value = f"(CASE WHEN {col} IS NULL THEN NULL WHEN {col} THEN 'true' ELSE 'false' END)"
            exprs.append(f"{value} AS u{i}")
        selects.append(f"SELECT {', '.join(exprs)} FROM ({sql})")
    sql = selects[0]
    for joiner, select in zip(union_all, selects[1:]):
        sql += f" UNION {'ALL ' if joiner else ''}{select}"
    return sql, [OutputColumn(n, kinds[n]) for n in names]


# ------------------------------------------------------------------ executor


class Executor:
    def __init__(
        self, conn: sqlite3.Connection, json_arrow: bool, read_only: bool = False
    ) -> None:
        self.conn = conn
        self.json_arrow = json_arrow
        self.read_only = read_only
        self._temp_counter = 0

    def execute(self, text: str, params: Mapping[str, object] | None = None) -> Result:
        params = dict(params or {})
        stmt = parse(text)
        if isinstance(stmt, a.SchemaCommand):
            return self._schema(stmt)
        if isinstance(stmt, a.Explain):
            return Result(
                [EG_COL_QUERY_PLAN],
                [(f" * {op}",) for op in plan_operator_names(stmt.query)],
            )
        sig = tuple(sorted((k, value_type(v)) for k, v in params.items()))
        writes = _has_updates(stmt)
        mutates = _mutates(stmt)
        if mutates and self.read_only:
            raise CompileError(EG_ERR_READ_ONLY)
        # IMMEDIATE takes the write lock up front, so two writers queue on
        # the busy timeout instead of one failing to upgrade a read lock.
        self.conn.execute("BEGIN IMMEDIATE" if mutates else "BEGIN")
        try:
            result = self._run(stmt, text, sig, params, writes)
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return result

    def _run(
        self,
        stmt: a.Query,
        text: str,
        sig: tuple[tuple[str, T], ...],
        params: dict[str, object],
        writes: bool,
    ) -> Result:
        if not writes:
            plan = compile_read(text, sig, self.json_arrow)
            binds = {spec.placeholder: bind_value(spec, params) for spec in plan.params}
            rows = self.conn.execute(plan.sql, binds).fetchall()
            return Result(
                [c.name for c in plan.columns],
                [
                    tuple(decode(c.decode, v) for c, v in zip(plan.columns, row))
                    for row in rows
                ],
            )
        if len(stmt.parts) > 1:
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what="UNION with updating clauses")
            )
        ctx = Ctx(dict(sig), self.json_arrow)
        return _Run(self, ctx, params).single(stmt.parts[0])

    def fresh_temp(self) -> str:
        self._temp_counter += 1
        return f"eg_t{self._temp_counter}"

    # ----------------------------------------------------------------- schema

    def _schema(self, cmd: a.SchemaCommand) -> Result:
        if cmd.kind == EgSchemaKind.SHOW_CONSTRAINTS:
            return Result(
                [EG_COL_LABEL, EG_COL_PROPERTIES],
                [(label, [prop]) for label, prop in NODE_UNIQUE_CONSTRAINTS.items()],
            )
        if cmd.kind == EgSchemaKind.SHOW_STORAGE:
            nodes = self.conn.execute("SELECT count(*) FROM cgr_nodes").fetchone()[0]
            edges = self.conn.execute("SELECT count(*) FROM cgr_edges").fetchone()[0]
            return Result(
                [EG_COL_STORAGE_NAME, EG_COL_STORAGE_VALUE],
                [("vertex_count", nodes), ("edge_count", edges)],
            )
        if cmd.kind == EgSchemaKind.SHOW_INDEXES:
            rows = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'cgr_nodes'"
            ).fetchall()
            return Result([EG_COL_LABEL, EG_COL_COUNT], [(r[0], None) for r in rows])
        if cmd.kind == EgSchemaKind.CREATE_INDEX and cmd.label:
            for prop in cmd.props:
                if NODE_UNIQUE_CONSTRAINTS.get(cmd.label) == prop:
                    continue
                if not prop.replace("_", "").isalnum():
                    continue
                self.conn.execute(
                    EG_PROP_INDEX_SQL.format(
                        name=EG_PROP_INDEX_NAME.format(prop=prop), path=json_path(prop)
                    )
                )
        return Result([], [])


class _Run:
    """One execution of a query that has updating clauses."""

    def __init__(self, ex: Executor, ctx: Ctx, params: dict[str, object]) -> None:
        self.ex = ex
        self.conn = ex.conn
        self.ctx = ctx
        self.params = params
        self.qc = QueryCompiler(ctx)
        self.temps: list[str] = []
        self.mutates = False

    # ------------------------------------------------------------- lifecycle

    def single(self, part: a.SingleQuery) -> Result:
        self.mutates = _mutates(a.Query((part,)))
        try:
            pipeline = Pipeline()
            stage = self.qc.start_stage(pipeline)
            live = live_after(part.clauses)
            for clause, after in zip(part.clauses, live):
                if isinstance(clause, a.Projection) and clause.is_return:
                    sql, outputs, _ = self.qc.returning(stage, clause)
                    rows = self.conn.execute(
                        pipeline.with_clause() + sql, self._binds()
                    ).fetchall()
                    return Result(
                        [c.name for c in outputs],
                        [
                            tuple(decode(c.decode, v) for c, v in zip(outputs, row))
                            for row in rows
                        ],
                    )
                if isinstance(clause, (*_READ_CLAUSES, a.Projection)):
                    stage = self.qc.clause(pipeline, stage, clause, after)
                    continue
                stage = self._materialize(pipeline, stage)
                pipeline = Pipeline()
                if (
                    isinstance(clause, a.Call)
                    and not clause.yields
                    and len(part.clauses) == 1
                ):
                    return self._standalone_call(clause)
                stage = self._update(stage, clause)
            return Result([], [])
        finally:
            for name in self.temps:
                self.conn.execute(f"DROP TABLE IF EXISTS temp.{name}")

    def _binds(self) -> dict[str, SqlValue]:
        return _binds(self.ctx, self.params)

    def _materialize(self, pipeline: Pipeline, stage: Stage) -> Stage:
        if not pipeline.ctes[1:] and stage.name.startswith("temp."):
            return stage
        select, cols = self.qc.project_rows(stage)
        name = self.ex.fresh_temp()
        self.conn.execute(
            f"CREATE TEMP TABLE {name} AS {pipeline.with_clause()}{select}",
            self._binds(),
        )
        self.temps.append(name)
        # Constants read from a CTE of the pipeline just run; from here on
        # they are ordinary columns of the temp table.
        plain = {
            var: VarInfo(info.col, info.t, info.names)
            for var, info in stage.vars.items()
        }
        return Stage(f"temp.{name}", plain, cols or ["c_one"])

    def _rows(self, stage: Stage) -> list[list[SqlValue]]:
        cols = [info.col for info in stage.vars.values()]
        if not cols:
            count = self.conn.execute(f"SELECT count(*) FROM {stage.name}").fetchone()[
                0
            ]
            return [[] for _ in range(count)]
        return [
            list(r)
            for r in self.conn.execute(
                f"SELECT {', '.join(cols)} FROM {stage.name} ORDER BY rowid"
            ).fetchall()
        ]

    def _store(self, vars_: dict[str, VarInfo], rows: list[list[SqlValue]]) -> Stage:
        cols = [info.col for info in vars_.values()]
        name = self.ex.fresh_temp()
        if cols:
            self.conn.execute(f"CREATE TEMP TABLE {name} ({', '.join(cols)})")
            placeholders = ", ".join("?" for _ in cols)
            self.conn.executemany(f"INSERT INTO {name} VALUES ({placeholders})", rows)
        else:
            self.conn.execute(f"CREATE TEMP TABLE {name} (c_one)")
            self.conn.executemany(f"INSERT INTO {name} VALUES (1)", [() for _ in rows])
        self.temps.append(name)
        return Stage(f"temp.{name}", dict(vars_), cols or ["c_one"])

    def _eval(
        self, stage: Stage, exprs: Sequence[tuple[a.Expr, str]]
    ) -> list[list[SqlValue]]:
        """Per row (in rowid order), each expression in the requested form."""
        if not exprs:
            count = self.conn.execute(f"SELECT count(*) FROM {stage.name}").fetchone()[
                0
            ]
            return [[] for _ in range(count)]
        scope = stage.scope("p")
        parts = []
        for expr, form in exprs:
            ce = self.qc.x.compile(expr, scope)
            parts.append(self._form(ce, form))
        sql = f"SELECT {', '.join(parts)} FROM {stage.name} AS p ORDER BY p.rowid"
        return [list(r) for r in self.conn.execute(sql, self._binds()).fetchall()]

    def _form(self, ce: CE, form: str) -> str:
        if form == "json":
            return self.qc.x.jsonv(ce)
        if form == "native":
            return self.qc.x.native(ce)
        return ce.sql

    def _new_col(self) -> str:
        return self.ctx.fresh("c")

    # --------------------------------------------------------------- updates

    def _update(self, stage: Stage, clause: a.Clause) -> Stage:
        if isinstance(clause, a.SetClause):
            self._set(stage, clause.items, None)
            return stage
        if isinstance(clause, a.Merge):
            return self._merge(stage, clause)
        if isinstance(clause, a.Create):
            return self._create(stage, clause)
        if isinstance(clause, a.Delete):
            self._delete(stage, clause)
            return stage
        if isinstance(clause, a.Remove):
            self._remove(stage, clause)
            return stage
        if isinstance(clause, a.Foreach):
            self._foreach(stage, clause)
            return stage
        if isinstance(clause, a.Call):
            return self._call(stage, clause)
        raise CompileError(EG_ERR_UNSUPPORTED.format(what=type(clause).__name__))

    def _table_of(self, t: T) -> str:
        return "cgr_edges" if t.kind == Kind.REL else "cgr_nodes"

    def _set(
        self, stage: Stage, items: Sequence[a.SetItem], only: set[int] | None
    ) -> None:
        if not items:
            return
        rows = self._rows(stage)
        exprs = [(item.value, "json") for item in items if item.value is not None]
        values = self._eval(stage, exprs)
        cols = list(stage.vars)
        for i, row in enumerate(rows):
            if only is not None and i not in only:
                continue
            k = 0
            for item in items:
                value = None
                if item.value is not None:
                    value = values[i][k]
                    k += 1
                target_info = stage.vars[item.var]
                target = row[cols.index(item.var)]
                if target is None:
                    continue
                self._apply_set(item, target_info.t, _entity_id(target), value)

    def _apply_set(self, item: a.SetItem, t: T, target: int, value: SqlValue) -> None:
        table = self._table_of(t)
        if item.kind == EgSetKind.PROP:
            path = json_path(str(item.key))
            if value is None:
                self.conn.execute(
                    f"UPDATE {table} SET props = json_remove(props, {path}) WHERE id = ?",
                    (target,),
                )
            else:
                self.conn.execute(
                    f"UPDATE {table} SET props = json_set(props, {path}, json(?)) WHERE id = ?",
                    (value, target),
                )
        elif item.kind == EgSetKind.MERGE:
            if value is None:
                return
            self.conn.execute(
                f"UPDATE {table} SET props = json_patch(props, ?) WHERE id = ?",
                (value, target),
            )
        elif item.kind == EgSetKind.REPLACE:
            self.conn.execute(
                f"UPDATE {table} SET props = json_patch('{{}}', coalesce(?, '{{}}')) WHERE id = ?",
                (value, target),
            )
        else:
            label = self.conn.execute(
                "SELECT label FROM cgr_nodes WHERE id = ?", (target,)
            ).fetchone()
            if label and label[0] != item.key:
                raise CompileError(
                    EG_ERR_LABEL_COUNT.format(labels=f"{label[0]}:{item.key}")
                )
            return
        if table == "cgr_nodes":
            self.conn.execute(_SQL_REFRESH_KEY, (target,))

    def _remove(self, stage: Stage, clause: a.Remove) -> None:
        rows = self._rows(stage)
        cols = list(stage.vars)
        for row in rows:
            for item in clause.items:
                target = row[cols.index(item.var)]
                if target is None:
                    continue
                if item.kind == EgRemoveKind.LABEL:
                    raise CompileError(
                        EG_ERR_UNSUPPORTED.format(what="REMOVE of a label")
                    )
                table = self._table_of(stage.vars[item.var].t)
                self.conn.execute(
                    f"UPDATE {table} SET props = json_remove(props, {json_path(item.key)}) WHERE id = ?",
                    (_entity_id(target),),
                )
                if table == "cgr_nodes":
                    self.conn.execute(_SQL_REFRESH_KEY, (_entity_id(target),))

    def _delete(self, stage: Stage, clause: a.Delete) -> None:
        exprs = [(e, "raw") for e in clause.exprs]
        scope = stage.scope("p")
        types = [self.qc.x.compile(e, scope).t for e in clause.exprs]
        values = self._eval(stage, exprs)
        nodes: list[int] = []
        rels: list[int] = []
        for row in values:
            for t, value in zip(types, row):
                for kind, ident in _entities(t, value):
                    (rels if kind == Kind.REL else nodes).append(ident)
        for rel in dict.fromkeys(rels):
            self.conn.execute("DELETE FROM cgr_edges WHERE id = ?", (rel,))
        for node in dict.fromkeys(nodes):
            if clause.detach:
                self.conn.execute(
                    "DELETE FROM cgr_edges WHERE src = ? OR dst = ?", (node, node)
                )
            elif self.conn.execute(
                "SELECT 1 FROM cgr_edges WHERE src = ? OR dst = ? LIMIT 1", (node, node)
            ).fetchone():
                raise CompileError(EG_ERR_DELETE_ATTACHED.format(id=node))
            self.conn.execute("DELETE FROM cgr_nodes WHERE id = ?", (node,))

    def _merge(self, stage: Stage, clause: a.Merge) -> Stage:
        pattern = clause.pattern
        if pattern.var is not None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what="path variables"))
        if not pattern.rels:
            return self._merge_node(stage, clause, pattern.nodes[0])
        if len(pattern.rels) == 1 and all(
            n.var and n.var in stage.vars and not n.labels and n.props is None
            for n in pattern.nodes
        ):
            return self._merge_rel(stage, clause)
        raise CompileError(
            EG_ERR_UNSUPPORTED.format(what="MERGE of a path with unbound nodes")
        )

    def _merge_node(self, stage: Stage, clause: a.Merge, np: a.NodePattern) -> Stage:
        if len(np.labels) != 1:
            raise CompileError(EG_ERR_LABEL_COUNT.format(labels=":".join(np.labels)))
        label = np.labels[0]
        key_prop = NODE_UNIQUE_CONSTRAINTS.get(label)
        props = np.props.items if isinstance(np.props, a.MapLit) else ()
        exprs = [(value, "json") for _, value in props]
        values = self._eval(stage, exprs)
        rows = self._rows(stage)
        created: set[int] = set()
        bound: list[int] = []
        for i, row_values in enumerate(values):
            doc: dict[str, JsonValue] = {}
            for (key, _), raw in zip(props, row_values):
                if raw is None:
                    raise CompileError(EG_ERR_MERGE_NULL.format(key=key))
                doc[key] = _json(raw)
            node_id, was_created = self._find_or_create_node(label, key_prop, doc)
            bound.append(node_id)
            if was_created:
                created.add(i)
        new_vars = dict(stage.vars)
        out_rows = rows
        if np.var and np.var not in stage.vars:
            col = self._new_col()
            new_vars[np.var] = VarInfo(col, ty.NODE, (label,))
            out_rows = [row + [node_id] for row, node_id in zip(rows, bound)]
        out = self._store(new_vars, out_rows)
        self._on_merge(out, clause, created, len(rows))
        return out

    def _find_or_create_node(
        self, label: str, key_prop: str | None, doc: dict[str, JsonValue]
    ) -> tuple[int, bool]:
        if key_prop is not None and key_prop in doc:
            found = self.conn.execute(
                "SELECT id FROM cgr_nodes WHERE label = ? AND key = ?",
                (label, str(doc[key_prop])),
            ).fetchone()
        else:
            conds = " AND ".join(
                f"json_extract(props, {json_path(k)}) = ?" for k in doc
            )
            args = [int(v) if isinstance(v, bool) else v for v in doc.values()]
            if any(isinstance(v, (list, dict)) for v in args):
                conds = " AND ".join(
                    f"json(json_extract(props, {json_path(k)})) = json(?)"
                    if isinstance(v, (list, dict))
                    else f"json_extract(props, {json_path(k)}) = ?"
                    for k, v in doc.items()
                )
                args = [
                    json.dumps(v) if isinstance(v, (list, dict)) else v for v in args
                ]
            where = f" AND {conds}" if conds else ""
            found = self.conn.execute(
                f"SELECT id FROM cgr_nodes WHERE label = ?{where} ORDER BY id LIMIT 1",
                (label, *args),
            ).fetchone()
        if found:
            return int(found[0]), False
        return self._create_node(label, doc), True

    def _merge_rel(self, stage: Stage, clause: a.Merge) -> Stage:
        pattern = clause.pattern
        rp = pattern.rels[0]
        if len(rp.types) != 1 or rp.var_length is not None:
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what="MERGE of this relationship")
            )
        rel_type = rp.types[0]
        left_var, right_var = str(pattern.nodes[0].var), str(pattern.nodes[1].var)
        props = rp.props.items if isinstance(rp.props, a.MapLit) else ()
        exprs = [(a.Var(left_var), "raw"), (a.Var(right_var), "raw")] + [
            (value, "json") for _, value in props
        ]
        values = self._eval(stage, exprs)
        rows = self._rows(stage)
        created: set[int] = set()
        bound: list[int | None] = []
        for i, row_values in enumerate(values):
            left, right = row_values[0], row_values[1]
            if left is None or right is None:
                bound.append(None)
                continue
            src, dst = (
                (right, left) if rp.direction == EgDirection.IN else (left, right)
            )
            doc = {
                key: _json(raw) if raw is not None else None
                for (key, _), raw in zip(props, row_values[2:])
            }
            conds = "".join(
                f" AND json_extract(props, {json_path(k)}) IS ?" for k in doc
            )
            args = [int(v) if isinstance(v, bool) else v for v in doc.values()]
            if rp.direction == EgDirection.BOTH:
                found = self.conn.execute(
                    f"SELECT id FROM cgr_edges WHERE ((src = ? AND dst = ?) OR (src = ? AND dst = ?)) "
                    f"AND type = ?{conds} ORDER BY id LIMIT 1",
                    (src, dst, dst, src, rel_type, *args),
                ).fetchone()
            else:
                found = self.conn.execute(
                    f"SELECT id FROM cgr_edges WHERE src = ? AND dst = ? AND type = ?{conds} "
                    f"ORDER BY id LIMIT 1",
                    (src, dst, rel_type, *args),
                ).fetchone()
            if found:
                bound.append(int(found[0]))
                continue
            clean = {k: v for k, v in doc.items() if v is not None}
            cur = self.conn.execute(
                _SQL_INSERT_EDGE,
                (src, dst, rel_type, json.dumps(clean, default=_json_default)),
            )
            bound.append(int(cur.lastrowid or 0))
            created.add(i)
        new_vars = dict(stage.vars)
        out_rows = rows
        if rp.var and rp.var not in stage.vars:
            col = self._new_col()
            new_vars[rp.var] = VarInfo(col, ty.REL, (rel_type,))
            out_rows = [row + [rel] for row, rel in zip(rows, bound)]
        out = self._store(new_vars, out_rows)
        self._on_merge(out, clause, created, len(rows))
        return out

    def _on_merge(
        self, stage: Stage, clause: a.Merge, created: set[int], total: int
    ) -> None:
        if clause.on_create:
            self._set(stage, clause.on_create, created)
        if clause.on_match:
            self._set(stage, clause.on_match, set(range(total)) - created)

    def _create(self, stage: Stage, clause: a.Create) -> Stage:
        rows = self._rows(stage)
        new_vars = dict(stage.vars)
        # Every property map, evaluated against the incoming rows.
        exprs: list[tuple[a.Expr, str]] = []
        for pattern in clause.patterns:
            for node in pattern.nodes:
                if node.props is not None and not (node.var and node.var in stage.vars):
                    exprs.append((node.props, "json"))
            for rel in pattern.rels:
                if rel.props is not None:
                    exprs.append((rel.props, "json"))
        values = self._eval(stage, exprs)
        new_cols: dict[str, list[SqlValue]] = {}
        order: list[tuple[str, VarInfo]] = []
        seen: set[str] = set(stage.vars)
        for pattern in clause.patterns:
            for node in pattern.nodes:
                if node.var and node.var not in seen:
                    seen.add(node.var)
                    order.append(
                        (node.var, VarInfo(self._new_col(), ty.NODE, node.labels))
                    )
            for rel in pattern.rels:
                if rel.var and rel.var not in seen:
                    seen.add(rel.var)
                    order.append((rel.var, VarInfo(self._new_col(), ty.REL, rel.types)))
        for i, row in enumerate(rows):
            k = 0
            local: dict[str, int] = {}
            for pattern in clause.patterns:
                ids: list[int] = []
                for node in pattern.nodes:
                    if node.var and node.var in stage.vars:
                        ids.append(_entity_id(row[list(stage.vars).index(node.var)]))
                        continue
                    if node.var and node.var in local:
                        ids.append(local[node.var])
                        continue
                    doc = {}
                    if node.props is not None:
                        doc = _json_map(values[i][k])
                        k += 1
                    if len(node.labels) > 1:
                        raise CompileError(
                            EG_ERR_LABEL_COUNT.format(labels=":".join(node.labels))
                        )
                    label = node.labels[0] if node.labels else ""
                    node_id = self._create_node(label, doc)
                    ids.append(node_id)
                    if node.var:
                        local[node.var] = node_id
                for j, rel in enumerate(pattern.rels):
                    doc = {}
                    if rel.props is not None:
                        doc = _json_map(values[i][k])
                        k += 1
                    if len(rel.types) != 1 or rel.direction == EgDirection.BOTH:
                        raise CompileError(
                            EG_ERR_UNSUPPORTED.format(
                                what="CREATE of this relationship"
                            )
                        )
                    src, dst = ids[j], ids[j + 1]
                    if rel.direction == EgDirection.IN:
                        src, dst = dst, src
                    cur = self.conn.execute(
                        _SQL_INSERT_EDGE,
                        (
                            src,
                            dst,
                            rel.types[0],
                            json.dumps(
                                {k2: v for k2, v in doc.items() if v is not None},
                                default=_json_default,
                            ),
                        ),
                    )
                    if rel.var:
                        local[rel.var] = int(cur.lastrowid or 0)
            for var, _ in order:
                new_cols.setdefault(var, []).append(local.get(var))
        for var, info in order:
            new_vars[var] = info
        out_rows = [
            row + [new_cols[var][i] for var, _ in order] for i, row in enumerate(rows)
        ]
        return self._store(new_vars, out_rows)

    def _create_node(self, label: str, doc: Mapping[str, JsonValue]) -> int:
        clean = {k: v for k, v in doc.items() if v is not None}
        key_prop = NODE_UNIQUE_CONSTRAINTS.get(label)
        key = clean.get(key_prop) if key_prop else None
        cur = self.conn.execute(
            "INSERT INTO cgr_nodes(label, key, props) VALUES (?, ?, ?)",
            (
                label,
                None if key is None else str(key),
                json.dumps(clean, default=_json_default),
            ),
        )
        return int(cur.lastrowid or 0)

    def _foreach(self, stage: Stage, clause: a.Foreach) -> None:
        scope = stage.scope("p")
        source = self.qc.x.compile(clause.source, scope)
        elem = (source.t.elem if source.t.kind == Kind.LIST else None) or ty.ANY
        values = self._eval(stage, [(clause.source, "json")])
        rows = self._rows(stage)
        sub_rows: list[list[SqlValue]] = []
        for row, (raw,) in zip(rows, values):
            for item in _json_list(raw):
                sub_rows.append(row + [_to_column(elem, item)])
        sub_vars = dict(stage.vars)
        sub_vars[clause.var] = VarInfo(self._new_col(), elem)
        sub = self._store(sub_vars, sub_rows)
        for inner in clause.clauses:
            sub = self._update(sub, inner)

    # ----------------------------------------------------------- procedures

    def _call(self, stage: Stage, clause: a.Call) -> Stage:
        spec = PROCEDURES.get(clause.procedure)
        if spec is None:
            from ..constants.embedded_graph import EG_ERR_UNKNOWN_PROCEDURE

            raise CompileError(EG_ERR_UNKNOWN_PROCEDURE.format(name=clause.procedure))
        columns = dict(spec.columns)
        yields = clause.yields or tuple(a.YieldItem(c, c) for c in columns)
        for y in yields:
            if y.name not in columns:
                raise CompileError(
                    EG_ERR_YIELD.format(name=clause.procedure, column=y.name)
                )
        rows = self._rows(stage)
        args_values = self._eval(stage, [(arg, "raw") for arg in clause.args])
        new_vars = dict(stage.vars)
        added: list[tuple[str, str]] = []
        for y in yields:
            col = self._new_col()
            new_vars[y.alias] = VarInfo(col, columns[y.name])
            added.append((y.name, col))
        out_rows: list[list[SqlValue]] = []
        for row, args in zip(rows, args_values):
            for record in run_procedure(
                self.conn, clause.procedure, args, not self.mutates
            ):
                out_rows.append(
                    row + [_to_column(columns[name], record[name]) for name, _ in added]
                )
        out = self._store(new_vars, out_rows)
        if clause.where is None:
            return out
        cond = self.qc.x.boolean(self.qc.x.compile(clause.where, out.scope("p")))
        name = self.ex.fresh_temp()
        self.conn.execute(
            f"CREATE TEMP TABLE {name} AS SELECT * FROM {out.name} AS p WHERE ({cond})",
            self._binds(),
        )
        self.temps.append(name)
        return Stage(f"temp.{name}", out.vars, out.cols)

    def _standalone_call(self, clause: a.Call) -> Result:
        spec = PROCEDURES.get(clause.procedure)
        if spec is None:
            from ..constants.embedded_graph import EG_ERR_UNKNOWN_PROCEDURE

            raise CompileError(EG_ERR_UNKNOWN_PROCEDURE.format(name=clause.procedure))
        args = self._eval(self._store({}, [[]]), [(arg, "raw") for arg in clause.args])[
            0
        ]
        names = [c for c, _ in spec.columns]
        out = []
        for record in run_procedure(self.conn, clause.procedure, args):
            out.append(
                tuple(
                    decode(decode_for(t), _to_column(t, record[c]))
                    for c, t in spec.columns
                )
            )
        return Result(names, out)


def _entities(t: T, value: SqlValue) -> list[tuple[Kind, int]]:
    if value is None:
        return []
    if t.kind in (Kind.NODE, Kind.REL):
        return [(t.kind, _entity_id(value))]
    if (
        t.kind == Kind.LIST
        and t.elem is not None
        and t.elem.kind in (Kind.NODE, Kind.REL)
    ):
        return [
            (t.elem.kind, _entity_id(x)) for x in _json_list(value) if x is not None
        ]
    return []


def _to_column(t: T, value: JsonValue | ProcValue) -> SqlValue:
    """A Python value in the column representation of type `t`."""
    if value is None:
        return None
    if t.kind == Kind.BOOL:
        return int(bool(value))
    if isinstance(value, int | float | str) and (
        t.is_entity or t.kind in (Kind.INT, Kind.FLOAT, Kind.STRING)
    ):
        return value
    return json.dumps(value, default=_json_default)


def plan_operator_names(query: a.Query) -> list[str]:
    """Operator names for the read-only guard, in Memgraph's vocabulary."""
    ops: list[str] = []
    for i, part in enumerate(query.parts):
        if i:
            ops.append(EG_PLAN_UNION)
        for clause in part.clauses:
            ops.extend(_clause_ops(clause))
    return ops


def _clause_ops(clause: a.Clause) -> list[str]:
    if isinstance(clause, a.Match):
        ops = [EG_PLAN_SCAN]
        if any(r.var_length for p in clause.patterns for r in p.rels):
            ops.append(EG_PLAN_EXPAND_VARIABLE)
        if any(p.rels for p in clause.patterns):
            ops.append(EG_PLAN_EXPAND)
        if clause.where is not None:
            ops.append(EG_PLAN_FILTER)
        if clause.optional:
            ops.append(EG_PLAN_OPTIONAL)
        return ops
    if isinstance(clause, a.Unwind):
        return [EG_PLAN_UNWIND]
    if isinstance(clause, a.Projection):
        return [EG_PLAN_PRODUCE]
    if isinstance(clause, a.Call):
        return [f"{EG_PLAN_CALL}<{clause.procedure}>"]
    if isinstance(clause, a.Create):
        return [EG_PLAN_CREATE]
    if isinstance(clause, a.Merge):
        return [EG_PLAN_MERGE]
    if isinstance(clause, a.SetClause):
        return [EG_PLAN_SET]
    if isinstance(clause, a.Remove):
        return [EG_PLAN_REMOVE]
    if isinstance(clause, a.Delete):
        return [EG_PLAN_DELETE]
    if isinstance(clause, a.Foreach):
        return [EG_PLAN_FOREACH]
    return [EG_PLAN_SCHEMA]
