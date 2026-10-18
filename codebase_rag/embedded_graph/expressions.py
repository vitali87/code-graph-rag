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
from dataclasses import dataclass, field

from ..constants.embedded_graph import (
    EG_AGGREGATES,
    EG_ERR_UNBOUND,
    EG_ERR_UNKNOWN_FUNCTION,
    EG_ERR_UNSUPPORTED,
    EG_VOLATILE_FUNCTIONS,
    EG_WHAT_PATH_FUNCTIONS,
    EgBinary,
    EgFn,
    EgParamMode,
    EgQuantifier,
    EgSqlFn,
    EgUnary,
)
from ..constants.graph import NODE_UNIQUE_CONSTRAINTS
from . import cypher_ast as a
from . import sql as sq
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
    mode: EgParamMode


@dataclass
class Ctx:
    """Per-compilation state shared by every expression of one query."""

    param_types: dict[str, T]
    json_arrow: bool
    # Variable-length walks call the Rust extension (native.py) instead of
    # a recursive CTE.
    native_walks: bool = False
    params: list[ParamSpec] = field(default_factory=list)
    _param_index: dict[tuple[str, EgParamMode], str] = field(default_factory=dict)
    _counter: int = 0

    def fresh(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}"

    def param(self, name: str, mode: EgParamMode) -> str:
        key = (name, mode)
        placeholder = self._param_index.get(key)
        if placeholder is None:
            bind_name = self.fresh(sq.PARAM_PREFIX)
            placeholder = sq.PARAM_SIGIL + bind_name
            self._param_index[key] = placeholder
            self.params.append(ParamSpec(bind_name, name, mode))
        return placeholder


def is_aggregate(e: a.Expr) -> bool:
    if isinstance(e, a.FuncCall) and e.name in EG_AGGREGATES:
        return True
    return any(is_aggregate(c) for c in children(e))


def children(e: a.Expr) -> list[a.Expr]:
    if isinstance(e, (a.Prop, a.LabelTest)):
        return [e.subject]
    if isinstance(e, (a.Index,)):
        return [e.subject, e.index]
    if isinstance(e, a.Slice):
        return _present((e.subject, e.start, e.end))
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
        return [x for pair in e.whens for x in pair] + _present((e.subject, e.default))
    if isinstance(e, a.BinOp):
        return [e.left, e.right]
    if isinstance(e, (a.UnaryOp, a.IsNull)):
        return [e.operand]
    return []


def _present(exprs: tuple[a.Expr | None, ...]) -> list[a.Expr]:
    return [x for x in exprs if x is not None]


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
            return sq.json_value(ce.sql)
        return ce.sql

    def jsonv(self, ce: CE) -> str:
        """JSON text for storage: entities stay ids, scalars become JSON."""
        kind = ce.t.kind
        if kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING):
            return sq.json_of_scalar(ce.sql)
        if kind == Kind.BOOL:
            return sq.json_of_bool(ce.sql)
        if kind == Kind.NULL:
            return sq.NULL
        return ce.sql

    def embed(self, ce: CE) -> str:
        """A value placed inside json_array/json_object/json_group_array."""
        if ce.t.is_json:
            return sq.json_text(ce.sql)
        if ce.t.kind == Kind.BOOL:
            return sq.json_text(self.jsonv(ce))
        if ce.t.kind == Kind.NULL:
            return sq.NULL
        return ce.sql

    def boolean(self, ce: CE) -> str:
        # SQLite reads JSON true/false back as 1/0, so a condition is just
        # the native value; callers still name which use they mean.
        return self.native(ce)

    def output(self, ce: CE) -> str:
        """SQL whose value the executor decodes into the Python result."""
        return self._output(ce.sql, ce.t)

    def _output(self, sql: str, t: T) -> str:
        # Nodes and relationships travel as ids; a result shows them whole,
        # at any depth: `nxalg.strongly_connected_components` yields lists
        # of lists of nodes.
        if t.kind in (Kind.NODE, Kind.REL):
            return sq.entity_object(sql, rel=t.kind == Kind.REL)
        if t.kind == Kind.LIST and t.elem is not None and _holds_entities(t.elem):
            alias = self.ctx.fresh(sq.EACH_PREFIX)
            element = self._output(sq.column(alias, sq.VALUE), t.elem)
            return sq.null_if_null(sql, sq.map_list(sql, alias, element))
        return sql

    def props_of(self, ce: CE) -> str:
        return self.column_of(ce, sq.PROPS)

    def column_of(self, ce: CE, name: str) -> str:
        if ce.alias is not None:
            return sq.column(ce.alias, name)
        source = sq.EDGES if ce.t.kind == Kind.REL else sq.NODES
        return sq.lookup(source, name, ce.sql)

    def exact_json_at(self, doc: str, path: str) -> str:
        return sq.json_at(doc, path, self.ctx.json_arrow)

    def typed_extract(self, doc: str, key: str, t: T) -> CE:
        path = sq.json_path(key)
        if t.is_json and t.kind == Kind.ANY:
            return CE(self.exact_json_at(doc, path), t)
        return CE(sq.json_extract(doc, path), t)

    # ------------------------------------------------------------ expressions

    def compile(self, e: a.Expr, scope: Scope) -> CE:
        method = _DISPATCH.get(type(e))
        if method is None:
            raise CompileError(EG_ERR_UNSUPPORTED.format(what=type(e).__name__))
        ce = method(self, e, scope)
        if not ce.const and _is_constant(e, scope):
            return CE(ce.sql, ce.t, ce.alias, ce.names, const=True)
        return ce

    def _literal(self, e: a.Literal, _scope: Scope) -> CE:
        v = e.value
        if v is None:
            return CE(sq.NULL, ty.NULL)
        if isinstance(v, bool):
            return CE(sq.TRUE if v else sq.FALSE, ty.BOOL)
        if isinstance(v, int):
            return CE(str(v), ty.INT)
        if isinstance(v, float):
            return CE(repr(v), ty.FLOAT)
        return CE(sq.sql_str(str(v)), ty.STRING)

    def _param(self, e: a.Param, _scope: Scope) -> CE:
        t = self.ctx.param_types.get(e.name, ty.ANY)
        if t.is_json:
            return CE(self.ctx.param(e.name, EgParamMode.JSON), t)
        return CE(self.ctx.param(e.name, EgParamMode.NATIVE), t)

    def _var(self, e: a.Var, scope: Scope) -> CE:
        return scope.get(e.name)

    def _prop(self, e: a.Prop, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        kind = subject.t.kind
        if kind == Kind.NODE:
            if subject.names and all(
                NODE_UNIQUE_CONSTRAINTS.get(n) == e.key for n in subject.names
            ):
                return CE(self.column_of(subject, sq.KEY), ty.STRING)
            t = ty.node_property_type(subject.names, e.key)
            return self.typed_extract(self.props_of(subject), e.key, t)
        if kind == Kind.REL:
            t = ty.rel_property_type(subject.names, e.key)
            return self.typed_extract(self.props_of(subject), e.key, t)
        if kind == Kind.NULL:
            return CE(sq.NULL, ty.NULL)
        return CE(self.exact_json_at(subject.sql, sq.json_path(e.key)), ty.ANY)

    def _map(self, e: a.MapLit, scope: Scope) -> CE:
        if not e.items:
            return CE(sq.EMPTY_JSON_OBJECT, ty.MAP)
        parts = []
        for key, value in e.items:
            parts.append(sq.sql_str(key))
            parts.append(self.embed(self.compile(value, scope)))
        return CE(sq.json_object(parts), ty.MAP)

    def _list(self, e: a.ListLit, scope: Scope) -> CE:
        items = [self.compile(x, scope) for x in e.items]
        elem = ty.unify([i.t for i in items]) if items else ty.ANY
        if not items:
            return CE(sq.EMPTY_JSON_ARRAY, ty.list_of(elem))
        return CE(sq.json_array(self.embed(i) for i in items), ty.list_of(elem))

    def _index(self, e: a.Index, scope: Scope) -> CE:
        if (
            isinstance(e.subject, a.FuncCall)
            and e.subject.name == EgFn.LABELS
            and isinstance(e.index, a.Literal)
            and e.index.value == 0
        ):
            node = self.compile(e.subject.args[0], scope)
            return CE(self.column_of(node, sq.LABEL), ty.STRING)
        subject = self.compile(e.subject, scope)
        index = self.compile(e.index, scope)
        if subject.t.kind == Kind.MAP or (
            subject.t.kind == Kind.ANY and index.t.kind == Kind.STRING
        ):
            path = sq.dynamic_key_path(self.native(index))
            return CE(self.exact_json_at(subject.sql, path), ty.ANY)
        if subject.t.kind in (Kind.NODE, Kind.REL):
            path = sq.dynamic_key_path(self.native(index))
            return CE(self.exact_json_at(self.props_of(subject), path), ty.ANY)
        path = sq.dynamic_index_path(self.native(index))
        elem = subject.t.elem or ty.ANY
        return self._element_at(subject.sql, path, elem)

    def _element_at(self, doc: str, path: str, elem: T) -> CE:
        if elem.kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING):
            return CE(sq.json_extract(doc, path), elem)
        if elem.kind == Kind.BOOL:
            return CE(sq.json_extract(doc, path), elem)
        return CE(self.exact_json_at(doc, path), elem if elem.is_json else ty.ANY)

    def _slice(self, e: a.Slice, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        start = self.native(self.compile(e.start, scope)) if e.start else sq.NULL
        end = self.native(self.compile(e.end, scope)) if e.end else sq.NULL
        if subject.t.kind == Kind.STRING:
            return CE(sq.call(EgSqlFn.STR_SLICE, subject.sql, start, end), ty.STRING)
        return CE(sq.call(EgSqlFn.SLICE, self.jsonv(subject), start, end), subject.t)

    def _quantifier(self, e: a.Quantifier, scope: Scope) -> CE:
        source = self.compile(e.source, scope)
        each, _, inner = self._each(source, e.var, scope)
        cond = sq.paren(self.boolean(self.compile(e.where, inner)))
        some_true = sq.exists(each, cond)
        some_false = sq.exists(each, sq.negate(cond))
        some_null = sq.exists(each, sq.is_null(cond))
        if e.kind == EgQuantifier.ANY:
            body = sq.case(None, [(some_true, sq.TRUE), (some_null, sq.NULL)], sq.FALSE)
        elif e.kind == EgQuantifier.ALL:
            body = sq.case(
                None, [(some_false, sq.FALSE), (some_null, sq.NULL)], sq.TRUE
            )
        elif e.kind == EgQuantifier.NONE:
            body = sq.case(None, [(some_true, sq.FALSE), (some_null, sq.NULL)], sq.TRUE)
        else:
            # Memgraph: one true element is true whatever else is null; with
            # none true, a null leaves it unknown.
            matches = sq.count_where(each, cond)
            body = sq.case(
                None,
                [
                    (sq.eq(matches, sq.ONE), sq.TRUE),
                    (sq.all_of([sq.eq(matches, sq.ZERO), some_null]), sq.NULL),
                ],
                sq.FALSE,
            )
        return CE(sq.null_if_null(self.jsonv(source), body), ty.BOOL)

    def _each(self, source: CE, var: str, scope: Scope) -> tuple[str, str, Scope]:
        """`json_each` over a list and its alias, plus a scope binding its
        element to `var`."""
        alias = self.ctx.fresh(sq.EACH_PREFIX)
        elem = (source.t.elem if source.t.kind == Kind.LIST else None) or ty.ANY
        value = each_value_sql(alias, elem)
        each = sq.json_each(self.jsonv(source), alias)
        names: tuple[str, ...] = ()
        return each, alias, scope.with_var(var, CE(value, elem, None, names))

    def _list_comp(self, e: a.ListComp, scope: Scope) -> CE:
        source = self.compile(e.source, scope)
        each, alias, inner = self._each(source, e.var, scope)
        filters = [self.boolean(self.compile(e.where, inner))] if e.where else []
        projection = (
            self.compile(e.projection, inner) if e.projection else inner.get(e.var)
        )
        return CE(
            sq.list_comprehension(
                self.jsonv(source), self.embed(projection), each, alias, filters
            ),
            ty.list_of(projection.t),
        )

    def _case(self, e: a.Case, scope: Scope) -> CE:
        whens = [(self.compile(c, scope), self.compile(v, scope)) for c, v in e.whens]
        default = self.compile(e.default, scope) if e.default else CE(sq.NULL, ty.NULL)
        out_t = ty.unify([v.t for _, v in whens] + [default.t])
        conv = self.jsonv if out_t.is_json else self.native
        subject = None
        # A simple CASE compares values; a searched CASE tests conditions.
        test = self.boolean
        if e.subject is not None:
            subject = self.native(self.compile(e.subject, scope))
            test = self.native
        arms = [(test(cond), conv(value)) for cond, value in whens]
        return CE(sq.case(subject, arms, conv(default)), out_t)

    def _unary(self, e: a.UnaryOp, scope: Scope) -> CE:
        operand = self.compile(e.operand, scope)
        if e.op == EgUnary.NOT:
            return CE(sq.negate(self.boolean(operand)), ty.BOOL)
        if e.op == EgUnary.NEG:
            return CE(
                sq.minus(self.native(operand)),
                operand.t if operand.t.is_native else ty.ANY,
            )
        return operand

    def _is_null(self, e: a.IsNull, scope: Scope) -> CE:
        operand = self.compile(e.operand, scope)
        return CE(sq.is_null(operand.sql, e.negated), ty.BOOL)

    def _label_test(self, e: a.LabelTest, scope: Scope) -> CE:
        subject = self.compile(e.subject, scope)
        label = self.column_of(subject, sq.LABEL)
        conds = [sq.eq(label, sq.sql_str(lbl)) for lbl in e.labels]
        return CE(sq.null_if_null(subject.sql, sq.paren(sq.all_of(conds))), ty.BOOL)

    def _binop(self, e: a.BinOp, scope: Scope) -> CE:
        left = self.compile(e.left, scope)
        right = self.compile(e.right, scope)
        op = EgBinary(e.op)
        if op in (EgBinary.AND, EgBinary.OR):
            return CE(
                sq.binary(self.boolean(left), op.upper(), self.boolean(right)), ty.BOOL
            )
        if op == EgBinary.XOR:
            return CE(sq.xor(self.boolean(left), self.boolean(right)), ty.BOOL)
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
            return CE(sq.ends_with(self.native(left), self.native(right)), ty.BOOL)
        if op == EgBinary.CONTAINS:
            return CE(sq.contains(self.native(left), self.native(right)), ty.BOOL)
        if op == EgBinary.REGEX:
            return CE(
                sq.call(EgSqlFn.REGEX, self.native(left), self.native(right)), ty.BOOL
            )
        return self._arithmetic(op, left, right)

    def _comparison(self, op: EgBinary, left: CE, right: CE) -> CE:
        if left.t.is_json or right.t.is_json:
            lj, rj = left.t, right.t
            if (lj.kind in (Kind.LIST, Kind.MAP)) or (rj.kind in (Kind.LIST, Kind.MAP)):
                return CE(
                    sq.binary(
                        sq.json_text(self.jsonv(left)),
                        op.value,
                        sq.json_text(self.jsonv(right)),
                    ),
                    ty.BOOL,
                )
        return CE(sq.binary(self.native(left), op.value, self.native(right)), ty.BOOL)

    def _in(self, left: CE, right: CE) -> CE:
        lv = self.native(left)
        alias = self.ctx.fresh(sq.EACH_PREFIX)
        if right.const:
            return CE(sq.in_constant_list(lv, self.jsonv(right), alias), ty.BOOL)
        return CE(sq.in_list(lv, self.jsonv(right), alias), ty.BOOL)

    def _starts_with(self, left: CE, right: CE, e: a.BinOp) -> CE:
        lv, rv = self.native(left), self.native(right)
        exact = sq.starts_with(lv, rv)
        # A key column compared with a parameter prefix also gets a range
        # bound the (label, key) index can serve; the exact test stays.
        if sq.is_key_column(lv) and isinstance(e.right, a.Param):
            upper = self.ctx.param(e.right.name, EgParamMode.UPPER)
            return CE(sq.prefix_range(lv, rv, upper, exact), ty.BOOL)
        return CE(exact, ty.BOOL)

    def _arithmetic(self, op: EgBinary, left: CE, right: CE) -> CE:
        if op == EgBinary.ADD:
            return self._add(left, right)
        if op == EgBinary.POW:
            return CE(
                sq.call(EgSqlFn.POW, self.native(left), self.native(right)), ty.FLOAT
            )
        return self._numeric(op, left, right)

    def _add(self, left: CE, right: CE) -> CE:
        lk, rk = left.t.kind, right.t.kind
        if Kind.LIST in (lk, rk):
            elems = [
                (side.t.elem or ty.ANY) if side.t.kind == Kind.LIST else side.t
                for side in (left, right)
            ]
            return CE(
                sq.call(EgSqlFn.LIST_ADD, self.jsonv(left), self.jsonv(right)),
                ty.list_of(ty.unify(elems)),
            )
        if lk == Kind.STRING and rk == Kind.STRING:
            return CE(sq.concat(left.sql, right.sql), ty.STRING)
        if Kind.STRING in (lk, rk):
            return CE(
                sq.call(EgSqlFn.CONCAT, self.jsonv(left), self.jsonv(right)),
                ty.STRING,
            )
        if left.t.is_json or right.t.is_json:
            return CE(sq.call(EgSqlFn.ADD, self.jsonv(left), self.jsonv(right)), ty.ANY)
        return self._numeric(EgBinary.ADD, left, right)

    def _numeric(self, op: EgBinary, left: CE, right: CE) -> CE:
        # SQLite's own operators already match Cypher's numeric semantics:
        # integer `/` truncates toward zero, either float operand makes a
        # float. The result is a native number either way.
        both_int = left.t.kind == Kind.INT and right.t.kind == Kind.INT
        number = ty.INT if both_int else ty.FLOAT
        if op == EgBinary.MOD:
            return CE(
                sq.call(EgSqlFn.MOD, self.native(left), self.native(right)), number
            )
        return CE(sq.binary(self.native(left), op.value, self.native(right)), number)

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
        if e.name == EgFn.COUNT:
            if e.star:
                return CE(sq.COUNT_ALL, ty.INT)
            arg = self.compile(e.args[0], scope)
            value = self.native(arg) if not arg.t.is_json else arg.sql
            return CE(sq.aggregate(e.name, value, e.distinct), ty.INT)
        arg = self.compile(e.args[0], scope)
        if e.name == EgFn.COLLECT:
            value = self.embed(arg) if not arg.t.is_entity else arg.sql
            present = arg.sql if not arg.t.is_json else sq.json_type(arg.sql)
            return CE(sq.collect(value, present, e.distinct), ty.list_of(arg.t))
        native = self.native(arg)
        if e.name == EgFn.AVG:
            return CE(sq.aggregate(e.name, native, e.distinct), ty.FLOAT)
        if e.name == EgFn.SUM:
            number = ty.INT if arg.t.kind == Kind.INT else ty.FLOAT
            return CE(sq.sum_or_zero(native, e.distinct), number)
        return CE(sq.aggregate(e.name, native, e.distinct), arg.t)


def _holds_entities(t: T) -> bool:
    if t.kind in (Kind.NODE, Kind.REL):
        return True
    return t.kind == Kind.LIST and t.elem is not None and _holds_entities(t.elem)


def _is_constant(e: a.Expr, scope: Scope) -> bool:
    if is_aggregate(e) or _calls_volatile(e):
        return False
    return all(scope.has(v) and scope.get(v).const for v in free_vars(e))


def _calls_volatile(e: a.Expr) -> bool:
    if isinstance(e, a.FuncCall) and e.name in EG_VOLATILE_FUNCTIONS:
        return True
    # `children` leaves out a comprehension's inner expressions, which see
    # its variable rather than the outer scope; a volatile call there still
    # makes the whole expression volatile.
    inner: tuple[a.Expr | None, ...] = ()
    if isinstance(e, a.ListComp):
        inner = (e.where, e.projection)
    elif isinstance(e, a.Quantifier):
        inner = (e.where,)
    return any(_calls_volatile(c) for c in children(e)) or any(
        _calls_volatile(x) for x in inner if x is not None
    )


def each_value_sql(alias: str, elem: T) -> str:
    """One element of a `json_each` row, in the representation of `elem`."""
    if elem.kind in (Kind.NODE, Kind.REL, Kind.INT, Kind.FLOAT, Kind.STRING, Kind.BOOL):
        return sq.column(alias, sq.VALUE)
    return sq.each_element(alias)


# --------------------------------------------------------------- scalar library


def _f_id(_c: ExprCompiler, args: list[CE]) -> CE:
    return CE(args[0].sql, ty.INT)


def _f_labels(c: ExprCompiler, args: list[CE]) -> CE:
    node = args[0]
    return CE(
        sq.labels_of(node.sql, c.column_of(node, sq.LABEL)), ty.list_of(ty.STRING)
    )


def _f_type(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], sq.TYPE), ty.STRING)


def _f_properties(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.is_entity:
        return CE(sq.null_if_null(arg.sql, c.props_of(arg)), ty.MAP)
    return CE(c.jsonv(arg), ty.MAP)


def _f_keys(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    doc = c.props_of(arg) if arg.t.is_entity else c.jsonv(arg)
    return CE(sq.keys_of(doc), ty.list_of(ty.STRING))


def _f_start(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], sq.SRC), ty.NODE)


def _f_end(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(c.column_of(args[0], sq.DST), ty.NODE)


def _f_coalesce(c: ExprCompiler, args: list[CE]) -> CE:
    t = ty.unify([x.t for x in args])
    if t.is_json or t.kind == Kind.NULL:
        return CE(
            sq.call(EgSqlFn.COALESCE, *(c.jsonv(x) for x in args)),
            t if t.kind != Kind.NULL else ty.ANY,
        )
    if t.is_entity:
        return CE(sq.call(EgSqlFn.COALESCE, *(x.sql for x in args)), t)
    return CE(sq.call(EgSqlFn.COALESCE, *(c.native(x) for x in args)), t)


def _string_fn(sql_name: EgSqlFn) -> Callable[[ExprCompiler, list[CE]], CE]:
    def handler(c: ExprCompiler, args: list[CE]) -> CE:
        return CE(sq.call(sql_name, *(c.native(x) for x in args)), ty.STRING)

    return handler


def _f_substring(c: ExprCompiler, args: list[CE]) -> CE:
    length = c.native(args[2]) if len(args) > 2 else None
    return CE(sq.substring(c.native(args[0]), c.native(args[1]), length), ty.STRING)


def _f_left(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(sq.left_part(c.native(args[0]), c.native(args[1])), ty.STRING)


def _f_right(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(sq.right_part(c.native(args[0]), c.native(args[1])), ty.STRING)


def _f_size(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.kind == Kind.STRING:
        return CE(sq.call(EgSqlFn.LENGTH, arg.sql), ty.INT)
    if arg.t.kind == Kind.LIST:
        return CE(sq.call(EgSqlFn.JSON_ARRAY_LENGTH, arg.sql), ty.INT)
    return CE(sq.call(EgSqlFn.SIZE, c.jsonv(arg)), ty.INT)


def _f_head(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    return c._element_at(c.jsonv(arg), sq.JSON_FIRST, arg.t.elem or ty.ANY)


def _f_last(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    return c._element_at(c.jsonv(arg), sq.JSON_LAST, arg.t.elem or ty.ANY)


def _f_tail(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(sq.call(EgSqlFn.SLICE, c.jsonv(args[0]), sq.ONE, sq.NULL), args[0].t)


def _f_range(c: ExprCompiler, args: list[CE]) -> CE:
    step = c.native(args[2]) if len(args) > 2 else sq.ONE
    return CE(
        sq.call(EgSqlFn.RANGE, c.native(args[0]), c.native(args[1]), step),
        ty.list_of(ty.INT),
    )


def _f_split(c: ExprCompiler, args: list[CE]) -> CE:
    return CE(
        sq.call(EgSqlFn.SPLIT, c.native(args[0]), c.native(args[1])),
        ty.list_of(ty.STRING),
    )


def _f_reverse(c: ExprCompiler, args: list[CE]) -> CE:
    arg = args[0]
    if arg.t.kind == Kind.STRING:
        return CE(sq.call(EgSqlFn.REVERSE_STR, arg.sql), ty.STRING)
    return CE(sq.call(EgSqlFn.REVERSE_LIST, c.jsonv(arg)), arg.t)


def _converter(sql_name: EgSqlFn, out: T) -> Callable[[ExprCompiler, list[CE]], CE]:
    def handler(c: ExprCompiler, args: list[CE]) -> CE:
        return CE(sq.call(sql_name, c.jsonv(args[0])), out)

    return handler


def _math(
    sql_name: EgSqlFn, out: T | None = None
) -> Callable[[ExprCompiler, list[CE]], CE]:
    def handler(c: ExprCompiler, args: list[CE]) -> CE:
        arg = args[0]
        t = out or (arg.t if arg.t.kind in (Kind.INT, Kind.FLOAT) else ty.FLOAT)
        return CE(sq.call(sql_name, *(c.native(x) for x in args)), t)

    return handler


def _f_exists(_c: ExprCompiler, args: list[CE]) -> CE:
    return CE(sq.is_null(args[0].sql, negated=True), ty.BOOL)


def _f_timestamp(_c: ExprCompiler, _args: list[CE]) -> CE:
    return CE(sq.call(EgSqlFn.TIMESTAMP), ty.INT)


def _f_rand(_c: ExprCompiler, _args: list[CE]) -> CE:
    return CE(sq.RANDOM_UNIT, ty.FLOAT)


def _f_nodes_unsupported(_c: ExprCompiler, _args: list[CE]) -> CE:
    raise CompileError(EG_ERR_UNSUPPORTED.format(what=EG_WHAT_PATH_FUNCTIONS))


_FUNCTIONS: dict[str, Callable[[ExprCompiler, list[CE]], CE]] = {
    EgFn.ID: _f_id,
    EgFn.ELEMENT_ID: _f_id,
    EgFn.LABELS: _f_labels,
    EgFn.TYPE: _f_type,
    EgFn.PROPERTIES: _f_properties,
    EgFn.KEYS: _f_keys,
    EgFn.START_NODE: _f_start,
    EgFn.END_NODE: _f_end,
    EgFn.COALESCE: _f_coalesce,
    EgFn.TO_LOWER: _string_fn(EgSqlFn.LOWER),
    EgFn.TO_UPPER: _string_fn(EgSqlFn.UPPER),
    EgFn.LOWER: _string_fn(EgSqlFn.LOWER),
    EgFn.UPPER: _string_fn(EgSqlFn.UPPER),
    EgFn.TRIM: _string_fn(EgSqlFn.TRIM),
    EgFn.LTRIM: _string_fn(EgSqlFn.LTRIM),
    EgFn.RTRIM: _string_fn(EgSqlFn.RTRIM),
    EgFn.REPLACE: _string_fn(EgSqlFn.REPLACE),
    EgFn.SUBSTRING: _f_substring,
    EgFn.LEFT: _f_left,
    EgFn.RIGHT: _f_right,
    EgFn.SIZE: _f_size,
    EgFn.LENGTH: _f_size,
    EgFn.HEAD: _f_head,
    EgFn.LAST: _f_last,
    EgFn.TAIL: _f_tail,
    EgFn.RANGE: _f_range,
    EgFn.SPLIT: _f_split,
    EgFn.REVERSE: _f_reverse,
    EgFn.TO_STRING: _converter(EgSqlFn.TO_STRING, ty.STRING),
    EgFn.TO_INTEGER: _converter(EgSqlFn.TO_INTEGER, ty.INT),
    EgFn.TO_INT: _converter(EgSqlFn.TO_INTEGER, ty.INT),
    EgFn.TO_FLOAT: _converter(EgSqlFn.TO_FLOAT, ty.FLOAT),
    EgFn.TO_BOOLEAN: _converter(EgSqlFn.TO_BOOLEAN, ty.BOOL),
    EgFn.ABS: _math(EgSqlFn.ABS),
    EgFn.CEIL: _math(EgSqlFn.CEIL, ty.FLOAT),
    EgFn.FLOOR: _math(EgSqlFn.FLOOR, ty.FLOAT),
    EgFn.ROUND: _math(EgSqlFn.ROUND, ty.FLOAT),
    EgFn.SQRT: _math(EgSqlFn.SQRT, ty.FLOAT),
    EgFn.SIGN: _math(EgSqlFn.SIGN, ty.INT),
    EgFn.LOG: _math(EgSqlFn.LOG, ty.FLOAT),
    EgFn.EXP: _math(EgSqlFn.EXP, ty.FLOAT),
    EgFn.EXISTS: _f_exists,
    EgFn.TIMESTAMP: _f_timestamp,
    EgFn.RAND: _f_rand,
    EgFn.NODES: _f_nodes_unsupported,
    EgFn.RELATIONSHIPS: _f_nodes_unsupported,
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
