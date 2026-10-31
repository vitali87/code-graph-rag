//! The algorithms on graphs small enough to check by hand. Agreement with
//! the Python implementations, record for record, is checked from the
//! Python side (codebase_rag/tests/test_embedded_graph_rust.py).

use cgr_graph_rs::{betweenness, degree, reach, run, scc, wcc, Arg, Graph, Value};

/// a -> b -> a is a cycle; b -> c leaves it; d stands alone.
fn sample() -> Graph {
    Graph::from_edges(vec![1, 2, 3, 4], &[(1, 2), (2, 1), (2, 3)])
}

fn column(records: &[Vec<(&'static str, Value)>], name: &str) -> Vec<Value> {
    records
        .iter()
        .map(|r| r.iter().find(|(k, _)| *k == name).expect("column").1.clone())
        .collect()
}

#[test]
fn strongly_connected_components_group_the_cycle() {
    let records = scc(&sample());
    let Value::IdLists(comps) = &records[0][0].1 else { panic!("components") };
    let mut sizes: Vec<usize> = comps.iter().map(Vec::len).collect();
    sizes.sort_unstable();
    assert_eq!(sizes, vec![1, 1, 2]);
}

#[test]
fn weak_components_number_by_first_member() {
    let ids = column(&wcc(&sample()), "component_id");
    assert_eq!(ids, vec![Value::Int(0), Value::Int(0), Value::Int(0), Value::Int(1)]);
}

#[test]
fn degree_counts_both_ends_by_default() {
    let counts = column(&degree(&sample(), &[]), "degree");
    assert_eq!(counts, vec![Value::Int(2), Value::Int(3), Value::Int(1), Value::Int(0)]);
    let out = column(&degree(&sample(), &[Arg::Text("OUT".into())]), "degree");
    assert_eq!(out, vec![Value::Int(1), Value::Int(2), Value::Int(0), Value::Int(0)]);
}

#[test]
fn reach_excludes_its_start_even_on_a_cycle() {
    let descendants = reach(&sample(), Some(1), false, "descendants");
    assert_eq!(descendants[0][0].1, Value::Ids(vec![2, 3]));
    let ancestors = reach(&sample(), Some(3), true, "ancestors");
    assert_eq!(ancestors[0][0].1, Value::Ids(vec![1, 2]));
    let unknown = reach(&sample(), Some(99), false, "descendants");
    assert_eq!(unknown[0][0].1, Value::Ids(vec![]));
}

#[test]
fn betweenness_of_the_middle_of_a_path() {
    let path = Graph::from_edges(vec![1, 2, 3], &[(1, 2), (2, 3)]);
    let scores = column(&betweenness(&path), "betweenness_centrality");
    assert_eq!(scores, vec![Value::Float(0.0), Value::Float(0.5), Value::Float(0.0)]);
}

#[test]
fn pagerank_is_a_distribution() {
    let records = run(&sample(), "pagerank.get", &[]).expect("implemented");
    let total: f64 = column(&records, "rank")
        .into_iter()
        .map(|v| match v {
            Value::Float(f) => f,
            _ => panic!("rank"),
        })
        .sum();
    assert!((total - 1.0).abs() < 1e-9);
}

/// Python's `for _ in range(iterations)` runs any count until the ranks
/// converge; a count past u32::MAX is no different.
#[test]
fn pagerank_runs_an_iteration_count_past_u32_max() {
    let tight = |iterations: i64| {
        let args = [Arg::Int(iterations), Arg::Float(0.85), Arg::Float(1e-12)];
        run(&sample(), "pagerank.get", &args).expect("implemented")
    };
    assert_eq!(tight((1 << 32) + 1), tight(1000));
}

/// `range()` of a negative count is empty: the ranks stay uniform.
#[test]
fn pagerank_runs_no_iteration_for_a_negative_count() {
    let records = run(&sample(), "pagerank.get", &[Arg::Int(-5)]).expect("implemented");
    assert_eq!(column(&records, "rank"), vec![Value::Float(0.25); 4]);
}

#[test]
fn procedures_left_to_python_are_declined() {
    assert!(run(&sample(), "nxalg.simple_cycles", &[]).is_none());
}
