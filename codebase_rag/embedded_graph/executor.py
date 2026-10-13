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
from collections.abc import Callable, Iterator, Mapping, Sequence
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
    EG_ERR_TIMEOUT,
    EG_ERR_UNKNOWN_PROCEDURE,
    EG_ERR_UNSUPPORTED,
    EG_ERR_YIELD,
    EG_IDENT_EXTRA,
    EG_PLAN_CALL_OF,
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
    EG_PLAN_ROW,
    EG_PLAN_SCAN,
    EG_PLAN_SCHEMA,
    EG_PLAN_SET,
    EG_PLAN_UNION,
    EG_PLAN_UNWIND,
    EG_PREFIX_UPPER_BOUND,
    EG_PROGRESS_STEPS,
    EG_PROP_INDEX_NAME,
    EG_PROP_INDEX_SQL,
    EG_STORAGE_EDGE_COUNT,
    EG_STORAGE_VERTEX_COUNT,
    EG_WHAT_CREATE_REL,
    EG_WHAT_MERGE_PATH,
    EG_WHAT_MERGE_REL,
    EG_WHAT_PATH_VARIABLES,
    EG_WHAT_READ_WITHOUT_RETURN,
    EG_WHAT_REMOVE_LABEL,
    EG_WHAT_UNION_UPDATES,
    EgDecode,
    EgDirection,
    EgForm,
    EgParamMode,
    EgPunct,
    EgRemoveKind,
    EgSchemaKind,
    EgSetKind,
)
from ..constants.graph import NODE_UNIQUE_CONSTRAINTS
from ..types_defs import CypherParams, CypherParamValue, JsonValue, ResultValue
from . import cypher_ast as a
from . import sql as sq
from . import types as ty
from .compiler import (
    OutputColumn,
    Pipeline,
    QueryCompiler,
    Stage,
    VarInfo,
    live_after,
)
from .expressions import CE, CompileError, Ctx, ParamSpec
from .limits import Deadline, QueryTimeout, running_until
from .native import NativeGraph, decode_rows, with_walks
from .parser import parse
from .procedures import PROCEDURES, ProcValue, run_procedure
from .types import Kind, SqlValue, T


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple[ResultValue, ...]]


_RETURN_ALL = a.Projection(is_return=True, distinct=False, star=True, items=())
_READ_CLAUSES = (a.Match, a.Unwind)
_WRITE_CLAUSES = (a.Create, a.Merge, a.SetClause, a.Remove, a.Delete, a.Foreach, a.Call)

# The one operator each clause other than MATCH and CALL plans as.
_PLAN_OPS: tuple[tuple[type[a.Clause], str], ...] = (
    (a.Unwind, EG_PLAN_UNWIND),
    (a.Projection, EG_PLAN_PRODUCE),
    (a.Create, EG_PLAN_CREATE),
    (a.Merge, EG_PLAN_MERGE),
    (a.SetClause, EG_PLAN_SET),
    (a.Remove, EG_PLAN_REMOVE),
    (a.Delete, EG_PLAN_DELETE),
    (a.Foreach, EG_PLAN_FOREACH),
)

_SQL_REFRESH_KEY = sq.refresh_key(NODE_UNIQUE_CONSTRAINTS)


# ---------------------------------------------------------------- parameters


def value_type(value: CypherParamValue) -> T:
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


def _json_default(
    value: Mapping[str, CypherParamValue] | Sequence[CypherParamValue],
) -> dict[str, CypherParamValue] | list[CypherParamValue]:
    """A mapping or sequence json cannot write as-is (a tuple-like or
    read-only container a caller built), as the dict or list it equals."""
    if isinstance(value, Mapping):
        return dict(value)
    return list(value)


def bind_value(spec: ParamSpec, params: CypherParams) -> SqlValue:
    """A parameter as SQLite binds it: JSON text for anything structured."""
    if spec.name not in params:
        raise CompileError(EG_ERR_MISSING_PARAM.format(name=spec.name))
    value = params[spec.name]
    if spec.mode == EgParamMode.UPPER:
        return None if value is None else f"{value}{EG_PREFIX_UPPER_BOUND}"
    if value is None:
        return None
    if spec.mode == EgParamMode.JSON:
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


def _binds(ctx: Ctx, params: CypherParams) -> dict[str, SqlValue]:
    return {spec.placeholder: bind_value(spec, params) for spec in ctx.params}


def decode(kind: EgDecode, value: SqlValue) -> ResultValue:
    if value is None or kind == EgDecode.RAW:
        return value
    if kind == EgDecode.BOOL:
        return bool(value)
    if kind == EgDecode.JSON:
        return json.loads(value) if isinstance(value, str) else value
    if kind == EgDecode.NULL:
        return None
    return value


# ---------------------------------------------------------------- read plans


@dataclass(frozen=True)
class ReadPlan:
    sql: str
    params: tuple[ParamSpec, ...]
    columns: tuple[OutputColumn, ...]


# One part of a UNION as QueryCompiler.returning gives it: the SQL, its
# output columns and the SQL column behind each.
type _ReturnPart = tuple[str, list[OutputColumn], list[str]]


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
    text: str,
    param_sig: tuple[tuple[str, T], ...],
    json_arrow: bool,
    native_walks: bool = False,
) -> ReadPlan:
    stmt = parse(text)
    query = stmt.query if isinstance(stmt, a.Explain) else stmt
    assert isinstance(query, a.Query)
    ctx = Ctx(dict(param_sig), json_arrow, native_walks)
    qc = QueryCompiler(ctx)
    pipeline = Pipeline()
    parts: list[_ReturnPart] = []
    for part in query.parts:
        stage = qc.start_stage(pipeline)
        *body, last = part.clauses
        if not (isinstance(last, a.Projection) and last.is_return):
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what=EG_WHAT_READ_WITHOUT_RETURN)
            )
        live = live_after(part.clauses)
        for clause, after in zip(body, live):
            stage = qc.clause(pipeline, stage, clause, after)
        parts.append(qc.returning(stage, last))
    sql, columns = _union(parts, query.union_all)
    return ReadPlan(pipeline.with_clause() + sql, tuple(ctx.params), tuple(columns))


def _union(
    parts: list[_ReturnPart], union_all: tuple[bool, ...]
) -> tuple[str, list[OutputColumn]]:
    if len(parts) == 1:
        return parts[0][0], parts[0][1]
    names = [c.name for c in parts[0][1]]
    kinds = _union_kinds(parts)
    selects = [_union_select(part, names, kinds) for part in parts]
    return sq.union(selects, union_all), [OutputColumn(n, kinds[n]) for n in names]


def _union_kinds(parts: list[_ReturnPart]) -> dict[str, EgDecode]:
    """Each column's decode; JSON where the parts disagree on it."""
    kinds: dict[str, EgDecode] = {}
    for _, outs, _ in parts:
        for out in outs:
            prior = kinds.setdefault(out.name, out.decode)
            if prior != out.decode:
                kinds[out.name] = EgDecode.JSON
    return kinds


def _union_select(
    part: _ReturnPart, names: list[str], kinds: dict[str, EgDecode]
) -> str:
    sql, outs, cols = part
    by_name = {out.name: (out, col) for out, col in zip(outs, cols)}
    exprs = []
    for i, name in enumerate(names):
        out, col = by_name[name]
        value = _union_value(col, out.decode, kinds[name])
        exprs.append(sq.aliased(value, f"{sq.UNION_PREFIX}{i}"))
    return sq.select(exprs, sq.paren(sql))


def _union_value(col: str, decode: EgDecode, kind: EgDecode) -> str:
    # A scalar or boolean column decodes as JSON once another part's column
    # of the same name does, so it has to read as JSON text too.
    if kind == EgDecode.JSON and decode == EgDecode.RAW:
        return sq.json_of_scalar(col)
    if kind == EgDecode.JSON and decode == EgDecode.BOOL:
        return sq.json_of_bool(col)
    return col


# ------------------------------------------------------------------ executor


class Executor:
    def __init__(
        self,
        conn: sqlite3.Connection,
        json_arrow: bool,
        read_only: bool = False,
        native: NativeGraph | None = None,
    ) -> None:
        self.conn = conn
        self.json_arrow = json_arrow
        self.read_only = read_only
        self.native = native
        self._temp_counter = 0

    def execute(
        self,
        text: str,
        params: CypherParams | None = None,
        timeout_s: float | None = None,
    ) -> Result:
        params = dict(params or {})
        stmt = parse(text)
        if isinstance(stmt, a.SchemaCommand):
            return self._schema(stmt)
        if isinstance(stmt, a.Explain):
            return _explain(stmt.query)
        sig = tuple(sorted((k, value_type(v)) for k, v in params.items()))
        writes = _has_updates(stmt)
        mutates = _mutates(stmt)
        if mutates and self.read_only:
            raise CompileError(EG_ERR_READ_ONLY)
        return self._transaction(
            mutates,
            timeout_s,
            lambda walks: self._run(stmt, text, sig, params, writes, walks),
        )

    def _transaction(
        self, mutates: bool, timeout_s: float | None, run: Callable[[bool], Result]
    ) -> Result:
        """`run` in one transaction: committed if it returns, rolled back if
        it raises."""
        self.conn.execute(sq.BEGIN_WRITE if mutates else sq.BEGIN_READ)
        deadline = Deadline.after(timeout_s)
        if timeout_s is not None:
            self.conn.set_progress_handler(deadline.passed, EG_PROGRESS_STEPS)
        try:
            if mutates:
                # Once per transaction that changes the graph (native.py).
                self.conn.execute(sq.BUMP_GRAPH_VERSION)
            with running_until(deadline):
                result = with_walks(None if mutates else self.native, run)
        except BaseException as error:
            self.conn.set_progress_handler(None, 0)
            # SQLite rolls the whole transaction back itself when it
            # interrupts a write (or runs out of disk or memory), and a
            # second ROLLBACK would then replace the real error with its own.
            if self.conn.in_transaction:
                self.conn.execute(sq.ROLLBACK)
            # The progress handler aborts a statement as "interrupted".
            if isinstance(error, sqlite3.OperationalError) and deadline.passed():
                raise QueryTimeout(EG_ERR_TIMEOUT) from error
            raise
        self.conn.set_progress_handler(None, 0)
        self.conn.execute(sq.COMMIT)
        return result

    def _run(
        self,
        stmt: a.Query,
        text: str,
        sig: tuple[tuple[str, T], ...],
        params: CypherParams,
        writes: bool,
        walks: bool = False,
    ) -> Result:
        if not writes:
            plan = compile_read(text, sig, self.json_arrow, walks)
            binds = {spec.placeholder: bind_value(spec, params) for spec in plan.params}
            rows = self.conn.execute(plan.sql, binds).fetchall()
            return Result(
                [c.name for c in plan.columns],
                decode_rows([c.decode for c in plan.columns], rows, decode),
            )
        if len(stmt.parts) > 1:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_UNION_UPDATES))
        ctx = Ctx(dict(sig), self.json_arrow, walks)
        return _Run(self, ctx, params).single(stmt.parts[0])

    def fresh_temp(self) -> str:
        self._temp_counter += 1
        return f"{sq.TEMP_PREFIX}{self._temp_counter}"

    # ----------------------------------------------------------------- schema

    def _schema(self, cmd: a.SchemaCommand) -> Result:
        if cmd.kind == EgSchemaKind.SHOW_CONSTRAINTS:
            return Result(
                [EG_COL_LABEL, EG_COL_PROPERTIES],
                [(label, [prop]) for label, prop in NODE_UNIQUE_CONSTRAINTS.items()],
            )
        if cmd.kind == EgSchemaKind.SHOW_STORAGE:
            return self._storage_info()
        if cmd.kind == EgSchemaKind.SHOW_INDEXES:
            rows = self.conn.execute(sq.LIST_NODE_INDEXES).fetchall()
            return Result([EG_COL_LABEL, EG_COL_COUNT], [(r[0], None) for r in rows])
        if cmd.kind == EgSchemaKind.CREATE_INDEX and cmd.label:
            self._create_indexes(cmd.label, cmd.props)
        return Result([], [])

    def _storage_info(self) -> Result:
        nodes = self.conn.execute(sq.COUNT_NODES).fetchone()[0]
        edges = self.conn.execute(sq.COUNT_EDGES).fetchone()[0]
        return Result(
            [EG_COL_STORAGE_NAME, EG_COL_STORAGE_VALUE],
            [(EG_STORAGE_VERTEX_COUNT, nodes), (EG_STORAGE_EDGE_COUNT, edges)],
        )

    def _create_indexes(self, label: str, props: Sequence[str]) -> None:
        for prop in props:
            if NODE_UNIQUE_CONSTRAINTS.get(label) == prop:
                continue
            if not prop.replace(EG_IDENT_EXTRA, sq.NOTHING).isalnum():
                continue
            self.conn.execute(
                EG_PROP_INDEX_SQL.format(
                    name=EG_PROP_INDEX_NAME.format(prop=prop),
                    path=sq.json_path(prop),
                )
            )


class _Run:
    """One execution of a query that has updating clauses."""

    def __init__(self, ex: Executor, ctx: Ctx, params: CypherParams) -> None:
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
            return self._clauses(part)
        finally:
            for name in self.temps:
                self.conn.execute(sq.drop_temp(name))

    def _clauses(self, part: a.SingleQuery) -> Result:
        pipeline = Pipeline()
        stage = self.qc.start_stage(pipeline)
        live = live_after(part.clauses)
        for clause, after in zip(part.clauses, live):
            if isinstance(clause, a.Projection) and clause.is_return:
                return self._return(pipeline, stage, clause)
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

    def _return(self, pipeline: Pipeline, stage: Stage, clause: a.Projection) -> Result:
        sql, outputs, _ = self.qc.returning(stage, clause)
        rows = self.conn.execute(pipeline.with_clause() + sql, self._binds()).fetchall()
        return Result(
            [c.name for c in outputs],
            decode_rows([c.decode for c in outputs], rows, decode),
        )

    def _binds(self) -> dict[str, SqlValue]:
        return _binds(self.ctx, self.params)

    def _materialize(self, pipeline: Pipeline, stage: Stage) -> Stage:
        if not pipeline.ctes[1:] and sq.is_temp_table(stage.name):
            return stage
        select, cols = self.qc.project_rows(stage)
        name = self.ex.fresh_temp()
        self.conn.execute(
            sq.create_temp_as(name, pipeline.with_clause() + select), self._binds()
        )
        self.temps.append(name)
        # Constants read from a CTE of the pipeline just run; from here on
        # they are ordinary columns of the temp table.
        plain = {
            var: VarInfo(info.col, info.t, info.names)
            for var, info in stage.vars.items()
        }
        return Stage(sq.temp_table(name), plain, cols or [sq.ONE_COLUMN_NAME])

    def _count(self, stage: Stage) -> int:
        return int(self.conn.execute(sq.count_rows(stage.name)).fetchone()[0])

    def _rows(self, stage: Stage) -> list[list[SqlValue]]:
        cols = [info.col for info in stage.vars.values()]
        if not cols:
            return [[] for _ in range(self._count(stage))]
        return [
            list(r)
            for r in self.conn.execute(sq.in_row_order(cols, stage.name)).fetchall()
        ]

    def _store(self, vars_: dict[str, VarInfo], rows: list[list[SqlValue]]) -> Stage:
        cols = [info.col for info in vars_.values()]
        name = self.ex.fresh_temp()
        if cols:
            self.conn.execute(sq.create_temp(name, cols))
            self.conn.executemany(sq.insert_row(name, len(cols)), rows)
        else:
            self.conn.execute(sq.create_temp(name, [sq.ONE_COLUMN_NAME]))
            self.conn.executemany(sq.insert_constant(name), [() for _ in rows])
        self.temps.append(name)
        return Stage(sq.temp_table(name), dict(vars_), cols or [sq.ONE_COLUMN_NAME])

    def _eval(
        self, stage: Stage, exprs: Sequence[tuple[a.Expr, EgForm]]
    ) -> list[list[SqlValue]]:
        """Per row (in rowid order), each expression in the requested form."""
        if not exprs:
            return [[] for _ in range(self._count(stage))]
        scope = stage.scope(sq.INPUT)
        parts = []
        for expr, form in exprs:
            ce = self.qc.x.compile(expr, scope)
            parts.append(self._form(ce, form))
        sql = sq.in_row_order(parts, stage.name, sq.INPUT)
        return [list(r) for r in self.conn.execute(sql, self._binds()).fetchall()]

    def _form(self, ce: CE, form: EgForm) -> str:
        if form == EgForm.JSON:
            return self.qc.x.jsonv(ce)
        if form == EgForm.NATIVE:
            return self.qc.x.native(ce)
        return ce.sql

    def _new_col(self) -> str:
        return self.ctx.fresh(sq.COLUMN_PREFIX)

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
        return sq.EDGES if t.kind == Kind.REL else sq.NODES

    def _set(
        self, stage: Stage, items: Sequence[a.SetItem], only: set[int] | None
    ) -> None:
        if not items:
            return
        rows = self._rows(stage)
        exprs = [(item.value, EgForm.JSON) for item in items if item.value is not None]
        values = self._eval(stage, exprs)
        cols = list(stage.vars)
        for i, row in enumerate(rows):
            if only is None or i in only:
                self._set_row(stage, items, row, iter(values[i]), cols)

    def _set_row(
        self,
        stage: Stage,
        items: Sequence[a.SetItem],
        row: list[SqlValue],
        values: Iterator[SqlValue],
        cols: list[str],
    ) -> None:
        for item in items:
            # Only the items that carry a value took a column of `values`.
            value = next(values) if item.value is not None else None
            target = row[cols.index(item.var)]
            if target is not None:
                self._apply_set(item, stage.vars[item.var].t, _entity_id(target), value)

    def _apply_set(self, item: a.SetItem, t: T, target: int, value: SqlValue) -> None:
        table = self._table_of(t)
        if item.kind == EgSetKind.PROP:
            key = str(item.key)
            if value is None:
                self.conn.execute(sq.remove_prop(table, key), (target,))
            else:
                self.conn.execute(sq.set_prop(table, key), (value, target))
        elif item.kind == EgSetKind.MERGE:
            if value is None:
                return
            self.conn.execute(sq.merge_props(table), (value, target))
        elif item.kind == EgSetKind.REPLACE:
            self.conn.execute(sq.replace_props(table), (value, target))
        else:
            label = self.conn.execute(sq.NODE_LABEL, (target,)).fetchone()
            if label and label[0] != item.key:
                raise CompileError(
                    EG_ERR_LABEL_COUNT.format(
                        labels=EgPunct.COLON.join((label[0], str(item.key)))
                    )
                )
            return
        if table == sq.NODES:
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
                        EG_ERR_UNSUPPORTED.format(what=EG_WHAT_REMOVE_LABEL)
                    )
                table = self._table_of(stage.vars[item.var].t)
                self.conn.execute(
                    sq.remove_prop(table, item.key), (_entity_id(target),)
                )
                if table == sq.NODES:
                    self.conn.execute(_SQL_REFRESH_KEY, (_entity_id(target),))

    def _delete(self, stage: Stage, clause: a.Delete) -> None:
        exprs = [(e, EgForm.RAW) for e in clause.exprs]
        scope = stage.scope(sq.INPUT)
        types = [self.qc.x.compile(e, scope).t for e in clause.exprs]
        values = self._eval(stage, exprs)
        nodes: list[int] = []
        rels: list[int] = []
        for row in values:
            for t, value in zip(types, row):
                for kind, ident in _entities(t, value):
                    (rels if kind == Kind.REL else nodes).append(ident)
        for rel in dict.fromkeys(rels):
            self.conn.execute(sq.DELETE_EDGE, (rel,))
        for node in dict.fromkeys(nodes):
            if clause.detach:
                self.conn.execute(sq.DETACH_NODE, (node, node))
            elif self.conn.execute(sq.NODE_HAS_EDGES, (node, node)).fetchone():
                raise CompileError(EG_ERR_DELETE_ATTACHED.format(id=node))
            self.conn.execute(sq.DELETE_NODE, (node,))

    def _merge(self, stage: Stage, clause: a.Merge) -> Stage:
        pattern = clause.pattern
        if pattern.var is not None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_PATH_VARIABLES))
        if not pattern.rels:
            return self._merge_node(stage, clause, pattern.nodes[0])
        if len(pattern.rels) == 1 and all(
            n.var and n.var in stage.vars and not n.labels and n.props is None
            for n in pattern.nodes
        ):
            return self._merge_rel(stage, clause)
        raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_MERGE_PATH))

    def _merge_node(self, stage: Stage, clause: a.Merge, np: a.NodePattern) -> Stage:
        if len(np.labels) != 1:
            raise CompileError(
                EG_ERR_LABEL_COUNT.format(labels=EgPunct.COLON.join(np.labels))
            )
        label = np.labels[0]
        key_prop = NODE_UNIQUE_CONSTRAINTS.get(label)
        props = np.props.items if isinstance(np.props, a.MapLit) else ()
        exprs = [(value, EgForm.JSON) for _, value in props]
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
        out = self._with_merged(stage, rows, np.var, ty.NODE, label, bound)
        self._on_merge(out, clause, created, len(rows))
        return out

    def _find_or_create_node(
        self, label: str, key_prop: str | None, doc: dict[str, JsonValue]
    ) -> tuple[int, bool]:
        if key_prop is not None and key_prop in doc:
            found = self.conn.execute(
                sq.FIND_NODE_BY_KEY, (label, str(doc[key_prop]))
            ).fetchone()
        else:
            structured = [isinstance(v, (list, dict)) for v in doc.values()]
            args = [_node_match_arg(v) for v in doc.values()]
            found = self.conn.execute(
                sq.find_node(list(doc), structured), (label, *args)
            ).fetchone()
        if found:
            return int(found[0]), False
        return self._create_node(label, doc), True

    def _merge_rel(self, stage: Stage, clause: a.Merge) -> Stage:
        pattern = clause.pattern
        rp = pattern.rels[0]
        if len(rp.types) != 1 or rp.var_length is not None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_MERGE_REL))
        rel_type = rp.types[0]
        left_var, right_var = str(pattern.nodes[0].var), str(pattern.nodes[1].var)
        props = rp.props.items if isinstance(rp.props, a.MapLit) else ()
        exprs = [(a.Var(left_var), EgForm.RAW), (a.Var(right_var), EgForm.RAW)] + [
            (value, EgForm.JSON) for _, value in props
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
            doc = _rel_doc(props, row_values[2:])
            rel_id, was_created = self._find_or_create_edge(rp, left, right, doc)
            bound.append(rel_id)
            if was_created:
                created.add(i)
        out = self._with_merged(stage, rows, rp.var, ty.REL, rel_type, bound)
        self._on_merge(out, clause, created, len(rows))
        return out

    def _find_or_create_edge(
        self,
        rp: a.RelPattern,
        left: SqlValue,
        right: SqlValue,
        doc: dict[str, JsonValue],
    ) -> tuple[int, bool]:
        rel_type = rp.types[0]
        src, dst = (right, left) if rp.direction == EgDirection.IN else (left, right)
        args = [int(v) if isinstance(v, bool) else v for v in doc.values()]
        either_way = rp.direction == EgDirection.BOTH
        ends = (src, dst, dst, src) if either_way else (src, dst)
        found = self.conn.execute(
            sq.find_edge(list(doc), either_way), (*ends, rel_type, *args)
        ).fetchone()
        if found:
            return int(found[0]), False
        clean = {k: v for k, v in doc.items() if v is not None}
        cur = self.conn.execute(
            sq.INSERT_EDGE,
            (src, dst, rel_type, json.dumps(clean, default=_json_default)),
        )
        return int(cur.lastrowid or 0), True

    def _with_merged(
        self,
        stage: Stage,
        rows: list[list[SqlValue]],
        var: str | None,
        t: T,
        name: str,
        bound: Sequence[int | None],
    ) -> Stage:
        """The merge's input rows, plus the entity each row bound when the
        pattern names a variable the input does not have yet."""
        new_vars = dict(stage.vars)
        out_rows = rows
        if var and var not in stage.vars:
            col = self._new_col()
            new_vars[var] = VarInfo(col, t, (name,))
            out_rows = [row + [entity] for row, entity in zip(rows, bound)]
        return self._store(new_vars, out_rows)

    def _on_merge(
        self, stage: Stage, clause: a.Merge, created: set[int], total: int
    ) -> None:
        if clause.on_create:
            self._set(stage, clause.on_create, created)
        if clause.on_match:
            self._set(stage, clause.on_match, set(range(total)) - created)

    def _create(self, stage: Stage, clause: a.Create) -> Stage:
        rows = self._rows(stage)
        values = self._eval(stage, _create_props(clause, stage.vars))
        order = self._created_vars(clause, stage.vars)
        out_rows: list[list[SqlValue]] = []
        for row, row_values in zip(rows, values):
            # The property maps are consumed in pattern order, as
            # _create_props listed them.
            local = self._create_row(clause, stage.vars, row, iter(row_values))
            out_rows.append(row + [local.get(var) for var, _ in order])
        new_vars = dict(stage.vars)
        new_vars.update(order)
        return self._store(new_vars, out_rows)

    def _created_vars(
        self, clause: a.Create, bound: Mapping[str, VarInfo]
    ) -> list[tuple[str, VarInfo]]:
        """The variables the patterns introduce, in order, each in a new column."""
        order: list[tuple[str, VarInfo]] = []
        seen: set[str] = set(bound)
        for var, t, names in _pattern_vars(clause):
            if var not in seen:
                seen.add(var)
                order.append((var, VarInfo(self._new_col(), t, names)))
        return order

    def _create_row(
        self,
        clause: a.Create,
        bound: Mapping[str, VarInfo],
        row: list[SqlValue],
        props: Iterator[SqlValue],
    ) -> dict[str, int]:
        """Creates the patterns for one input row; the ids its variables got."""
        local: dict[str, int] = {}
        for pattern in clause.patterns:
            ids: list[int] = []
            for node in pattern.nodes:
                ids.append(self._pattern_node(node, bound, row, props, local))
            for j, rel in enumerate(pattern.rels):
                rel_id = self._create_rel(rel, ids[j], ids[j + 1], props)
                if rel.var:
                    local[rel.var] = rel_id
        return local

    def _pattern_node(
        self,
        node: a.NodePattern,
        bound: Mapping[str, VarInfo],
        row: list[SqlValue],
        props: Iterator[SqlValue],
        local: dict[str, int],
    ) -> int:
        """The id of a pattern's node: the one its variable holds, or a new one."""
        if node.var and node.var in bound:
            return _entity_id(row[list(bound).index(node.var)])
        if node.var and node.var in local:
            return local[node.var]
        doc = _json_map(next(props)) if node.props is not None else {}
        if len(node.labels) > 1:
            raise CompileError(
                EG_ERR_LABEL_COUNT.format(labels=EgPunct.COLON.join(node.labels))
            )
        label = node.labels[0] if node.labels else sq.NOTHING
        node_id = self._create_node(label, doc)
        if node.var:
            local[node.var] = node_id
        return node_id

    def _create_rel(
        self, rel: a.RelPattern, left: int, right: int, props: Iterator[SqlValue]
    ) -> int:
        doc = _json_map(next(props)) if rel.props is not None else {}
        if len(rel.types) != 1 or rel.direction == EgDirection.BOTH:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_CREATE_REL))
        src, dst = (right, left) if rel.direction == EgDirection.IN else (left, right)
        cur = self.conn.execute(
            sq.INSERT_EDGE,
            (
                src,
                dst,
                rel.types[0],
                json.dumps(
                    {k: v for k, v in doc.items() if v is not None},
                    default=_json_default,
                ),
            ),
        )
        return int(cur.lastrowid or 0)

    def _create_node(self, label: str, doc: Mapping[str, JsonValue]) -> int:
        clean = {k: v for k, v in doc.items() if v is not None}
        key_prop = NODE_UNIQUE_CONSTRAINTS.get(label)
        key = clean.get(key_prop) if key_prop else None
        cur = self.conn.execute(
            sq.INSERT_NODE,
            (
                label,
                None if key is None else str(key),
                json.dumps(clean, default=_json_default),
            ),
        )
        return int(cur.lastrowid or 0)

    def _foreach(self, stage: Stage, clause: a.Foreach) -> None:
        scope = stage.scope(sq.INPUT)
        source = self.qc.x.compile(clause.source, scope)
        elem = (source.t.elem if source.t.kind == Kind.LIST else None) or ty.ANY
        values = self._eval(stage, [(clause.source, EgForm.JSON)])
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
            raise CompileError(EG_ERR_UNKNOWN_PROCEDURE.format(name=clause.procedure))
        columns = dict(spec.columns)
        yields = clause.yields or tuple(a.YieldItem(c, c) for c in columns)
        for y in yields:
            if y.name not in columns:
                raise CompileError(
                    EG_ERR_YIELD.format(name=clause.procedure, column=y.name)
                )
        rows = self._rows(stage)
        args_values = self._eval(stage, [(arg, EgForm.RAW) for arg in clause.args])
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
        cond = self.qc.x.boolean(self.qc.x.compile(clause.where, out.scope(sq.INPUT)))
        name = self.ex.fresh_temp()
        self.conn.execute(sq.filtered_copy(name, out.name, cond), self._binds())
        self.temps.append(name)
        return Stage(sq.temp_table(name), out.vars, out.cols)

    def _standalone_call(self, clause: a.Call) -> Result:
        # `CALL p()` alone returns what `CALL p() YIELD * RETURN *` would,
        # so nodes come back whole rather than as ids.
        stage = self._call(self._store({}, [[]]), clause)
        sql, outputs, _ = self.qc.returning(stage, _RETURN_ALL)
        rows = self.conn.execute(sql, self._binds()).fetchall()
        return Result(
            [c.name for c in outputs],
            [tuple(decode(c.decode, v) for c, v in zip(outputs, row)) for row in rows],
        )


def _create_props(
    clause: a.Create, bound: Mapping[str, VarInfo]
) -> list[tuple[a.Expr, EgForm]]:
    """Every property map a CREATE writes, to evaluate against the incoming
    rows; a node its input already binds is not created, so has none."""
    exprs: list[tuple[a.Expr, EgForm]] = []
    for pattern in clause.patterns:
        for node in pattern.nodes:
            if node.props is not None and not (node.var and node.var in bound):
                exprs.append((node.props, EgForm.JSON))
        for rel in pattern.rels:
            if rel.props is not None:
                exprs.append((rel.props, EgForm.JSON))
    return exprs


def _pattern_vars(clause: a.Create) -> list[tuple[str, T, tuple[str, ...]]]:
    """Each variable a CREATE's patterns name, as often as they name it."""
    found: list[tuple[str, T, tuple[str, ...]]] = []
    for pattern in clause.patterns:
        found.extend((n.var, ty.NODE, n.labels) for n in pattern.nodes if n.var)
        found.extend((r.var, ty.REL, r.types) for r in pattern.rels if r.var)
    return found


def _rel_doc(
    props: Sequence[tuple[str, a.Expr]], raws: Sequence[SqlValue]
) -> dict[str, JsonValue]:
    # A null value stays in: sq.find_edge matches it to an absent property.
    return {
        key: _json(raw) if raw is not None else None
        for (key, _), raw in zip(props, raws)
    }


def _node_match_arg(value: JsonValue) -> JsonValue:
    """A property value as sq.find_node binds it: a list or map as JSON
    text, a boolean as the integer json_extract reads it as."""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    if isinstance(value, bool):
        return int(value)
    return value


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


def _explain(query: a.Query) -> Result:
    return Result(
        [EG_COL_QUERY_PLAN],
        [(EG_PLAN_ROW.format(op=op),) for op in plan_operator_names(query)],
    )


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
        return _match_ops(clause)
    if isinstance(clause, a.Call):
        return [EG_PLAN_CALL_OF.format(procedure=clause.procedure)]
    for kind, op in _PLAN_OPS:
        if isinstance(clause, kind):
            return [op]
    return [EG_PLAN_SCHEMA]


def _match_ops(clause: a.Match) -> list[str]:
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
