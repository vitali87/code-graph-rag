//! The syntax tree: one type per class in
//! `codebase_rag/embedded_graph/cypher_ast.py`, field for field, so the
//! Python binding can build the reference tree by walking this one. Python's
//! `where` fields are named `filter` here, `where` being a Rust keyword.

/// An enum whose variants the Python tree holds as members of a `StrEnum`
/// in `codebase_rag.constants.embedded_graph`.
pub trait Mirrored: Copy + 'static {
    /// The Python enum's class name.
    const PY_ENUM: &'static str;
    /// Every variant, in declaration order: `ordinal` indexes this.
    const ALL: &'static [Self];
    /// The name of the Python member this variant stands for.
    fn member(self) -> &'static str;
    fn ordinal(self) -> usize;
}

macro_rules! mirrored {
    ($(#[$meta:meta])* $name:ident => $py:literal { $($variant:ident => $member:literal),+ $(,)? }) => {
        $(#[$meta])*
        #[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
        pub enum $name {
            $($variant),+
        }

        impl Mirrored for $name {
            const PY_ENUM: &'static str = $py;
            const ALL: &'static [Self] = &[$($name::$variant),+];

            fn member(self) -> &'static str {
                match self {
                    $($name::$variant => $member),+
                }
            }

            fn ordinal(self) -> usize {
                self as usize
            }
        }
    };
}

mirrored! {
    BinaryOp => "EgBinary" {
        Or => "OR",
        Xor => "XOR",
        And => "AND",
        Eq => "EQ",
        Ne => "NE",
        Lt => "LT",
        Gt => "GT",
        Le => "LE",
        Ge => "GE",
        Regex => "REGEX",
        In => "IN",
        StartsWith => "STARTS_WITH",
        EndsWith => "ENDS_WITH",
        Contains => "CONTAINS",
        Add => "ADD",
        Sub => "SUB",
        Mul => "MUL",
        Div => "DIV",
        Mod => "MOD",
        Pow => "POW",
    }
}

mirrored! {
    /// The parser never builds `+x` (a unary plus is dropped), but the
    /// Python enum has the member and so does this mirror.
    UnaryOp => "EgUnary" {
        Not => "NOT",
        Neg => "NEG",
        Pos => "POS",
    }
}

mirrored! {
    Quantifier => "EgQuantifier" {
        Any => "ANY",
        All => "ALL",
        None => "NONE",
        Single => "SINGLE",
    }
}

mirrored! {
    Direction => "EgDirection" {
        Out => "OUT",
        In => "IN",
        Both => "BOTH",
    }
}

mirrored! {
    SetKind => "EgSetKind" {
        Prop => "PROP",
        Merge => "MERGE",
        Replace => "REPLACE",
        Label => "LABEL",
    }
}

mirrored! {
    RemoveKind => "EgRemoveKind" {
        Prop => "PROP",
        Label => "LABEL",
    }
}

mirrored! {
    SchemaKind => "EgSchemaKind" {
        CreateIndex => "CREATE_INDEX",
        CreateConstraint => "CREATE_CONSTRAINT",
        DropIndex => "DROP_INDEX",
        DropConstraint => "DROP_CONSTRAINT",
        ShowConstraints => "SHOW_CONSTRAINTS",
        ShowIndexes => "SHOW_INDEXES",
        ShowStorage => "SHOW_STORAGE",
    }
}

/// A literal's value; Python tells `1`, `1.0` and `true` apart by type.
#[derive(Clone, Debug, PartialEq)]
pub enum Literal {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

#[derive(Clone, Debug, PartialEq)]
pub enum Expr {
    Literal(Literal),
    Param(String),
    Var(String),
    Prop {
        subject: Box<Expr>,
        key: String,
    },
    MapLit(Vec<(String, Expr)>),
    ListLit(Vec<Expr>),
    Index {
        subject: Box<Expr>,
        index: Box<Expr>,
    },
    Slice {
        subject: Box<Expr>,
        start: Option<Box<Expr>>,
        end: Option<Box<Expr>>,
    },
    FuncCall {
        name: String,
        args: Vec<Expr>,
        distinct: bool,
        star: bool,
    },
    ListComp {
        var: String,
        source: Box<Expr>,
        filter: Option<Box<Expr>>,
        projection: Option<Box<Expr>>,
    },
    Quantifier {
        kind: Quantifier,
        var: String,
        source: Box<Expr>,
        filter: Box<Expr>,
    },
    Case {
        subject: Option<Box<Expr>>,
        whens: Vec<(Expr, Expr)>,
        default: Option<Box<Expr>>,
    },
    BinOp {
        op: BinaryOp,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    UnaryOp {
        op: UnaryOp,
        operand: Box<Expr>,
    },
    IsNull {
        operand: Box<Expr>,
        negated: bool,
    },
    LabelTest {
        subject: Box<Expr>,
        labels: Vec<String>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct NodePattern {
    pub var: Option<String>,
    /// `(n:A|B)`: any of these labels. Empty means unconstrained.
    pub labels: Vec<String>,
    pub props: Option<Expr>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RelPattern {
    pub var: Option<String>,
    pub types: Vec<String>,
    pub direction: Direction,
    pub props: Option<Expr>,
    /// (min, max) hops for a variable-length relationship; max None means
    /// unbounded.
    pub var_length: Option<(i64, Option<i64>)>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PathPattern {
    pub var: Option<String>,
    pub nodes: Vec<NodePattern>,
    pub rels: Vec<RelPattern>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ProjectionItem {
    pub expr: Expr,
    pub alias: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SortItem {
    pub expr: Expr,
    pub descending: bool,
}

/// WITH or RETURN.
#[derive(Clone, Debug, PartialEq)]
pub struct Projection {
    pub is_return: bool,
    pub distinct: bool,
    pub star: bool,
    pub items: Vec<ProjectionItem>,
    pub order: Vec<SortItem>,
    pub skip: Option<Expr>,
    pub limit: Option<Expr>,
    pub filter: Option<Expr>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SetItem {
    pub kind: SetKind,
    pub var: String,
    pub key: Option<String>,
    pub value: Option<Expr>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RemoveItem {
    pub kind: RemoveKind,
    pub var: String,
    pub key: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct YieldItem {
    pub name: String,
    pub alias: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Clause {
    Match {
        optional: bool,
        patterns: Vec<PathPattern>,
        filter: Option<Expr>,
    },
    Unwind {
        expr: Expr,
        var: String,
    },
    Projection(Projection),
    Create {
        patterns: Vec<PathPattern>,
    },
    Merge {
        pattern: PathPattern,
        on_create: Vec<SetItem>,
        on_match: Vec<SetItem>,
    },
    /// Python's `SetClause`.
    Set {
        items: Vec<SetItem>,
    },
    Remove {
        items: Vec<RemoveItem>,
    },
    Delete {
        detach: bool,
        exprs: Vec<Expr>,
    },
    Foreach {
        var: String,
        source: Expr,
        clauses: Vec<Clause>,
    },
    Call {
        procedure: String,
        args: Vec<Expr>,
        yields: Vec<YieldItem>,
        filter: Option<Expr>,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub struct SingleQuery {
    pub clauses: Vec<Clause>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Query {
    pub parts: Vec<SingleQuery>,
    /// `union_all[i]` joins `parts[i]` and `parts[i + 1]`.
    pub union_all: Vec<bool>,
}

/// Index / constraint DDL and introspection; the store owns the schema.
#[derive(Clone, Debug, PartialEq)]
pub struct SchemaCommand {
    pub kind: SchemaKind,
    pub label: Option<String>,
    pub props: Vec<String>,
}

/// What `parse` returns: Python's `Query | Explain | SchemaCommand`.
#[derive(Clone, Debug, PartialEq)]
pub enum Statement {
    Query(Query),
    Explain(Query),
    Schema(SchemaCommand),
}
