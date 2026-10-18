//! Reading the graph as the caller's statement sees it.
//!
//! The extension reads on a connection of its own, whose snapshot need not
//! be the snapshot of the engine statement that called it: another process
//! may commit in between, and a walk or a procedure answered from the newer
//! graph would combine with the statement's older rows into a result that
//! never existed. Every transaction that changes the graph therefore bumps a
//! counter (`cgr_meta.graph_version`, in the engine's executor and in
//! `ingest`), the caller reads it inside its own transaction, and the
//! extension reads only within a transaction that holds the same version;
//! any other is declined, and the engine answers from its own snapshot.

use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension};

/// The counter as text, however SQLite stored it (`None` when absent).
pub fn read_version(conn: &Connection, version_sql: &str) -> rusqlite::Result<Option<String>> {
    let value: Option<Value> = conn.query_row(version_sql, [], |r| r.get(0)).optional()?;
    Ok(match value {
        Some(Value::Text(text)) => Some(text),
        Some(Value::Integer(n)) => Some(n.to_string()),
        Some(Value::Real(x)) => Some(x.to_string()),
        Some(Value::Blob(bytes)) => Some(String::from_utf8_lossy(&bytes).into_owned()),
        Some(Value::Null) | None => None,
    })
}

/// `work` inside one read transaction of `conn`, if that transaction reads
/// the graph at `expected`; `Ok(None)` when it sees another version.
pub fn at_version<T, E: From<rusqlite::Error>>(
    conn: &Connection,
    version_sql: &str,
    expected: Option<&str>,
    work: impl FnOnce(&Connection) -> Result<T, E>,
) -> Result<Option<T>, E> {
    // The first read of a deferred transaction fixes its snapshot; every
    // later read in it, `work`'s included, sees that same graph.
    let tx = conn.unchecked_transaction()?;
    if read_version(&tx, version_sql)?.as_deref() != expected {
        return Ok(None);
    }
    let out = work(&tx)?;
    tx.commit()?;
    Ok(Some(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    const VERSION_SQL: &str =
        "SELECT CAST(value AS TEXT) FROM cgr_meta WHERE name = 'graph_version'";

    fn meta(version: Option<&str>) -> Connection {
        let conn = Connection::open_in_memory().expect("memory db");
        conn.execute_batch("CREATE TABLE cgr_meta(name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            .expect("schema");
        if let Some(v) = version {
            conn.execute("INSERT INTO cgr_meta VALUES ('graph_version', ?1)", [v])
                .expect("seed");
        }
        conn
    }

    #[test]
    fn work_runs_only_at_the_expected_version() {
        let conn = meta(Some("3"));
        assert_eq!(
            at_version(&conn, VERSION_SQL, Some("3"), |_| Ok::<_, rusqlite::Error>(
                7
            ))
            .unwrap(),
            Some(7)
        );
        assert_eq!(
            at_version(&conn, VERSION_SQL, Some("2"), |_| Ok::<_, rusqlite::Error>(
                7
            ))
            .unwrap(),
            None
        );
        assert_eq!(
            at_version(&conn, VERSION_SQL, None, |_| Ok::<_, rusqlite::Error>(7)).unwrap(),
            None
        );
    }

    #[test]
    fn a_missing_counter_matches_only_a_missing_one() {
        let conn = meta(None);
        assert_eq!(
            at_version(&conn, VERSION_SQL, None, |_| Ok::<_, rusqlite::Error>(1)).unwrap(),
            Some(1)
        );
        assert_eq!(
            at_version(&conn, VERSION_SQL, Some("0"), |_| Ok::<_, rusqlite::Error>(
                1
            ))
            .unwrap(),
            None
        );
    }

    #[test]
    fn an_integer_counter_reads_as_its_digits() {
        let conn = meta(None);
        conn.execute("INSERT INTO cgr_meta VALUES ('graph_version', 12)", [])
            .expect("seed");
        assert_eq!(
            read_version(&conn, VERSION_SQL).unwrap().as_deref(),
            Some("12")
        );
    }
}
