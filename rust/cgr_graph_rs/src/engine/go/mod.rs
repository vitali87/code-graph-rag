//! The Rust engine's Go (docs/reports/PHASE3_GO.md, section 3).
//!
//! Phase 3 builds it in steps that nothing a user runs reaches until Go
//! joins the Rust engine's languages (step 10). This first one is its
//! skeleton: Go files parsed on the engine's pool, one parser per worker,
//! with the grammar compiled into the crate (`syntax` holds the node kinds
//! and field names the later steps read, each checked against it); the build
//! constraints (`builds`, a port of `parsers/go/builds.py`); and the package
//! clause (`package`), the two things `editing/rename.py` asks of a Go file
//! without parsing it. Each answers exactly as the Python it ports, on any
//! number of threads.

pub mod builds;
pub mod package;
pub mod syntax;
#[cfg(test)]
mod tests;

use std::collections::{BTreeSet, HashMap};
use std::fs::{File, OpenOptions};
use std::io::Read;
use std::path::{Path, PathBuf};

use tree_sitter::{Language, Parser, Tree};

use super::Engine;
use builds::Expr;

/// The Go grammar compiled into the crate, the release `uv.lock` holds for
/// the Python package (`grammars`).
pub fn language() -> Language {
    Language::new(tree_sitter_go::LANGUAGE)
}

/// `path` as pathlib spells it: its `.` parts, doubled separators and a
/// trailing separator dropped, so `x.go/` names the file `x.go`, as
/// `Path("x.go/")` does.
pub(crate) fn normalized(path: &Path) -> PathBuf {
    path.components().collect()
}

/// `root / rel_path` as pathlib joins them (`normalized`).
pub(crate) fn joined(root: &Path, rel_path: &str) -> PathBuf {
    root.join(normalized(Path::new(rel_path)))
}

/// A Go file read whole and parsed.
pub struct ParsedFile {
    pub source: Vec<u8>,
    pub tree: Tree,
}

/// A parser for the Go grammar; None if the runtime refuses it, which the
/// grammar's tests rule out for the one compiled in.
fn go_parser() -> Option<Parser> {
    let mut parser = Parser::new();
    parser.set_language(&language()).ok()?;
    Some(parser)
}

/// `path` opened for reading, never blocking: a pipe with no writer opens at
/// once on Unix, and so is told apart from a file instead of waiting.
fn open_without_blocking(path: &Path) -> std::io::Result<File> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.custom_flags(libc::O_NONBLOCK);
    }
    options.open(path)
}

/// The first `limit` bytes of the regular file at `path` (a link followed),
/// as Python's `open(path, "rb").read(limit)` gives them; None where that
/// raises OSError, and for a folder, which Python cannot open either. A
/// pipe, socket or device reads as None too, where Python's read would wait
/// for a writer or read the device: the walk never yields one as a file,
/// so no index depends on the difference.
pub(crate) fn read_head(path: &Path, limit: usize) -> Option<Vec<u8>> {
    let file = open_without_blocking(path).ok()?;
    if !file.metadata().ok()?.is_file() {
        return None;
    }
    // `read_to_end` reads again where a read was interrupted, as Python's
    // buffered read does.
    let mut head = Vec::new();
    file.take(limit as u64).read_to_end(&mut head).ok()?;
    Some(head)
}

/// The whole regular file at `path`, as `read_head` reads a head.
fn read_whole(path: &Path) -> Option<Vec<u8>> {
    read_head(path, usize::MAX)
}

impl Engine {
    /// `work` applied to each Go file of `paths`, read once and parsed on the
    /// pool, each worker with a parser of its own; `None` for a file that
    /// cannot be read or whose parse did not finish. The results keep the
    /// order of `paths`.
    pub fn go_parse_map<P, R, F>(&self, paths: &[P], work: F) -> Vec<R>
    where
        P: AsRef<Path> + Sync,
        R: Send,
        F: Fn(Option<&ParsedFile>) -> R + Sync + Send,
    {
        self.map_init_ordered(paths, go_parser, |parser, path| {
            let parsed = parser.as_mut().and_then(|parser| {
                let source = read_whole(path.as_ref())?;
                let tree = parser.parse(&source, None)?;
                Some(ParsedFile { source, tree })
            });
            work(parsed.as_ref())
        })
    }

    /// The build constraint of each file of `rel_paths` under `root`
    /// (`builds::file_constraint`), in order, each read on the pool.
    pub fn go_build_constraints<S: AsRef<str> + Sync>(
        &self,
        root: &Path,
        rel_paths: &[S],
    ) -> Vec<Expr> {
        self.map_ordered(rel_paths, |rel_path| {
            builds::file_constraint(root, rel_path.as_ref())
        })
    }

    /// For each group of files under `root`, whether one build compiles them
    /// all, as `GoBuilds` decides it: `Some(true)` when one certainly does,
    /// `Some(false)` when none does (the files are apart), `None` when it
    /// cannot tell. Each distinct file is read once and each distinct set of
    /// constraints decided once, on the pool; the answers keep the groups'
    /// order.
    pub fn go_build_decisions<S: AsRef<str> + Sync>(
        &self,
        root: &Path,
        groups: &[Vec<S>],
    ) -> Vec<Option<bool>> {
        let mut index: HashMap<&str, usize> = HashMap::new();
        let mut files: Vec<&str> = Vec::new();
        for path in groups.iter().flatten() {
            index.entry(path.as_ref()).or_insert_with(|| {
                files.push(path.as_ref());
                files.len() - 1
            });
        }
        let constraints = self.go_build_constraints(root, &files);
        let keys: Vec<BTreeSet<Expr>> = groups
            .iter()
            .map(|group| {
                group
                    .iter()
                    .map(|path| constraints[index[path.as_ref()]].clone())
                    .collect()
            })
            .collect();
        let mut distinct: HashMap<&BTreeSet<Expr>, usize> = HashMap::new();
        let mut sets: Vec<&BTreeSet<Expr>> = Vec::new();
        let slots: Vec<usize> = keys
            .iter()
            .map(|key| {
                *distinct.entry(key).or_insert_with(|| {
                    sets.push(key);
                    sets.len() - 1
                })
            })
            .collect();
        let decided = self.map_ordered(&sets, |set| builds::decide(set.iter().cloned()));
        slots.into_iter().map(|slot| decided[slot]).collect()
    }

    /// The `package` clause of each Go file of `paths`
    /// (`package::read_package_clause`), in order, each read on the pool.
    pub fn go_package_clauses<P: AsRef<Path> + Sync>(&self, paths: &[P]) -> Vec<Option<String>> {
        self.map_ordered(paths, |path| package::read_package_clause(path.as_ref()))
    }
}
