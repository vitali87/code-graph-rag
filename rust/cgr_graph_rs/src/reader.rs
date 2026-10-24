//! The extension's read-only connection to one graph file.
//!
//! The engine keeps one `Reader` per file for as long as any of its own
//! connections to the file is open (codebase_rag/embedded_graph/native.py
//! says why), and serves from it the walks its compiled queries call and the
//! procedures `run_procedure` ports. Both read at the caller's snapshot or
//! not at all (see `snapshot`), and stop at the caller's deadline, raising
//! `Timeout` (see `deadline`).

use std::sync::Mutex;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyList;
use rusqlite::Connection;

use crate::deadline::{Deadline, Stopped};
use crate::python::{records_to_list, stopped_error, to_arg};
use crate::snapshot::at_version;
use crate::walk::{parse_seeds, to_json, walk, Direction, WalkSpec};
use crate::{implements, open, run_until, Arg, Graph};

#[pyclass(frozen)]
pub struct Reader {
    path: String,
    busy_timeout_ms: i64,
    cache_kib: i64,
    /// The engine's query for `cgr_meta.graph_version`.
    version_sql: String,
    conn: Mutex<Option<Connection>>,
}

impl Reader {
    /// `work` on the connection (opened on first use, configured as the
    /// engine configures its own) at the caller's `version`, if `deadline`
    /// has not passed.
    fn at<T: Send>(
        &self,
        py: Python<'_>,
        version: Option<&str>,
        deadline: &Deadline,
        work: impl FnOnce(&Connection) -> Result<T, Stopped> + Send,
    ) -> PyResult<Option<T>> {
        py.detach(|| -> Result<Option<T>, Stopped> {
            deadline.check()?;
            let mut guard = self.conn.lock().unwrap_or_else(|e| e.into_inner());
            if guard.is_none() {
                let conn = open(&self.path)?;
                conn.pragma_update(None, "busy_timeout", self.busy_timeout_ms)?;
                conn.pragma_update(None, "cache_size", -self.cache_kib)?;
                *guard = Some(conn);
            }
            at_version(
                guard.as_ref().expect("opened above"),
                &self.version_sql,
                version,
                work,
            )
        })
        .map_err(stopped_error)
    }
}

#[pymethods]
impl Reader {
    #[new]
    fn new(path: String, busy_timeout_ms: i64, cache_kib: i64, version_sql: String) -> Self {
        Reader {
            path,
            busy_timeout_ms,
            cache_kib,
            version_sql,
            conn: Mutex::new(None),
        }
    }

    /// The SQL function a compiled walk calls (through native.py, which
    /// puts the statement's `seconds_left` first): its rows as JSON text,
    /// `[[seed, node, depth, path], ...]` (see `walk`), or None where the
    /// graph is newer than `version`. A statement cannot fall back halfway,
    /// so native.py then fails it and runs it again with SQL's walk.
    #[pyo3(signature = (seconds_left, seeds, version, direction, max_depth, *types))]
    #[allow(clippy::too_many_arguments)]
    fn walk(
        &self,
        py: Python<'_>,
        seconds_left: Option<f64>,
        seeds: &str,
        version: Option<&str>,
        direction: &str,
        max_depth: Option<i64>,
        types: Vec<String>,
    ) -> PyResult<Option<String>> {
        let seeds = parse_seeds(seeds).map_err(|e| PyValueError::new_err(e.0))?;
        let direction = Direction::parse(direction)
            .ok_or_else(|| PyValueError::new_err(direction.to_string()))?;
        let spec = WalkSpec {
            direction,
            types,
            max_depth,
        };
        let deadline = Deadline::after(seconds_left);
        self.at(py, version, &deadline, |conn| {
            Ok(to_json(&walk(conn, &seeds, &spec, &deadline)?))
        })
    }

    /// A ported procedure's records at `version`, as `run_procedure`
    /// returns them; None for one not ported or a graph that changed.
    /// Raises `Timeout` once `seconds_left` run out.
    fn run_procedure<'py>(
        &self,
        py: Python<'py>,
        version: Option<&str>,
        name: &str,
        args: Vec<Bound<'py, PyAny>>,
        seconds_left: Option<f64>,
    ) -> PyResult<Option<Bound<'py, PyList>>> {
        if !implements(name) {
            return Ok(None);
        }
        let args: Vec<Arg> = args.iter().map(to_arg).collect();
        let deadline = Deadline::after(seconds_left);
        let records = self.at(py, version, &deadline, |conn| {
            let graph = Graph::load(conn)?;
            deadline.check()?;
            Ok(run_until(&graph, name, &args, &deadline)?)
        })?;
        match records.flatten() {
            Some(records) => records_to_list(py, records).map(Some),
            None => Ok(None),
        }
    }

    /// Close the connection; the engine calls this once none of its own
    /// connections to the file is open.
    fn close(&self) {
        let mut guard = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        guard.take();
    }
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Reader>()
}
