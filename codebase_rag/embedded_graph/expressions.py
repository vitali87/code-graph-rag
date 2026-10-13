"""Cypher expressions compiled to SQLite SQL.

Every compiled value carries its static type, which fixes its SQL
representation:

* node / relationship: the integer row id of `cgr_nodes` / `cgr_edges`
  (lists of them are JSON arrays of ids);
* bool / int / float / string / null: a native SQL value, booleans as 0/1;
* list / map / unknown: JSON text, so a value round-trips exactly.

`native()`, `jsonv()` and `boolean()` convert between them where an operator
needs one form. Semantics follow Memgraph where engines differ (measured
against Memgraph 3.3, issue #386): `1 IN [null]` is false, nulls sort last
ascending, `collect()` drops nulls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from ..constants.embedded_graph import (
    EG_AGGREGATES,
    EG_ERR_UNBOUND,
    EG_ERR_UNKNOWN_FUNCTION,
    EG_ERR_UNSUPPORTED,
    EG_NODE_ID,
    EG_NODE_LABELS,
    EG_NODE_PROPERTIES,
    EG_REL_END,
    EG_REL_START,
    EG_REL_TYPE,
    EgBinary,
    EgQuantifier,
    EgUnary,
)
from ..constants.graph import NODE_UNIQUE_CONSTRAINTS
from . import cypher_ast as a
from . import types as ty
from .types import Kind, T


class CompileError(ValueError):
    pass


@dataclass(frozen=True)
class CE:
    """A compiled expression: SQL text plus what the compiler knows of it."""

    sql: str
    t: T
    # For a node or relationship joined into the current FROM: its alias,
    # so property reads use the joined row instead of a lookup by id.
    alias: str | None = None
    # Static labels (nodes) or relationship types the value can have.
    names: tuple[str, ...] = ()
    # Same value on every row of the current binding table: it reads from
    # a single-row stage (or is a literal/parameter), never from a column.
    const: bool = False


@dataclass
class Bind:
    ce: CE


class Scope:
    """Variables visible at one point of a query, in binding order."""

    def __init__(self, binds: dict[str, Bind] | None = None) -> None:
        self.binds: dict[str, Bind] = dict(binds or {})

    def get(self, name: str) -> CE:
        bind = self.binds.get(name)
        if bind is None:
            raise CompileError(EG_ERR_UNBOUND.format(name=name))
        return bind.ce

    def has(self, name: str) -> bool:
        return name in self.binds

    def with_var(self, name: str, ce: CE) -> Scope:
        out = Scope(self.binds)
        out.binds[name] = Bind(ce)
        return out

    def names(self) -> list[str]:
        return list(self.binds)


@dataclass
class ParamSpec:
    placeholder: str
    name: str
    # "native", "json" or "upper" (prefix range bound for STARTS WITH)
    mode: str


@dataclass
class Ctx:
    """Per-compilation state shared by every expression of one query."""

    param_types: dict[str, T]
    json_arrow: bool
    params: list[ParamSpec] = field(default_factory=list)
    _param_index: dict[tuple[str, str], str] = field(default_factory=dict)
    _counter: int = 0

    def fresh(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def param(self, name: str, mode: str) -> str:
        key = (name, mode)
        placeholder = self._param_index.get(key)
        if placeholder is None:
            placeholder = f":{self.fresh('p')}"
            self._param_index[key] = placeholder
            self.params.append(ParamSpec(placeholder[1:], name, mode))
        return placeholder


MODE_NATIVE = "native"
MODE_JSON = "json"
MODE_UPPER = "upper"
_JSON_NULL_TEXT = "'null'"


def sql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def json_path(key: str) -> str:
    return sql_str('$."' + key.replace('"', '\\"') + '"')


def sql_in_list(names: tuple[str, ...]) -> str:
    return "(" + ", ".join(sql_str(n) for n in names) + ")"


def is_aggregate(e: a.Expr) -> bool:
    if isinstance(e, a.FuncCall) and e.name in EG_AGGREGATES:
        return True
    return any(is_aggregate(c) for c in children(e))


def children(e: a.Expr) -> list[a.Expr]:
    if isinstance(e, a.Prop):
        return [e.subject]
    if isinstance(e, (a.Index,)):
        return [e.subject, e.index]
    if isinstance(e, a.Slice):
        return [x for x in (e.subject, e.start, e.end) if x is not None]
    if isinstance(e, a.MapLit):
        return [v for _, v in e.items]
    if isinstance(e, a.ListLit):
        return list(e.items)
    if isinstance(e, a.FuncCall):
        return list(e.args)
    if isinstance(e, (a.ListComp, a.Quantifier)):
        # The inner expressions see the comprehension variable, not outer
        # aggregates; only the source can hold one.
        return [e.source]
    if isinstance(e, a.Case):
        out = [x for pair in e.whens for x in pair]
        if e.subject is not None:
            out.append(e.subject)
        if e.default is not None:
            out.append(e.default)
        return out
    if isinstance(e, a.BinOp):
        return [e.left, e.right]
    if isinstance(e, (a.UnaryOp, a.IsNull)):
        return [e.operand]
    if isinstance(e, a.LabelTest):
        return [e.subject]
    return []


def free_vars(e: a.Expr, bound: frozenset[str] = frozenset()) -> set[str]:
    if isinstance(e, a.Var):
        return set() if e.name in bound else {e.name}
    if isinstance(e, (a.ListComp, a.Quantifier)):
        inner = bound | {e.var}
        out = free_vars(e.source, bound)
        for sub in (e.where, e.projection) if isinstance(e, a.ListComp) else (e.where,):
            if sub is not None:
                out |= free_vars(sub, inner)
        return out
    out: set[str] = set()
    for c in children(e):
        out |= free_vars(c, bound)
    return out


class ExprCompiler:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------- representations

    def native(self, ce: CE) -> str:
        if ce.t.is_json:
            return f"json_extract({ce.sql}, '$')"
        return ce.sql

    def jsonv(self, ce: CE) -> str:
        """JSON text for storage: entities stay ids, scalars become JSON."""
        kind = ce.t.kind
        if kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING):
            return f"nullif(json_quote({ce.sql}), {_JSON_NULL_TEXT})"
        if kind == Kind.BOOL:
            return f"(CASE WHEN {ce.sql} IS NULL THEN NULL WHEN {ce.sql} THEN 'true' ELSE 'false' END)"
        if kind == Kind.NULL:
            return "NULL"
        return ce.sql

    def embed(self, ce: CE) -> str:
        """A value placed inside json_array/json_object/json_group_array."""
        if ce.t.is_json:
            return f"json({ce.sql})"
        if ce.t.kind == Kind.BOOL:
            return f"json({self.jsonv(ce)})"
        if ce.t.kind == Kind.NULL:
            return "NULL"
        return ce.sql

    def boolean(self, ce: CE) -> str:
        if ce.t.is_json:
            return f"json_extract({ce.sql}, '$')"
        return ce.sql

    def output(self, ce: CE) -> str:
        """SQL whose value the executor decodes into the Python result."""
        kind = ce.t.kind
        if kind == Kind.NODE:
            return node_object_sql(ce.sql)
        if kind == Kind.REL:
            return rel_object_sql(ce.sql)
        if kind == Kind.LIST and ce.t.elem is not None and ce.t.elem.kind == Kind.NODE:
            return (
                f"(SELECT json_group_array(json({node_object_sql('je.value')})) "
                f"FROM json_each({ce.sql}) je)"
            )
        if kind == Kind.LIST and ce.t.elem is not None and ce.t.elem.kind == Kind.REL:
            return (
                f"(SELECT json_group_array(json({rel_object_sql('je.value')})) "
                f"FROM json_each({ce.sql}) je)"
            )
        return ce.sql

    def props_of(self, ce: CE) -> str:
        if ce.alias is not None:
            return f"{ce.alias}.props"
        table = "cgr_edges" if ce.t.kind == Kind.REL else "cgr_nodes"
        return f"(SELECT props FROM {table} WHERE id = {ce.sql})"

    def column_of(self, ce: CE, column: str) -> str:
        if ce.alias is not None:
            return f"{ce.alias}.{column}"
        table = "cgr_edges" if ce.t.kind == Kind.REL else "cgr_nodes"
        return f"(SELECT {column} FROM {table} WHERE id = {ce.sql})"

    def exact_json_at(self, doc: str, path: str) -> str:
        if self.ctx.json_arrow:
            return f"({doc} -> {path})"
        # SQLite before 3.38 has no `->`: rebuild the JSON text from the type.
        return (
            f"(CASE json_type({doc}, {path}) WHEN 'true' THEN 'true' "
            f"WHEN 'false' THEN 'false' WHEN 'null' THEN NULL "
            f"WHEN 'object' THEN json_extract({doc}, {path}) "
            f"WHEN 'array' THEN json_extract({doc}, {path}) "
            f"ELSE json_quote(json_extract({doc}, {path})) END)"
        )

    def typed_extract(self, doc: str, key: str, t: T) -> CE:
        path = json_path(key)
        if t.is_json and t.kind == Kind.ANY:
            return CE(self.exact_json_at(doc, path), t)
        return CE(f"json_extract({doc}, {path})", t)

    # ------------------------------------------------------------ expressions

    def compile(self, e: a.Expr, scope: Scope) -> CE:
        method = _DISPATCH.get(type(e))
        if method is None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=type(e).__name__))
        ce = method(self, e, scope)
        if not ce.const and _is_constant(e, scope):
            return replace(ce, const=True)
        return ce

    def _literal(self, e: a.Literal, scope: Scope) -> CE:
        v = e.value
        if v is None:
            return CE("NULL", ty.NULL)
        if isinstance(v, bool):
            return CE("1" if v else "0", ty.BOOL)
        if isinstance(v, int):
            return CE(str(v), ty.INT)
        if isinstance(v, float):
            return CE(repr(v), ty.FLOAT)
        return CE(sql_str(str(v)), ty.STRING)

    def _param(self, e: a.Param, scope: Scope) -> CE:
        t = self.ctx.param_types.get(e.name, ty.ANY)
        if t.is_json:
            return CE(self.ctx.param(e.name, MODE_JSON), t)
        return CE(self.ctx.param(e.name, MODE_NATIVE), t)

    def _var(self, e: a.Var, scope: Scope) -> CE:
        return scope.get(e.name)

    def _prop(self, e: a.Prop, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        kind = subject.t.kind
        if kind == Kind.NODE:
            if subject.names and all(
                NODE_UNIQUE_CONSTRAINTS.get(n) == e.key for n in subject.names
            ):
                return CE(self.column_of(subject, "key"), ty.STRING)
            t = ty.node_property_type(subject.names, e.key)
            return self.typed_extract(self.props_of(subject), e.key, t)
        if kind == Kind.REL:
            t = ty.rel_property_type(subject.names, e.key)
            return self.typed_extract(self.props_of(subject), e.key, t)
        if kind == Kind.NULL:
            return CE("NULL", ty.NULL)
        return CE(self.exact_json_at(subject.sql, json_path(e.key)), ty.ANY)

    def _map(self, e: a.MapLit, scope: Scope) -> CE:
        if not e.items:
            return CE("json_object()", ty.MAP)
        parts = []
        for key, value in e.items:
            parts.append(sql_str(key))
            parts.append(self.embed(self.compile(value, scope)))
        return CE(f"json_object({', '.join(parts)})", ty.MAP)

    def _list(self, e: a.ListLit, scope: Scope) -> CE:
        items = [self.compile(x, scope) for x in e.items]
        elem = ty.unify([i.t for i in items]) if items else ty.ANY
        if not items:
            return CE("json_array()", ty.list_of(elem))
        return CE(
            f"json_array({', '.join(self.embed(i) for i in items)})", ty.list_of(elem)
        )

    def _index(self, e: a.Index, scope: Scope) -> CE:
        if (
            isinstance(e.subject, a.FuncCall)
            and e.subject.name == "labels"
            and isinstance(e.index, a.Literal)
            and e.index.value == 0
        ):
            node = self.compile(e.subject.args[0], scope)
            return CE(self.column_of(node, "label"), ty.STRING)
        subject = self.compile(e.subject, scope)
        index = self.compile(e.index, scope)
        if subject.t.kind == Kind.MAP or (
            subject.t.kind == Kind.ANY and index.t.kind == Kind.STRING
        ):
            path = f"('$.\"' || {self.native(index)} || '\"')"
            return CE(self.exact_json_at(subject.sql, path), ty.ANY)
        if subject.t.kind in (Kind.NODE, Kind.REL):
            path = f"('$.\"' || {self.native(index)} || '\"')"
            return CE(self.exact_json_at(self.props_of(subject), path), ty.ANY)
        idx = self.native(index)
        path = f"(CASE WHEN {idx} < 0 THEN '$[#' || {idx} || ']' ELSE '$[' || {idx} || ']' END)"
        elem = subject.t.elem or ty.ANY
        return self._element_at(subject.sql, path, elem)

    def _element_at(self, doc: str, path: str, elem: T) -> CE:
        if elem.kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING):
            return CE(f"json_extract({doc}, {path})", elem)
        if elem.kind == Kind.BOOL:
            return CE(f"json_extract({doc}, {path})", elem)
        return CE(self.exact_json_at(doc, path), elem if elem.is_json else ty.ANY)

    def _slice(self, e: a.Slice, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        start = self.native(self.compile(e.start, scope)) if e.start else "NULL"
        end = self.native(self.compile(e.end, scope)) if e.end else "NULL"
        if subject.t.kind == Kind.STRING:
            return CE(f"eg_str_slice({subject.sql}, {start}, {end})", ty.STRING)
        return CE(f"eg_slice({self.jsonv(subject)}, {start}, {end})", subject.t)

    def _quantifier(self, e: a.Quantifier, scope: Scope) -> CE:
        source = self.compile(e.source, scope)
        each, inner = self._each(source, e.var, scope)
        cond = self.boolean(self.compile(e.where, inner))
        exists_true = f"EXISTS (SELECT 1 FROM {each} WHERE ({cond}))"
        exists_false = f"EXISTS (SELECT 1 FROM {each} WHERE NOT ({cond}))"
        exists_null = f"EXISTS (SELECT 1 FROM {each} WHERE ({cond}) IS NULL)"
        src = self.jsonv(source)
        if e.kind == EgQuantifier.ANY:
            body = f"CASE WHEN {exists_true} THEN 1 WHEN {exists_null} THEN NULL ELSE 0 END"
        elif e.kind == EgQuantifier.ALL:
            body = f"CASE WHEN {exists_false} THEN 0 WHEN {exists_null} THEN NULL ELSE 1 END"
        elif e.kind == EgQuantifier.NONE:
            body = f"CASE WHEN {exists_true} THEN 0 WHEN {exists_null} THEN NULL ELSE 1 END"
        else:
            body = f"(SELECT count(*) FROM {each} WHERE ({cond})) = 1"
        return CE(f"(CASE WHEN {src} IS NULL THEN NULL ELSE ({body}) END)", ty.BOOL)

    def _each(self, source: CE, var: str, scope: Scope) -> tuple[str, Scope]:
        """`json_each` over a list, plus a scope binding its element to `var`."""
        alias = self.ctx.fresh("je")
        elem = (source.t.elem if source.t.kind == Kind.LIST else None) or ty.ANY
        value = each_value_sql(alias, elem)
        each = f"json_each({self.jsonv(source)}) AS {alias}"
        names: tuple[str, ...] = ()
        return each, scope.with_var(var, CE(value, elem, None, names))

    def _list_comp(self, e: a.ListComp, scope: Scope) -> CE:
        source = self.compile(e.source, scope)
        each, inner = self._each(source, e.var, scope)
        where = (
            f" WHERE ({self.boolean(self.compile(e.where, inner))})" if e.where else ""
        )
        projection = (
            self.compile(e.projection, inner) if e.projection else inner.get(e.var)
        )
        alias = each.rsplit(" AS ", 1)[1]
        return CE(
            f"(SELECT CASE WHEN {self.jsonv(source)} IS NULL THEN NULL ELSE "
            f"coalesce(json_group_array({self.embed(projection)}), json_array()) END "
            f"FROM (SELECT * FROM {each}{where} ORDER BY {alias}.key) AS {alias})",
            ty.list_of(projection.t),
        )

    def _case(self, e: a.Case, scope: Scope) -> CE:
        whens = [(self.compile(c, scope), self.compile(v, scope)) for c, v in e.whens]
        default = self.compile(e.default, scope) if e.default else CE("NULL", ty.NULL)
        out_t = ty.unify([v.t for _, v in whens] + [default.t])
        conv = self.jsonv if out_t.is_json else self.native
        parts = ["CASE"]
        if e.subject is not None:
            subject = self.native(self.compile(e.subject, scope))
            parts.append(subject)
            for cond, value in whens:
                parts.append(f"WHEN {self.native(cond)} THEN {conv(value)}")
        else:
            for cond, value in whens:
                parts.append(f"WHEN {self.boolean(cond)} THEN {conv(value)}")
        parts.append(f"ELSE {conv(default)} END")
        return CE("(" + " ".join(parts) + ")", out_t)

    def _unary(self, e: a.UnaryOp, scope: Scope) -> CE:
        operand = self.compile(e.operand, scope)
        if e.op == EgUnary.NOT:
            return CE(f"(NOT {self.boolean(operand)})", ty.BOOL)
        if e.op == EgUnary.NEG:
            return CE(
                f"(-{self.native(operand)})",
                operand.t if operand.t.is_native else ty.ANY,
            )
        return operand

    def _is_null(self, e: a.IsNull, scope: Scope) -> CE:
        operand = self.compile(e.operand, scope)
        op = "IS NOT NULL" if e.negated else "IS NULL"
        return CE(f"({operand.sql} {op})", ty.BOOL)

    def _label_test(self, e: a.LabelTest, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        label = self.column_of(subject, "label")
        conds = [f"{label} = {sql_str(lbl)}" for lbl in e.labels]
        return CE(
            f"(CASE WHEN {subject.sql} IS NULL THEN NULL ELSE ({' AND '.join(conds)}) END)",
            ty.BOOL,
        )

    def _binop(self, e: a.BinOp, scope: Scope) -> CE:
        left = self.compile(e.left, scope)
        right = self.compile(e.right, scope)
        op = EgBinary(e.op)
        if op in (EgBinary.AND, EgBinary.OR):
            return CE(
                f"({self.boolean(left)} {op.upper()} {self.boolean(right)})", ty.BOOL
            )
        if op == EgBinary.XOR:
            lb, rb = self.boolean(left), self.boolean(right)
            return CE(f"(({lb} OR {rb}) AND NOT ({lb} AND {rb}))", ty.BOOL)
        if op in (
            EgBinary.EQ,
            EgBinary.NE,
            EgBinary.LT,
            EgBinary.GT,
            EgBinary.LE,
            EgBinary.GE,
        ):
            return self._comparison(op, left, right)
        if op == EgBinary.IN:
            return self._in(left, right)
        if op == EgBinary.STARTS_WITH:
            return self._starts_with(left, right, e)
        if op == EgBinary.ENDS_WITH:
            lv, rv = self.native(left), self.native(right)
            return CE(
                f"(CASE WHEN typeof({lv}) <> 'text' OR typeof({rv}) <> 'text' THEN NULL "
                f"ELSE (length({rv}) = 0 OR substr({lv}, -length({rv})) = {rv}) END)",
                ty.BOOL,
            )
        if op == EgBinary.CONTAINS:
            lv, rv = self.native(left), self.native(right)
            return CE(
                f"(CASE WHEN typeof({lv}) <> 'text' OR typeof({rv}) <> 'text' THEN NULL "
                f"ELSE instr({lv}, {rv}) > 0 END)",
                ty.BOOL,
            )
        if op == EgBinary.REGEX:
            return CE(f"eg_regex({self.native(left)}, {self.native(right)})", ty.BOOL)
        return self._arithmetic(op, left, right)

    def _comparison(self, op: EgBinary, left: CE, right: CE) -> CE:
        sql_op = "<>" if op == EgBinary.NE else op.value
        if left.t.is_json or right.t.is_json:
            lj, rj = left.t, right.t
            if (lj.kind in (Kind.LIST, Kind.MAP)) or (rj.kind in (Kind.LIST, Kind.MAP)):
                return CE(
                    f"(json({self.jsonv(left)}) {sql_op} json({self.jsonv(right)}))",
                    ty.BOOL,
                )
        return CE(f"({self.native(left)} {sql_op} {self.native(right)})", ty.BOOL)

    def _in(self, left: CE, right: CE) -> CE:
        lv = self.native(left)
        alias = self.ctx.fresh("je")
        if right.const:
            # The same list on every row: an uncorrelated IN, which SQLite
            # turns into one ephemeral index instead of a scan per row.
            return CE(
                f"(CASE WHEN {lv} IS NULL THEN NULL WHEN {self.jsonv(right)} IS NULL THEN NULL "
                f"WHEN {lv} IN (SELECT {alias}.value FROM json_each({self.jsonv(right)}) {alias} "
                f"WHERE {alias}.type NOT IN ('null', 'object', 'array')) THEN 1 ELSE 0 END)",
                ty.BOOL,
            )
        return CE(
            f"(CASE WHEN {lv} IS NULL THEN NULL WHEN {self.jsonv(right)} IS NULL THEN NULL "
            f"WHEN EXISTS (SELECT 1 FROM json_each({self.jsonv(right)}) {alias} "
            f"WHERE {alias}.value = {lv} AND {alias}.type NOT IN ('object', 'array')) "
            f"THEN 1 ELSE 0 END)",
            ty.BOOL,
        )

    def _starts_with(self, left: CE, right: CE, e: a.BinOp) -> CE:
        lv, rv = self.native(left), self.native(right)
        exact = (
            f"(CASE WHEN typeof({lv}) <> 'text' OR typeof({rv}) <> 'text' THEN NULL "
            f"ELSE substr({lv}, 1, length({rv})) = {rv} END)"
        )
        # A key column compared with a parameter prefix also gets a range
        # bound the (label, key) index can serve; the exact test stays.
        if lv.endswith(".key") and isinstance(e.right, a.Param):
            upper = self.ctx.param(e.right.name, MODE_UPPER)
            return CE(f"({lv} >= {rv} AND {lv} < {upper} AND {exact})", ty.BOOL)
        return CE(exact, ty.BOOL)

    def _arithmetic(self, op: EgBinary, left: CE, right: CE) -> CE:
        lk, rk = left.t.kind, right.t.kind
        if op == EgBinary.ADD:
            if Kind.LIST in (lk, rk):
                elems = [
                    (side.t.elem or ty.ANY) if side.t.kind == Kind.LIST else side.t
                    for side in (left, right)
                ]
                return CE(
                    f"eg_list_add({self.jsonv(left)}, {self.jsonv(right)})",
                    ty.list_of(ty.unify(elems)),
                )
            if lk == Kind.STRING and rk == Kind.STRING:
                return CE(f"({left.sql} || {right.sql})", ty.STRING)
            if Kind.STRING in (lk, rk):
                return CE(
                    f"eg_concat({self.jsonv(left)}, {self.jsonv(right)})", ty.STRING
                )
            if left.t.is_json or right.t.is_json:
                return CE(f"eg_add({self.jsonv(left)}, {self.jsonv(right)})", ty.ANY)
        if op == EgBinary.POW:
            return CE(f"eg_pow({self.native(left)}, {self.native(right)})", ty.FLOAT)
        # SQLite's own operators already match Cypher's numeric semantics:
        # integer `/` truncates toward zero, either float operand makes a
        # float. The result is a native number either way.
        number = ty.INT if lk == Kind.INT and rk == Kind.INT else ty.FLOAT
        if op == EgBinary.MOD:
            return CE(f"eg_mod({self.native(left)}, {self.native(right)})", number)
        return CE(f"({self.native(left)} {op.value} {self.native(right)})", number)

    # ---------------------------------------------------------------- functions

    def _function(self, e: a.FuncCall, scope: Scope) -> CE:
        if e.name in EG_AGGREGATES:
            return self.aggregate(e, scope)
        handler = _FUNCTIONS.get(e.name)
        if handler is None:
            raise CompileError(EG_ERR_UNKNOWN_FUNCTION.format(name=e.name))
        args = [self.compile(x, scope) for x in e.args]
        return handler(self, args)

    def aggregate(self, e: a.FuncCall, scope: Scope) -> CE:
        distinct = "DISTINCT " if e.distinct else ""
        if e.name == "count":
            if e.star:
                return CE("count(*)", ty.INT)
            arg = self.compile(e.args[0], scope)
            return CE(
                f"count({distinct}{self.native(arg) if not arg.t.is_json else arg.sql})",
                ty.INT,
            )
        arg = self.compile(e.args[0], scope)
        if e.name == "collect":
            value = self.embed(arg) if not arg.t.is_entity else arg.sql
            nonnull = arg.sql if not arg.t.is_json else f"json_type({arg.sql})"
            return CE(
                f"coalesce(json_group_array({distinct}{value}) FILTER (WHERE {nonnull} IS NOT NULL "
                f"AND {nonnull} <> 'null'), json_array())",
                ty.list_of(arg.t),
            )
        native = self.native(arg)
        if e.name == "avg":
            return CE(f"avg({distinct}{native})", ty.FLOAT)
        if e.name == "sum":
            number = ty.INT if arg.t.kind == Kind.INT else ty.FLOAT
            return CE(f"coalesce(sum({distinct}{native}), 0)", number)
        return CE(f"{e.name}({distinct}{native})", arg.t)


_VOLATILE = frozenset({"rand", "timestamp"})


def _is_constant(e: a.Expr, scope: Scope) -> bool:
    if is_aggregate(e) or _calls_volatile(e):
        return False
    return all(scope.has(v) and scope.get(v).const for v in free_vars(e))


def _calls_volatile(e: a.Expr) -> bool:
    if isinstance(e, a.FuncCall) and e.name in _VOLATILE:
        return True
    return any(_calls_volatile(c) for c in children(e)) or (
        isinstance(e, (a.ListComp, a.Quantifier))
        and any(
            _calls_volatile(x)
            for x in (e.where, getattr(e, "projection", None))
            if x is not None
        )
    )


def each_value_sql(alias: str, elem: T) -> str:
    """One element of a `json_each` row, in the representation of `elem`."""
    if elem.kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING, Kind.BOOL):
        return f"{alias}.value"
    # Arrays and objects arrive as JSON text already; scalars need quoting
    # back into JSON so the element keeps its exact type.
    return (
        f"(CASE {alias}.type WHEN 'object' THEN {alias}.value WHEN 'array' THEN {alias}.value "
        f"WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' WHEN 'null' THEN NULL "
        f"ELSE json_quote({alias}.value) END)"
    )


def node_object_sql(id_sql: str) -> str:
    return (
        f"(SELECT json_object('{EG_NODE_ID}', x.id, '{EG_NODE_LABELS}', json_array(x.label), "
        f"'{EG_NODE_PROPERTIES}', json(x.props)) FROM cgr_nodes x WHERE x.id = {id_sql})"
    )


def rel_object_sql(id_sql: str) -> str:
    return (
        f"(SELECT json_object('{EG_NODE_ID}', x.id, '{EG_REL_TYPE}', x.type, "
        f"'{EG_REL_START}', x.src, '{EG_REL_END}', x.dst, '{EG_NODE_PROPERTIES}', json(x.props)) "
        f"FROM cgr_edges x WHERE x.id = {id_sql})"
    )


# --------------------------------------------------------------- scalar library


def _f_id(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(args[0].sql, ty.INT)


def _f_labels(c: ExprCompiler, args: list[CE]) -> CE:
    node = args[0]
    return CE(
        f"(CASE WHEN {node.sql} IS NULL THEN NULL ELSE json_array({c.column_of(node, 'label')}) END)",
        ty.list_of(ty.STRING),
    )


def _f_type(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], "type"), ty.STRING)


def _f_properties(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.is_entity:
        return CE(
            f"(CASE WHEN {arg.sql} IS NULL THEN NULL ELSE {c.props_of(arg)} END)",
            ty.MAP,
        )
    return CE(c.jsonv(arg), ty.MAP)


def _f_keys(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    doc = c.props_of(arg) if arg.t.is_entity else c.jsonv(arg)
    return CE(
        f"(SELECT CASE WHEN {doc} IS NULL THEN NULL ELSE coalesce(json_group_array(k.key), json_array()) END "
        f"FROM (SELECT key FROM json_each({doc}) ORDER BY id) k)",
        ty.list_of(ty.STRING),
    )


def _f_start(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], "src"), ty.NODE)


def _f_end(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], "dst"), ty.NODE)


def _f_coalesce(c: ExprCompiler, args: list[CE]) -> CE:
    t = ty.unify([x.t for x in args])
    if t.is_json or t.kind == Kind.NULL:
        return CE(
            f"coalesce({', '.join(c.jsonv(x) for x in args)})",
            t if t.kind != Kind.NULL else ty.ANY,
        )
    if t.is_entity:
        return CE(f"coalesce({', '.join(x.sql for x in args)})", t)
    return CE(f"coalesce({', '.join(c.native(x) for x in args)})", t)


def _string_fn(sql_name: str) -> Callable[[ExprCompiler, list[CE]], CE]:
    def handler(c: ExprCompiler, args: list[CE]) -> CE:
        return CE(f"{sql_name}({', '.join(c.native(x) for x in args)})", ty.STRING)

    return handler


def _f_substring(c: ExprCompiler, args: list[CE]) -> CE:
    s, start = c.native(args[0]), c.native(args[1])
    if len(args) > 2:
        return CE(f"substr({s}, ({start}) + 1, {c.native(args[2])})", ty.STRING)
    return CE(f"substr({s}, ({start}) + 1)", ty.STRING)


def _f_left(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"substr({c.native(args[0])}, 1, {c.native(args[1])})", ty.STRING)


def _f_right(c: ExprCompiler, args: list[CE]) -> CE:
    s, n = c.native(args[0]), c.native(args[1])
    return CE(f"(CASE WHEN {n} = 0 THEN '' ELSE substr({s}, -({n})) END)", ty.STRING)


def _f_size(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.kind == Kind.STRING:
        return CE(f"length({arg.sql})", ty.INT)
    if arg.t.kind == Kind.LIST:
        return CE(f"json_array_length({arg.sql})", ty.INT)
    return CE(f"eg_size({c.jsonv(arg)})", ty.INT)


def _f_head(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    return c._element_at(c.jsonv(arg), "'$[0]'", arg.t.elem or ty.ANY)


def _f_last(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    return c._element_at(c.jsonv(arg), "'$[#-1]'", arg.t.elem or ty.ANY)


def _f_tail(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"eg_slice({c.jsonv(args[0])}, 1, NULL)", args[0].t)


def _f_range(c: ExprCompiler, args: list[CE]) -> CE:
    step = c.native(args[2]) if len(args) > 2 else "1"
    return CE(
        f"eg_range({c.native(args[0])}, {c.native(args[1])}, {step})",
        ty.list_of(ty.INT),
    )


def _f_split(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(
        f"eg_split({c.native(args[0])}, {c.native(args[1])})", ty.list_of(ty.STRING)
    )


def _f_reverse(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.kind == Kind.STRING:
        return CE(f"eg_reverse_str({arg.sql})", ty.STRING)
    return CE(f"eg_reverse_list({c.jsonv(arg)})", arg.t)


def _f_to_string(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"eg_to_string({c.jsonv(args[0])})", ty.STRING)


def _f_to_integer(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"eg_to_integer({c.jsonv(args[0])})", ty.INT)


def _f_to_float(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"eg_to_float({c.jsonv(args[0])})", ty.FLOAT)


def _f_to_boolean(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"eg_to_boolean({c.jsonv(args[0])})", ty.BOOL)


def _math(
    sql_name: str, out: T | None = None
) -> Callable[[ExprCompiler, list[CE]], CE]:
    def handler(c: ExprCompiler, args: list[CE]) -> CE:
        arg = args[0]
        t = out or (arg.t if arg.t.kind in (Kind.INT, Kind.FLOAT) else ty.FLOAT)
        return CE(f"{sql_name}({', '.join(c.native(x) for x in args)})", t)

    return handler


def _f_exists(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(f"({args[0].sql} IS NOT NULL)", ty.BOOL)


def _f_timestamp(c: ExprCompiler, args: list[CE]) -> CE:
    return CE("eg_timestamp()", ty.INT)


def _f_rand(c: ExprCompiler, args: list[CE]) -> CE:
    return CE("((random() + 9223372036854775808.0) / 18446744073709551616.0)", ty.FLOAT)


def _f_nodes_unsupported(c: ExprCompiler, args: list[CE]) -> CE:
    raise CompileError(EG_ERR_UNSUPPORTED.format(what="path functions"))


_FUNCTIONS: dict[str, Callable[[ExprCompiler, list[CE]], CE]] = {
    "id": _f_id,
    "elementid": _f_id,
    "labels": _f_labels,
    "type": _f_type,
    "properties": _f_properties,
    "keys": _f_keys,
    "startnode": _f_start,
    "endnode": _f_end,
    "coalesce": _f_coalesce,
    "tolower": _string_fn("lower"),
    "toupper": _string_fn("upper"),
    "lower": _string_fn("lower"),
    "upper": _string_fn("upper"),
    "trim": _string_fn("trim"),
    "ltrim": _string_fn("ltrim"),
    "rtrim": _string_fn("rtrim"),
    "replace": _string_fn("replace"),
    "substring": _f_substring,
    "left": _f_left,
    "right": _f_right,
    "size": _f_size,
    "length": _f_size,
    "head": _f_head,
    "last": _f_last,
    "tail": _f_tail,
    "range": _f_range,
    "split": _f_split,
    "reverse": _f_reverse,
    "tostring": _f_to_string,
    "tointeger": _f_to_integer,
    "toint": _f_to_integer,
    "tofloat": _f_to_float,
    "toboolean": _f_to_boolean,
    "abs": _math("abs"),
    "ceil": _math("eg_ceil", ty.FLOAT),
    "floor": _math("eg_floor", ty.FLOAT),
    "round": _math("eg_round", ty.FLOAT),
    "sqrt": _math("eg_sqrt", ty.FLOAT),
    "sign": _math("eg_sign", ty.INT),
    "log": _math("eg_log", ty.FLOAT),
    "exp": _math("eg_exp", ty.FLOAT),
    "exists": _f_exists,
    "timestamp": _f_timestamp,
    "rand": _f_rand,
    "nodes": _f_nodes_unsupported,
    "relationships": _f_nodes_unsupported,
}

# Each handler takes its own Expr subclass, which a Callable over the base
# class cannot express; dispatch is by that class, so the pairing holds.
_DISPATCH: dict[type, Callable[..., CE]] = {
    a.Literal: ExprCompiler._literal,
    a.Param: ExprCompiler._param,
    a.Var: ExprCompiler._var,
    a.Prop: ExprCompiler._prop,
    a.MapLit: ExprCompiler._map,
    a.ListLit: ExprCompiler._list,
    a.Index: ExprCompiler._index,
    a.Slice: ExprCompiler._slice,
    a.FuncCall: ExprCompiler._function,
    a.ListComp: ExprCompiler._list_comp,
    a.Quantifier: ExprCompiler._quantifier,
    a.Case: ExprCompiler._case,
    a.BinOp: ExprCompiler._binop,
    a.UnaryOp: ExprCompiler._unary,
    a.IsNull: ExprCompiler._is_null,
    a.LabelTest: ExprCompiler._label_test,
}
