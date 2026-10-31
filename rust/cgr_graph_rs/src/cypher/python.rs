//! `parse_cypher(text)`: the tree `codebase_rag.embedded_graph.parser`
//! would return, built from the very dataclasses of `cypher_ast` and the
//! `StrEnum` members of `codebase_rag.constants.embedded_graph`, so the
//! engine cannot tell which parser produced it.

use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString, PyTuple};

use super::ast::{
    BinaryOp, Clause, Direction, Expr, Literal, Mirrored, NodePattern, PathPattern, Projection,
    Quantifier, Query, RelPattern, RemoveKind, SchemaKind, SetItem, SetKind, SingleQuery,
    Statement, UnaryOp,
};
use super::error::{py_repr, Error, SyntaxError};
use super::parse;

const AST_MODULE: &str = "codebase_rag.embedded_graph.cypher_ast";
const ENUM_MODULE: &str = "codebase_rag.constants.embedded_graph";
const LEXER_MODULE: &str = "codebase_rag.embedded_graph.lexer";
const SYNTAX_ERROR: &str = "CypherSyntaxError";

/// Declares the `cypher_ast` classes the builder calls, by Python name.
macro_rules! ast_classes {
    ($($field:ident => $name:literal),+ $(,)?) => {
        struct Classes {
            $($field: Py<PyAny>),+
        }

        impl Classes {
            fn load(module: &Bound<'_, PyModule>) -> PyResult<Self> {
                Ok(Classes { $($field: module.getattr($name)?.unbind()),+ })
            }
        }
    };
}

ast_classes! {
    literal => "Literal",
    param => "Param",
    var => "Var",
    prop => "Prop",
    map_lit => "MapLit",
    list_lit => "ListLit",
    index => "Index",
    slice => "Slice",
    func_call => "FuncCall",
    list_comp => "ListComp",
    quantifier => "Quantifier",
    case => "Case",
    bin_op => "BinOp",
    unary_op => "UnaryOp",
    is_null => "IsNull",
    label_test => "LabelTest",
    node_pattern => "NodePattern",
    rel_pattern => "RelPattern",
    path_pattern => "PathPattern",
    match_clause => "Match",
    unwind => "Unwind",
    projection_item => "ProjectionItem",
    sort_item => "SortItem",
    projection => "Projection",
    set_item => "SetItem",
    create => "Create",
    merge => "Merge",
    set_clause => "SetClause",
    remove_item => "RemoveItem",
    remove => "Remove",
    delete => "Delete",
    foreach => "Foreach",
    yield_item => "YieldItem",
    call => "Call",
    single_query => "SingleQuery",
    query => "Query",
    explain => "Explain",
    schema_command => "SchemaCommand",
}

/// One Python enum's members, indexed by the mirror's `ordinal`.
struct Members(Vec<Py<PyAny>>);

impl Members {
    fn load<E: Mirrored>(module: &Bound<'_, PyModule>) -> PyResult<Self> {
        let class = module.getattr(E::PY_ENUM)?;
        let members = E::ALL
            .iter()
            .map(|v| Ok(class.getattr(v.member())?.unbind()));
        Ok(Members(members.collect::<PyResult<_>>()?))
    }

    fn get<'py, E: Mirrored>(&self, py: Python<'py>, value: E) -> Bound<'py, PyAny> {
        self.0[value.ordinal()].bind(py).clone()
    }
}

/// Everything a tree is built from, looked up once per interpreter.
struct Builder {
    classes: Classes,
    binary: Members,
    unary: Members,
    quantifiers: Members,
    directions: Members,
    set_kinds: Members,
    remove_kinds: Members,
    schema_kinds: Members,
    syntax_error: Py<PyAny>,
}

static BUILDER: PyOnceLock<Builder> = PyOnceLock::new();

type Built<'py> = PyResult<Bound<'py, PyAny>>;

fn string<'py>(py: Python<'py>, text: &str) -> Bound<'py, PyAny> {
    PyString::new(py, text).into_any()
}

fn optional<'py, T>(
    py: Python<'py>,
    value: Option<&T>,
    build: impl FnOnce(&T) -> Built<'py>,
) -> Built<'py> {
    value.map_or_else(|| Ok(py.None().into_bound(py)), build)
}

fn optional_string<'py>(py: Python<'py>, value: Option<&String>) -> Bound<'py, PyAny> {
    value.map_or_else(|| py.None().into_bound(py), |text| string(py, text))
}

fn tuple<'py, T>(py: Python<'py>, items: &[T], build: impl FnMut(&T) -> Built<'py>) -> Built<'py> {
    let items = items.iter().map(build).collect::<PyResult<Vec<_>>>()?;
    Ok(PyTuple::new(py, items)?.into_any())
}

fn strings<'py>(py: Python<'py>, items: &[String]) -> Built<'py> {
    Ok(PyTuple::new(py, items)?.into_any())
}

impl Builder {
    fn load(py: Python<'_>) -> PyResult<Self> {
        let ast = PyModule::import(py, AST_MODULE)?;
        let enums = PyModule::import(py, ENUM_MODULE)?;
        Ok(Builder {
            classes: Classes::load(&ast)?,
            binary: Members::load::<BinaryOp>(&enums)?,
            unary: Members::load::<UnaryOp>(&enums)?,
            quantifiers: Members::load::<Quantifier>(&enums)?,
            directions: Members::load::<Direction>(&enums)?,
            set_kinds: Members::load::<SetKind>(&enums)?,
            remove_kinds: Members::load::<RemoveKind>(&enums)?,
            schema_kinds: Members::load::<SchemaKind>(&enums)?,
            syntax_error: PyModule::import(py, LEXER_MODULE)?
                .getattr(SYNTAX_ERROR)?
                .unbind(),
        })
    }

    /// `CypherSyntaxError(pos, message)`, the message rendered with
    /// Python's own `repr()` so non-ASCII text is escaped exactly as there.
    fn syntax_error(&self, py: Python<'_>, err: &SyntaxError) -> PyErr {
        let repr = |text: &str| {
            PyString::new(py, text)
                .repr()
                .and_then(|r| r.to_str().map(str::to_owned))
                .unwrap_or_else(|_| py_repr(text))
        };
        match self
            .syntax_error
            .bind(py)
            .call1((err.pos, err.message(repr)))
        {
            Ok(exc) => PyErr::from_value(exc),
            Err(err) => err,
        }
    }

    fn statement<'py>(&self, py: Python<'py>, stmt: &Statement) -> Built<'py> {
        let c = &self.classes;
        match stmt {
            Statement::Query(query) => self.query(py, query),
            Statement::Explain(query) => c.explain.bind(py).call1((self.query(py, query)?,)),
            Statement::Schema(cmd) => c.schema_command.bind(py).call1((
                self.schema_kinds.get(py, cmd.kind),
                optional_string(py, cmd.label.as_ref()),
                strings(py, &cmd.props)?,
            )),
        }
    }

    fn query<'py>(&self, py: Python<'py>, query: &Query) -> Built<'py> {
        let parts = tuple(py, &query.parts, |part| self.single_query(py, part))?;
        let union_all = PyTuple::new(py, &query.union_all)?;
        self.classes.query.bind(py).call1((parts, union_all))
    }

    fn single_query<'py>(&self, py: Python<'py>, part: &SingleQuery) -> Built<'py> {
        let clauses = tuple(py, &part.clauses, |clause| self.clause(py, clause))?;
        self.classes.single_query.bind(py).call1((clauses,))
    }

    fn clause<'py>(&self, py: Python<'py>, clause: &Clause) -> Built<'py> {
        let c = &self.classes;
        let exprs = |items: &[Expr]| tuple(py, items, |e| self.expr(py, e));
        let set_items = |items: &[SetItem]| tuple(py, items, |item| self.set_item(py, item));
        match clause {
            Clause::Match {
                optional: is_optional,
                patterns,
                filter,
            } => c.match_clause.bind(py).call1((
                *is_optional,
                tuple(py, patterns, |p| self.path(py, p))?,
                optional(py, filter.as_ref(), |e| self.expr(py, e))?,
            )),
            Clause::Unwind { expr, var } => c
                .unwind
                .bind(py)
                .call1((self.expr(py, expr)?, string(py, var))),
            Clause::Projection(projection) => self.projection(py, projection),
            Clause::Create { patterns } => c
                .create
                .bind(py)
                .call1((tuple(py, patterns, |p| self.path(py, p))?,)),
            Clause::Merge {
                pattern,
                on_create,
                on_match,
            } => c.merge.bind(py).call1((
                self.path(py, pattern)?,
                set_items(on_create)?,
                set_items(on_match)?,
            )),
            Clause::Set { items } => c.set_clause.bind(py).call1((set_items(items)?,)),
            Clause::Remove { items } => {
                let items = tuple(py, items, |item| {
                    c.remove_item.bind(py).call1((
                        self.remove_kinds.get(py, item.kind),
                        string(py, &item.var),
                        string(py, &item.key),
                    ))
                })?;
                c.remove.bind(py).call1((items,))
            }
            Clause::Delete {
                detach,
                exprs: items,
            } => c.delete.bind(py).call1((*detach, exprs(items)?)),
            Clause::Foreach {
                var,
                source,
                clauses,
            } => c.foreach.bind(py).call1((
                string(py, var),
                self.expr(py, source)?,
                tuple(py, clauses, |inner| self.clause(py, inner))?,
            )),
            Clause::Call {
                procedure,
                args,
                yields,
                filter,
            } => {
                let yields = tuple(py, yields, |item| {
                    c.yield_item
                        .bind(py)
                        .call1((string(py, &item.name), string(py, &item.alias)))
                })?;
                c.call.bind(py).call1((
                    string(py, procedure),
                    exprs(args)?,
                    yields,
                    optional(py, filter.as_ref(), |e| self.expr(py, e))?,
                ))
            }
        }
    }

    fn projection<'py>(&self, py: Python<'py>, p: &Projection) -> Built<'py> {
        let c = &self.classes;
        let items = tuple(py, &p.items, |item| {
            c.projection_item
                .bind(py)
                .call1((self.expr(py, &item.expr)?, string(py, &item.alias)))
        })?;
        let order = tuple(py, &p.order, |item| {
            c.sort_item
                .bind(py)
                .call1((self.expr(py, &item.expr)?, item.descending))
        })?;
        c.projection.bind(py).call1((
            p.is_return,
            p.distinct,
            p.star,
            items,
            order,
            optional(py, p.skip.as_ref(), |e| self.expr(py, e))?,
            optional(py, p.limit.as_ref(), |e| self.expr(py, e))?,
            optional(py, p.filter.as_ref(), |e| self.expr(py, e))?,
        ))
    }

    fn set_item<'py>(&self, py: Python<'py>, item: &SetItem) -> Built<'py> {
        self.classes.set_item.bind(py).call1((
            self.set_kinds.get(py, item.kind),
            string(py, &item.var),
            optional_string(py, item.key.as_ref()),
            optional(py, item.value.as_ref(), |e| self.expr(py, e))?,
        ))
    }

    fn path<'py>(&self, py: Python<'py>, path: &PathPattern) -> Built<'py> {
        self.classes.path_pattern.bind(py).call1((
            optional_string(py, path.var.as_ref()),
            tuple(py, &path.nodes, |n| self.node(py, n))?,
            tuple(py, &path.rels, |r| self.rel(py, r))?,
        ))
    }

    fn node<'py>(&self, py: Python<'py>, node: &NodePattern) -> Built<'py> {
        self.classes.node_pattern.bind(py).call1((
            optional_string(py, node.var.as_ref()),
            strings(py, &node.labels)?,
            optional(py, node.props.as_ref(), |e| self.expr(py, e))?,
        ))
    }

    fn rel<'py>(&self, py: Python<'py>, rel: &RelPattern) -> Built<'py> {
        let var_length = optional(py, rel.var_length.as_ref(), |bounds| {
            Ok(bounds.into_pyobject(py)?.into_any())
        })?;
        self.classes.rel_pattern.bind(py).call1((
            optional_string(py, rel.var.as_ref()),
            strings(py, &rel.types)?,
            self.directions.get(py, rel.direction),
            optional(py, rel.props.as_ref(), |e| self.expr(py, e))?,
            var_length,
        ))
    }

    fn literal<'py>(py: Python<'py>, literal: &Literal) -> Bound<'py, PyAny> {
        match literal {
            Literal::Null => py.None().into_bound(py),
            Literal::Bool(value) => PyBool::new(py, *value).to_owned().into_any(),
            Literal::Int(value) => PyInt::new(py, *value).into_any(),
            Literal::Float(value) => PyFloat::new(py, *value).into_any(),
            Literal::Str(value) => string(py, value),
        }
    }

    fn expr<'py>(&self, py: Python<'py>, expr: &Expr) -> Built<'py> {
        let c = &self.classes;
        let sub = |e: &Expr| self.expr(py, e);
        match expr {
            Expr::Literal(literal) => c.literal.bind(py).call1((Self::literal(py, literal),)),
            Expr::Param(name) => c.param.bind(py).call1((string(py, name),)),
            Expr::Var(name) => c.var.bind(py).call1((string(py, name),)),
            Expr::Prop { subject, key } => c.prop.bind(py).call1((sub(subject)?, string(py, key))),
            Expr::MapLit(items) => {
                let items = tuple(py, items, |(key, value)| {
                    Ok(PyTuple::new(py, [string(py, key), sub(value)?])?.into_any())
                })?;
                c.map_lit.bind(py).call1((items,))
            }
            Expr::ListLit(items) => c.list_lit.bind(py).call1((tuple(py, items, sub)?,)),
            Expr::Index { subject, index } => c.index.bind(py).call1((sub(subject)?, sub(index)?)),
            Expr::Slice {
                subject,
                start,
                end,
            } => c.slice.bind(py).call1((
                sub(subject)?,
                optional(py, start.as_deref(), sub)?,
                optional(py, end.as_deref(), sub)?,
            )),
            Expr::FuncCall {
                name,
                args,
                distinct,
                star,
            } => c.func_call.bind(py).call1((
                string(py, name),
                tuple(py, args, sub)?,
                *distinct,
                *star,
            )),
            Expr::ListComp {
                var,
                source,
                filter,
                projection,
            } => c.list_comp.bind(py).call1((
                string(py, var),
                sub(source)?,
                optional(py, filter.as_deref(), sub)?,
                optional(py, projection.as_deref(), sub)?,
            )),
            Expr::Quantifier {
                kind,
                var,
                source,
                filter,
            } => c.quantifier.bind(py).call1((
                self.quantifiers.get(py, *kind),
                string(py, var),
                sub(source)?,
                sub(filter)?,
            )),
            Expr::Case {
                subject,
                whens,
                default,
            } => {
                let whens = tuple(py, whens, |(condition, value)| {
                    Ok(PyTuple::new(py, [sub(condition)?, sub(value)?])?.into_any())
                })?;
                c.case.bind(py).call1((
                    optional(py, subject.as_deref(), sub)?,
                    whens,
                    optional(py, default.as_deref(), sub)?,
                ))
            }
            Expr::BinOp { op, left, right } => {
                c.bin_op
                    .bind(py)
                    .call1((self.binary.get(py, *op), sub(left)?, sub(right)?))
            }
            Expr::UnaryOp { op, operand } => c
                .unary_op
                .bind(py)
                .call1((self.unary.get(py, *op), sub(operand)?)),
            Expr::IsNull { operand, negated } => {
                c.is_null.bind(py).call1((sub(operand)?, *negated))
            }
            Expr::LabelTest { subject, labels } => c
                .label_test
                .bind(py)
                .call1((sub(subject)?, strings(py, labels)?)),
        }
    }
}

/// The tree `parser.parse(text)` returns, or None when the query is one
/// this port leaves to the Python parser (see `Error::Defer`). A syntax
/// error raises the `CypherSyntaxError` Python's parser would.
#[pyfunction]
pub fn parse_cypher<'py>(
    py: Python<'py>,
    text: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    // A str with lone surrogates has no UTF-8 form; Python's parser reads it as is.
    let Ok(text) = text.to_str() else {
        return Ok(None);
    };
    let builder = BUILDER.get_or_try_init(py, || Builder::load(py))?;
    match parse(text) {
        Ok(stmt) => builder.statement(py, &stmt).map(Some),
        Err(Error::Syntax(err)) => Err(builder.syntax_error(py, &err)),
        Err(Error::Defer(_)) => Ok(None),
    }
}

/// Adds `parse_cypher` to the extension module.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(parse_cypher, module)?)
}
