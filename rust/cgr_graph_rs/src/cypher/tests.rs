//! The parser on its own, without Python. Agreement with the Python parser,
//! tree for tree and message for message, is checked from the Python side
//! (`codebase_rag/tests/test_embedded_graph_rust_parser.py`).

use super::ast::*;
use super::error::{DeferReason, Error};
use super::parse;

fn var(name: &str) -> Expr {
    Expr::Var(name.into())
}

fn int(value: i64) -> Expr {
    Expr::Literal(Literal::Int(value))
}

fn string(value: &str) -> Expr {
    Expr::Literal(Literal::Str(value.into()))
}

fn prop(subject: Expr, key: &str) -> Expr {
    Expr::Prop {
        subject: Box::new(subject),
        key: key.into(),
    }
}

fn bin(op: BinaryOp, left: Expr, right: Expr) -> Expr {
    Expr::BinOp {
        op,
        left: Box::new(left),
        right: Box::new(right),
    }
}

fn unary(op: UnaryOp, operand: Expr) -> Expr {
    Expr::UnaryOp {
        op,
        operand: Box::new(operand),
    }
}

fn call(name: &str, args: Vec<Expr>) -> Expr {
    Expr::FuncCall {
        name: name.into(),
        args,
        distinct: false,
        star: false,
    }
}

fn node(var: Option<&str>, labels: &[&str]) -> NodePattern {
    NodePattern {
        var: var.map(Into::into),
        labels: labels.iter().map(|l| (*l).into()).collect(),
        props: None,
    }
}

fn clauses(text: &str) -> Vec<Clause> {
    match parse(text) {
        Ok(Statement::Query(mut query)) => query.parts.remove(0).clauses,
        other => panic!("expected a query from {text:?}, got {other:?}"),
    }
}

/// The single item `RETURN <expr>` projects.
fn returned(expr: &str) -> Expr {
    match clauses(&format!("RETURN {expr}")).remove(0) {
        Clause::Projection(mut p) => p.items.remove(0).expr,
        other => panic!("expected a projection, got {other:?}"),
    }
}

fn error(text: &str) -> String {
    match parse(text) {
        Err(Error::Syntax(err)) => err.to_string(),
        other => panic!("expected a syntax error from {text:?}, got {other:?}"),
    }
}

fn deferred(text: &str) -> DeferReason {
    match parse(text) {
        Err(Error::Defer(defer)) => defer.reason,
        other => panic!("expected {text:?} to be deferred, got {other:?}"),
    }
}

#[test]
fn arithmetic_binds_tighter_than_comparison_and_logic() {
    use BinaryOp::*;
    assert_eq!(
        returned("1 + 2 * 3 ^ 2 ^ 1"),
        bin(
            Add,
            int(1),
            bin(Mul, int(2), bin(Pow, bin(Pow, int(3), int(2)), int(1)))
        )
    );
    assert_eq!(
        returned("NOT a = b AND c OR d XOR e"),
        bin(
            Or,
            bin(
                And,
                unary(UnaryOp::Not, bin(Eq, var("a"), var("b"))),
                var("c")
            ),
            bin(Xor, var("d"), var("e"))
        )
    );
    assert_eq!(
        returned("1 - 2 - 3"),
        bin(Sub, bin(Sub, int(1), int(2)), int(3))
    );
}

#[test]
fn unary_minus_nests_and_plus_vanishes() {
    let neg = |e| unary(UnaryOp::Neg, e);
    assert_eq!(returned("- -1"), neg(neg(int(1))));
    assert_eq!(returned("+x"), var("x"));
    assert_eq!(returned("-2 ^ 2"), bin(BinaryOp::Pow, neg(int(2)), int(2)));
}

#[test]
fn string_predicates_and_null_tests() {
    use BinaryOp::*;
    let n_name = || prop(var("n"), "name");
    assert_eq!(
        returned("n.name STARTS WITH 'a'"),
        bin(StartsWith, n_name(), string("a"))
    );
    assert_eq!(
        returned("n.name ends with 'a'"),
        bin(EndsWith, n_name(), string("a"))
    );
    assert_eq!(
        returned("n.name =~ 'a.*'"),
        bin(Regex, n_name(), string("a.*"))
    );
    assert_eq!(returned("1 != 2"), bin(Ne, int(1), int(2)));
    assert_eq!(
        returned("n.name IS NOT NULL"),
        Expr::IsNull {
            operand: Box::new(n_name()),
            negated: true
        }
    );
}

#[test]
fn literals_keep_their_types() {
    assert_eq!(returned("true"), Expr::Literal(Literal::Bool(true)));
    assert_eq!(returned("NULL"), Expr::Literal(Literal::Null));
    assert_eq!(returned("1.0"), Expr::Literal(Literal::Float(1.0)));
    assert_eq!(returned("1"), int(1));
    assert_eq!(returned(r#""\u0041\t""#), string("A\t"));
}

#[test]
fn postfix_accessors_slices_and_label_tests() {
    let l = || var("l");
    assert_eq!(
        returned("l[1..]"),
        Expr::Slice {
            subject: Box::new(l()),
            start: Some(Box::new(int(1))),
            end: None
        }
    );
    assert_eq!(
        returned("l[..2]"),
        Expr::Slice {
            subject: Box::new(l()),
            start: None,
            end: Some(Box::new(int(2)))
        }
    );
    assert_eq!(
        returned("labels(n)[0]"),
        Expr::Index {
            subject: Box::new(call("labels", vec![var("n")])),
            index: Box::new(int(0))
        }
    );
    assert_eq!(
        returned("n:A|B:C"),
        Expr::LabelTest {
            subject: Box::new(var("n")),
            labels: vec!["A".into(), "B".into(), "C".into()]
        }
    );
    // Only a bare variable takes a label test.
    assert!(parse("RETURN n.k:A").is_err());
}

#[test]
fn functions_lower_their_names_and_take_star_or_distinct() {
    assert_eq!(returned("toLower(x)"), call("tolower", vec![var("x")]));
    assert_eq!(
        returned("apoc.Text.join(l, ',')"),
        call("apoc.text.join", vec![var("l"), string(",")])
    );
    assert_eq!(
        returned("COUNT(*)"),
        Expr::FuncCall {
            name: "count".into(),
            args: vec![],
            distinct: false,
            star: true
        }
    );
    assert_eq!(
        returned("count(DISTINCT n)"),
        Expr::FuncCall {
            name: "count".into(),
            args: vec![var("n")],
            distinct: true,
            star: false
        }
    );
}

#[test]
fn quantifiers_comprehensions_case_and_maps() {
    assert_eq!(
        returned("any(x IN l WHERE x > 1)"),
        Expr::Quantifier {
            kind: Quantifier::Any,
            var: "x".into(),
            source: Box::new(var("l")),
            filter: Box::new(bin(BinaryOp::Gt, var("x"), int(1))),
        }
    );
    assert_eq!(
        returned("none(x IN l)"),
        Expr::Quantifier {
            kind: Quantifier::None,
            var: "x".into(),
            source: Box::new(var("l")),
            filter: Box::new(Expr::Literal(Literal::Bool(true))),
        }
    );
    // Not followed by `(v IN`, `all` is an ordinary function name.
    assert_eq!(returned("all(l)"), call("all", vec![var("l")]));
    assert_eq!(
        returned("[x IN l WHERE x > 1 | x * 2]"),
        Expr::ListComp {
            var: "x".into(),
            source: Box::new(var("l")),
            filter: Some(Box::new(bin(BinaryOp::Gt, var("x"), int(1)))),
            projection: Some(Box::new(bin(BinaryOp::Mul, var("x"), int(2)))),
        }
    );
    assert_eq!(
        returned("CASE n WHEN 1 THEN 'a' ELSE 'b' END"),
        Expr::Case {
            subject: Some(Box::new(var("n"))),
            whens: vec![(int(1), string("a"))],
            default: Some(Box::new(string("b"))),
        }
    );
    assert_eq!(
        returned("{a: 1, `b c`: [2, $p]}"),
        Expr::MapLit(vec![
            ("a".into(), int(1)),
            (
                "b c".into(),
                Expr::ListLit(vec![int(2), Expr::Param("p".into())])
            ),
        ])
    );
}

#[test]
fn unaliased_projections_are_named_by_their_source() {
    let Clause::Projection(p) =
        clauses("RETURN labels(n)[0], n.`a``b` /* c */ + 1, x AS y").remove(0)
    else {
        panic!("projection")
    };
    let aliases: Vec<&str> = p.items.iter().map(|i| i.alias.as_str()).collect();
    assert_eq!(aliases, ["labels(n)[0]", "n.`a``b` /* c */ + 1", "y"]);
}

#[test]
fn patterns_with_directions_types_and_lengths() {
    let Clause::Match {
        optional,
        patterns,
        filter,
    } = clauses("OPTIONAL MATCH p = (a:A|B)<-[r:R|:S*1..3]-(b)-->(:C {k: 1}) RETURN p").remove(0)
    else {
        panic!("match")
    };
    assert!(optional);
    assert_eq!(filter, None);
    let path = &patterns[0];
    assert_eq!(path.var.as_deref(), Some("p"));
    assert_eq!(path.nodes[0], node(Some("a"), &["A", "B"]));
    assert_eq!(
        path.rels[0],
        RelPattern {
            var: Some("r".into()),
            types: vec!["R".into(), "S".into()],
            direction: Direction::In,
            props: None,
            var_length: Some((1, Some(3))),
        }
    );
    assert_eq!(path.rels[1].direction, Direction::Out);
    assert_eq!(
        path.nodes[2].props,
        Some(Expr::MapLit(vec![("k".into(), int(1))]))
    );
    let lengths: Vec<_> = ["*", "*2", "*..4", "*2.."]
        .iter()
        .map(|star| {
            let Clause::Match { patterns, .. } =
                clauses(&format!("MATCH ()-[{star}]-() RETURN 1")).remove(0)
            else {
                panic!("match")
            };
            (
                patterns[0].rels[0].direction,
                patterns[0].rels[0].var_length,
            )
        })
        .collect();
    assert_eq!(
        lengths,
        [
            (Direction::Both, Some((1, None))),
            (Direction::Both, Some((2, Some(2)))),
            (Direction::Both, Some((1, Some(4)))),
            (Direction::Both, Some((2, None))),
        ]
    );
}

#[test]
fn updating_clauses() {
    let merged =
        clauses("MERGE (n:M {k: $k}) ON CREATE SET n.a = 1, n += $m ON MATCH SET n:L, n = {}");
    let Clause::Merge {
        on_create,
        on_match,
        ..
    } = &merged[0]
    else {
        panic!("merge")
    };
    let kinds = |items: &[SetItem]| items.iter().map(|i| i.kind).collect::<Vec<_>>();
    assert_eq!(kinds(on_create), [SetKind::Prop, SetKind::Merge]);
    assert_eq!(kinds(on_match), [SetKind::Label, SetKind::Replace]);
    let deleted = clauses("MATCH (n) DETACH DELETE n");
    assert_eq!(
        deleted[1],
        Clause::Delete {
            detach: true,
            exprs: vec![var("n")]
        }
    );
    let removed = clauses("MATCH (n) REMOVE n.k, n:L");
    assert_eq!(
        removed[1],
        Clause::Remove {
            items: vec![
                RemoveItem {
                    kind: RemoveKind::Prop,
                    var: "n".into(),
                    key: "k".into()
                },
                RemoveItem {
                    kind: RemoveKind::Label,
                    var: "n".into(),
                    key: "L".into()
                },
            ]
        }
    );
    let each = clauses("FOREACH (x IN [1] | CREATE (:N {k: x}))");
    let Clause::Foreach {
        var, clauses: body, ..
    } = &each[0]
    else {
        panic!("foreach")
    };
    assert_eq!((var.as_str(), body.len()), ("x", 1));
    let unwound = clauses("UNWIND $rows AS row RETURN row");
    assert_eq!(
        unwound[0],
        Clause::Unwind {
            expr: Expr::Param("rows".into()),
            var: "row".into()
        }
    );
}

#[test]
fn procedure_calls_yield_columns() {
    let called = clauses("CALL pagerank.get() YIELD node, rank AS r WHERE r > 0 RETURN node");
    assert_eq!(
        called[0],
        Clause::Call {
            procedure: "pagerank.get".into(),
            args: vec![],
            yields: vec![
                YieldItem {
                    name: "node".into(),
                    alias: "node".into()
                },
                YieldItem {
                    name: "rank".into(),
                    alias: "r".into()
                },
            ],
            filter: Some(bin(BinaryOp::Gt, var("r"), int(0))),
        }
    );
}

#[test]
fn statements_unions_explain_and_schema() {
    let Ok(Statement::Query(q)) =
        parse("RETURN 1 AS x UNION ALL RETURN 2 AS x UNION RETURN 3 AS x;")
    else {
        panic!("union")
    };
    assert_eq!((q.parts.len(), q.union_all), (3, vec![true, false]));
    assert!(matches!(
        parse("PROFILE MATCH (n) RETURN n"),
        Ok(Statement::Explain(_))
    ));
    assert_eq!(
        parse("CREATE INDEX ON :Function(name, qualified_name)"),
        Ok(Statement::Schema(SchemaCommand {
            kind: SchemaKind::CreateIndex,
            label: Some("Function".into()),
            props: vec!["name".into(), "qualified_name".into()],
        }))
    );
    assert_eq!(
        parse(
            "CREATE CONSTRAINT c IF NOT EXISTS FOR (n:Module) REQUIRE n.qualified_name IS UNIQUE"
        ),
        Ok(Statement::Schema(SchemaCommand {
            kind: SchemaKind::CreateConstraint,
            label: Some("Module".into()),
            props: vec!["qualified_name".into()],
        }))
    );
    assert_eq!(
        parse("SHOW CONSTRAINT INFO"),
        Ok(Statement::Schema(SchemaCommand {
            kind: SchemaKind::ShowConstraints,
            label: None,
            props: vec![],
        }))
    );
}

#[test]
fn errors_name_the_offset_and_what_was_expected() {
    assert_eq!(
        error("MATCH (n RETURN n"),
        "Cypher syntax error at offset 9: unexpected 'RETURN', expected ')'"
    );
    assert_eq!(
        error("MATCH (n) RETURN"),
        "Cypher syntax error at offset 16: unexpected 'end of query', expected an expression"
    );
    assert_eq!(
        error("UNWIND [1] x"),
        "Cypher syntax error at offset 11: unexpected 'x', expected AS"
    );
    assert_eq!(
        error("RETURN 1 'it''s'"),
        "Cypher syntax error at offset 9: unexpected \"'it'\", expected a clause"
    );
    // An empty backtick name has no text, so it reads as the end.
    assert_eq!(
        error("RETURN 1 ``"),
        "Cypher syntax error at offset 9: unexpected 'end of query', expected a clause"
    );
    assert_eq!(
        error("MATCH p = shortestPath((a)-->(b)) RETURN p"),
        "Cypher syntax error at offset 10: shortestPath"
    );
    assert_eq!(
        error("CREATE INDEX"),
        "Cypher syntax error at offset 12: unexpected 'end of query', expected an index or constraint statement"
    );
    assert_eq!(
        error("MATCH (n) SET n"),
        "Cypher syntax error at offset 15: unexpected 'end of query', expected a SET item"
    );
    assert_eq!(
        error("MATCH n RETURN n"),
        "Cypher syntax error at offset 6: unexpected 'n', expected a pattern"
    );
    assert_eq!(
        error("RETURN match"),
        "Cypher syntax error at offset 7: unexpected 'match', expected an expression"
    );
}

#[test]
fn deep_nesting_is_left_to_python() {
    let nested = format!("RETURN {}1{}", "(".repeat(40), ")".repeat(40));
    assert_eq!(deferred(&nested), DeferReason::Nesting);
    let shallow = format!("RETURN {}1{}", "(".repeat(20), ")".repeat(20));
    assert_eq!(returned(&shallow[7..]), int(1));
    assert_eq!(
        deferred(&format!("RETURN {}1", "-".repeat(40))),
        DeferReason::Nesting
    );
    assert_eq!(
        deferred(&format!("RETURN 1{}", " + 1".repeat(2000))),
        DeferReason::Nesting
    );
}
