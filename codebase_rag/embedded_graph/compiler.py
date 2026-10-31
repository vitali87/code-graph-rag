"""Read clauses compiled to one SQLite statement of chained CTEs.

Each clause turns the binding table of the previous one into a new CTE:
MATCH joins `cgr_nodes`/`cgr_edges` onto it, OPTIONAL MATCH left-joins a
match over its distinct inputs (so a partly matched pattern binds nothing,
as Cypher requires), WITH/RETURN project, group and sort. Variable-length
relationships are recursive CTEs that keep each path's edge list, giving
Cypher's one-row-per-path result with no relationship repeated in a path.

Two things keep big values from being copied row by row:

* liveness: a stage carries only the variables some later clause reads;
* single-row stages (`WITH collect(n) AS owned`): what they bind is read
  back with a scalar subquery instead of riding along as a column, so a
  36k-element list is stored once rather than once per downstream row.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants.embedded_graph import (
    EG_ALL_LIVE,
    EG_ERR_NOT_NODE,
    EG_ERR_NOT_REL,
    EG_ERR_UNSUPPORTED,
    EG_ERR_VARLEN_VAR,
    EG_WHAT_PARAM_PATTERN_PROPS,
    EG_WHAT_PATH_VARIABLES,
    EG_WHAT_VARLEN_ROW_PROPS,
    EgBinary,
    EgDecode,
    EgDirection,
)
from ..constants.graph import NODE_UNIQUE_CONSTRAINTS
from . import cypher_ast as a
from . import sql as sq
from . import types as ty
from .expressions import (
    CE,
    CompileError,
    Ctx,
    ExprCompiler,
    Scope,
    each_value_sql,
    free_vars,
    is_aggregate,
)
from .types import Kind, T

ALL_LIVE = EG_ALL_LIVE


@dataclass
class VarInfo:
    col: str
    t: T
    names: tuple[str, ...] = ()
    # Set for a value read from a single-row stage: the SQL that reads it.
    const_sql: str | None = None


@dataclass
class Stage:
    """A binding table: a CTE (or temp table) and the variables it holds."""

    name: str
    vars: dict[str, VarInfo]
    cols: list[str]
    # At most one row, so its variables can be read with a scalar subquery.
    single_row: bool = False

    def scope(self, alias: str) -> Scope:
        scope = Scope()
        for var, info in self.vars.items():
            if info.const_sql is not None:
                ce = CE(info.const_sql, info.t, None, info.names, const=True)
            else:
                ce = CE(sq.column(alias, info.col), info.t, None, info.names)
            scope = scope.with_var(var, ce)
        return scope

    def carried(self, live: set[str]) -> list[tuple[str, VarInfo]]:
        return [
            (var, info)
            for var, info in self.vars.items()
            if info.const_sql is None and _is_live(var, live)
        ]

    def consts(self, live: set[str]) -> dict[str, VarInfo]:
        return {
            var: info
            for var, info in self.vars.items()
            if info.const_sql is not None and _is_live(var, live)
        }


def _is_live(var: str, live: set[str]) -> bool:
    return ALL_LIVE in live or var in live


@dataclass
class OutputColumn:
    name: str
    decode: EgDecode


@dataclass
class Pipeline:
    ctes: list[tuple[str, str]] = field(default_factory=list)

    def add(self, name: str, sql: str) -> None:
        self.ctes.append((name, sql))

    def with_clause(self) -> str:
        return sq.with_ctes(sq.cte(name, sql) for name, sql in self.ctes)


def decode_for(t: T) -> EgDecode:
    if t.kind == Kind.BOOL:
        return EgDecode.BOOL
    if t.kind == Kind.NULL:
        return EgDecode.NULL
    if t.is_entity or t.is_json:
        return EgDecode.JSON
    return EgDecode.RAW


# ------------------------------------------------------------------- liveness


def clause_refs(clause: a.Clause) -> set[str]:
    """Variables a clause reads; ALL_LIVE for a `*` projection."""
    if isinstance(clause, (a.Match, a.Create)):
        return _match_refs(clause)
    if isinstance(clause, a.Unwind):
        return free_vars(clause.expr)
    if isinstance(clause, a.Projection):
        return _projection_refs(clause)
    if isinstance(clause, a.Merge):
        return _merge_refs(clause)
    if isinstance(clause, a.SetClause):
        return _set_clause_refs(clause)
    if isinstance(clause, a.Remove):
        return {item.var for item in clause.items}
    if isinstance(clause, a.Delete):
        return _union_vars(clause.exprs)
    if isinstance(clause, a.Foreach):
        return _foreach_refs(clause)
    if isinstance(clause, a.Call):
        return _call_refs(clause)
    return set()


def _match_refs(clause: a.Match | a.Create) -> set[str]:
    out: set[str] = set()
    for pattern in clause.patterns:
        out |= _pattern_refs(pattern)
    if isinstance(clause, a.Match) and clause.where is not None:
        out |= free_vars(clause.where)
    return out


def _merge_refs(clause: a.Merge) -> set[str]:
    out = _pattern_refs(clause.pattern)
    for item in (*clause.on_create, *clause.on_match):
        out |= _set_refs(item)
    return out


def _set_clause_refs(clause: a.SetClause) -> set[str]:
    out: set[str] = set()
    for item in clause.items:
        out |= _set_refs(item)
    return out


def _foreach_refs(clause: a.Foreach) -> set[str]:
    out = free_vars(clause.source)
    for inner in clause.clauses:
        out |= clause_refs(inner) - {clause.var}
    return out


def _call_refs(clause: a.Call) -> set[str]:
    out = _union_vars(clause.args)
    if clause.where is not None:
        out |= free_vars(clause.where) - {y.alias for y in clause.yields}
    return out


def _union_vars(exprs: tuple[a.Expr, ...]) -> set[str]:
    out: set[str] = set()
    for e in exprs:
        out |= free_vars(e)
    return out


def _projection_refs(proj: a.Projection) -> set[str]:
    if proj.star:
        return {ALL_LIVE}
    out = _union_vars(tuple(item.expr for item in proj.items))
    # ORDER BY may also read variables from before the projection.
    aliases = {item.alias for item in proj.items}
    out |= _union_vars(tuple(s.expr for s in proj.order)) - aliases
    return out | _union_vars(tuple(x for x in (proj.skip, proj.limit) if x is not None))


def _pattern_refs(pattern: a.PathPattern) -> set[str]:
    out: set[str] = set()
    for item in (*pattern.nodes, *pattern.rels):
        if item.var:
            out.add(item.var)
        if item.props is not None:
            out |= free_vars(item.props)
    return out


def _set_refs(item: a.SetItem) -> set[str]:
    out = {item.var}
    if item.value is not None:
        out |= free_vars(item.value)
    return out


def live_after(clauses: tuple[a.Clause, ...]) -> list[set[str]]:
    """For each clause, the variables some later clause reads."""
    result: list[set[str]] = [set() for _ in clauses]
    acc: set[str] = set()
    for i in range(len(clauses) - 1, -1, -1):
        result[i] = set(acc)
        refs = clause_refs(clauses[i])
        if isinstance(clauses[i], a.Projection) and ALL_LIVE not in refs:
            # A projection ends the scope: before it, only its inputs matter.
            acc = refs
        else:
            acc |= refs
    return result


# ------------------------------------------------------------------- compiler


class QueryCompiler:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.x = ExprCompiler(ctx)

    def start_stage(self, pipeline: Pipeline) -> Stage:
        name = self.ctx.fresh(sq.STAGE_PREFIX)
        pipeline.add(name, sq.select_constant())
        return Stage(name, {}, [], single_row=True)

    def _new_col(self) -> str:
        return self.ctx.fresh(sq.COLUMN_PREFIX)

    def clause(
        self, pipeline: Pipeline, stage: Stage, clause: a.Clause, live: set[str]
    ) -> Stage:
        if isinstance(clause, a.Match):
            if clause.optional:
                return self._optional_match(pipeline, stage, clause, live)
            return self._match(pipeline, stage, clause, live)
        if isinstance(clause, a.Unwind):
            return self._unwind(pipeline, stage, clause, live)
        if isinstance(clause, a.Projection) and not clause.is_return:
            return self._with(pipeline, stage, clause)
        raise CompileError(EG_ERR_UNSUPPORTED.format(what=type(clause).__name__))

    def _extend(
        self,
        pipeline: Pipeline,
        stage: Stage,
        live: set[str],
        source: str,
        added: list[tuple[str, str, VarInfo]],
        tail: str = sq.NOTHING,
    ) -> Stage:
        """A stage of the live input variables plus `added` (var, sql, info)."""
        name = self.ctx.fresh(sq.STAGE_PREFIX)
        out_vars = stage.consts(live)
        cols: list[str] = []
        select: list[str] = []
        for var, info in stage.carried(live):
            out_vars[var] = info
            cols.append(info.col)
            select.append(sq.column(sq.INPUT, info.col))
        for var, sql, info in added:
            out_vars[var] = info
            cols.append(info.col)
            select.append(sq.aliased(sql, info.col))
        pipeline.add(name, sq.select(select, source, tail))
        return Stage(name, out_vars, cols)

    # ------------------------------------------------------------------ MATCH

    def _match(
        self, pipeline: Pipeline, stage: Stage, m: a.Match, live: set[str]
    ) -> Stage:
        conjuncts = _conjuncts(m.where) if m.where is not None else []
        builder = _MatchBuilder(self, pipeline, stage, conjuncts)
        for pattern in m.patterns:
            builder.add_pattern(pattern)
        conds = list(builder.conds)
        if m.where is not None:
            conds.append(self.x.boolean(self.x.compile(m.where, builder.scope)))
        conds.extend(builder.uniqueness())
        added = [
            (var, ce.sql, VarInfo(self._new_col(), ce.t, ce.names))
            for var, ce in builder.new_vars.items()
            if _is_live(var, live)
        ]
        return self._extend(
            pipeline,
            stage,
            live,
            sq.commas(builder.from_items),
            added,
            sq.where(conds),
        )

    def _optional_match(
        self, pipeline: Pipeline, stage: Stage, m: a.Match, live: set[str]
    ) -> Stage:
        used = clause_refs(m)
        consts = stage.consts(used)
        inputs = dict(stage.carried(used))
        used_cols = [info.col for info in inputs.values()]
        inp = self.ctx.fresh(sq.STAGE_PREFIX)
        if used_cols:
            pipeline.add(inp, sq.select(used_cols, stage.name, distinct=True))
        else:
            pipeline.add(inp, sq.select_constant())
        inner = Stage(inp, {**consts, **inputs}, used_cols)
        inner_live = live | set(inputs)
        matched = self._match(
            pipeline, inner, a.Match(False, m.patterns, m.where), inner_live
        )
        on = sq.all_of(
            sq.is_same(sq.column(sq.OPTIONAL, c), sq.column(sq.INPUT, c))
            for c in used_cols
        )
        added = [
            (var, sq.column(sq.OPTIONAL, info.col), info)
            for var, info in matched.vars.items()
            if var not in stage.vars and info.const_sql is None
        ]
        source = sq.left_join(
            sq.table(stage.name, sq.INPUT),
            sq.table(matched.name, sq.OPTIONAL),
            on or sq.ONE,
        )
        return self._extend(pipeline, stage, live, source, added)

    # ----------------------------------------------------------------- UNWIND

    def _unwind(
        self, pipeline: Pipeline, stage: Stage, u: a.Unwind, live: set[str]
    ) -> Stage:
        source = self.x.compile(u.expr, stage.scope(sq.INPUT))
        alias = self.ctx.fresh(sq.EACH_PREFIX)
        elem = (source.t.elem if source.t.kind == Kind.LIST else None) or ty.ANY
        if source.t.kind not in (Kind.LIST, Kind.ANY, Kind.NULL):
            elem = source.t
        added = [(u.var, each_value_sql(alias, elem), VarInfo(self._new_col(), elem))]
        from_sql = sq.commas(
            [sq.table(stage.name, sq.INPUT), sq.json_each(self.x.jsonv(source), alias)]
        )
        # Flattened into the next MATCH, the unwound rows had SQLite put
        # json_each innermost and scan the whole label per row (6 s for 200
        # rows; 0.7 ms kept apart).
        return self._extend(pipeline, stage, live, from_sql, added, sq.UNFLATTENED)

    # --------------------------------------------------------- WITH / RETURN

    def _project(
        self, stage: Stage, proj: a.Projection, for_output: bool
    ) -> tuple[str, dict[str, VarInfo], list[str], list[OutputColumn], bool]:
        scope = stage.scope(sq.INPUT)
        items = self._projection_items(stage, proj, scope)
        aggregating = any(agg for _, _, agg in items)
        has_keys = any(not agg for _, _, agg in items)
        select: list[str] = []
        group_by: list[str] = []
        new_vars: dict[str, VarInfo] = {}
        cols: list[str] = []
        outputs: list[OutputColumn] = []
        alias_scope = Scope(scope.binds)
        for alias, ce, agg in items:
            col = self._new_col()
            select.append(sq.aliased(self.x.output(ce) if for_output else ce.sql, col))
            # A constant key is the same in every group: grouping by it
            # would only make SQLite compare a large value per row.
            if aggregating and not agg and not ce.const:
                group_by.append(ce.sql)
            new_vars[alias] = VarInfo(col, ce.t, ce.names)
            cols.append(col)
            outputs.append(OutputColumn(alias, decode_for(ce.t)))
            alias_scope = alias_scope.with_var(alias, ce)
        sql = sq.select(select, sq.table(stage.name, sq.INPUT), distinct=proj.distinct)
        sql += _grouping(group_by, aggregating and has_keys)
        sql += sq.order_by(self._order_by(proj, alias_scope))
        sql += self._limit(proj, scope)
        single_row = (aggregating and not group_by) or (
            stage.single_row and not aggregating
        )
        return sql, new_vars, cols, outputs, single_row

    def _projection_items(
        self, stage: Stage, proj: a.Projection, scope: Scope
    ) -> list[tuple[str, CE, bool]]:
        """Each projected (alias, value, is_aggregate), `*` first."""
        items: list[tuple[str, CE, bool]] = []
        if proj.star:
            items.extend((var, scope.get(var), False) for var in stage.vars)
        for item in proj.items:
            items.append(
                (item.alias, self.x.compile(item.expr, scope), is_aggregate(item.expr))
            )
        return items

    def _order_by(self, proj: a.Projection, scope: Scope) -> list[str]:
        parts = []
        for item in proj.order:
            ce = self.x.compile(item.expr, scope)
            key = ce.sql if ce.t.is_entity else self.x.native(ce)
            parts.append(sq.descending(key) if item.descending else sq.ascending(key))
        return parts

    def _limit(self, proj: a.Projection, scope: Scope) -> str:
        if proj.limit is None and proj.skip is None:
            return sq.NOTHING
        count = (
            self.x.native(self.x.compile(proj.limit, scope))
            if proj.limit
            else sq.ALL_ROWS
        )
        skip = (
            self.x.native(self.x.compile(proj.skip, scope))
            if proj.skip
            else sq.NO_ROWS_SKIPPED
        )
        return sq.limit(count, skip)

    def _with(self, pipeline: Pipeline, stage: Stage, proj: a.Projection) -> Stage:
        sql, new_vars, cols, _, single_row = self._project(
            stage, proj, for_output=False
        )
        name = self.ctx.fresh(sq.STAGE_PREFIX)
        pipeline.add(name, sql)
        out = Stage(name, new_vars, cols, single_row)
        if single_row:
            # Later stages read these back once; their own rows still come
            # from this stage, so an empty one still empties what follows.
            out = Stage(
                name,
                {
                    var: VarInfo(
                        info.col, info.t, info.names, sq.scalar(info.col, name)
                    )
                    for var, info in new_vars.items()
                },
                [],
                single_row=True,
            )
        if proj.where is None:
            return out
        filtered = self.ctx.fresh(sq.STAGE_PREFIX)
        cond = self.x.boolean(self.x.compile(proj.where, out.scope(sq.INPUT)))
        pipeline.add(
            filtered, sq.select(out.cols, sq.table(name, sq.INPUT), sq.where([cond]))
        )
        return Stage(filtered, out.vars, out.cols, out.single_row)

    def returning(
        self, stage: Stage, proj: a.Projection
    ) -> tuple[str, list[OutputColumn], list[str]]:
        sql, _, cols, outputs, _ = self._project(stage, proj, for_output=True)
        return sql, outputs, cols

    def project_rows(self, stage: Stage) -> tuple[str, list[str]]:
        """Every variable of a stage as a plain column, for materializing it."""
        scope = stage.scope(sq.INPUT)
        parts = [
            sq.aliased(scope.get(var).sql, info.col) for var, info in stage.vars.items()
        ]
        cols = [info.col for info in stage.vars.values()]
        return sq.select(parts, sq.table(stage.name, sq.INPUT)), cols


class _MatchBuilder:
    """Accumulates the FROM items and conditions of one MATCH clause."""

    def __init__(
        self,
        qc: QueryCompiler,
        pipeline: Pipeline,
        stage: Stage,
        where: list[a.Expr],
    ) -> None:
        self.x = qc.x
        self.ctx = qc.ctx
        self.pipeline = pipeline
        self.stage = stage
        self.scope = stage.scope(sq.INPUT)
        self.from_items = [sq.table(stage.name, sq.INPUT)]
        self.conds: list[str] = []
        self.new_vars: dict[str, CE] = {}
        self.fixed_rels: list[str] = []
        # Rows of the variable-length walks; each carries its path's edges.
        self.walks: list[str] = []
        # The WHERE clause's conjuncts; those on one start node alone also
        # narrow where a variable-length walk begins.
        self.where = where

    def add_pattern(self, pattern: a.PathPattern) -> None:
        if pattern.var is not None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_PATH_VARIABLES))
        nodes = [self._node(n) for n in pattern.nodes]
        for i, rel in enumerate(pattern.rels):
            if rel.var_length is not None:
                self._var_length(
                    rel, pattern.nodes[i], pattern.nodes[i + 1], nodes[i], nodes[i + 1]
                )
            else:
                self._rel(rel, nodes[i], nodes[i + 1])

    def _bind(self, var: str, ce: CE) -> None:
        self.new_vars[var] = ce
        self.scope = self.scope.with_var(var, ce)

    def _node(self, np: a.NodePattern) -> CE:
        alias = self.ctx.fresh(sq.NODE_PREFIX)
        self.from_items.append(sq.table(sq.NODES, alias))
        bound = self.scope.get(np.var) if np.var and self.scope.has(np.var) else None
        if bound is not None:
            if bound.t.kind not in (Kind.NODE, Kind.ANY, Kind.NULL):
                raise CompileError(EG_ERR_NOT_NODE.format(name=np.var))
            self.conds.append(sq.eq(sq.column(alias, sq.ID), bound.sql))
        labels = np.labels or self._labels_from_where(np.var)
        names = labels or (bound.names if bound is not None else ())
        if labels:
            self.conds.append(sq.is_in(sq.column(alias, sq.LABEL), labels))
        ce = CE(sq.column(alias, sq.ID), ty.NODE, alias, names)
        self._prop_conditions(alias, np.props, names, is_node=True)
        if np.var and bound is None:
            self._bind(np.var, ce)
        elif np.var and bound is not None:
            # Same node, now joined: later reads use the joined row.
            self.scope = self.scope.with_var(
                np.var, CE(bound.sql, ty.NODE, alias, names, const=bound.const)
            )
        return ce

    def _labels_from_where(self, var: str | None) -> tuple[str, ...]:
        """Labels a WHERE conjunct requires of an unlabelled node, if any.

        `MATCH (n {qualified_name: $qn}) WHERE (n:Function OR n:Method)` is
        the portable spelling of `(n:Function|Method {...})`; reading the
        labels back lets its key lookup stay an index seek.
        """
        if not var:
            return ()
        required: set[str] | None = None
        for cond in self.where:
            labels = _label_alternatives(cond, var)
            if labels is not None:
                required = labels if required is None else required & labels
        return tuple(sorted(required)) if required is not None else ()

    def _prop_conditions(
        self, alias: str, props: a.Expr | None, names: tuple[str, ...], is_node: bool
    ) -> None:
        if props is None:
            return
        if not isinstance(props, a.MapLit):
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what=EG_WHAT_PARAM_PATTERN_PROPS)
            )
        for key, value in props.items:
            rhs = self.x.native(self.x.compile(value, self.scope))
            if is_node and _is_key(names, key):
                self.conds.append(sq.eq(sq.column(alias, sq.KEY), rhs))
            else:
                self.conds.append(sq.eq(sq.prop_of(alias, key), rhs))

    def _rel(self, rp: a.RelPattern, left: CE, right: CE) -> None:
        alias = self.ctx.fresh(sq.REL_PREFIX)
        self.from_items.append(sq.table(sq.EDGES, alias))
        if rp.types:
            self.conds.append(sq.is_in(sq.column(alias, sq.TYPE), rp.types))
        lid, rid = left.sql, right.sql
        if rp.direction == EgDirection.OUT:
            self.conds.append(sq.outgoing(alias, lid, rid))
        elif rp.direction == EgDirection.IN:
            self.conds.append(sq.outgoing(alias, rid, lid))
        else:
            self.conds.append(sq.either_way(alias, lid, rid))
        self._prop_conditions(alias, rp.props, rp.types, is_node=False)
        if rp.var:
            if self.scope.has(rp.var):
                bound = self.scope.get(rp.var)
                if bound.t.kind != Kind.REL:
                    raise CompileError(EG_ERR_NOT_REL.format(name=rp.var))
                self.conds.append(sq.eq(sq.column(alias, sq.ID), bound.sql))
            else:
                self._bind(rp.var, CE(sq.column(alias, sq.ID), ty.REL, alias, rp.types))
        self.fixed_rels.append(alias)

    def uniqueness(self) -> list[str]:
        """No relationship twice in one MATCH, across all its patterns.

        Fixed relationships compare ids; a walk's path must avoid every
        fixed relationship and share none with another walk.
        """
        rels, walks = self.fixed_rels, self.walks
        conds = [
            sq.ne(sq.column(rels[i], sq.ID), sq.column(rels[j], sq.ID))
            for i in range(len(rels))
            for j in range(i + 1, len(rels))
        ]
        conds += [
            sq.path_avoids(sq.column(w, sq.WALK_PATH), sq.column(r, sq.ID))
            for w in walks
            for r in rels
        ]
        conds += [
            sq.paths_disjoint(
                sq.column(walks[i], sq.WALK_PATH), sq.column(walks[j], sq.WALK_PATH)
            )
            for i in range(len(walks))
            for j in range(i + 1, len(walks))
        ]
        return conds

    def _input_seed(self, np: a.NodePattern) -> str | None:
        """Start ids for a path whose end node the input already binds."""
        if not np.var or np.var not in self.stage.vars:
            return None
        info = self.stage.vars[np.var]
        if info.const_sql is not None:
            return sq.input_seed(info.const_sql)
        return sq.input_seeds(info.col, self.stage.name)

    def _static_seed(self, pattern: a.NodePattern) -> str | None:
        """Ids matching what a node pattern and the WHERE say of it alone.

        Seeding a walk from these instead of from its whole label is what
        keeps `MATCH (m:Module)-[*]->(f) WHERE m.qualified_name IN $qns`
        from walking out of every module only to keep a handful.
        """
        labels = pattern.labels or self._labels_from_where(pattern.var)
        np = a.NodePattern(pattern.var, labels, pattern.props)
        conds = []
        if np.labels:
            conds.append(sq.is_in(sq.column(sq.SEED, sq.LABEL), np.labels))
        conds += self._seed_prop_conditions(np)
        conds += self._seed_where_conditions(np)
        if not conds:
            return None
        return sq.seed_ids(conds)

    def _seed_prop_conditions(self, np: a.NodePattern) -> list[str]:
        """The pattern's property tests that read no variable."""
        if not isinstance(np.props, a.MapLit):
            return []
        conds: list[str] = []
        for key, value in np.props.items:
            if free_vars(value):
                continue
            rhs = self.x.native(self.x.compile(value, Scope()))
            if _is_key(np.labels, key):
                conds.append(sq.eq(sq.column(sq.SEED, sq.KEY), rhs))
            else:
                conds.append(sq.eq(sq.prop_of(sq.SEED, key), rhs))
        return conds

    def _seed_where_conditions(self, np: a.NodePattern) -> list[str]:
        """The WHERE conjuncts on this node alone, read against the seed row."""
        if not np.var or np.var in self.stage.vars:
            return []
        node = CE(sq.column(sq.SEED, sq.ID), ty.NODE, sq.SEED, np.labels)
        scope = Scope().with_var(np.var, node)
        return [
            self.x.boolean(self.x.compile(cond, scope))
            for cond in self.where
            if free_vars(cond) == {np.var}
        ]

    def _var_length(
        self,
        rp: a.RelPattern,
        left_np: a.NodePattern,
        right_np: a.NodePattern,
        left: CE,
        right: CE,
    ) -> None:
        if rp.var is not None:
            raise CompileError(EG_ERR_VARLEN_VAR)
        low, high = rp.var_length or (1, None)
        seeds, forward = self._walk_seeds(left_np, right_np)
        # Walked from its far end, an outgoing pattern follows edges in.
        direction_out: bool | None = None
        if rp.direction != EgDirection.BOTH:
            direction_out = (rp.direction == EgDirection.OUT) == forward
        name = self.ctx.fresh(sq.WALK_PREFIX)
        seed_np = left_np if forward else right_np
        edge_conds = self._walk_edge_conditions(rp)
        # The Rust walk filters edges by type alone.
        if edge_conds or not self._walks_natively(seed_np):
            body = sq.walk(name, seeds, direction_out, rp.types, high, edge_conds)
        else:
            body = sq.native_walk(name, seeds, direction_out, rp.types, high)
        self.pipeline.add(sq.walk_columns(name), body)
        alias = self.ctx.fresh(sq.WALK_ROW_PREFIX)
        self.from_items.append(sq.table(name, alias))
        self.walks.append(alias)
        seed_side, node_side = (left, right) if forward else (right, left)
        self.conds.append(sq.eq(sq.column(alias, sq.WALK_SEED), seed_side.sql))
        self.conds.append(sq.eq(sq.column(alias, sq.WALK_NODE), node_side.sql))
        if low > 0:
            self.conds.append(sq.at_least(sq.column(alias, sq.WALK_DEPTH), str(low)))

    def _walk_edge_conditions(self, rp: a.RelPattern) -> list[str]:
        """The pattern's property tests, which every walked edge must pass.

        The walk is its own stage, run before this MATCH binds anything, so
        a value that reads a variable has no row to read it from.
        """
        if rp.props is None:
            return []
        if not isinstance(rp.props, a.MapLit):
            raise CompileError(
                EG_ERR_UNSUPPORTED.format(what=EG_WHAT_PARAM_PATTERN_PROPS)
            )
        conds: list[str] = []
        for key, value in rp.props.items:
            if free_vars(value):
                raise CompileError(
                    EG_ERR_UNSUPPORTED.format(what=EG_WHAT_VARLEN_ROW_PROPS)
                )
            rhs = self.x.native(self.x.compile(value, Scope()))
            conds.append(sq.eq(sq.prop_of(sq.EDGE, key), rhs))
        return conds

    def _walk_seeds(
        self, left_np: a.NodePattern, right_np: a.NodePattern
    ) -> tuple[str, bool]:
        """A walk's start ids, and whether it starts from the left end."""
        seeds = self._input_seed(left_np)
        if seeds is not None:
            return seeds, True
        seeds = self._input_seed(right_np)
        if seeds is not None:
            return seeds, False
        left_seed = self._static_seed(left_np)
        right_seed = self._static_seed(right_np)
        if left_seed is None and right_seed is not None:
            return right_seed, False
        return left_seed or sq.all_node_ids(), True

    def _walks_natively(self, seed_np: a.NodePattern) -> bool:
        # The Rust extension walks from node ids only (native.py); a seed of
        # another type keeps the CTE, which compares it as SQL would.
        seed_var = self.stage.vars.get(seed_np.var) if seed_np.var else None
        return self.ctx.native_walks and (seed_var is None or seed_var.t == ty.NODE)


def _label_alternatives(e: a.Expr, var: str) -> set[str] | None:
    """The labels `e` allows `var`, when it is `var:A` or an OR of those."""
    if (
        isinstance(e, a.LabelTest)
        and isinstance(e.subject, a.Var)
        and e.subject.name == var
        and len(e.labels) == 1
    ):
        return {e.labels[0]}
    if isinstance(e, a.BinOp) and e.op == EgBinary.OR:
        left = _label_alternatives(e.left, var)
        right = _label_alternatives(e.right, var)
        if left is not None and right is not None:
            return left | right
    return None


def _conjuncts(e: a.Expr) -> list[a.Expr]:
    if isinstance(e, a.BinOp) and e.op == EgBinary.AND:
        return _conjuncts(e.left) + _conjuncts(e.right)
    return [e]


def _is_key(labels: tuple[str, ...], key: str) -> bool:
    """Whether `key` is the unique-constraint property of every label."""
    return bool(labels) and all(NODE_UNIQUE_CONSTRAINTS.get(n) == key for n in labels)


def _grouping(group_by: list[str], keyed_aggregate: bool) -> str:
    if group_by:
        return sq.group_by(group_by)
    if keyed_aggregate:
        # Grouping keys over no rows give no groups, unlike a bare
        # aggregate, which gives one row.
        return sq.NONEMPTY_GROUPS
    return sq.NOTHING
