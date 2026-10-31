// SPDX-License-Identifier: MIT
//! Fuzz target: the graph procedures (`pagerank`, `degree`, `wcc`, `tarjan`,
//! `scc`, `betweenness`, `reach`) and `run`, which dispatches to them, on
//! a graph and arguments the input chooses.
//!
//! The graph comes from a repository being indexed and the arguments from
//! a Cypher `CALL`, so both are untrusted. The input reads as: PageRank's
//! arguments, `degree`'s mode, up to 8 node pairs to compare, a procedure
//! name, then the graph: the node count (up to 1,000), the first id and
//! the gaps between ascending ids, and up to 4,000 edges, each its two
//! ends. The graph ends where the input does, so a short input is a small
//! graph. Self-loops and parallel edges are allowed, as in the engine's
//! graph. An unknown procedure name is an expected outcome: `run` returns
//! None.
#![no_main]

use cgr_graph_rs::{
    betweenness, degree, implements, pagerank, reach, run, scc, tarjan, wcc, Arg, Graph, Record,
    Value, ANCESTORS, BETWEENNESS, DEGREE, DESCENDANTS, PAGERANK, SCC, WCC,
};
use cgr_graph_rs_fuzz::{require, Input};
use libfuzzer_sys::fuzz_target;

const MAX_NODES: usize = 1000;
const MAX_EDGES: usize = 4000;
const MAX_PAIRS: usize = 8;
/// PageRank stops after its iteration count unless it converges first, and
/// the input chooses both, so the count is bounded.
const MAX_ITERATIONS: i64 = 200;
const MODES: [&str; 5] = ["undirected", "out", "in", "OUT", "In"];

fuzz_target!(|data: &[u8]| {
    let mut input = Input::new(data);
    let pagerank_args = read_pagerank_args(&mut input);
    let degree_args = match input.below(3) {
        0 => vec![],
        1 => vec![Arg::Text(input.pick(&MODES).to_string())],
        _ => vec![Arg::Text(input.text(8))],
    };
    let raw_pairs: Vec<(u16, u16)> = (0..input.below(MAX_PAIRS + 1))
        .map(|_| (input.u16(), input.u16()))
        .collect();
    let unknown = input.text(32);
    let (ids, edges) = read_graph(&mut input);
    let graph = Graph::from_edges(ids.clone(), &edges);
    let n = ids.len();
    let pairs: Vec<(usize, usize)> = if n == 0 {
        vec![]
    } else {
        raw_pairs
            .iter()
            .map(|&(a, b)| (a as usize % n, b as usize % n))
            .collect()
    };

    let component = check_tarjan(&graph);
    check_wcc(&graph, &pairs);
    check_reach(&graph, &pairs, &component);
    check_pagerank(&graph, &pagerank_args);
    check_degree(&graph);
    check_betweenness(&graph);

    // `run` returns what the procedure it names returns, for the names
    // `implements` accepts, and None for every other name.
    // Mutant: in lib.rs `run_until`, dispatch ANCESTORS with `false`.
    // Input (hex): 000040ec0800b500020000000000000000001900ff aborts.
    // Mutant: in lib.rs `implements`, drop DESCENDANTS. Input: the empty
    // input aborts.
    let start = pairs.first().map(|&(a, _)| Arg::Int(ids[a]));
    let reach_args: Vec<Arg> = start.into_iter().collect();
    let reach_start = match reach_args.first() {
        Some(Arg::Int(id)) => Some(*id),
        _ => None,
    };
    let expected: [(&str, &[Arg], Vec<Record>); 7] = [
        (PAGERANK, &pagerank_args, pagerank(&graph, &pagerank_args)),
        (DEGREE, &degree_args, degree(&graph, &degree_args)),
        (WCC, &[], wcc(&graph)),
        (SCC, &[], scc(&graph)),
        (BETWEENNESS, &[], betweenness(&graph)),
        (
            ANCESTORS,
            &reach_args,
            reach(&graph, reach_start, true, "ancestors"),
        ),
        (
            DESCENDANTS,
            &reach_args,
            reach(&graph, reach_start, false, "descendants"),
        ),
    ];
    for (name, args, direct) in &expected {
        require!(implements(name), "implements({name:?}) is false");
        let ran = run(&graph, name, args);
        require!(
            ran.as_ref().is_some_and(|r| same_records(r, direct)),
            "run({name:?}) differs from calling it"
        );
    }
    let known = expected.iter().any(|(name, _, _)| *name == unknown);
    require!(
        implements(&unknown) == known,
        "implements({unknown:?}) is {}",
        !known
    );
    require!(
        run(&graph, &unknown, &[]).is_some() == known,
        "run({unknown:?}) disagrees with implements"
    );
});

/// Ascending, distinct ids, and edges between them, until the input ends.
fn read_graph(input: &mut Input) -> (Vec<i64>, Vec<(i64, i64)>) {
    let wanted = input.below(MAX_NODES + 1);
    let mut ids = Vec::with_capacity(wanted);
    let mut next = Some(input.i64());
    while let Some(id) = next {
        if ids.len() == wanted || input.is_empty() {
            break;
        }
        ids.push(id);
        next = id.checked_add(1 + input.byte() as i64);
    }
    let n = ids.len();
    let mut edges = Vec::new();
    while n > 0 && edges.len() < MAX_EDGES && !input.is_empty() {
        edges.push((ids[input.below(n)], ids[input.below(n)]));
    }
    (ids, edges)
}

/// Up to three arguments of any type, as the engine passes them. Any float
/// is allowed but in the first slot, the iteration count, where the
/// count is bounded.
fn read_pagerank_args(input: &mut Input) -> Vec<Arg> {
    (0..input.below(4))
        .map(|slot| match input.below(4) {
            0 => Arg::Null,
            1 if slot == 0 => Arg::Int(input.i64() % (MAX_ITERATIONS + 1)),
            1 => Arg::Int(input.i64()),
            2 if slot == 0 => Arg::Float((input.i64() % (MAX_ITERATIONS + 1)) as f64 + 0.5),
            2 => Arg::Float(input.f64()),
            _ => Arg::Text(input.text(8)),
        })
        .collect()
}

fn records_of(records: &[Record], column: &str) -> Vec<Value> {
    records
        .iter()
        .map(|r| {
            r.iter()
                .find(|(k, _)| *k == column)
                .map(|(_, v)| v.clone())
                .expect("the column is in every record")
        })
        .collect()
}

fn ints(values: Vec<Value>) -> Vec<i64> {
    values
        .into_iter()
        .map(|v| match v {
            Value::Int(i) => i,
            other => panic!("{other:?} is not an int"),
        })
        .collect()
}

fn floats(values: Vec<Value>) -> Vec<f64> {
    values
        .into_iter()
        .map(|v| match v {
            Value::Float(f) => f,
            other => panic!("{other:?} is not a float"),
        })
        .collect()
}

/// One record per node, in the graph's node order.
fn check_nodes(graph: &Graph, records: &[Record], what: &str) {
    let nodes = ints(records_of(records, "node"));
    require!(
        nodes == graph.ids,
        "{what} does not list each node once, in id order"
    );
}

/// Equal records, comparing floats by their bits so that NaN equals NaN.
fn same_records(a: &[Record], b: &[Record]) -> bool {
    fn same(a: &Value, b: &Value) -> bool {
        match (a, b) {
            (Value::Float(x), Value::Float(y)) => x.to_bits() == y.to_bits(),
            _ => a == b,
        }
    }
    a.len() == b.len()
        && a.iter().zip(b).all(|(ra, rb)| {
            ra.len() == rb.len()
                && ra
                    .iter()
                    .zip(rb)
                    .all(|((ka, va), (kb, vb))| ka == kb && same(va, vb))
        })
}

/// Tarjan's components partition the nodes, and come out in reverse
/// topological order: an edge between two components points from the later
/// one to the earlier one. `scc` returns the same components, by id.
/// Returns each node's component index.
///
/// Mutant: in lib.rs `tarjan_until`, update `low[v]` from `low[v]` in place
/// of `index[w]`. Input (hex): 00fd040021020000000000000000000000006701023100
/// aborts. Mutant: in lib.rs `scc_until`, list each component's members
/// reversed. Input: the same aborts.
fn check_tarjan(graph: &Graph) -> Vec<usize> {
    let n = graph.ids.len();
    let comps = tarjan(graph);
    let mut component = vec![usize::MAX; n];
    for (c, members) in comps.iter().enumerate() {
        require!(!members.is_empty(), "component {c} is empty");
        for &v in members {
            require!(
                v < n && component[v] == usize::MAX,
                "node {v} is in two components, or is not a node"
            );
            component[v] = c;
        }
    }
    require!(
        component.iter().all(|&c| c != usize::MAX),
        "a node is in no component"
    );
    for &(s, d) in &graph.edges {
        require!(
            component[d] <= component[s],
            "the edge {s}->{d} points from component {} to the later {}",
            component[s],
            component[d]
        );
    }
    let by_id: Vec<Vec<i64>> = comps
        .iter()
        .map(|c| c.iter().map(|&v| graph.ids[v]).collect())
        .collect();
    let records = scc(graph);
    require!(
        records.len() == 1 && records[0] == vec![("components", Value::IdLists(by_id))],
        "scc does not return tarjan's components"
    );
    component
}

/// Weakly connected components: each node's id is dense and numbered in
/// node order, the two ends of an edge share one, and two nodes share one
/// exactly when an undirected path joins them.
///
/// Mutant: in lib.rs `wcc`, number a component `next + 1`. Input (hex):
/// 0033000000e6003333332c323637cb aborts. Mutant: make the union a no-op
/// (`parent[rs.max(rd)] = rs.max(rd)`). Input (hex):
/// 00fd0400210200000000000000000000003f3000 aborts. Mutant: give every
/// node the component of root 0. Input (hex):
/// 00004036fb363600606060606060ffffffffffffff aborts.
fn check_wcc(graph: &Graph, pairs: &[(usize, usize)]) {
    let records = wcc(graph);
    check_nodes(graph, &records, "wcc");
    let weak = ints(records_of(&records, "component_id"));
    let mut next = 0;
    for &c in &weak {
        require!(c <= next && c >= 0, "component id {c} skips past {next}");
        if c == next {
            next += 1;
        }
    }
    let mut undirected = vec![Vec::new(); graph.ids.len()];
    for &(s, d) in &graph.edges {
        require!(weak[s] == weak[d], "the edge {s}->{d} joins two WCCs");
        undirected[s].push(d);
        undirected[d].push(s);
    }
    for &(a, b) in pairs {
        let joined = bfs(&undirected, a)[b];
        require!(
            (weak[a] == weak[b]) == joined,
            "nodes {a} and {b}: same WCC is {}, joined is {joined}",
            weak[a] == weak[b]
        );
    }
}

/// Which nodes a breadth-first search from `start` reaches, `start` included.
fn bfs(adjacency: &[Vec<usize>], start: usize) -> Vec<bool> {
    let mut seen = vec![false; adjacency.len()];
    seen[start] = true;
    let mut queue = std::collections::VecDeque::from([start]);
    while let Some(v) = queue.pop_front() {
        for &w in &adjacency[v] {
            if !seen[w] {
                seen[w] = true;
                queue.push_back(w);
            }
        }
    }
    seen
}

fn reach_ids(graph: &Graph, start: Option<i64>, reverse: bool) -> Vec<i64> {
    let column = if reverse { "ancestors" } else { "descendants" };
    let records = reach(graph, start, reverse, column);
    require!(
        records.len() == 1,
        "reach returns {} records, not one",
        records.len()
    );
    match records_of(&records, column).pop() {
        Some(Value::Ids(ids)) => ids,
        other => panic!("reach returned {other:?}"),
    }
}

/// `reach` lists what a directed path reaches, in id order, without the
/// start: b is a descendant of a exactly when a is an ancestor of b, and
/// two distinct nodes share a strongly connected component exactly when
/// each reaches the other. No start, or one that is not a node, reaches
/// nothing.
///
/// Mutant: in lib.rs `reach_until`, drop the `w != origin` test. Input
/// (hex): e800b501000155000100000018344a5777344a57 aborts. Mutant: build the
/// reverse adjacency forwards. Input (hex):
/// 00004014f7ff4f00020000000000000019f8d348d52a aborts.
fn check_reach(graph: &Graph, pairs: &[(usize, usize)], component: &[usize]) {
    require!(
        reach_ids(graph, None, false).is_empty(),
        "reach from no start is not empty"
    );
    let missing = graph.ids.last().map_or(0, |&last| last.wrapping_add(1));
    if graph.ids.binary_search(&missing).is_err() {
        require!(
            reach_ids(graph, Some(missing), true).is_empty(),
            "reach from a non-node"
        );
    }
    for &(a, b) in pairs {
        let (ia, ib) = (graph.ids[a], graph.ids[b]);
        let down_a = reach_ids(graph, Some(ia), false);
        let up_b = reach_ids(graph, Some(ib), true);
        for ids in [&down_a, &up_b] {
            require!(
                ids.windows(2).all(|w| w[0] < w[1])
                    && ids.iter().all(|id| graph.ids.binary_search(id).is_ok()),
                "reach did not list nodes in ascending id order"
            );
        }
        require!(!down_a.contains(&ia), "node {ia} is its own descendant");
        require!(!up_b.contains(&ib), "node {ib} is its own ancestor");
        let a_reaches_b = down_a.contains(&ib);
        require!(
            a_reaches_b == up_b.contains(&ia),
            "{ib} is a descendant of {ia} is {a_reaches_b}, but the ancestors of {ib} disagree"
        );
        if a != b {
            let b_reaches_a = reach_ids(graph, Some(ib), false).contains(&ia);
            require!(
                (component[a] == component[b]) == (a_reaches_b && b_reaches_a),
                "nodes {a} and {b}: same SCC disagrees with mutual reach"
            );
        }
    }
}

/// With a damping factor in [0, 1], PageRank keeps the total rank at 1
/// (dangling nodes share theirs with every node) and no rank is negative,
/// whatever the iteration count and tolerance. Any other arguments still
/// return one finite-or-not rank per node, never a crash.
///
/// Mutant: in lib.rs `pagerank_until`, drop `damping * dangling / n` from
/// `base`. Input (hex): 0033000000e6003333332c323636cb aborts.
fn check_pagerank(graph: &Graph, args: &[Arg]) {
    let records = pagerank(graph, args);
    check_nodes(graph, &records, "pagerank");
    let damping = match args.get(1) {
        Some(Arg::Int(v)) => *v as f64,
        Some(Arg::Float(v)) => *v,
        _ => 0.85,
    };
    if !(0.0..=1.0).contains(&damping) || graph.ids.is_empty() {
        return;
    }
    let ranks = floats(records_of(&records, "rank"));
    let total: f64 = ranks.iter().sum();
    require!(
        ranks.iter().all(|&r| (0.0..=1.0 + 1e-9).contains(&r)) && (total - 1.0).abs() < 1e-9,
        "with damping {damping}, the ranks sum to {total} or one is outside [0, 1]"
    );
}

/// A node's out-degree counts the edges leaving it, its in-degree the
/// edges entering it (a self-loop counts in both), and its undirected
/// degree is their sum. The mode is not case-sensitive, and no mode means
/// undirected.
///
/// Mutant: in lib.rs `degree`, count `counts[d]` for "out". Input (hex):
/// 00fd0400210200000000000000000000003f3000 aborts.
fn check_degree(graph: &Graph) {
    let of = |mode: &str| {
        let records = degree(graph, &[Arg::Text(mode.into())]);
        check_nodes(graph, &records, "degree");
        ints(records_of(&records, "degree"))
    };
    let mut out = vec![0i64; graph.ids.len()];
    let mut inward = vec![0i64; graph.ids.len()];
    for &(s, d) in &graph.edges {
        out[s] += 1;
        inward[d] += 1;
    }
    let both: Vec<i64> = out.iter().zip(&inward).map(|(o, i)| o + i).collect();
    require!(
        of("out") == out,
        "the out-degrees do not count each node's leaving edges"
    );
    require!(
        of("in") == inward,
        "the in-degrees do not count each node's entering edges"
    );
    require!(
        of("undirected") == both,
        "an undirected degree is not out + in"
    );
    require!(
        of("OUT") == out && of("In") == inward,
        "the mode is case-sensitive"
    );
    require!(
        same_records(
            &degree(graph, &[]),
            &degree(graph, &[Arg::Text("undirected".into())])
        ),
        "degree with no mode is not undirected"
    );
}

/// Normalized betweenness is between 0 and 1. A node that no path can pass
/// through, because no other node has an edge into it or none gets an edge
/// out of it, scores exactly 0.
///
/// Mutant: in lib.rs `betweenness_until`, drop the `w != s` test. Input
/// (hex): 00fd0400210200000000000000000000003f3000 aborts.
fn check_betweenness(graph: &Graph) {
    let records = betweenness(graph);
    check_nodes(graph, &records, "betweenness");
    let scores = floats(records_of(&records, "betweenness_centrality"));
    let n = graph.ids.len();
    let mut has_in = vec![false; n];
    let mut has_out = vec![false; n];
    for &(s, d) in &graph.edges {
        if s != d {
            has_out[s] = true;
            has_in[d] = true;
        }
    }
    for v in 0..n {
        let score = scores[v];
        require!(
            (0.0..=1.0 + 1e-9).contains(&score),
            "node {v} has betweenness {score}, outside [0, 1]"
        );
        if !has_in[v] || !has_out[v] {
            require!(
                score == 0.0,
                "node {v} cannot be between two nodes but scores {score}"
            );
        }
    }
}
