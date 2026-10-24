"""Syntax tree for the openCypher subset the embedded engine runs.

Frozen dataclasses so a parsed query can be cached and shared between
connections: compilation never mutates the tree it reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..constants.embedded_graph import EgDirection, EgSchemaKind

# ---------------------------------------------------------------- expressions


@dataclass(frozen=True)
class Expr:
    pass


@dataclass(frozen=True)
class Literal(Expr):
    value: str | int | float | bool | None


@dataclass(frozen=True)
class Param(Expr):
    name: str


@dataclass(frozen=True)
class Var(Expr):
    name: str


@dataclass(frozen=True)
class Prop(Expr):
    subject: Expr
    key: str


@dataclass(frozen=True)
class MapLit(Expr):
    items: tuple[tuple[str, Expr], ...]


@dataclass(frozen=True)
class ListLit(Expr):
    items: tuple[Expr, ...]


@dataclass(frozen=True)
class Index(Expr):
    subject: Expr
    index: Expr


@dataclass(frozen=True)
class Slice(Expr):
    subject: Expr
    start: Expr | None
    end: Expr | None


@dataclass(frozen=True)
class FuncCall(Expr):
    name: str
    args: tuple[Expr, ...]
    distinct: bool = False
    star: bool = False


@dataclass(frozen=True)
class ListComp(Expr):
    var: str
    source: Expr
    where: Expr | None
    projection: Expr | None


@dataclass(frozen=True)
class Quantifier(Expr):
    kind: str  # EgQuantifier
    var: str
    source: Expr
    where: Expr


@dataclass(frozen=True)
class Case(Expr):
    subject: Expr | None
    whens: tuple[tuple[Expr, Expr], ...]
    default: Expr | None


@dataclass(frozen=True)
class BinOp(Expr):
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str
    operand: Expr


@dataclass(frozen=True)
class IsNull(Expr):
    operand: Expr
    negated: bool


@dataclass(frozen=True)
class LabelTest(Expr):
    subject: Expr
    labels: tuple[str, ...]


# ------------------------------------------------------------------- patterns


@dataclass(frozen=True)
class NodePattern:
    var: str | None
    # `(n:A|B)`: any of these labels. Empty means unconstrained.
    labels: tuple[str, ...] = ()
    props: Expr | None = None


@dataclass(frozen=True)
class RelPattern:
    var: str | None
    types: tuple[str, ...] = ()
    direction: EgDirection = EgDirection.OUT
    props: Expr | None = None
    # (min, max) hops for a variable-length relationship; max None = unbounded
    var_length: tuple[int, int | None] | None = None


@dataclass(frozen=True)
class PathPattern:
    var: str | None
    nodes: tuple[NodePattern, ...]
    rels: tuple[RelPattern, ...]


# -------------------------------------------------------------------- clauses


@dataclass(frozen=True)
class Clause:
    pass


@dataclass(frozen=True)
class Match(Clause):
    optional: bool
    patterns: tuple[PathPattern, ...]
    where: Expr | None


@dataclass(frozen=True)
class Unwind(Clause):
    expr: Expr
    var: str


@dataclass(frozen=True)
class ProjectionItem:
    expr: Expr
    alias: str


@dataclass(frozen=True)
class SortItem:
    expr: Expr
    descending: bool


@dataclass(frozen=True)
class Projection(Clause):
    """WITH or RETURN."""

    is_return: bool
    distinct: bool
    star: bool
    items: tuple[ProjectionItem, ...]
    order: tuple[SortItem, ...] = ()
    skip: Expr | None = None
    limit: Expr | None = None
    where: Expr | None = None


@dataclass(frozen=True)
class SetItem:
    kind: str  # EgSetKind
    var: str
    key: str | None
    value: Expr | None


@dataclass(frozen=True)
class Create(Clause):
    patterns: tuple[PathPattern, ...]


@dataclass(frozen=True)
class Merge(Clause):
    pattern: PathPattern
    on_create: tuple[SetItem, ...] = ()
    on_match: tuple[SetItem, ...] = ()


@dataclass(frozen=True)
class SetClause(Clause):
    items: tuple[SetItem, ...]


@dataclass(frozen=True)
class RemoveItem:
    kind: str  # EgRemoveKind
    var: str
    key: str


@dataclass(frozen=True)
class Remove(Clause):
    items: tuple[RemoveItem, ...]


@dataclass(frozen=True)
class Delete(Clause):
    detach: bool
    exprs: tuple[Expr, ...]


@dataclass(frozen=True)
class Foreach(Clause):
    var: str
    source: Expr
    clauses: tuple[Clause, ...]


@dataclass(frozen=True)
class YieldItem:
    name: str
    alias: str


@dataclass(frozen=True)
class Call(Clause):
    procedure: str
    args: tuple[Expr, ...]
    yields: tuple[YieldItem, ...]
    where: Expr | None = None


# ------------------------------------------------------------------ statements


@dataclass(frozen=True)
class SingleQuery:
    clauses: tuple[Clause, ...]


@dataclass(frozen=True)
class Query:
    parts: tuple[SingleQuery, ...]
    # union_all[i] joins parts[i] and parts[i + 1]
    union_all: tuple[bool, ...] = ()


@dataclass(frozen=True)
class Explain:
    query: Query


@dataclass(frozen=True)
class SchemaCommand:
    """Index / constraint DDL and introspection; the store owns the schema."""

    kind: EgSchemaKind
    label: str | None = None
    props: tuple[str, ...] = field(default=())
