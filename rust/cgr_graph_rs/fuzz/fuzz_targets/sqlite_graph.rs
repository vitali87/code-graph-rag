// SPDX-License-Identifier: MIT
//! Fuzz target: the bulk writer (`ingest::merge_nodes`,
//! `ingest::merge_relationships`), the variable-length walk (`walk::walk`,
//! `walk::to_json`, `walk::parse_seeds`) and `Graph::load`, on an in-memory
//! database with the engine's schema.
//!
//! The input is a program of up to 8 steps. Each step is a node batch, a
//! relationship batch or a walk, each of up to 4 rows, over 2 labels, 4
//! keys and 2 relationship types, so the graph stays small enough for every
//! walk to end quickly. Graph contents come from the repository being
//! indexed, so names and properties are untrusted. A batch that fails
//! (malformed property JSON, or a SET that moves a node onto another
//! node's key) is an expected outcome; it must change nothing. The target
//! keeps a model of the graph and checks the database against it after
//! every step.
#![no_main]

use cgr_graph_rs::deadline::Deadline;
use cgr_graph_rs::ingest::{merge_nodes, merge_relationships, KeyValue, NodeBatch, RelBatch};
use cgr_graph_rs::json_text::push_string;
use cgr_graph_rs::walk::{parse_seeds, to_json, walk, Direction, WalkSpec};
use cgr_graph_rs::Graph;
use cgr_graph_rs_fuzz::{require, Input};
use libfuzzer_sys::fuzz_target;
use rusqlite::types::Value as Sql;
use rusqlite::Connection;

/// `EG_SCHEMA_SQL` and `BUMP_GRAPH_VERSION`, from
/// codebase_rag/constants/embedded_graph.py and embedded_graph/sql.py.
const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS cgr_nodes(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    key TEXT,
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS cgr_nodes_label_key ON cgr_nodes(label, key) WHERE key IS NOT NULL;
CREATE INDEX IF NOT EXISTS cgr_nodes_key ON cgr_nodes(key);
CREATE INDEX IF NOT EXISTS cgr_nodes_label ON cgr_nodes(label);
CREATE TABLE IF NOT EXISTS cgr_edges(
    id INTEGER PRIMARY KEY,
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    type TEXT NOT NULL,
    props TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS cgr_edges_src ON cgr_edges(src, type);
CREATE INDEX IF NOT EXISTS cgr_edges_dst ON cgr_edges(dst, type);
CREATE INDEX IF NOT EXISTS cgr_edges_type ON cgr_edges(type);
CREATE TABLE IF NOT EXISTS cgr_meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);
";
const BUMP: &str = "INSERT INTO cgr_meta(name, value) VALUES ('graph_version', '1') \
     ON CONFLICT(name) DO UPDATE SET value = CAST(value AS INTEGER) + 1";

const MAX_STEPS: usize = 8;
const MAX_ROWS: usize = 4;
const LABELS: [&str; 2] = ["Function", "Class"];
const KEYS: [&str; 4] = ["f", "g", "h", "1"];
const TYPES: [&str; 3] = ["CALLS", "DEFINES", "IMPORTS"];
/// Merge-key values as the engine binds them, with their JSON text. No two
/// compare equal in SQLite, so the model compares them by position.
const LINES: [(KeyValue, &str); 5] = [
    (KeyValue::Null, "null"),
    (KeyValue::Int(0), "0"),
    (KeyValue::Int(7), "7"),
    (KeyValue::Text(String::new()), "\"7\""),
    (KeyValue::Float(1.5), "1.5"),
];
/// The walk's most hops. An unbounded walk lists every relationship-unique
/// path, which grows exponentially with the graph.
const MAX_DEPTH: i64 = 3;

#[derive(Clone, Debug, PartialEq)]
struct Node {
    id: i64,
    label: &'static str,
    key: String,
    n: Option<i64>,
}

#[derive(Clone, Debug, PartialEq)]
struct Edge {
    id: i64,
    src: i64,
    dst: i64,
    ty: &'static str,
    /// An index into LINES; 0 (null) when the edge has no merge key.
    line: usize,
    w: Option<i64>,
}

#[derive(Clone, Debug, Default, PartialEq)]
struct Model {
    nodes: Vec<Node>,
    edges: Vec<Edge>,
    version: i64,
}

/// What a row's SET writes.
#[derive(Clone, Copy)]
enum Patch {
    /// The key property as the row wrote it, and a counter.
    Counter(i64),
    /// Moves the key property to another key.
    Move(&'static str),
    /// Text that is not JSON: the batch must fail.
    Malformed,
}

fuzz_target!(|data: &[u8]| {
    let mut input = Input::new(data);
    let mut conn = Connection::open_in_memory().expect("an in-memory database");
    conn.execute_batch(SCHEMA).expect("the schema");
    let mut model = Model::default();
    let mut steps = 0;
    while !input.is_empty() && steps < MAX_STEPS {
        steps += 1;
        match input.below(3) {
            0 => node_batch(&mut input, &mut conn, &mut model),
            1 => relationship_batch(&mut input, &mut conn, &mut model),
            _ => check_walk(&mut input, &conn, &model),
        }
        check_database(&conn, &model);
    }
    // Graph::load reads the nodes in id order and the edges in id order,
    // keeping each one whose two ends exist.
    // Mutant: in lib.rs, order SQL_EDGES by `src`. Input (hex):
    // 090084000e0000cdd6aad6252a84 aborts.
    let graph = Graph::load(&conn).expect("Graph::load on the model's database");
    let ids: Vec<i64> = model.nodes.iter().map(|n| n.id).collect();
    let position = |id: i64| ids.binary_search(&id).expect("an edge between nodes");
    let edges: Vec<(usize, usize)> = model
        .edges
        .iter()
        .map(|e| (position(e.src), position(e.dst)))
        .collect();
    require!(
        graph.ids == ids,
        "Graph::load read nodes {:?}, not {ids:?}",
        graph.ids
    );
    require!(
        graph.edges == edges,
        "Graph::load read edges {:?}, not {edges:?}",
        graph.edges
    );
    for (v, out) in graph.out.iter().enumerate() {
        let want: Vec<usize> = edges.iter().filter(|e| e.0 == v).map(|e| e.1).collect();
        require!(
            *out == want,
            "node {v}'s out-neighbours are {out:?}, not {want:?}"
        );
    }
});

fn json_text(s: &str) -> String {
    let mut out = String::new();
    push_string(&mut out, s);
    out
}

/// A node batch: `MERGE (n:label {qn: row.id}) SET n += row.props`. Rows
/// find their node by key, or create it, in row order; then each row's SET
/// patches the properties, and a SET that changes the key property moves
/// the key, failing if another node of the label has it. The batch is one
/// transaction that bumps the graph version once.
///
/// Mutant: in ingest.rs `merge_nodes`, drop the `refresh.execute` call.
/// Input (hex): 00002f0701fa aborts. Mutant: make SQL_FIND_NODE find
/// nothing (`AND 0`). Input (hex): 09dd03 aborts.
fn node_batch(input: &mut Input, conn: &mut Connection, model: &mut Model) {
    let label = *input.pick(&LABELS);
    let rows = input.below(MAX_ROWS + 1);
    let keys: Vec<String> = (0..rows).map(|_| input.pick(&KEYS).to_string()).collect();
    let patches: Vec<Option<Patch>> = (0..rows)
        .map(|_| match input.below(6) {
            0 | 1 => None,
            2 | 3 => Some(Patch::Counter(input.byte() as i64)),
            4 => Some(Patch::Move(*input.pick(&KEYS))),
            _ => Some(Patch::Malformed),
        })
        .collect();
    let created: Vec<String> = keys
        .iter()
        .map(|k| format!("{{\"qn\": {}}}", json_text(k)))
        .collect();
    let texts: Vec<Option<String>> = keys
        .iter()
        .zip(&patches)
        .map(|(k, p)| {
            p.map(|p| match p {
                Patch::Counter(n) => format!("{{\"qn\":{},\"n\":{n}}}", json_text(k)),
                Patch::Move(to) => format!("{{\"qn\":{}}}", json_text(to)),
                Patch::Malformed => "not json".to_string(),
            })
        })
        .collect();

    let mut want = model.clone();
    let mut ok = true;
    let mut ids = Vec::new();
    for key in &keys {
        let found = want
            .nodes
            .iter()
            .find(|n| n.label == label && n.key == *key);
        let id = match found {
            Some(n) => n.id,
            None => {
                let id = want.nodes.last().map_or(1, |n| n.id + 1);
                want.nodes.push(Node {
                    id,
                    label,
                    key: key.clone(),
                    n: None,
                });
                id
            }
        };
        ids.push(id);
    }
    for ((&id, key), patch) in ids.iter().zip(&keys).zip(&patches) {
        let (to, n) = match patch {
            None => continue,
            Some(Patch::Malformed) => {
                ok = false;
                break;
            }
            Some(Patch::Counter(n)) => (key.as_str(), Some(*n)),
            Some(Patch::Move(to)) => (*to, None),
        };
        if want
            .nodes
            .iter()
            .any(|m| m.label == label && m.key == to && m.id != id)
        {
            ok = false;
            break;
        }
        let node = want
            .nodes
            .iter_mut()
            .find(|m| m.id == id)
            .expect("a merged node");
        node.key = to.to_string();
        if n.is_some() {
            node.n = n;
        }
    }

    let batch = NodeBatch {
        label,
        key_path: "'$.qn'",
        keys: &keys,
        created: &created,
        patches: &texts,
    };
    let result = merge_nodes(conn, BUMP, &batch);
    settle(result.is_ok(), ok, model, want, "merge_nodes");
}

/// A relationship batch: `MATCH (a:from {qn: ..}), (b:to {qn: ..}) MERGE
/// (a)-[r:type {line: ..}]->(b) [SET r += row.props] RETURN count(DISTINCT
/// row_index)`. A row whose ends do not both exist is skipped; each other
/// row finds the first relationship (by id) of its ends, type and merge
/// keys, or creates one; then the SETs run in row order. It returns how
/// many distinct row indexes matched.
///
/// Mutant: in ingest.rs, find the last match (`ORDER BY id DESC`). Input
/// (hex):
///   ae894022ff30002400008585858585858585858585857b788560600b61600700
///   60db20ee8585767a7a7a858585858585854384767a7a7a850057858585858585
///   858585858585858500000000a0a8a8a8
/// aborts. Mutant: return `matched.len()` in place of the distinct count.
/// Input (hex): 00200b9090a09090909004 aborts. Mutant: look both ends up
/// in the `from` cache. Input (hex): 60600b60600760cf531d0b aborts.
fn relationship_batch(input: &mut Input, conn: &mut Connection, model: &mut Model) {
    let from_label = *input.pick(&LABELS);
    let to_label = *input.pick(&LABELS);
    let ty = *input.pick(&TYPES);
    let keyed = input.bool();
    let rows = input.below(MAX_ROWS + 1);
    let end = |input: &mut Input| match input.below(5) {
        4 => None,
        k => Some(KEYS[k].to_string()),
    };
    let from_keys: Vec<Option<String>> = (0..rows).map(|_| end(input)).collect();
    let to_keys: Vec<Option<String>> = (0..rows).map(|_| end(input)).collect();
    let lines: Vec<usize> = (0..rows).map(|_| input.below(LINES.len())).collect();
    let row_indexes: Vec<i64> = (0..rows).map(|_| input.below(3) as i64).collect();
    let sets = input.bool();
    let patches: Vec<Option<Patch>> = (0..rows)
        .map(|_| match input.below(4) {
            0 => None,
            1 => Some(Patch::Malformed),
            _ => Some(Patch::Counter(input.byte() as i64)),
        })
        .collect();

    let merge_paths: Vec<String> = if keyed {
        vec!["'$.line'".into()]
    } else {
        vec![]
    };
    let merge_values: Vec<Vec<KeyValue>> = lines
        .iter()
        .map(|&l| if keyed { vec![line_value(l)] } else { vec![] })
        .collect();
    let created: Vec<String> = lines
        .iter()
        .map(|&l| {
            if keyed {
                format!("{{\"line\": {}}}", LINES[l].1)
            } else {
                "{}".into()
            }
        })
        .collect();
    let texts: Vec<Option<String>> = patches
        .iter()
        .map(|p| {
            p.map(|p| match p {
                Patch::Counter(n) => format!("{{\"w\":{n}}}"),
                _ => "not json".to_string(),
            })
        })
        .collect();

    let mut want = model.clone();
    let mut ok = true;
    let find = |label: &str, key: &Option<String>| {
        let key = key.as_ref()?;
        model
            .nodes
            .iter()
            .find(|n| n.label == label && n.key == *key)
            .map(|n| n.id)
    };
    let matched: Vec<(usize, i64, i64)> = (0..rows)
        .filter_map(|i| {
            Some((
                i,
                find(from_label, &from_keys[i])?,
                find(to_label, &to_keys[i])?,
            ))
        })
        .collect();
    let mut bound = Vec::new();
    for &(i, a, b) in &matched {
        let line = if keyed { lines[i] } else { 0 };
        let found = want
            .edges
            .iter()
            .find(|e| e.src == a && e.dst == b && e.ty == ty && (!keyed || e.line == line));
        let id = match found {
            Some(e) => e.id,
            None => {
                let id = want.edges.last().map_or(1, |e| e.id + 1);
                want.edges.push(Edge {
                    id,
                    src: a,
                    dst: b,
                    ty,
                    line,
                    w: None,
                });
                id
            }
        };
        bound.push(id);
    }
    if sets {
        for (&(i, _, _), &id) in matched.iter().zip(&bound) {
            match patches[i] {
                None => {}
                Some(Patch::Counter(n)) => {
                    want.edges
                        .iter_mut()
                        .find(|e| e.id == id)
                        .expect("a merged edge")
                        .w = Some(n)
                }
                Some(_) => ok = false,
            }
        }
    }
    let mut distinct: Vec<i64> = matched.iter().map(|&(i, _, _)| row_indexes[i]).collect();
    distinct.sort_unstable();
    distinct.dedup();

    let batch = RelBatch {
        from_label,
        to_label,
        rel_type: ty,
        from_keys: &from_keys,
        to_keys: &to_keys,
        merge_paths: &merge_paths,
        merge_values: &merge_values,
        created: &created,
        patches: if sets { Some(&texts) } else { None },
        row_indexes: &row_indexes,
    };
    let result = merge_relationships(conn, BUMP, &batch);
    if let Ok(counted) = result {
        require!(
            counted == distinct.len() as i64,
            "merge_relationships counted {counted}, not the {} distinct matched row indexes",
            distinct.len()
        );
    }
    settle(result.is_ok(), ok, model, want, "merge_relationships");
}

fn line_value(l: usize) -> KeyValue {
    match &LINES[l].0 {
        KeyValue::Text(_) => KeyValue::Text("7".into()),
        other => other.clone(),
    }
}

/// A batch succeeds exactly when the model says it does. A success applies
/// the model's changes and bumps the version once; a failure changes
/// nothing (`check_database` compares after each step).
///
/// Mutant: in ingest.rs `merge_nodes`, make the transaction commit when it
/// is dropped (`DropBehavior::Commit`). Input (hex): 30002f2f2f2f aborts.
fn settle(succeeded: bool, expected: bool, model: &mut Model, mut want: Model, what: &str) {
    require!(
        succeeded == expected,
        "{what} {} where the model {}",
        if succeeded { "succeeded" } else { "failed" },
        if expected { "succeeds" } else { "fails" }
    );
    if succeeded {
        want.version += 1;
        *model = want;
    }
}

/// The database holds exactly the model: nodes, their key and properties,
/// relationships, their merge key and properties, and the graph version.
fn check_database(conn: &Connection, model: &Model) {
    let mut stmt = conn
        .prepare(
            "SELECT id, label, key, json_extract(props, '$.qn'), json_extract(props, '$.n') \
             FROM cgr_nodes ORDER BY id",
        )
        .expect("prepare");
    let nodes: Vec<(i64, String, Option<String>, Option<String>, Option<i64>)> = stmt
        .query_map([], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("node rows");
    let want: Vec<_> = model
        .nodes
        .iter()
        .map(|n| {
            (
                n.id,
                n.label.to_string(),
                Some(n.key.clone()),
                Some(n.key.clone()),
                n.n,
            )
        })
        .collect();
    require!(nodes == want, "the nodes are {nodes:?}, not {want:?}");

    let mut stmt = conn
        .prepare(
            "SELECT id, src, dst, type, json_extract(props, '$.line'), json_extract(props, '$.w') \
             FROM cgr_edges ORDER BY id",
        )
        .expect("prepare");
    let edges: Vec<(i64, i64, i64, String, Sql, Option<i64>)> = stmt
        .query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
            ))
        })
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("edge rows");
    let want: Vec<_> = model
        .edges
        .iter()
        .map(|e| {
            let line = match line_value(e.line) {
                KeyValue::Null => Sql::Null,
                KeyValue::Int(v) => Sql::Integer(v),
                KeyValue::Float(v) => Sql::Real(v),
                KeyValue::Text(v) => Sql::Text(v),
            };
            (e.id, e.src, e.dst, e.ty.to_string(), line, e.w)
        })
        .collect();
    require!(
        edges == want,
        "the relationships are {edges:?}, not {want:?}"
    );

    let version: Option<String> = conn
        .query_row(
            "SELECT value FROM cgr_meta WHERE name = 'graph_version'",
            [],
            |r| r.get(0),
        )
        .ok();
    let expected = (model.version > 0).then(|| model.version.to_string());
    require!(
        version == expected,
        "the graph version is {version:?}, not {expected:?}"
    );
}

/// `json_group_array(id)` as SQLite renders it.
fn render_seeds(seeds: &[Option<i64>]) -> String {
    let items: Vec<String> = seeds
        .iter()
        .map(|s| s.map_or("null".to_string(), |v| v.to_string()))
        .collect();
    format!("[{}]", items.join(","))
}

/// A walk from seeds the input picks (nodes, NULL, or an id no node has),
/// in a direction, over relationship types (repeats allowed), up to a depth.
///
/// `parse_seeds` reads back what `json_group_array` renders. The walk's
/// rows are the rows of the engine's recursive CTE, in its order, as the
/// walk module's documentation states them: a FIFO queue of the seeds,
/// each row followed by its expansions; one row per relationship-unique
/// path; each node's edges in (type, id) order, and for an undirected step
/// the edges leaving the node, then the ones entering it that are not
/// self-loops. `to_json` writes the same rows, with each row's path.
///
/// Mutant: in walk.rs `prefetch`, drop the self-loop filter. Input (hex):
///   0051514747515151510019510108518051515151515151546020e0e0e0e0e0e0
///   e0e0e0e0e0e0e0e0e0310000000000ff
/// aborts, and so does the mutant that sorts edge ids descending in
/// walk.rs `read`.
/// Mutant: in walk.rs `walk`, drop the `on_path` test. Input (hex):
///   090084610000cdd6aad6a6a69999a6a6a6a6a6a6ffa6a6ffffffff00007fffff
///   03ff00d22323ffffffa6000000302b
/// aborts. Mutant: in walk.rs `to_json`, write `entry.edge + 1`. Input
/// (hex):
///   090084610000cdd6a6a6a6ffa6a6ffffffffff0000007fffeaff2d2323ffffff
///   a6a6a6a6252b84
/// aborts.
fn check_walk(input: &mut Input, conn: &Connection, model: &Model) {
    // Any text: if it parses, its rendering parses to the same seeds.
    // Mutant: in walk.rs `parse_seeds`, negate each id. Input (hex):
    // 02035b355d aborts.
    let raw = input.text(16);
    if let Ok(seeds) = parse_seeds(&raw) {
        require!(
            parse_seeds(&render_seeds(&seeds)).as_ref() == Ok(&seeds),
            "{raw:?} parses to {seeds:?}, whose rendering does not parse back"
        );
    }

    let missing = model.nodes.last().map_or(1, |n| n.id + 1);
    let seeds: Vec<Option<i64>> = (0..input.below(MAX_ROWS) + 1)
        .map(|_| match input.below(model.nodes.len() + 2) {
            0 => None,
            1 => Some(missing),
            k => Some(model.nodes[k - 2].id),
        })
        .collect();
    // Mutant: in walk.rs `parse_seeds`, map "null" to Some(0). Input
    // (hex): 0b aborts.
    let text = render_seeds(&seeds);
    let parsed = parse_seeds(&text);
    require!(
        parsed.as_ref() == Ok(&seeds),
        "{text:?} parses to {parsed:?}"
    );

    let direction = *input.pick(&[Direction::Out, Direction::In, Direction::Both]);
    let types: Vec<String> = (0..input.below(4))
        .map(|_| input.pick(&TYPES).to_string())
        .collect();
    let max_depth = input.below(MAX_DEPTH as usize + 2) as i64 - 1;
    let spec = WalkSpec {
        direction,
        types: types.clone(),
        max_depth: Some(max_depth),
    };
    let rows = match walk(conn, &seeds, &spec, &Deadline::Never) {
        Ok(rows) => rows,
        Err(_) => {
            require!(false, "walk failed on the model's database");
            return;
        }
    };

    let want = expected_walk(model, &seeds, direction, &types, max_depth);
    let got: Vec<(Option<i64>, Option<i64>, i64, Vec<i64>)> = rows
        .steps()
        .enumerate()
        .map(|(i, s)| (s.seed, s.node, s.depth, rows.edges(i)))
        .collect();
    require!(got == want, "walk returned {got:?}, not {want:?}");
    for (i, row) in want.iter().enumerate() {
        let path = path_text(&row.3);
        require!(
            rows.path(i) == path,
            "row {i}'s path is {:?}, not {path:?}",
            rows.path(i)
        );
    }
    let json = format!(
        "[{}]",
        want.iter()
            .map(|(seed, node, depth, edges)| {
                let id = |v: &Option<i64>| v.map_or("null".to_string(), |v| v.to_string());
                format!(
                    "[{},{},{depth},\"{}\"]",
                    id(seed),
                    id(node),
                    path_text(edges)
                )
            })
            .collect::<Vec<_>>()
            .join(",")
    );
    require!(
        to_json(&rows) == json,
        "to_json wrote {:?}, not {json:?}",
        to_json(&rows)
    );
}

fn path_text(edges: &[i64]) -> String {
    let mut path = String::from(",");
    for e in edges {
        path.push_str(&format!("{e},"));
    }
    path
}

/// The CTE's rows, computed from the model.
fn expected_walk(
    model: &Model,
    seeds: &[Option<i64>],
    direction: Direction,
    types: &[String],
    max_depth: i64,
) -> Vec<(Option<i64>, Option<i64>, i64, Vec<i64>)> {
    let takes = |e: &&Edge| types.is_empty() || types.iter().any(|t| t == e.ty);
    fn sorted(mut edges: Vec<&Edge>) -> Vec<&Edge> {
        edges.sort_by(|a, b| a.ty.cmp(b.ty).then(a.id.cmp(&b.id)));
        edges
    }
    let adjacent = |node: i64| -> Vec<(i64, i64)> {
        let out = sorted(
            model
                .edges
                .iter()
                .filter(takes)
                .filter(|e| e.src == node)
                .collect(),
        );
        let inbound = sorted(
            model
                .edges
                .iter()
                .filter(takes)
                .filter(|e| e.dst == node)
                .collect(),
        );
        match direction {
            Direction::Out => out.iter().map(|e| (e.id, e.dst)).collect(),
            Direction::In => inbound.iter().map(|e| (e.id, e.src)).collect(),
            Direction::Both => out
                .iter()
                .map(|e| (e.id, e.dst))
                .chain(
                    inbound
                        .iter()
                        .filter(|e| e.src != node)
                        .map(|e| (e.id, e.src)),
                )
                .collect(),
        }
    };
    let mut queue: Vec<(Option<i64>, Option<i64>, i64, Vec<i64>)> =
        seeds.iter().map(|&s| (s, s, 0, Vec::new())).collect();
    let mut head = 0;
    while head < queue.len() {
        let (seed, node, depth, edges) = queue[head].clone();
        if let (true, Some(node)) = (depth < max_depth, node) {
            for (edge, far) in adjacent(node) {
                if !edges.contains(&edge) {
                    let mut path = edges.clone();
                    path.push(edge);
                    queue.push((seed, Some(far), depth + 1, path));
                }
            }
        }
        head += 1;
    }
    queue
}
