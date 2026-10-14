//! The ingestor's node and relationship batches, written in bulk.
//!
//! The ingestor flushes `UNWIND $batch AS row MERGE (n:L {key: row.id})
//! SET n += row.props` and `... MATCH (a), (b) MERGE (a)-[r:T {k: ...}]->(b)
//! SET r += row.props RETURN count(...)` through the Cypher engine, whose
//! executor runs a MERGE row by row from Python. This module issues the same
//! SQL statements in the same order (every MERGE of the batch, then every
//! SET), so rows get the same ids and the JSON functions write the same
//! property text; only the per-row round trips through Python are gone.
//!
//! The caller (codebase_rag/embedded_graph/native.py) renders each row's
//! JSON exactly as the engine would have and hands over only batches whose
//! values it can render that way; everything else stays on the engine. A
//! batch is one IMMEDIATE transaction, as the engine's statement is, that
//! bumps the graph version once, as the engine's does (see `snapshot`); a
//! failure rolls it back and is reported, so the engine can run it instead.

use std::collections::{HashMap, HashSet};

use rusqlite::types::{ToSqlOutput, Value as SqlValue};
use rusqlite::{
    params, params_from_iter, Connection, OptionalExtension, ToSql, TransactionBehavior,
};

const SQL_FIND_NODE: &str = "SELECT id FROM cgr_nodes WHERE label = ?1 AND key = ?2";
const SQL_INSERT_NODE: &str = "INSERT INTO cgr_nodes(label, key, props) VALUES (?1, ?2, ?3)";
const SQL_PATCH_NODE: &str = "UPDATE cgr_nodes SET props = json_patch(props, ?1) WHERE id = ?2";
const SQL_INSERT_EDGE: &str =
    "INSERT INTO cgr_edges(src, dst, type, props) VALUES (?1, ?2, ?3, ?4)";
const SQL_COUNT_FROM: &str = "SELECT count(*) FROM cgr_edges WHERE src = ?1 AND type = ?2";
const SQL_COUNT_TO: &str = "SELECT count(*) FROM cgr_edges WHERE dst = ?1 AND type = ?2";
// The engine's MERGE lookup, `SELECT id FROM cgr_edges WHERE src = ? AND
// dst = ? AND type = ? [AND json_extract(props, k) IS ?]... ORDER BY id
// LIMIT 1`, pinned to one end's index; either gives the same first id.
const SQL_FIND_EDGE_FROM: &str =
    "SELECT id FROM cgr_edges INDEXED BY cgr_edges_src WHERE src = ?1 AND dst = ?2 AND type = ?3";
const SQL_FIND_EDGE_TO: &str =
    "SELECT id FROM cgr_edges INDEXED BY cgr_edges_dst WHERE src = ?1 AND dst = ?2 AND type = ?3";
const SQL_FIND_EDGE_ORDER: &str = " ORDER BY id LIMIT 1";
const SQL_PATCH_EDGE: &str = "UPDATE cgr_edges SET props = json_patch(props, ?1) WHERE id = ?2";

/// A merge-key value as the engine binds it: booleans arrive as integers.
#[derive(Clone, Debug, PartialEq)]
pub enum KeyValue {
    Null,
    Int(i64),
    Float(f64),
    Text(String),
}

impl ToSql for KeyValue {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(match self {
            KeyValue::Null => ToSqlOutput::Owned(SqlValue::Null),
            KeyValue::Int(v) => ToSqlOutput::from(*v),
            KeyValue::Float(v) => ToSqlOutput::from(*v),
            KeyValue::Text(v) => ToSqlOutput::from(v.as_str()),
        })
    }
}

/// One node batch: `MERGE (n:label {key: row.id}) SET n += row.props`.
pub struct NodeBatch<'a> {
    pub label: &'a str,
    /// `json_path(key)` as the engine spells it, an SQL string literal.
    pub key_path: &'a str,
    /// Each row's key, as the engine looks it up (`str(row.id)`).
    pub keys: &'a [String],
    /// Each row's property text should it create the node.
    pub created: &'a [String],
    /// Each row's `row.props` as SQLite renders it; None skips the SET.
    pub patches: &'a [Option<String>],
}

/// One relationship batch: `MATCH (a:from {..}), (b:to {..})
/// MERGE (a)-[r:rel_type {merge keys}]->(b) [SET r += row.props]`.
pub struct RelBatch<'a> {
    pub from_label: &'a str,
    pub to_label: &'a str,
    pub rel_type: &'a str,
    /// Each row's endpoint keys; None matches no node.
    pub from_keys: &'a [Option<String>],
    pub to_keys: &'a [Option<String>],
    /// `json_path(k)` of each merge-key property, SQL string literals.
    pub merge_paths: &'a [String],
    /// Each row's merge-key values, in `merge_paths` order.
    pub merge_values: &'a [Vec<KeyValue>],
    /// Each row's property text should it create the relationship.
    pub created: &'a [String],
    /// Each row's `row.props` text when the query SETs them.
    pub patches: Option<&'a [Option<String>]>,
    /// Each row's `row_index`, which the RETURN counts.
    pub row_indexes: &'a [i64],
}

fn find_node(conn: &Connection, label: &str, key: &str) -> rusqlite::Result<Option<i64>> {
    conn.prepare_cached(SQL_FIND_NODE)?
        .query_row(params![label, key], |r| r.get(0))
        .optional()
}

/// Write one node batch; the transaction commits only if every row does.
/// `bump` is the engine's statement that bumps the graph version.
///
/// After each SET the engine rewrites the key column from the properties.
/// Here that rewrite is skipped where it would store what is already there
/// (`IS NOT` compares through the column's TEXT affinity, as storing would):
/// the same column either way, without rewriting the row and its key
/// indexes for every node.
pub fn merge_nodes(conn: &mut Connection, bump: &str, batch: &NodeBatch) -> rusqlite::Result<()> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(bump)?;
    let refresh = format!(
        "UPDATE cgr_nodes SET key = json_extract(props, {path}) WHERE id = ?1 AND key IS NOT json_extract(props, {path})",
        path = batch.key_path
    );
    {
        let mut insert = tx.prepare_cached(SQL_INSERT_NODE)?;
        let mut ids = Vec::with_capacity(batch.keys.len());
        for (key, created) in batch.keys.iter().zip(batch.created) {
            let id = match find_node(&tx, batch.label, key)? {
                Some(id) => id,
                None => {
                    insert.execute(params![batch.label, key, created])?;
                    tx.last_insert_rowid()
                }
            };
            ids.push(id);
        }
        let mut patch = tx.prepare_cached(SQL_PATCH_NODE)?;
        let mut refresh = tx.prepare(&refresh)?;
        for (id, text) in ids.iter().zip(batch.patches) {
            if let Some(text) = text {
                patch.execute(params![text, id])?;
                refresh.execute(params![id])?;
            }
        }
    }
    tx.commit()
}

/// How many relationships of the batch's type leave (`from`) or enter a
/// node, read once per node and kept current as the batch inserts.
struct Degrees {
    from: HashMap<i64, i64>,
    to: HashMap<i64, i64>,
}

impl Degrees {
    fn of(
        map: &mut HashMap<i64, i64>,
        conn: &Connection,
        sql: &str,
        node: i64,
        rel_type: &str,
    ) -> rusqlite::Result<i64> {
        if let Some(&n) = map.get(&node) {
            return Ok(n);
        }
        let n: i64 = conn
            .prepare_cached(sql)?
            .query_row(params![node, rel_type], |r| r.get(0))?;
        map.insert(node, n);
        Ok(n)
    }
}

/// Write one relationship batch and return what its RETURN counts: the
/// distinct `row_index` of the rows whose two endpoints exist.
///
/// The engine's statements, in its order: every row's endpoints, then each
/// matched row's MERGE (find, else insert), then each one's SET. Two things
/// differ only in cost: a node is looked up once per batch (a relationship
/// batch writes no nodes), and the MERGE lookup seeks from whichever end has
/// fewer relationships of the type. SQLite's statistics pick the target's
/// index, which for a much-called function scanned all its callers' edges
/// for every new call to it.
pub fn merge_relationships(
    conn: &mut Connection,
    bump: &str,
    batch: &RelBatch,
) -> rusqlite::Result<i64> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(bump)?;
    let conds: String = (0..batch.merge_paths.len())
        .map(|i| {
            format!(
                " AND json_extract(props, {}) IS ?{}",
                batch.merge_paths[i],
                i + 4
            )
        })
        .collect();
    let find_from = format!("{SQL_FIND_EDGE_FROM}{conds}{SQL_FIND_EDGE_ORDER}");
    let find_to = format!("{SQL_FIND_EDGE_TO}{conds}{SQL_FIND_EDGE_ORDER}");
    let mut nodes: HashMap<(bool, String), Option<i64>> = HashMap::new();
    let mut node = |to: bool, key: &str| -> rusqlite::Result<Option<i64>> {
        let label = if to { batch.to_label } else { batch.from_label };
        if let Some(&found) = nodes.get(&(to, key.to_string())) {
            return Ok(found);
        }
        let found = find_node(&tx, label, key)?;
        nodes.insert((to, key.to_string()), found);
        Ok(found)
    };
    let mut matched: Vec<(usize, i64, i64)> = Vec::new();
    for (i, (from, to)) in batch.from_keys.iter().zip(batch.to_keys).enumerate() {
        let (Some(from), Some(to)) = (from, to) else {
            continue;
        };
        if let (Some(a), Some(b)) = (node(false, from)?, node(true, to)?) {
            matched.push((i, a, b));
        }
    }
    {
        let mut degrees = Degrees {
            from: HashMap::new(),
            to: HashMap::new(),
        };
        let mut insert = tx.prepare_cached(SQL_INSERT_EDGE)?;
        let mut bound = Vec::with_capacity(matched.len());
        for &(i, a, b) in &matched {
            let out = Degrees::of(&mut degrees.from, &tx, SQL_COUNT_FROM, a, batch.rel_type)?;
            let inbound = Degrees::of(&mut degrees.to, &tx, SQL_COUNT_TO, b, batch.rel_type)?;
            let head = [
                SqlValue::Integer(a),
                SqlValue::Integer(b),
                SqlValue::Text(batch.rel_type.to_string()),
            ];
            let args: Vec<&dyn ToSql> = head
                .iter()
                .map(|v| v as &dyn ToSql)
                .chain(batch.merge_values[i].iter().map(|v| v as &dyn ToSql))
                .collect();
            let mut find = tx.prepare_cached(if out <= inbound { &find_from } else { &find_to })?;
            let found: Option<i64> = find
                .query_row(params_from_iter(args), |r| r.get(0))
                .optional()?;
            let id = match found {
                Some(id) => id,
                None => {
                    insert.execute(params![a, b, batch.rel_type, batch.created[i]])?;
                    *degrees.from.entry(a).or_default() += 1;
                    *degrees.to.entry(b).or_default() += 1;
                    tx.last_insert_rowid()
                }
            };
            bound.push(id);
        }
        if let Some(patches) = batch.patches {
            let mut patch = tx.prepare_cached(SQL_PATCH_EDGE)?;
            for (&(i, _, _), id) in matched.iter().zip(&bound) {
                if let Some(text) = &patches[i] {
                    patch.execute(params![text, id])?;
                }
            }
        }
    }
    tx.commit()?;
    let distinct: HashSet<i64> = matched
        .iter()
        .map(|&(i, _, _)| batch.row_indexes[i])
        .collect();
    Ok(distinct.len() as i64)
}

/// The connection the writer uses: the engine's file, never created here,
/// configured as the engine configures its own (`busy_timeout`,
/// `cache_size` and the pragmas store.py runs).
///
/// It never checkpoints on close: SQLite takes this connection's close for
/// the file's last when the engine's own connections live in another copy of
/// SQLite in the same process, and would delete the WAL beneath them.
pub fn open_writer(
    path: &str,
    busy_timeout_ms: i64,
    cache_kib: i64,
    pragmas: &[String],
) -> rusqlite::Result<Connection> {
    use rusqlite::config::DbConfig;
    use rusqlite::OpenFlags;
    let conn = Connection::open_with_flags(
        path,
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_URI,
    )?;
    conn.set_db_config(DbConfig::SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, true)?;
    conn.pragma_update(None, "busy_timeout", busy_timeout_ms)?;
    conn.pragma_update(None, "cache_size", -cache_kib)?;
    for pragma in pragmas {
        conn.execute_batch(pragma)?;
    }
    Ok(conn)
}

#[cfg(feature = "python")]
pub mod python {
    use std::sync::Mutex;

    use pyo3::exceptions::PyRuntimeError;
    use pyo3::prelude::*;
    use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
    use rusqlite::Connection;

    use super::{merge_nodes, merge_relationships, open_writer, KeyValue, NodeBatch, RelBatch};
    use crate::json_text::push_string;

    /// Deeper than this a value is left to the engine (json.dumps would
    /// recurse into it; the engine writes it, or fails on it, itself).
    const MAX_DEPTH: usize = 128;

    /// A value the engine would write differently from plain `json.dumps`:
    /// only through its `default` hook (sets, arbitrary objects), with a
    /// rewrite (NaN and infinities), or not at all. The writer then declines
    /// the whole batch and the engine writes it.
    struct Declined;

    type Prepared<T> = Result<T, Declined>;

    fn write_value(
        out: &mut String,
        value: &Bound<'_, PyAny>,
        compact: bool,
        depth: usize,
    ) -> Prepared<()> {
        if depth > MAX_DEPTH {
            return Err(Declined);
        }
        // `_json.c`'s order: None, booleans, str, int, float, list or tuple, dict.
        if value.is_none() {
            out.push_str("null");
        } else if let Ok(flag) = value.cast::<PyBool>() {
            out.push_str(if flag.is_true() { "true" } else { "false" });
        } else if let Ok(text) = value.cast::<PyString>() {
            push_string(out, text.to_str().map_err(|_| Declined)?);
        } else if value.is_instance_of::<PyInt>() {
            // `int.__repr__`; beyond 64 bits the engine's text may differ.
            let n: i64 = value.extract().map_err(|_| Declined)?;
            out.push_str(&n.to_string());
        } else if value.is_instance_of::<PyFloat>() {
            let x: f64 = value.extract().map_err(|_| Declined)?;
            if !x.is_finite() {
                return Err(Declined);
            }
            // `float.__repr__`, from Python itself: the shortest text that
            // reads back as `x`, in Python's own layout (1e-05, 1e+16).
            let repr = PyFloat::new(value.py(), x).repr().map_err(|_| Declined)?;
            out.push_str(repr.to_str().map_err(|_| Declined)?);
        } else if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
            out.push('[');
            for (i, item) in value.try_iter().map_err(|_| Declined)?.enumerate() {
                if i > 0 {
                    out.push_str(if compact { "," } else { ", " });
                }
                write_value(out, &item.map_err(|_| Declined)?, compact, depth + 1)?;
            }
            out.push(']');
        } else if let Ok(map) = value.cast::<PyDict>() {
            write_object(out, map, compact, depth)?;
        } else {
            return Err(Declined);
        }
        Ok(())
    }

    fn write_object(
        out: &mut String,
        map: &Bound<'_, PyDict>,
        compact: bool,
        depth: usize,
    ) -> Prepared<()> {
        out.push('{');
        for (i, (key, item)) in map.iter().enumerate() {
            if i > 0 {
                out.push_str(if compact { "," } else { ", " });
            }
            let key = key.cast::<PyString>().map_err(|_| Declined)?;
            push_string(out, key.to_str().map_err(|_| Declined)?);
            out.push_str(if compact { ":" } else { ": " });
            write_value(out, &item, compact, depth + 1)?;
        }
        out.push('}');
        Ok(())
    }

    /// `json.dumps(value)`, or with `separators=(",", ":")` when `compact`.
    fn dumps(value: &Bound<'_, PyAny>, compact: bool) -> Prepared<String> {
        let mut out = String::new();
        write_value(&mut out, value, compact, 0)?;
        Ok(out)
    }

    /// The renderer the writer uses, for its parity test with `json.dumps`:
    /// None where the writer would decline the value.
    #[pyfunction]
    fn render_json(value: &Bound<'_, PyAny>, compact: bool) -> Option<String> {
        dumps(value, compact).ok()
    }

    /// A key as the engine looks it up, for the values it reads back
    /// unchanged: `str(value)` of a string or a 64-bit integer. A boolean
    /// (`True` against the `1` it later rewrites the key to) or a float is
    /// left to the engine.
    fn key_text(value: &Bound<'_, PyAny>) -> Prepared<String> {
        if let Ok(text) = value.cast::<PyString>() {
            return text.to_str().map(str::to_string).map_err(|_| Declined);
        }
        if value.get_type().is(value.py().get_type::<PyInt>()) {
            return value
                .extract::<i64>()
                .map(|n| n.to_string())
                .map_err(|_| Declined);
        }
        Err(Declined)
    }

    /// `SET x += row.props`: the patch text SQLite renders `row.props` as
    /// (its tokens as `json.dumps` wrote them, without the spaces), None to
    /// skip the SET for a row without properties.
    fn patch(props: Option<&Bound<'_, PyAny>>) -> Prepared<Option<String>> {
        match props {
            None => Ok(None),
            Some(p) if p.is_none() => Ok(None),
            Some(p) => {
                let map = p.cast::<PyDict>().map_err(|_| Declined)?;
                let mut out = String::new();
                write_object(&mut out, map, true, 0)?;
                Ok(Some(out))
            }
        }
    }

    /// A merge-key value as the engine binds it (booleans as integers) and
    /// the token it stores, or None for a null it binds and does not store.
    fn merge_value(value: Option<Bound<'_, PyAny>>) -> Prepared<(KeyValue, Option<String>)> {
        let Some(value) = value.filter(|v| !v.is_none()) else {
            return Ok((KeyValue::Null, None));
        };
        let bound = if let Ok(flag) = value.cast::<PyBool>() {
            KeyValue::Int(i64::from(flag.is_true()))
        } else if let Ok(text) = value.cast::<PyString>() {
            KeyValue::Text(text.to_str().map_err(|_| Declined)?.to_string())
        } else if value.is_instance_of::<PyInt>() {
            KeyValue::Int(value.extract().map_err(|_| Declined)?)
        } else if value.is_instance_of::<PyFloat>() {
            KeyValue::Float(value.extract().map_err(|_| Declined)?)
        } else {
            return Err(Declined);
        };
        Ok((bound, Some(dumps(&value, false)?)))
    }

    fn field<'py>(row: &Bound<'py, PyDict>, name: &str) -> Option<Bound<'py, PyAny>> {
        row.get_item(name).ok().flatten()
    }

    fn row_dict<'py>(row: Bound<'py, PyAny>) -> Prepared<Bound<'py, PyDict>> {
        row.cast_into::<PyDict>().map_err(|_| Declined)
    }

    struct NodeRows {
        keys: Vec<String>,
        created: Vec<String>,
        patches: Vec<Option<String>>,
    }

    fn node_rows(
        key_prop: &str,
        id_field: &str,
        props_field: &str,
        rows: &Bound<'_, PyList>,
    ) -> Prepared<NodeRows> {
        let mut prepared = NodeRows {
            keys: Vec::new(),
            created: Vec::new(),
            patches: Vec::new(),
        };
        let mut head = String::new();
        push_string(&mut head, key_prop);
        head.push_str(": ");
        for row in rows.iter() {
            let row = row_dict(row)?;
            let ident = field(&row, id_field).ok_or(Declined)?;
            prepared.keys.push(key_text(&ident)?);
            // `json.dumps({key_prop: row.id})`, the node a MERGE creates.
            prepared
                .created
                .push(format!("{{{head}{}}}", dumps(&ident, false)?));
            prepared
                .patches
                .push(patch(field(&row, props_field).as_ref())?);
        }
        Ok(prepared)
    }

    struct RelRows {
        from_keys: Vec<Option<String>>,
        to_keys: Vec<Option<String>>,
        values: Vec<Vec<KeyValue>>,
        created: Vec<String>,
        patches: Vec<Option<String>>,
        indexes: Vec<i64>,
    }

    /// The names the ingestor gives a relationship row's fields.
    #[derive(FromPyObject)]
    struct RelFields {
        from_val: String,
        to_val: String,
        props: String,
        row_index: String,
    }

    fn endpoint(value: Option<Bound<'_, PyAny>>) -> Prepared<Option<String>> {
        match value {
            Some(v) if !v.is_none() => key_text(&v).map(Some),
            // A null end matches no node, so the row writes nothing.
            _ => Ok(None),
        }
    }

    fn rel_rows(
        merge_keys: &[String],
        fields: &RelFields,
        rows: &Bound<'_, PyList>,
    ) -> Prepared<RelRows> {
        let mut prepared = RelRows {
            from_keys: Vec::new(),
            to_keys: Vec::new(),
            values: Vec::new(),
            created: Vec::new(),
            patches: Vec::new(),
            indexes: Vec::new(),
        };
        let quoted: Vec<String> = merge_keys
            .iter()
            .map(|k| {
                let mut q = String::new();
                push_string(&mut q, k);
                q
            })
            .collect();
        for row in rows.iter() {
            let row = row_dict(row)?;
            prepared
                .from_keys
                .push(endpoint(field(&row, &fields.from_val))?);
            prepared
                .to_keys
                .push(endpoint(field(&row, &fields.to_val))?);
            let props = field(&row, &fields.props);
            let map = match &props {
                Some(p) if !p.is_none() => Some(p.cast::<PyDict>().map_err(|_| Declined)?.clone()),
                _ => None,
            };
            let mut bound = Vec::with_capacity(merge_keys.len());
            let mut stored: Vec<String> = Vec::new();
            for (key, quoted_key) in merge_keys.iter().zip(&quoted) {
                let (value, token) =
                    merge_value(map.as_ref().and_then(|m| m.get_item(key).ok().flatten()))?;
                bound.push(value);
                if let Some(token) = token {
                    stored.push(format!("{quoted_key}: {token}"));
                }
            }
            prepared.values.push(bound);
            // `json.dumps` of the merge keys that are not null, the
            // relationship a MERGE creates.
            prepared.created.push(format!("{{{}}}", stored.join(", ")));
            prepared.patches.push(patch(props.as_ref())?);
            let index = field(&row, &fields.row_index).ok_or(Declined)?;
            if !index.get_type().is(row.py().get_type::<PyInt>()) {
                return Err(Declined);
            }
            prepared
                .indexes
                .push(index.extract().map_err(|_| Declined)?);
        }
        Ok(prepared)
    }

    /// The bulk writer for one engine file, connected on first use.
    #[pyclass(frozen)]
    pub struct Writer {
        path: String,
        busy_timeout_ms: i64,
        cache_kib: i64,
        pragmas: Vec<String>,
        /// The engine's statement that bumps the graph version.
        bump_sql: String,
        conn: Mutex<Option<Connection>>,
    }

    impl Writer {
        fn with_conn<T: Send>(
            &self,
            py: Python<'_>,
            work: impl FnOnce(&mut Connection) -> rusqlite::Result<T> + Send,
        ) -> PyResult<T> {
            py.detach(|| -> rusqlite::Result<T> {
                let mut guard = self.conn.lock().unwrap_or_else(|e| e.into_inner());
                if guard.is_none() {
                    *guard = Some(open_writer(
                        &self.path,
                        self.busy_timeout_ms,
                        self.cache_kib,
                        &self.pragmas,
                    )?);
                }
                work(guard.as_mut().expect("opened above"))
            })
            .map_err(|e| PyRuntimeError::new_err(e.to_string()))
        }
    }

    #[pymethods]
    impl Writer {
        #[new]
        fn new(
            path: String,
            busy_timeout_ms: i64,
            cache_kib: i64,
            pragmas: Vec<String>,
            bump_sql: String,
        ) -> Self {
            Writer {
                path,
                busy_timeout_ms,
                cache_kib,
                pragmas,
                bump_sql,
                conn: Mutex::new(None),
            }
        }

        /// `MERGE (n:label {key_prop: row.id}) SET n += row.props` for every
        /// row (`key_path` is `json_path(key_prop)`, an SQL literal).
        ///
        /// False, writing nothing, when a row holds a value the engine would
        /// write differently; raises, after rolling back, if SQLite fails.
        #[allow(clippy::too_many_arguments)]
        fn merge_node_rows(
            &self,
            py: Python<'_>,
            label: &str,
            key_prop: &str,
            key_path: &str,
            id_field: &str,
            props_field: &str,
            rows: &Bound<'_, PyList>,
        ) -> PyResult<bool> {
            let Ok(prepared) = node_rows(key_prop, id_field, props_field, rows) else {
                return Ok(false);
            };
            let batch = NodeBatch {
                label,
                key_path,
                keys: &prepared.keys,
                created: &prepared.created,
                patches: &prepared.patches,
            };
            self.with_conn(py, |conn| merge_nodes(conn, &self.bump_sql, &batch))
                .map(|()| true)
        }

        /// `MATCH (a:from_label {..: row.from_val}), (b:to_label {..: row.to_val})
        /// MERGE (a)-[r:rel_type {k: row.props.k, ...}]->(b) [SET r += row.props]`
        /// for every row, returning what the RETURN counts, or None, writing
        /// nothing, when a row holds a value the engine would write differently.
        #[allow(clippy::too_many_arguments)]
        fn merge_relationship_rows(
            &self,
            py: Python<'_>,
            from_label: &str,
            to_label: &str,
            rel_type: &str,
            merge_keys: Vec<String>,
            merge_paths: Vec<String>,
            has_props: bool,
            fields: RelFields,
            rows: &Bound<'_, PyList>,
        ) -> PyResult<Option<i64>> {
            let Ok(prepared) = rel_rows(&merge_keys, &fields, rows) else {
                return Ok(None);
            };
            let batch = RelBatch {
                from_label,
                to_label,
                rel_type,
                from_keys: &prepared.from_keys,
                to_keys: &prepared.to_keys,
                merge_paths: &merge_paths,
                merge_values: &prepared.values,
                created: &prepared.created,
                patches: has_props.then_some(prepared.patches.as_slice()),
                row_indexes: &prepared.indexes,
            };
            self.with_conn(py, |conn| merge_relationships(conn, &self.bump_sql, &batch))
                .map(Some)
        }

        /// Close the connection; see `Reader.close` for when.
        fn close(&self) {
            let mut guard = self.conn.lock().unwrap_or_else(|e| e.into_inner());
            guard.take();
        }
    }

    pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<Writer>()?;
        m.add_function(wrap_pyfunction!(render_json, m)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const SCHEMA: &str = "CREATE TABLE cgr_nodes(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, key TEXT, \
         props TEXT NOT NULL DEFAULT '{}');
         CREATE UNIQUE INDEX cgr_nodes_label_key ON cgr_nodes(label, key) WHERE key IS NOT NULL;
         CREATE TABLE cgr_edges(id INTEGER PRIMARY KEY, src INTEGER NOT NULL, dst INTEGER NOT NULL, \
         type TEXT NOT NULL, props TEXT NOT NULL DEFAULT '{}');
         CREATE INDEX cgr_edges_src ON cgr_edges(src, type);
         CREATE INDEX cgr_edges_dst ON cgr_edges(dst, type);
         CREATE TABLE cgr_meta(name TEXT PRIMARY KEY, value TEXT NOT NULL);";
    const BUMP: &str = "INSERT INTO cgr_meta(name, value) VALUES ('graph_version', '1') \
         ON CONFLICT(name) DO UPDATE SET value = CAST(value AS INTEGER) + 1";

    fn graph() -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch(SCHEMA).expect("schema");
        conn
    }

    fn version(conn: &Connection) -> Option<String> {
        conn.query_row(
            "SELECT value FROM cgr_meta WHERE name = 'graph_version'",
            [],
            |r| r.get(0),
        )
        .optional()
        .expect("version")
    }

    type NodeRow = (i64, String, Option<String>, String);
    type EdgeRow = (i64, i64, i64, String, String);

    fn nodes(conn: &Connection) -> Vec<NodeRow> {
        let mut stmt = conn
            .prepare("SELECT id, label, key, props FROM cgr_nodes ORDER BY id")
            .expect("prepare");
        stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))
            .expect("query")
            .collect::<rusqlite::Result<_>>()
            .expect("rows")
    }

    fn edges(conn: &Connection) -> Vec<EdgeRow> {
        let mut stmt = conn
            .prepare("SELECT id, src, dst, type, props FROM cgr_edges ORDER BY id")
            .expect("prepare");
        stmt.query_map([], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?))
        })
        .expect("query")
        .collect::<rusqlite::Result<_>>()
        .expect("rows")
    }

    fn strings(values: &[&str]) -> Vec<String> {
        values.iter().map(|v| v.to_string()).collect()
    }

    fn some(values: &[&str]) -> Vec<Option<String>> {
        values.iter().map(|v| Some(v.to_string())).collect()
    }

    fn merge_functions(conn: &mut Connection) {
        let keys = strings(&["f", "g"]);
        let created = strings(&[r#"{"qn": "f"}"#, r#"{"qn": "g"}"#]);
        let batch = NodeBatch {
            label: "Function",
            key_path: "'$.qn'",
            keys: &keys,
            created: &created,
            patches: &[None, None],
        };
        merge_nodes(conn, BUMP, &batch).expect("merge");
    }

    #[test]
    fn nodes_are_found_or_created_in_row_order_then_patched() {
        let mut conn = graph();
        merge_functions(&mut conn);
        let keys = strings(&["g", "h", "h"]);
        let created = strings(&[r#"{"qn": "g"}"#, r#"{"qn": "h"}"#, r#"{"qn": "h"}"#]);
        let patches = [
            Some(r#"{"qn":"g","n":1}"#.to_string()),
            None,
            Some(r#"{"qn":"h2"}"#.to_string()),
        ];
        let batch = NodeBatch {
            label: "Function",
            key_path: "'$.qn'",
            keys: &keys,
            created: &created,
            patches: &patches,
        };
        merge_nodes(&mut conn, BUMP, &batch).expect("merge");
        // The third row finds the node the second created; its SET then
        // moves the key, as the engine's key refresh does.
        let expected: Vec<NodeRow> = vec![
            (
                1,
                "Function".into(),
                Some("f".into()),
                r#"{"qn": "f"}"#.into(),
            ),
            (
                2,
                "Function".into(),
                Some("g".into()),
                r#"{"qn":"g","n":1}"#.into(),
            ),
            (
                3,
                "Function".into(),
                Some("h2".into()),
                r#"{"qn":"h2"}"#.into(),
            ),
        ];
        assert_eq!(nodes(&conn), expected);
        assert_eq!(version(&conn).as_deref(), Some("2"));
    }

    #[test]
    fn relationships_merge_on_their_keys_and_count_matched_rows() {
        let mut conn = graph();
        merge_functions(&mut conn);
        let mut from_keys = some(&["f", "f", "f"]);
        from_keys.push(None);
        let to_keys = some(&["g", "g", "x", "g"]);
        let merge_values = vec![vec![KeyValue::Int(1)]; 4];
        let created = strings(&[r#"{"line": 1}"#; 4]);
        let patches = [
            Some(r#"{"w":1}"#.to_string()),
            Some(r#"{"w":2}"#.to_string()),
            None,
            None,
        ];
        let merge_paths = strings(&["'$.line'"]);
        let batch = RelBatch {
            from_label: "Function",
            to_label: "Function",
            rel_type: "CALLS",
            from_keys: &from_keys,
            to_keys: &to_keys,
            merge_paths: &merge_paths,
            merge_values: &merge_values,
            created: &created,
            patches: Some(&patches),
            row_indexes: &[0, 0, 1, 2],
        };
        let counted = merge_relationships(&mut conn, BUMP, &batch).expect("merge");
        // The second row merges into the first one's relationship and shares
        // its row_index; the other two match no node.
        assert_eq!(counted, 1);
        let expected: Vec<EdgeRow> = vec![(1, 1, 2, "CALLS".into(), r#"{"line":1,"w":2}"#.into())];
        assert_eq!(edges(&conn), expected);
        assert_eq!(version(&conn).as_deref(), Some("2"));
    }

    #[test]
    fn a_failed_batch_writes_nothing() {
        let mut conn = graph();
        merge_functions(&mut conn);
        let keys = strings(&["k"]);
        let created = strings(&[r#"{"qn": "k"}"#]);
        let batch = NodeBatch {
            label: "Function",
            key_path: "'$.qn'",
            keys: &keys,
            created: &created,
            patches: &[Some("not json".to_string())],
        };
        assert!(merge_nodes(&mut conn, BUMP, &batch).is_err());
        assert_eq!(nodes(&conn).len(), 2);
        assert_eq!(version(&conn).as_deref(), Some("1"));
    }

    #[test]
    fn the_writer_opens_only_an_existing_file() {
        let dir = std::env::temp_dir().join(format!("cgr_ingest_{}", std::process::id()));
        std::fs::create_dir_all(&dir).expect("dir");
        let path = dir.join("graph.sqlite");
        let path = path.to_str().expect("utf-8 path");
        assert!(open_writer(path, 1000, 1024, &[]).is_err());
        Connection::open(path)
            .expect("create")
            .execute_batch(SCHEMA)
            .expect("schema");
        let pragmas = strings(&["PRAGMA foreign_keys = ON"]);
        let conn = open_writer(path, 1234, 1024, &pragmas).expect("open");
        let busy: i64 = conn
            .query_row("PRAGMA busy_timeout", [], |r| r.get(0))
            .expect("busy");
        let keys: i64 = conn
            .query_row("PRAGMA foreign_keys", [], |r| r.get(0))
            .expect("pragma");
        assert_eq!((busy, keys), (1234, 1));
        drop(conn);
        std::fs::remove_dir_all(&dir).expect("cleanup");
    }
}
