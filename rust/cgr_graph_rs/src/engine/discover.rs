//! The repository walk: which files an index reads, in which order
//! (docs/reports/PHASE2_FRAME.md, section 5). A copy of
//! `walk_eligible_files` in `codebase_rag/utils/path_utils.py`, rule for
//! rule, giving what it gives: the files it yields, the folders it hands
//! `on_dir`, and the entries it reports to `on_symlink`, `on_special` and
//! `on_undecodable`.
//!
//! - **Order.** In each folder, its files sorted by name, then each kept
//!   subfolder in name order, recursively; the root's files first. Names
//!   compare by code point, which for UTF-8 is byte order, so `a/b.py`
//!   comes before `a-b/x.py`.
//! - **Folders** are pruned by `IgnoreRules::should_keep_dir`; one the rules
//!   keep is still left out, and reported, when it is a link or its name is
//!   not UTF-8. Links are never followed; the root is, when it is one.
//! - **Files**: cgr's own state files are dropped at any depth, then
//!   `should_skip_rel_file` decides; a file it keeps is left out, and
//!   reported, when it is a link, when its name is not UTF-8, or when it is
//!   no regular file (a pipe, socket or device). An entry counts as a
//!   folder when it is one after following links, as `os.walk` sorts them,
//!   so a link to a folder is judged by the folder rules.
//! - **A folder that cannot be listed** is skipped whole, with nothing of
//!   it reported, as `os.walk` skips it: a listing that fails part way
//!   gives nothing either.
//!
//! The rules see a name that is not UTF-8 as Python sees it
//! (`ignore::string_from_os_str`), so such a name the rules leave out is
//! dropped silently and only one they let through is reported, as in
//! Python. A reported path keeps the name's own bytes.
//!
//! Folders are listed on the engine's pool, a level of the tree at a time,
//! and the tree is then read back in walk order, so the plan is the same on
//! any number of threads. Which entries are reported, not the order they
//! were met in, is what the walk shares with Python: Python meets a
//! folder's subfolders in the order the system lists them.

use std::ffi::{OsStr, OsString};
use std::fs::{self, FileType};
use std::mem;
use std::path::{Path, PathBuf, MAIN_SEPARATOR_STR};

use rayon::prelude::*;

use super::ignore::{string_from_os_str, IgnoreRules, PatternError};
use super::Engine;

#[cfg(test)]
mod tests;

/// cgr's own state files, left out wherever they are
/// (`cs.CGR_STATE_FILENAMES`): an older cgr wrote some into the checkout.
pub const STATE_FILENAMES: &[&str] = &[
    ".cgr-delombok-state.json",
    ".cgr-dir-mtimes.json",
    ".cgr-edit-history-ledger.json",
    ".cgr-edit-history.json",
    ".cgr-edit-lock",
    ".cgr-embeddings-pending",
    ".cgr-exclusion-state.json",
    ".cgr-exposes-cleanup-pending",
    ".cgr-hash-cache.json",
    ".cgr-parser-fingerprint",
    ".cgr-prune-pending",
    ".cgr-sync-lock",
    ".cgr-tool-version-probes.json",
    ".cgr-tool-versions.json",
];
/// The root folder's key (`cs.ROOT_DIR_KEY`); every other folder's is its
/// path from the root.
pub const ROOT_DIR_KEY: &str = ".";
const SLASH: char = '/';

/// A file the walk keeps: `walk_eligible_files`' `(dirpath, filename,
/// rel_path)`, its folder named by its place in `FilePlan::dirs`. A kept
/// file's name is always UTF-8.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WalkedFile {
    pub dir: usize,
    pub name: String,
    pub rel_path: String,
}

/// A folder the walk listed: `on_dir`'s `(dir_key, dirpath)`. The path is
/// the root as given, then each name, joined as `os.path.join` joins them.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VisitedDir {
    pub key: String,
    pub dirpath: OsString,
}

/// What the walk finds: the files in walk order, the folders it listed, in
/// the order it listed them, the entries it reported, each by its path
/// from the root, and each file's md5 (`None` for a file it cannot read).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct FilePlan {
    pub files: Vec<WalkedFile>,
    pub dirs: Vec<VisitedDir>,
    pub links: Vec<OsString>,
    pub special: Vec<OsString>,
    pub undecodable: Vec<OsString>,
    pub hashes: Vec<Option<String>>,
}

impl FilePlan {
    /// Each file's path: its folder's path, then its name.
    pub fn paths(&self) -> Vec<PathBuf> {
        self.files
            .iter()
            .map(|file| Path::new(&self.dirs[file.dir].dirpath).join(&file.name))
            .collect()
    }
}

impl Engine {
    /// The plan for the repository at `root` under `rules`, its files
    /// hashed on the pool; an error where Python's walk raises on a pattern
    /// pathspec refuses.
    pub fn file_plan(&self, root: &OsStr, rules: &IgnoreRules) -> Result<FilePlan, PatternError> {
        let mut plan = self.walk(root, rules)?;
        plan.hashes = self.md5_files(&plan.paths());
        Ok(plan)
    }

    /// The plan without its hashes.
    pub fn walk(&self, root: &OsStr, rules: &IgnoreRules) -> Result<FilePlan, PatternError> {
        let mut nodes = vec![Node::root(root)];
        let mut level = vec![0];
        while !level.is_empty() {
            let visits: Vec<Visit> = self.pool.install(|| {
                level
                    .par_iter()
                    .map(|&id| visit(rules, &nodes[id]))
                    .collect()
            });
            let mut next = Vec::new();
            for (&id, mut visit) in level.iter().zip(visits) {
                if let Visit::Listed(listed) = &mut visit {
                    for child in listed.subdirs.drain(..) {
                        let child_id = nodes.len();
                        nodes[id].children.push(child_id);
                        next.push(child_id);
                        nodes.push(child);
                    }
                }
                nodes[id].visit = visit;
            }
            level = next;
        }
        flatten(nodes)
    }
}

/// One folder of the tree as the walk reaches it.
struct Node {
    key: String,
    /// Its path from the root, `""` for the root.
    rel: String,
    dirpath: OsString,
    /// `rel`'s names: the `dir_parts` its files are judged with.
    parts: Vec<String>,
    visit: Visit,
    children: Vec<usize>,
}

impl Node {
    fn root(root: &OsStr) -> Node {
        Node {
            key: ROOT_DIR_KEY.to_owned(),
            rel: String::new(),
            dirpath: root.to_owned(),
            parts: Vec::new(),
            visit: Visit::Pending,
            children: Vec::new(),
        }
    }

    /// The `dir_prefix` of its entries: `""` at the root, else its path and
    /// a `/`.
    fn prefix(&self) -> String {
        if self.rel.is_empty() {
            String::new()
        } else {
            format!("{}{SLASH}", self.rel)
        }
    }

    fn child(&self, prefix: &str, name: String) -> Node {
        let dirpath = child_dirpath(&self.dirpath, &name);
        let rel = format!("{prefix}{name}");
        let mut parts = self.parts.clone();
        parts.push(name);
        Node {
            key: rel.clone(),
            dirpath,
            rel,
            parts,
            visit: Visit::Pending,
            children: Vec::new(),
        }
    }
}

enum Visit {
    Pending,
    /// The folder could not be listed, so the walk skips it.
    Unlisted,
    Listed(Listed),
    /// A pattern the rules refuse, met while judging the folder's entries.
    Failed(PatternError),
}

struct Listed {
    /// Each kept file's name and path from the root, in order.
    files: Vec<(String, String)>,
    reports: Vec<(Report, OsString)>,
    subdirs: Vec<Node>,
}

#[derive(Clone, Copy)]
enum Report {
    Link,
    Special,
    Undecodable,
}

/// One entry of a listing.
struct Entry {
    name: OsString,
    /// Its own type, a link not followed; `None` when it cannot be read.
    kind: Option<FileType>,
    /// Whether it is a folder once links are followed: what puts it among
    /// `os.walk`'s `dirnames`.
    is_dir: bool,
}

impl Entry {
    /// `is_symlink_entry`.
    fn is_link(&self) -> bool {
        self.kind.is_some_and(|kind| kind.is_symlink())
    }

    /// `is_special_file_entry`: no regular file, folder or link. An entry
    /// whose type cannot be read is not special.
    fn is_special(&self) -> bool {
        self.kind
            .is_some_and(|kind| !kind.is_file() && !kind.is_dir() && !kind.is_symlink())
    }
}

fn visit(rules: &IgnoreRules, node: &Node) -> Visit {
    let Some(entries) = list(&node.dirpath) else {
        return Visit::Unlisted;
    };
    match judge(rules, node, entries) {
        Ok(listed) => Visit::Listed(listed),
        Err(error) => Visit::Failed(error),
    }
}

/// The folder's entries in the order the system lists them, or `None` when
/// it cannot be listed to the end.
fn list(dirpath: &OsStr) -> Option<Vec<Entry>> {
    let mut entries = Vec::new();
    for entry in fs::read_dir(dirpath).ok()? {
        let entry = entry.ok()?;
        let kind = entry.file_type().ok();
        // `DirEntry.is_dir()`, which follows a link and is false for any
        // entry it cannot stat.
        let is_dir = match kind {
            Some(kind) if kind.is_symlink() => {
                fs::metadata(entry.path()).is_ok_and(|metadata| metadata.is_dir())
            }
            Some(kind) => kind.is_dir(),
            None => false,
        };
        entries.push(Entry {
            name: entry.file_name(),
            kind,
            is_dir,
        });
    }
    Some(entries)
}

/// One folder's entries judged as `walk_eligible_files` judges them: its
/// subfolders, then its files.
fn judge(rules: &IgnoreRules, node: &Node, entries: Vec<Entry>) -> Result<Listed, PatternError> {
    let prefix = node.prefix();
    let (dirs, mut files): (Vec<Entry>, Vec<Entry>) =
        entries.into_iter().partition(|entry| entry.is_dir);
    let mut reports = Vec::new();
    let mut kept = Vec::new();
    for entry in dirs {
        if !rules.should_keep_dir(&string_from_os_str(&entry.name), &prefix)? {
            continue;
        }
        match (entry.is_link(), entry.name.to_str()) {
            (true, _) => reports.push((Report::Link, reported(&prefix, &entry.name))),
            (false, None) => reports.push((Report::Undecodable, reported(&prefix, &entry.name))),
            (false, Some(name)) => kept.push(name.to_owned()),
        }
    }
    kept.sort_unstable();
    // Byte order on POSIX, code point order on Windows; a name that is not
    // UTF-8 may sort elsewhere than in Python, but it is never kept, so the
    // kept files' order is Python's.
    files.sort_by(|a, b| a.name.cmp(&b.name));
    let mut walked = Vec::new();
    for entry in files {
        let name = string_from_os_str(&entry.name);
        if STATE_FILENAMES.contains(&name.as_ref()) {
            continue;
        }
        let rel_path = format!("{prefix}{name}");
        if rules.should_skip_rel_file(&rel_path, &node.parts)? {
            continue;
        }
        let report = if entry.is_link() {
            Some(Report::Link)
        } else if entry.name.to_str().is_none() {
            Some(Report::Undecodable)
        } else if entry.is_special() {
            Some(Report::Special)
        } else {
            None
        };
        match report {
            Some(report) => reports.push((report, reported(&prefix, &entry.name))),
            None => walked.push((name.into_owned(), rel_path)),
        }
    }
    let subdirs = kept
        .into_iter()
        .map(|name| node.child(&prefix, name))
        .collect();
    Ok(Listed {
        files: walked,
        reports,
        subdirs,
    })
}

/// A reported entry's path from the root, with its name's own bytes.
fn reported(prefix: &str, name: &OsStr) -> OsString {
    let mut path = OsString::from(prefix);
    path.push(name);
    path
}

/// `os.path.join(parent, name)` for a folder name: no separator added
/// after one already there, nor, on Windows, after a bare drive (`C:`).
fn child_dirpath(parent: &OsStr, name: &str) -> OsString {
    let mut path = parent.to_owned();
    if !joins_without_separator(parent) {
        path.push(MAIN_SEPARATOR_STR);
    }
    path.push(name);
    path
}

fn joins_without_separator(parent: &OsStr) -> bool {
    let bytes = parent.as_encoded_bytes();
    match bytes.last() {
        None | Some(b'/') => true,
        Some(b'\\') => cfg!(windows),
        Some(b':') => cfg!(windows) && bytes.len() == 2,
        Some(_) => false,
    }
}

/// The tree read back in walk order: each listed folder, its files and its
/// reports, then its subfolders'; the first refused pattern in that order,
/// where Python would have raised.
fn flatten(mut nodes: Vec<Node>) -> Result<FilePlan, PatternError> {
    let mut plan = FilePlan::default();
    let mut stack = vec![0];
    while let Some(id) = stack.pop() {
        let node = &mut nodes[id];
        let listed = match mem::replace(&mut node.visit, Visit::Pending) {
            Visit::Pending | Visit::Unlisted => continue,
            Visit::Failed(error) => return Err(error),
            Visit::Listed(listed) => listed,
        };
        let dir = plan.dirs.len();
        plan.dirs.push(VisitedDir {
            key: mem::take(&mut node.key),
            dirpath: mem::take(&mut node.dirpath),
        });
        for (report, path) in listed.reports {
            match report {
                Report::Link => plan.links.push(path),
                Report::Special => plan.special.push(path),
                Report::Undecodable => plan.undecodable.push(path),
            }
        }
        plan.files
            .extend(listed.files.into_iter().map(|(name, rel_path)| WalkedFile {
                dir,
                name,
                rel_path,
            }));
        stack.extend(node.children.iter().rev());
    }
    Ok(plan)
}
