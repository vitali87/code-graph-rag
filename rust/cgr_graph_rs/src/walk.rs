//! Variable-length relationship walks, row for row as the engine's CTE.
//!
//! `compiler._var_length` compiles `(a)-[:T*1..3]->(b)` to a recursive CTE
//! `eg_vlN(seed, node, depth, path)`: one row per relationship-unique path
//! from each seed, `path` listing its edge ids (`,4,9,`), which the MATCH
//! joins on seed, node and depth and checks against the pattern's other
//! relationships. This module yields the same rows in the same order, so a
//! query reads the same result whichever produced them:
//!
//! * SQLite runs a recursive CTE as a FIFO queue: the seeds in their
//!   subquery's order, then each extracted row's expansions appended. The
//!   walk is a breadth-first queue appended in the same order.
//! * Each expansion reads the edges through the same indexes the CTE's
//!   step uses, `cgr_edges_src (src, type)` or `cgr_edges_dst (dst, type)`,
//!   so in (type, id) order: one seek per node, keeping the pattern's types
//!   as they come (an `IN` list would seek once per type). An undirected step is SQLite's multi-index OR:
//!   the edges leaving the node, then those entering it that the first
//!   index did not already return, which leaves out its self-loops.
//! * A path never repeats a relationship (the CTE's `instr(path, ...)`).
//!
//! Edges are read with indexed lookups, each node's once per walk, on the
//! extension's read-only connection to the file (`reader`), at the calling
//! statement's snapshot (`snapshot`).

use std::collections::HashMap;
use std::fmt::Write as _;
use std::io::Write as _;
use std::rc::Rc;

use rusqlite::{params_from_iter, Connection};

use crate::deadline::{Deadline, Stopped};

/// Rows between two looks at the deadline within one level.
const DEADLINE_STRIDE: usize = 4096;

pub const OUT: &str = "out";
pub const IN: &str = "in";
pub const BOTH: &str = "both";

// A level's edges: the node, the edge id, its far end and type. CROSS JOIN
// keeps the id list outermost, so each node is a seek of the edge index
// (one per pattern type, bound from ?2 on).
const SQL_EDGES_OUT: &str =
    "SELECT e.src, e.id, e.dst, e.type FROM json_each(?1) AS j CROSS JOIN cgr_edges AS e ON e.src = j.value";
const SQL_EDGES_IN: &str =
    "SELECT e.dst, e.id, e.src, e.type FROM json_each(?1) AS j CROSS JOIN cgr_edges AS e ON e.dst = j.value";
const SQL_TYPE_FILTER: &str = " AND e.type IN (";

/// Which way a pattern's relationships point from the seed.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Out,
    In,
    Both,
}

impl Direction {
    /// The compiler's names for directions (`EgDirection`).
    pub fn parse(name: &str) -> Option<Direction> {
        match name {
            OUT => Some(Direction::Out),
            IN => Some(Direction::In),
            BOTH => Some(Direction::Both),
            _ => None,
        }
    }
}

/// What a variable-length pattern allows: a direction, relationship types
/// (none means any) and the most hops (`None` for unbounded).
#[derive(Clone, Debug)]
pub struct WalkSpec {
    pub direction: Direction,
    pub types: Vec<String>,
    pub max_depth: Option<i64>,
}

/// One row of the walk, but its path. A NULL seed (a node an OPTIONAL MATCH
/// left unbound) yields one all-NULL row at depth 0, which no join can
/// match, as in SQL.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Step {
    pub seed: Option<i64>,
    pub node: Option<i64>,
    pub depth: i64,
}

const ROOT: usize = usize::MAX;

/// A queued path: its last step, the edge that took it there, and the
/// queue position of the path it extends.
struct Entry {
    step: Step,
    edge: i64,
    parent: usize,
}

/// A walk's rows in the CTE's order; a row's path is its parents' edges.
pub struct Walk {
    entries: Vec<Entry>,
}

impl Walk {
    pub fn len(&self) -> usize {
        self.entries.len()
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn steps(&self) -> impl Iterator<Item = Step> + '_ {
        self.entries.iter().map(|e| e.step)
    }

    /// Row `i`'s edge ids, from its seed outwards.
    pub fn edges(&self, i: usize) -> Vec<i64> {
        let mut edges = Vec::new();
        let mut at = i;
        while at != ROOT && self.entries[at].parent != ROOT {
            edges.push(self.entries[at].edge);
            at = self.entries[at].parent;
        }
        edges.reverse();
        edges
    }

    /// Row `i`'s `path` column as the CTE builds it: `,`, then each edge
    /// id followed by `,`.
    pub fn path(&self, i: usize) -> String {
        let mut path = String::from(",");
        for edge in self.edges(i) {
            // Writing to a String cannot fail.
            let _ = write!(path, "{edge},");
        }
        path
    }
}

/// Whether the path ending at `at` already used `edge`.
fn on_path(entries: &[Entry], mut at: usize, edge: i64) -> bool {
    while at != ROOT {
        let entry = &entries[at];
        if entry.parent != ROOT && entry.edge == edge {
            return true;
        }
        at = entry.parent;
    }
    false
}

/// An edge as read: its type (an index into `Adjacency::type_names`), id
/// and far end.
type Read = (u32, i64, i64);

/// Each node's edges for this walk, read once and kept: an unbounded walk
/// reaches the same node along many paths.
struct Adjacency<'c> {
    conn: &'c Connection,
    direction: Direction,
    /// The pattern's types, bound after the ids; empty takes every type.
    types: Vec<String>,
    sql_out: String,
    sql_in: String,
    type_ids: HashMap<String, u32>,
    type_names: Vec<String>,
    seen: HashMap<i64, Rc<[(i64, i64)]>>,
}

impl<'c> Adjacency<'c> {
    fn new(conn: &'c Connection, spec: &WalkSpec) -> Adjacency<'c> {
        let mut types = spec.types.clone();
        // `type IN (...)` ignores repeats; a repeated type must not repeat edges.
        types.sort();
        types.dedup();
        let filter = if types.is_empty() {
            String::new()
        } else {
            let marks: Vec<String> = (0..types.len()).map(|i| format!("?{}", i + 2)).collect();
            format!("{SQL_TYPE_FILTER}{})", marks.join(", "))
        };
        Adjacency {
            conn,
            direction: spec.direction,
            sql_out: format!("{SQL_EDGES_OUT}{filter}"),
            sql_in: format!("{SQL_EDGES_IN}{filter}"),
            types,
            type_ids: HashMap::new(),
            type_names: Vec::new(),
            seen: HashMap::new(),
        }
    }

    /// The pattern's edges at each of `ids` (a JSON array), by node; one
    /// statement seeks every node through `sql`'s index.
    fn read(&mut self, sql: &str, ids: &str) -> rusqlite::Result<HashMap<i64, Vec<Read>>> {
        let mut by_node: HashMap<i64, Vec<Read>> = HashMap::new();
        let mut stmt = self.conn.prepare_cached(sql)?;
        let args = std::iter::once(ids).chain(self.types.iter().map(String::as_str));
        let mut rows = stmt.query(params_from_iter(args))?;
        while let Some(row) = rows.next()? {
            let name = row.get_ref(3)?.as_str()?;
            let t = match self.type_ids.get(name) {
                Some(&t) => t,
                None => {
                    let t = self.type_names.len() as u32;
                    self.type_ids.insert(name.to_string(), t);
                    self.type_names.push(name.to_string());
                    t
                }
            };
            by_node
                .entry(row.get(0)?)
                .or_default()
                .push((t, row.get(1)?, row.get(2)?));
        }
        // The CTE's step reads a node's edges in (type, id) index order.
        for edges in by_node.values_mut() {
            edges.sort_by(|a, b| {
                self.type_names[a.0 as usize]
                    .cmp(&self.type_names[b.0 as usize])
                    .then(a.1.cmp(&b.1))
            });
        }
        Ok(by_node)
    }

    /// Read the edges of every node in `nodes` not read yet.
    fn prefetch(&mut self, nodes: impl Iterator<Item = i64>) -> rusqlite::Result<()> {
        let mut wanted: Vec<i64> = nodes.filter(|n| !self.seen.contains_key(n)).collect();
        wanted.sort_unstable();
        wanted.dedup();
        if wanted.is_empty() {
            return Ok(());
        }
        let mut ids = String::with_capacity(wanted.len() * 8 + 2);
        ids.push('[');
        for (i, node) in wanted.iter().enumerate() {
            let _ = write!(ids, "{}{node}", if i > 0 { "," } else { "" });
        }
        ids.push(']');
        let (sql_out, sql_in) = (self.sql_out.clone(), self.sql_in.clone());
        let (out, inbound) = match self.direction {
            Direction::Out => (self.read(&sql_out, &ids)?, HashMap::new()),
            Direction::In => (HashMap::new(), self.read(&sql_in, &ids)?),
            Direction::Both => (self.read(&sql_out, &ids)?, self.read(&sql_in, &ids)?),
        };
        for node in wanted {
            let mut edges: Vec<(i64, i64)> = Vec::new();
            edges.extend(
                out.get(&node)
                    .into_iter()
                    .flatten()
                    .map(|&(_, e, f)| (e, f)),
            );
            // A self-loop matched both of an undirected step's OR terms;
            // SQLite returns it once, from the first.
            let skip_loops = self.direction == Direction::Both;
            edges.extend(
                inbound
                    .get(&node)
                    .into_iter()
                    .flatten()
                    .filter(|&&(_, _, far)| !(skip_loops && far == node))
                    .map(|&(_, e, f)| (e, f)),
            );
            self.seen.insert(node, edges.into());
        }
        Ok(())
    }

    /// (edge id, far end) for every edge the pattern can take from `node`.
    fn of(&mut self, node: i64) -> rusqlite::Result<Rc<[(i64, i64)]>> {
        if !self.seen.contains_key(&node) {
            self.prefetch(std::iter::once(node))?;
        }
        Ok(Rc::clone(&self.seen[&node]))
    }
}

/// Every row the CTE yields for these seeds, in its order.
///
/// It stops at `deadline`, checked at each level and every
/// `DEADLINE_STRIDE` rows in between, as SQLite's progress handler stops
/// the CTE.
pub fn walk(
    conn: &Connection,
    seeds: &[Option<i64>],
    spec: &WalkSpec,
    deadline: &Deadline,
) -> Result<Walk, Stopped> {
    let mut adjacency = Adjacency::new(conn, spec);
    let mut entries: Vec<Entry> = seeds
        .iter()
        .map(|&seed| Entry {
            step: Step {
                seed,
                node: seed,
                depth: 0,
            },
            edge: 0,
            parent: ROOT,
        })
        .collect();
    let expands = |step: &Step| spec.max_depth.is_none_or(|most| step.depth < most);
    let mut head = 0;
    let mut level_end = 0;
    while head < entries.len() {
        // A FIFO queue holds one depth at a time once the one before is
        // done: read the edges of that whole level at once.
        if head == level_end || head % DEADLINE_STRIDE == 0 {
            deadline.check()?;
        }
        if head == level_end {
            level_end = entries.len();
            let level = entries[head..level_end]
                .iter()
                .map(|e| e.step)
                .filter(expands);
            adjacency.prefetch(level.filter_map(|s| s.node))?;
        }
        let step = entries[head].step;
        if let (true, Some(node)) = (expands(&step), step.node) {
            for &(edge, far) in adjacency.of(node)?.iter() {
                if !on_path(&entries, head, edge) {
                    entries.push(Entry {
                        step: Step {
                            seed: step.seed,
                            node: Some(far),
                            depth: step.depth + 1,
                        },
                        edge,
                        parent: head,
                    });
                }
            }
        }
        head += 1;
    }
    Ok(Walk { entries })
}

/// Why a seed list from SQL could not be read.
#[derive(Debug, PartialEq, Eq)]
pub struct BadSeeds(pub String);

/// The seeds as `json_group_array(id)` renders them: integers and nulls.
/// The compiler only walks from node ids, so anything else is a bug there.
pub fn parse_seeds(text: &str) -> Result<Vec<Option<i64>>, BadSeeds> {
    let inner = text
        .trim()
        .strip_prefix('[')
        .and_then(|t| t.strip_suffix(']'))
        .ok_or_else(|| BadSeeds(text.to_string()))?;
    if inner.trim().is_empty() {
        return Ok(Vec::new());
    }
    inner
        .split(',')
        .map(|token| match token.trim() {
            "null" => Ok(None),
            t => t
                .parse::<i64>()
                .map(Some)
                .map_err(|_| BadSeeds(t.to_string())),
        })
        .collect()
}

// Writing to a Vec cannot fail.
fn push_int(out: &mut Vec<u8>, v: i64) {
    let _ = write!(out, "{v}");
}

fn push_id(out: &mut Vec<u8>, id: Option<i64>) {
    match id {
        Some(v) => push_int(out, v),
        None => out.extend_from_slice(b"null"),
    }
}

/// The rows as a JSON array of `[seed, node, depth, path]`, for `json_each`.
pub fn to_json(walk: &Walk) -> String {
    // A row's path is its parent's plus one edge, and the parent's was
    // written earlier: each path is copied from the text already out.
    let mut out: Vec<u8> = Vec::with_capacity(walk.len() * 32 + 2);
    let mut paths: Vec<(usize, usize)> = Vec::with_capacity(walk.len());
    out.push(b'[');
    for (i, entry) in walk.entries.iter().enumerate() {
        if i > 0 {
            out.push(b',');
        }
        out.push(b'[');
        push_id(&mut out, entry.step.seed);
        out.push(b',');
        push_id(&mut out, entry.step.node);
        out.push(b',');
        push_int(&mut out, entry.step.depth);
        out.extend_from_slice(b",\"");
        let start = out.len();
        if entry.parent == ROOT {
            out.push(b',');
        } else {
            let (from, len) = paths[entry.parent];
            out.extend_from_within(from..from + len);
            push_int(&mut out, entry.edge);
            out.push(b',');
        }
        paths.push((start, out.len() - start));
        out.extend_from_slice(b"\"]");
    }
    out.push(b']');
    // Only ASCII digits, signs and punctuation were written.
    String::from_utf8(out).expect("ASCII")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph() -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch(
            "CREATE TABLE cgr_edges(id INTEGER PRIMARY KEY, src INTEGER NOT NULL, dst INTEGER NOT NULL, \
             type TEXT NOT NULL, props TEXT NOT NULL DEFAULT '{}');
             CREATE INDEX cgr_edges_src ON cgr_edges(src, type);
             CREATE INDEX cgr_edges_dst ON cgr_edges(dst, type);
             INSERT INTO cgr_edges(id, src, dst, type) VALUES
               (1, 1, 2, 'B'), (2, 1, 3, 'A'), (3, 2, 1, 'A'), (4, 3, 3, 'A');",
        )
        .expect("schema");
        conn
    }

    fn spec(direction: Direction, types: &[&str], max_depth: Option<i64>) -> WalkSpec {
        WalkSpec {
            direction,
            types: types.iter().map(|t| t.to_string()).collect(),
            max_depth,
        }
    }

    fn rows(walk: &Walk) -> Vec<(Option<i64>, Option<i64>, i64, String)> {
        walk.steps()
            .enumerate()
            .map(|(i, s)| (s.seed, s.node, s.depth, walk.path(i)))
            .collect()
    }

    #[test]
    fn edges_come_in_type_then_id_order() {
        let w = walk(
            &graph(),
            &[Some(1)],
            &spec(Direction::Out, &[], Some(1)),
            &Deadline::Never,
        )
        .unwrap();
        // Edge 2 (type A) before edge 1 (type B), as the (src, type) index reads them.
        assert_eq!(
            rows(&w),
            vec![
                (Some(1), Some(1), 0, ",".into()),
                (Some(1), Some(3), 1, ",2,".into()),
                (Some(1), Some(2), 1, ",1,".into())
            ]
        );
    }

    #[test]
    fn a_path_never_repeats_an_edge() {
        // The self-loop at 3 is taken once per path, then the walk stops.
        let w = walk(
            &graph(),
            &[Some(3)],
            &spec(Direction::Out, &["A"], None),
            &Deadline::Never,
        )
        .unwrap();
        assert_eq!(
            rows(&w),
            vec![
                (Some(3), Some(3), 0, ",".into()),
                (Some(3), Some(3), 1, ",4,".into())
            ]
        );
    }

    #[test]
    fn an_undirected_step_takes_a_self_loop_once() {
        let w = walk(
            &graph(),
            &[Some(3)],
            &spec(Direction::Both, &["A"], Some(1)),
            &Deadline::Never,
        )
        .unwrap();
        let far: Vec<(Option<i64>, String)> = rows(&w).into_iter().map(|r| (r.1, r.3)).collect();
        assert_eq!(
            far,
            vec![
                (Some(3), ",".into()),
                (Some(3), ",4,".into()),
                (Some(1), ",2,".into())
            ]
        );
    }

    #[test]
    fn a_null_seed_yields_one_row_that_walks_nowhere() {
        let w = walk(
            &graph(),
            &[None],
            &spec(Direction::In, &[], None),
            &Deadline::Never,
        )
        .unwrap();
        assert_eq!(rows(&w), vec![(None, None, 0, ",".into())]);
    }

    #[test]
    fn a_passed_deadline_stops_the_walk() {
        let passed = Deadline::after(Some(0.0));
        let stopped = walk(
            &graph(),
            &[Some(1)],
            &spec(Direction::Out, &[], None),
            &passed,
        );
        assert!(matches!(stopped, Err(Stopped::TimedOut)));
    }

    #[test]
    fn seeds_read_integers_and_nulls() {
        assert_eq!(parse_seeds("[]"), Ok(vec![]));
        assert_eq!(
            parse_seeds("[3,null,-1]"),
            Ok(vec![Some(3), None, Some(-1)])
        );
        assert!(parse_seeds("[1.5]").is_err());
        assert!(parse_seeds("1").is_err());
    }

    #[test]
    fn rows_render_as_json_with_their_paths() {
        let w = walk(
            &graph(),
            &[Some(2), None],
            &spec(Direction::Out, &["A"], Some(2)),
            &Deadline::Never,
        )
        .unwrap();
        assert_eq!(
            to_json(&w),
            r#"[[2,2,0,","],[null,null,0,","],[2,1,1,",3,"],[2,3,2,",3,2,"]]"#
        );
    }
}
