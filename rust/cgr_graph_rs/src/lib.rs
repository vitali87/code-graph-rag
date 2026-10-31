//! Graph algorithms for code-graph-rag's embedded graph (issue #386).
//!
//! The embedded engine (`codebase_rag/embedded_graph`) keeps the graph in a
//! SQLite file and answers `CALL pagerank.get()` and the other procedures
//! the Cypher prompt advertises. Its Python implementations are the
//! reference; when this module is importable, `procedures.run_procedure`
//! hands the whole-graph algorithms here instead. Each one is a port of the
//! Python function of the same name: same node order (ascending id), same
//! adjacency order (ascending edge id), same floating-point operations in
//! the same order, so either path returns the same records.
//!
//! The file is opened read-only on a connection of its own, which sees
//! committed data only; the engine therefore keeps statements that write
//! before their CALL on the Python path. The `_until` variants stop at the
//! statement's deadline where the Python ones call `check_deadline()`.

use std::collections::{HashMap, VecDeque};

use rusqlite::{Connection, OpenFlags};

use deadline::{Deadline, TimedOut};

pub mod cypher;
pub mod deadline;
#[cfg(feature = "python")]
mod decode;
pub mod ingest;
pub mod json_text;
#[cfg(feature = "python")]
mod reader;
pub mod snapshot;
pub mod trees;
pub mod walk;

const DEFAULT_ITERATIONS: u64 = 100;
const DEFAULT_DAMPING: f64 = 0.85;
const DEFAULT_EPSILON: f64 = 1e-5;

pub const PAGERANK: &str = "pagerank.get";
pub const DEGREE: &str = "degree_centrality.get";
pub const WCC: &str = "weakly_connected_components.get";
pub const SCC: &str = "nxalg.strongly_connected_components";
pub const BETWEENNESS: &str = "betweenness_centrality.get";
pub const ANCESTORS: &str = "graph_util.ancestors";
pub const DESCENDANTS: &str = "graph_util.descendants";

const SQL_NODES: &str = "SELECT id FROM cgr_nodes ORDER BY id";
const SQL_EDGES: &str = "SELECT src, dst FROM cgr_edges ORDER BY id";

/// A procedure argument, as the engine evaluated it.
#[derive(Clone, Debug, PartialEq)]
pub enum Arg {
    Null,
    Int(i64),
    Float(f64),
    Text(String),
}

/// A value in an output record.
#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Int(i64),
    Float(f64),
    Ids(Vec<i64>),
    IdLists(Vec<Vec<i64>>),
}

pub type Record = Vec<(&'static str, Value)>;

/// Nodes in ascending id order, and each node's out-neighbours in
/// ascending edge-id order, both by position in `ids`.
pub struct Graph {
    pub ids: Vec<i64>,
    pub out: Vec<Vec<usize>>,
    pub edges: Vec<(usize, usize)>,
}

impl Graph {
    pub fn load(conn: &Connection) -> rusqlite::Result<Graph> {
        let mut ids = Vec::new();
        let mut stmt = conn.prepare(SQL_NODES)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            ids.push(row.get::<_, i64>(0)?);
        }
        let index: HashMap<i64, usize> = ids.iter().enumerate().map(|(i, &id)| (id, i)).collect();
        let mut out = vec![Vec::new(); ids.len()];
        let mut edges = Vec::new();
        let mut stmt = conn.prepare(SQL_EDGES)?;
        let mut rows = stmt.query([])?;
        while let Some(row) = rows.next()? {
            let (src, dst) = (row.get::<_, i64>(0)?, row.get::<_, i64>(1)?);
            // DETACH DELETE keeps edges and nodes consistent; an edge to a
            // missing node could only come from a hand-edited file.
            if let (Some(&s), Some(&d)) = (index.get(&src), index.get(&dst)) {
                out[s].push(d);
                edges.push((s, d));
            }
        }
        Ok(Graph { ids, out, edges })
    }

    pub fn from_edges(ids: Vec<i64>, edges: &[(i64, i64)]) -> Graph {
        let index: HashMap<i64, usize> = ids.iter().enumerate().map(|(i, &id)| (id, i)).collect();
        let mut out = vec![Vec::new(); ids.len()];
        let mut kept = Vec::new();
        for &(src, dst) in edges {
            let (s, d) = (index[&src], index[&dst]);
            out[s].push(d);
            kept.push((s, d));
        }
        Graph { ids, out, edges: kept }
    }

    fn position(&self, id: i64) -> Option<usize> {
        self.ids.binary_search(&id).ok()
    }
}

fn int_arg(args: &[Arg], i: usize) -> Option<i64> {
    match args.get(i) {
        Some(Arg::Int(v)) => Some(*v),
        Some(Arg::Float(v)) => Some(*v as i64),
        _ => None,
    }
}

fn float_arg(args: &[Arg], i: usize) -> Option<f64> {
    match args.get(i) {
        Some(Arg::Int(v)) => Some(*v as f64),
        Some(Arg::Float(v)) => Some(*v),
        _ => None,
    }
}

/// A run without a deadline, which cannot time out.
fn unlimited<T>(result: Result<T, TimedOut>) -> T {
    result.expect("no deadline to pass")
}

pub fn pagerank(g: &Graph, args: &[Arg]) -> Vec<Record> {
    unlimited(pagerank_until(g, args, &Deadline::Never))
}

/// `pagerank`, checking `deadline` before each iteration.
pub fn pagerank_until(
    g: &Graph,
    args: &[Arg],
    deadline: &Deadline,
) -> Result<Vec<Record>, TimedOut> {
    let iterations = int_arg(args, 0).map_or(DEFAULT_ITERATIONS, |v| v.max(0) as u64);
    let damping = float_arg(args, 1).unwrap_or(DEFAULT_DAMPING);
    let epsilon = float_arg(args, 2).unwrap_or(DEFAULT_EPSILON);
    let n = g.ids.len();
    if n == 0 {
        return Ok(Vec::new());
    }
    let mut rank = vec![1.0 / n as f64; n];
    let mut next = vec![0.0; n];
    for _ in 0..iterations {
        deadline.check()?;
        next.iter_mut().for_each(|x| *x = 0.0);
        let mut dangling = 0.0;
        for v in 0..n {
            if g.out[v].is_empty() {
                dangling += rank[v];
            }
        }
        for v in 0..n {
            if !g.out[v].is_empty() {
                let share = rank[v] / g.out[v].len() as f64;
                for &w in &g.out[v] {
                    next[w] += share;
                }
            }
        }
        let base = (1.0 - damping) / n as f64 + damping * dangling / n as f64;
        let mut delta = 0.0;
        for v in 0..n {
            let value = base + damping * next[v];
            delta += (value - rank[v]).abs();
            rank[v] = value;
        }
        if delta < epsilon {
            break;
        }
    }
    Ok((0..n)
        .map(|v| vec![("node", Value::Int(g.ids[v])), ("rank", Value::Float(rank[v]))])
        .collect())
}

pub fn degree(g: &Graph, args: &[Arg]) -> Vec<Record> {
    let mode = match args.first() {
        Some(Arg::Text(m)) => m.to_lowercase(),
        _ => "undirected".to_string(),
    };
    let mut counts = vec![0i64; g.ids.len()];
    for &(s, d) in &g.edges {
        if mode == "out" || mode == "undirected" {
            counts[s] += 1;
        }
        if mode == "in" || mode == "undirected" {
            counts[d] += 1;
        }
    }
    (0..g.ids.len())
        .map(|v| vec![("node", Value::Int(g.ids[v])), ("degree", Value::Int(counts[v]))])
        .collect()
}

pub fn wcc(g: &Graph) -> Vec<Record> {
    let n = g.ids.len();
    let mut parent: Vec<usize> = (0..n).collect();
    fn find(parent: &mut [usize], mut v: usize) -> usize {
        while parent[v] != v {
            parent[v] = parent[parent[v]];
            v = parent[v];
        }
        v
    }
    for &(s, d) in &g.edges {
        let (rs, rd) = (find(&mut parent, s), find(&mut parent, d));
        if rs != rd {
            parent[rs.max(rd)] = rs.min(rd);
        }
    }
    let mut component: HashMap<usize, i64> = HashMap::new();
    (0..n)
        .map(|v| {
            let root = find(&mut parent, v);
            let next = component.len() as i64;
            let id = *component.entry(root).or_insert(next);
            vec![("node", Value::Int(g.ids[v])), ("component_id", Value::Int(id))]
        })
        .collect()
}

/// Tarjan's algorithm with an explicit work stack, step for step as the
/// Python `_tarjan`, so components and their members come out in the
/// same order.
pub fn tarjan(g: &Graph) -> Vec<Vec<usize>> {
    unlimited(tarjan_until(g, &Deadline::Never))
}

/// `tarjan`, checking `deadline` before each new root.
pub fn tarjan_until(g: &Graph, deadline: &Deadline) -> Result<Vec<Vec<usize>>, TimedOut> {
    const UNSEEN: usize = usize::MAX;
    let n = g.ids.len();
    let mut index = vec![UNSEEN; n];
    let mut low = vec![0usize; n];
    let mut on_stack = vec![false; n];
    let mut stack: Vec<usize> = Vec::new();
    let mut comps: Vec<Vec<usize>> = Vec::new();
    let mut counter = 0usize;
    for root in 0..n {
        if index[root] != UNSEEN {
            continue;
        }
        deadline.check()?;
        let mut work: Vec<(usize, usize)> = vec![(root, 0)];
        while let Some((v, i)) = work.pop() {
            if i == 0 {
                index[v] = counter;
                low[v] = counter;
                counter += 1;
                stack.push(v);
                on_stack[v] = true;
            }
            let targets = &g.out[v];
            if i < targets.len() {
                work.push((v, i + 1));
                let w = targets[i];
                if index[w] == UNSEEN {
                    work.push((w, 0));
                } else if on_stack[w] {
                    low[v] = low[v].min(index[w]);
                }
                continue;
            }
            if low[v] == index[v] {
                let mut comp = Vec::new();
                loop {
                    let w = stack.pop().expect("v is on the stack");
                    on_stack[w] = false;
                    comp.push(w);
                    if w == v {
                        break;
                    }
                }
                comps.push(comp);
            }
            if let Some(&(parent, _)) = work.last() {
                low[parent] = low[parent].min(low[v]);
            }
        }
    }
    Ok(comps)
}

pub fn scc(g: &Graph) -> Vec<Record> {
    unlimited(scc_until(g, &Deadline::Never))
}

/// `scc`, checking `deadline` as `tarjan_until` does.
pub fn scc_until(g: &Graph, deadline: &Deadline) -> Result<Vec<Record>, TimedOut> {
    let comps = tarjan_until(g, deadline)?
        .into_iter()
        .map(|c| c.into_iter().map(|v| g.ids[v]).collect())
        .collect();
    Ok(vec![vec![("components", Value::IdLists(comps))]])
}

/// Brandes' algorithm over directed edges, unweighted.
pub fn betweenness(g: &Graph) -> Vec<Record> {
    unlimited(betweenness_until(g, &Deadline::Never))
}

/// `betweenness`, checking `deadline` before each source.
pub fn betweenness_until(g: &Graph, deadline: &Deadline) -> Result<Vec<Record>, TimedOut> {
    let n = g.ids.len();
    let mut score = vec![0.0f64; n];
    let mut sigma = vec![0.0f64; n];
    let mut dist = vec![-1i64; n];
    let mut delta = vec![0.0f64; n];
    let mut preds: Vec<Vec<usize>> = vec![Vec::new(); n];
    let mut order: Vec<usize> = Vec::with_capacity(n);
    let mut queue: VecDeque<usize> = VecDeque::new();
    for s in 0..n {
        deadline.check()?;
        sigma.iter_mut().for_each(|x| *x = 0.0);
        dist.iter_mut().for_each(|x| *x = -1);
        delta.iter_mut().for_each(|x| *x = 0.0);
        preds.iter_mut().for_each(Vec::clear);
        order.clear();
        sigma[s] = 1.0;
        dist[s] = 0;
        queue.push_back(s);
        while let Some(v) = queue.pop_front() {
            order.push(v);
            for &w in &g.out[v] {
                if dist[w] < 0 {
                    dist[w] = dist[v] + 1;
                    queue.push_back(w);
                }
                if dist[w] == dist[v] + 1 {
                    sigma[w] += sigma[v];
                    preds[w].push(v);
                }
            }
        }
        while let Some(w) = order.pop() {
            for &v in &preds[w] {
                delta[v] += sigma[v] / sigma[w] * (1.0 + delta[w]);
            }
            if w != s {
                score[w] += delta[w];
            }
        }
    }
    let scale = if n > 2 { 1.0 / ((n - 1) * (n - 2)) as f64 } else { 1.0 };
    Ok((0..n)
        .map(|v| {
            vec![
                ("node", Value::Int(g.ids[v])),
                ("betweenness_centrality", Value::Float(score[v] * scale)),
            ]
        })
        .collect())
}

/// Every node reachable from `start` (forwards, or backwards when
/// `reverse`), without `start` itself, in ascending id order.
pub fn reach(g: &Graph, start: Option<i64>, reverse: bool, column: &'static str) -> Vec<Record> {
    unlimited(reach_until(g, start, reverse, column, &Deadline::Never))
}

/// `reach`, checking `deadline` before each step of the search.
pub fn reach_until(
    g: &Graph,
    start: Option<i64>,
    reverse: bool,
    column: &'static str,
    deadline: &Deadline,
) -> Result<Vec<Record>, TimedOut> {
    let Some(start_id) = start else {
        return Ok(vec![vec![(column, Value::Ids(Vec::new()))]]);
    };
    let Some(origin) = g.position(start_id) else {
        return Ok(vec![vec![(column, Value::Ids(Vec::new()))]]);
    };
    let mut adjacency: Vec<Vec<usize>> = vec![Vec::new(); g.ids.len()];
    for &(s, d) in &g.edges {
        if reverse {
            adjacency[d].push(s);
        } else {
            adjacency[s].push(d);
        }
    }
    let mut seen = vec![false; g.ids.len()];
    let mut queue = VecDeque::from([origin]);
    while let Some(v) = queue.pop_front() {
        deadline.check()?;
        for &w in &adjacency[v] {
            if !seen[w] && w != origin {
                seen[w] = true;
                queue.push_back(w);
            }
        }
    }
    let ids = (0..g.ids.len()).filter(|&v| seen[v]).map(|v| g.ids[v]).collect();
    Ok(vec![vec![(column, Value::Ids(ids))]])
}

/// Whether this module implements `name`; the engine keeps the rest.
pub fn implements(name: &str) -> bool {
    matches!(name, PAGERANK | DEGREE | WCC | SCC | BETWEENNESS | ANCESTORS | DESCENDANTS)
}

pub fn run(g: &Graph, name: &str, args: &[Arg]) -> Option<Vec<Record>> {
    unlimited(run_until(g, name, args, &Deadline::Never))
}

/// `run`, stopping at `deadline` where the Python procedures stop.
pub fn run_until(
    g: &Graph,
    name: &str,
    args: &[Arg],
    deadline: &Deadline,
) -> Result<Option<Vec<Record>>, TimedOut> {
    Ok(Some(match name {
        PAGERANK => pagerank_until(g, args, deadline)?,
        DEGREE => degree(g, args),
        WCC => wcc(g),
        SCC => scc_until(g, deadline)?,
        BETWEENNESS => betweenness_until(g, deadline)?,
        ANCESTORS => reach_until(g, int_arg(args, 0), true, "ancestors", deadline)?,
        DESCENDANTS => reach_until(g, int_arg(args, 0), false, "descendants", deadline)?,
        _ => return Ok(None),
    }))
}

pub fn open(path: &str) -> rusqlite::Result<Connection> {
    Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX | OpenFlags::SQLITE_OPEN_URI,
    )
}

#[cfg(feature = "python")]
pub(crate) mod python {
    use super::{implements, open, run_until, Arg, Deadline, Graph, Record, Value};
    use crate::deadline::Stopped;
    use pyo3::exceptions::{PyRuntimeError, PyTimeoutError};
    use pyo3::prelude::*;
    use pyo3::types::{PyDict, PyList};

    pyo3::create_exception!(
        cgr_graph_rs,
        Timeout,
        PyTimeoutError,
        "The statement's deadline passed while the extension worked for it."
    );

    /// A stopped read as the Python exception the engine expects.
    pub(crate) fn stopped_error(stopped: Stopped) -> PyErr {
        match stopped {
            Stopped::Sql(error) => PyRuntimeError::new_err(error.to_string()),
            Stopped::TimedOut => Timeout::new_err(()),
        }
    }

    pub(crate) fn to_arg(value: &Bound<'_, PyAny>) -> Arg {
        if value.is_none() {
            Arg::Null
        } else if let Ok(v) = value.extract::<bool>() {
            Arg::Int(v as i64)
        } else if let Ok(v) = value.extract::<i64>() {
            Arg::Int(v)
        } else if let Ok(v) = value.extract::<f64>() {
            Arg::Float(v)
        } else if let Ok(v) = value.extract::<String>() {
            Arg::Text(v)
        } else {
            Arg::Null
        }
    }

    /// The procedure's records as dicts, or None when the engine should run
    /// its own implementation: a procedure not ported here, or a graph with
    /// no file (`:memory:`) to open. With `seconds_left`, it raises `Timeout`
    /// once they run out.
    #[pyfunction]
    #[pyo3(signature = (db_path, name, args, seconds_left=None))]
    fn run_procedure<'py>(
        py: Python<'py>,
        db_path: &str,
        name: &str,
        args: Vec<Bound<'py, PyAny>>,
        seconds_left: Option<f64>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        if db_path.is_empty() || !implements(name) {
            return Ok(None);
        }
        let args: Vec<Arg> = args.iter().map(to_arg).collect();
        let path = db_path.to_string();
        let name = name.to_string();
        let deadline = Deadline::after(seconds_left);
        let records = py
            .detach(move || -> Result<_, Stopped> {
                deadline.check()?;
                let conn = open(&path)?;
                let graph = Graph::load(&conn)?;
                Ok(run_until(&graph, &name, &args, &deadline)?)
            })
            .map_err(stopped_error)?;
        let Some(records) = records else {
            return Ok(None);
        };
        records_to_list(py, records).map(Some)
    }

    /// Records as the list of dicts the Python procedures return.
    pub(crate) fn records_to_list(
        py: Python<'_>,
        records: Vec<Record>,
    ) -> PyResult<Bound<'_, PyList>> {
        let out = PyList::empty(py);
        for record in records {
            let dict = PyDict::new(py);
            for (key, value) in record {
                match value {
                    Value::Int(v) => dict.set_item(key, v)?,
                    Value::Float(v) => dict.set_item(key, v)?,
                    Value::Ids(v) => dict.set_item(key, v)?,
                    Value::IdLists(v) => dict.set_item(key, v)?,
                }
            }
            out.append(dict)?;
        }
        Ok(out)
    }

    #[pymodule]
    fn cgr_graph_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(run_procedure, m)?)?;
        m.add("Timeout", m.py().get_type::<Timeout>())?;
        crate::cypher::python::register(m)?;
        crate::reader::register(m)?;
        crate::ingest::python::register(m)?;
        crate::decode::register(m)?;
        crate::trees::python::register(m)?;
        Ok(())
    }
}
