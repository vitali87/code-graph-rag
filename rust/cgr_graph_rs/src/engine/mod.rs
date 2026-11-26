//! The Rust indexing engine's frame (docs/reports/PHASE2_FRAME.md).
//!
//! Phase 2 of the rewrite builds the frame in steps, behind the flag in
//! `codebase_rag/engine_frame.py`, which stays off until the last step. This
//! first part is the thread pool and the rule every later part keeps: work
//! runs on any thread, and results come back in the order of their inputs,
//! never in the order threads finish. So the output is the same for any
//! number of threads, which `tests` checks and
//! `codebase_rag/tests/test_engine_frame.py` checks again from Python.
//!
//! Its first job is the hash the sync's change detection records for each
//! file: the md5 of its raw bytes, as `_hash_file` in
//! `codebase_rag/graph_updater.py` computes it. `ignore` holds the rules the
//! walk leaves paths out by, a port of the Python walk's own, and reads the
//! ignore files; `discover` is the walk, which with the hashes gives the
//! run's file plan. `state` is change detection's file half: the state
//! folder, the hash cache and folder times, the in-sync check, and each
//! run's new, changed and deleted files.

pub mod discover;
pub mod ignore;
#[cfg(feature = "python")]
pub mod python;
pub mod state;
#[cfg(test)]
mod tests;

use std::fs::File;
use std::io::{ErrorKind, Read};
use std::path::Path;

use md5::{Digest, Md5};
use rayon::prelude::*;
use rayon::{ThreadPool, ThreadPoolBuildError, ThreadPoolBuilder};

/// Bytes read at a time while hashing, so a large file is never held whole.
const READ_CHUNK: usize = 1 << 16;
/// Worker threads are named after the engine, for profiles and debuggers.
const THREAD_NAME: &str = "cgr-engine";

/// The frame's thread pool, of its own: the engine never shares rayon's
/// global pool, so its size is exactly what the caller asked for.
pub struct Engine {
    pool: ThreadPool,
}

impl Engine {
    /// An engine on `threads` worker threads; 0 means one per available core.
    pub fn new(threads: usize) -> Result<Engine, ThreadPoolBuildError> {
        let threads = match threads {
            0 => std::thread::available_parallelism().map_or(1, |n| n.get()),
            n => n,
        };
        let pool = ThreadPoolBuilder::new()
            .num_threads(threads)
            .thread_name(|i| format!("{THREAD_NAME}-{i}"))
            .build()?;
        Ok(Engine { pool })
    }

    /// How many worker threads the engine runs.
    pub fn threads(&self) -> usize {
        self.pool.current_num_threads()
    }

    /// `work` applied to every item on the pool, the results in the items'
    /// order whatever order the threads finish in.
    pub fn map_ordered<T, R, F>(&self, items: &[T], work: F) -> Vec<R>
    where
        T: Sync,
        R: Send,
        F: Fn(&T) -> R + Sync + Send,
    {
        self.pool.install(|| items.par_iter().map(work).collect())
    }

    /// Each file's md5, as lowercase hex, over its raw bytes; `None` for a
    /// file that cannot be opened or read to its end.
    pub fn md5_files<P: AsRef<Path> + Sync>(&self, paths: &[P]) -> Vec<Option<String>> {
        self.map_ordered(paths, |path| md5_file(path.as_ref()))
    }
}

/// `path`'s md5 as lowercase hex, or `None` when it cannot be read whole.
pub fn md5_file(path: &Path) -> Option<String> {
    let mut file = File::open(path).ok()?;
    let mut hasher = Md5::new();
    let mut chunk = vec![0u8; READ_CHUNK];
    loop {
        match file.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => hasher.update(&chunk[..n]),
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(_) => return None,
        }
    }
    Some(hex(&hasher.finalize()))
}

fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push(char::from(DIGITS[usize::from(byte >> 4)]));
        out.push(char::from(DIGITS[usize::from(byte & 0x0f)]));
    }
    out
}
