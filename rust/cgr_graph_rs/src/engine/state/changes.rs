//! The in-sync check and a run's new, changed and deleted files, as
//! `GraphUpdater` decides them (`_is_already_in_sync`, `_load_old_hashes`,
//! `_hash_baseline`, `_scan_eligible_file`, `_keys_deleted_before_parse`,
//! `_deleted_keys_for_run` and `_stash_pending_caches`).
//!
//! - **The in-sync check.** Each recorded folder's mtime matches exactly,
//!   or its listing shows no indexable file or folder added and none
//!   removed; each cached file is not `unreadable`, is a regular file, and
//!   either last changed at or under the cache's own mtime or still has its
//!   md5. A cache mtime in the future counts as 0.
//! - **The baseline**, read before the run writes its placeholder cache:
//!   the previous hashes, unless the run is forced or the cache is dead or
//!   another project's; the Delombok-stale files forced through a re-parse
//!   and left out; every cached file forced when a parser input changed.
//! - **A run** takes its watermark, walks (or takes its one file), and
//!   settles each file in walk order: a cached file at or under the
//!   cache's mtime keeps its hash unread, any other is hashed as raw bytes;
//!   one that cannot be read is marked `unreadable` unless it is gone.
//! - **Deleted files**, on a project run, are those the cache, the
//!   Delombok-stale set, the forced set and the graph name, less those
//!   walked and those unreadable; the graph's paths are Python's to pass.
//!   A single-file run deletes nothing this way, only its own file when
//!   that is gone; it merges its hash into an existing, usable cache only,
//!   keeps that cache's mtime, and leaves the folder times alone.

use std::collections::{BTreeSet, HashSet};
use std::ffi::OsStr;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use super::super::discover::{list, ROOT_DIR_KEY, STATE_FILENAMES};
use super::super::ignore::{IgnoreRules, PatternError};
use super::super::lstat::Kind;
use super::super::{md5_file, Engine};
use super::{
    child_path, last_changed, now, python_mtime, read_dir_mtimes, read_hash_cache,
    trustworthy_cache_mtime, Entries, UNREADABLE,
};

const SLASH: char = '/';

/// What the in-sync check reads.
pub struct SyncInputs<'a> {
    pub repo: &'a OsStr,
    pub cache_path: &'a Path,
    pub dir_mtimes_path: &'a Path,
    /// The cache is not to be trusted this run: discarded in memory, or
    /// written under other parser inputs.
    pub discard_cache: bool,
}

impl Engine {
    /// `_is_already_in_sync`'s file half: whether nothing the cache and the
    /// folder times record changed. An error where Python raises on a
    /// pattern pathspec refuses, met while judging a folder's additions.
    pub fn in_sync(
        &self,
        inputs: &SyncInputs<'_>,
        rules: &IgnoreRules,
    ) -> Result<bool, PatternError> {
        let cache_mtime = trustworthy_cache_mtime(inputs.cache_path, now());
        let old = if inputs.discard_cache {
            Entries::new()
        } else {
            read_hash_cache(inputs.cache_path).entries
        };
        let dirs = read_dir_mtimes(inputs.dir_mtimes_path);
        if old.is_empty() || dirs.is_empty() {
            return Ok(false);
        }
        if !dirs_unchanged(inputs.repo, &old, &dirs, rules)? {
            return Ok(false);
        }
        Ok(self.all_of(&old.items, |(key, hash)| {
            cached_file_unchanged(&child_path(inputs.repo, key), hash, cache_mtime)
        }))
    }
}

/// `_cached_dirs_unchanged`, in the map's order, stopping where Python
/// stops.
fn dirs_unchanged(
    repo: &OsStr,
    old: &Entries<String>,
    dirs: &Entries<f64>,
    rules: &IgnoreRules,
) -> Result<bool, PatternError> {
    for (key, &cached) in dirs.iter() {
        let path = if key == ROOT_DIR_KEY {
            PathBuf::from(repo)
        } else {
            child_path(repo, key)
        };
        let Ok(metadata) = fs::metadata(&path) else {
            return Ok(false);
        };
        if python_mtime(&metadata) == cached {
            continue;
        }
        if dir_changed(&path, key, old, dirs, rules)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_diff_dir_against_cache`: whether the folder's listing shows an
/// indexable file or folder the records lack, or lacks one they hold. A
/// folder that cannot be listed counts as removed.
fn dir_changed(
    path: &Path,
    key: &str,
    old: &Entries<String>,
    dirs: &Entries<f64>,
    rules: &IgnoreRules,
) -> Result<bool, PatternError> {
    let prefix = if key == ROOT_DIR_KEY {
        String::new()
    } else {
        format!("{key}{SLASH}")
    };
    let expected_files = direct_children(old.keys(), &prefix);
    let expected_dirs = direct_children(dirs.keys().filter(|k| *k != ROOT_DIR_KEY), &prefix);
    let Some((files, subdirs)) = scan_dir_children(path) else {
        return Ok(true);
    };
    let parts: Vec<&str> = if key == ROOT_DIR_KEY {
        Vec::new()
    } else {
        key.split(SLASH).collect()
    };
    let mut new_dirs: Vec<&String> = subdirs
        .iter()
        .filter(|name| !expected_dirs.contains(name.as_str()))
        .collect();
    new_dirs.sort_unstable();
    for name in new_dirs {
        if rules.should_keep_dir(name, &prefix)? {
            return Ok(true);
        }
    }
    let mut new_files: Vec<&String> = files
        .iter()
        .filter(|name| !expected_files.contains(name.as_str()))
        .collect();
    new_files.sort_unstable();
    for name in new_files {
        if !rules.should_skip_rel_file(&format!("{prefix}{name}"), &parts)? {
            return Ok(true);
        }
    }
    Ok(expected_files.iter().any(|name| !files.contains(*name))
        || expected_dirs.iter().any(|name| !subdirs.contains(*name)))
}

/// `_direct_children`: the names directly under `prefix`.
fn direct_children<'a>(keys: impl Iterator<Item = &'a str>, prefix: &str) -> HashSet<&'a str> {
    keys.filter_map(|key| key.strip_prefix(prefix))
        .filter(|rest| !rest.contains(SLASH))
        .collect()
}

/// `_scan_dir_children`: the folder's files and subfolders as the walk
/// could keep them, its entries judged as the walk's listing judges them,
/// leaving out cgr's state files, links, names that are not UTF-8, and
/// pipes, sockets and devices; `None` when it cannot be listed to the end.
fn scan_dir_children(path: &Path) -> Option<(HashSet<String>, HashSet<String>)> {
    let mut files = HashSet::new();
    let mut dirs = HashSet::new();
    for entry in list(path.as_os_str())? {
        if entry.is_link() {
            continue;
        }
        let (is_dir, is_special) = (entry.is_dir, entry.is_special());
        let Ok(name) = entry.name.into_string() else {
            continue;
        };
        if STATE_FILENAMES.contains(&name.as_str()) {
            continue;
        }
        if is_dir {
            dirs.insert(name);
        } else if !is_special {
            files.insert(name);
        }
    }
    Some((files, dirs))
}

/// `_cached_file_unchanged`.
fn cached_file_unchanged(path: &Path, hash: &str, cache_mtime: f64) -> bool {
    if hash == UNREADABLE {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if Kind::of(metadata.file_type(), || path) != Kind::File {
        return false;
    }
    if last_changed(&metadata) <= cache_mtime {
        return true;
    }
    md5_file(path).is_some_and(|current| current == hash)
}

/// What the baseline is read under.
pub struct BaselineInputs<'a> {
    pub cache_path: &'a Path,
    pub force: bool,
    pub single: bool,
    /// The cache could not be discarded on disk, so it is ignored.
    pub cache_dead: bool,
    /// The exclusion stamp names another project, whose hashes the cache
    /// holds.
    pub project_mismatch: bool,
    pub parser_changed: bool,
    /// The files a changed Delombok overlay forces through a re-parse.
    pub stale: BTreeSet<String>,
}

/// What a run's files are compared against, read before the run writes
/// its placeholder cache.
#[derive(Debug, Clone, PartialEq)]
pub struct Baseline {
    pub old: Entries<String>,
    /// A single-file run's cache to merge its hash into.
    pub pristine: Entries<String>,
    pub forced: BTreeSet<String>,
    pub stale: BTreeSet<String>,
    pub reindex_all: bool,
    pub full_build: bool,
    pub cache_mtime: f64,
    pub force: bool,
    pub single: bool,
}

/// `_hash_baseline` and `_load_old_hashes`, without the placeholder files.
pub fn baseline(inputs: BaselineInputs<'_>) -> Baseline {
    let trusted = !(inputs.cache_dead || inputs.project_mismatch);
    let mut old = if trusted && !inputs.force {
        read_hash_cache(inputs.cache_path).entries
    } else {
        Entries::new()
    };
    let pristine = if trusted && inputs.single {
        read_hash_cache(inputs.cache_path).entries
    } else {
        Entries::new()
    };
    let mut forced: BTreeSet<String> = inputs
        .stale
        .iter()
        .filter(|key| old.contains_key(key))
        .cloned()
        .collect();
    old.remove_all(&inputs.stale);
    let reindex_all = inputs.parser_changed && !old.is_empty();
    if reindex_all {
        forced.extend(old.keys().map(str::to_owned));
        old.clear();
    }
    let full_build = (inputs.force || (old.is_empty() && !reindex_all)) && !inputs.single;
    let cache_mtime = if fs::metadata(inputs.cache_path).is_ok_and(|m| m.is_file()) {
        trustworthy_cache_mtime(inputs.cache_path, now())
    } else {
        0.0
    };
    Baseline {
        old,
        pristine,
        forced,
        stale: inputs.stale,
        reindex_all,
        full_build,
        cache_mtime,
        force: inputs.force,
        single: inputs.single,
    }
}

/// A single-file run's target, and its key when the rules keep it.
pub struct SingleTarget {
    pub path: PathBuf,
    pub key: Option<String>,
}

/// What a run's change detection takes beyond its baseline.
pub struct ChangeInputs<'a> {
    pub repo: &'a OsStr,
    pub single: Option<SingleTarget>,
    /// The graph's module paths, on the runs that ask the graph; `None`
    /// when the graph could not answer.
    pub graph_paths: Option<HashSet<String>>,
}

/// What a run's change detection decides.
#[derive(Debug, Clone, PartialEq)]
pub struct Changes {
    /// Taken before any file is hashed: what a project run's cache is
    /// stamped with.
    pub watermark: f64,
    /// Each settled file's hash or `unreadable` mark, in walk order.
    pub hashes: Entries<String>,
    pub unreadable: BTreeSet<String>,
    /// The files hashed or kept unread, which still exist.
    pub current: BTreeSet<String>,
    /// The files to re-parse, in walk order, each with whether it is new.
    pub changed: Vec<(String, bool)>,
    /// A single-file run's target, gone since the run began.
    pub gone: Option<String>,
    /// The files whose subtrees go before the parse, sorted.
    pub deleted_first: Vec<String>,
    /// The files the run reconciles away after the parse.
    pub deleted: BTreeSet<String>,
    /// A single-file run's cache its hash merges into.
    pub merge_base: Option<Entries<String>>,
    /// The cache to publish, if any.
    pub prepared: Option<Entries<String>>,
    /// The cache's mtime once published: the watermark, or for a
    /// single-file run the previous cache's own (0 when it was not
    /// trusted).
    pub stamp: f64,
    /// A project run's folder times, in walk order.
    pub mtimes: Option<Entries<f64>>,
}

/// How one file settles.
enum Settled {
    /// A single-file run's target, gone.
    Gone,
    Unreadable {
        marked: bool,
    },
    Kept(String),
    Hashed(String),
}

impl Engine {
    /// A run's change detection from its baseline: the project walked
    /// under `rules`, or the single file, settled on the pool and folded in
    /// walk order. An error where Python raises on a pattern pathspec
    /// refuses.
    pub fn changes(
        &self,
        baseline: &Baseline,
        inputs: ChangeInputs<'_>,
        rules: &IgnoreRules,
    ) -> Result<Changes, PatternError> {
        let watermark = now();
        let single_path = inputs.single.as_ref().map(|single| single.path.clone());
        let (mut eligible, mtimes) = match inputs.single {
            Some(single) => (
                single
                    .key
                    .map(|key| (single.path, key))
                    .into_iter()
                    .collect::<Vec<_>>(),
                None,
            ),
            None => {
                let plan = self.walk(inputs.repo, rules)?;
                let mtimes: Entries<f64> = plan
                    .dirs
                    .iter()
                    .filter_map(|dir| Some((dir.key.clone(), dir.mtime?)))
                    .collect();
                let files = plan
                    .paths()
                    .into_iter()
                    .zip(plan.files.into_iter().map(|file| file.rel_path))
                    .collect();
                (files, Some(mtimes))
            }
        };
        let mut gone = None;
        if let Some(path) = &single_path {
            if !eligible.is_empty() && fs::metadata(path).is_err() {
                gone = Some(eligible[0].1.clone());
                eligible.clear();
            }
        }
        let settled = self.map_ordered(&eligible, |(path, key)| {
            settle(path, key, baseline, single_path.as_deref())
        });
        let mut changes = Changes {
            watermark,
            hashes: Entries::new(),
            unreadable: BTreeSet::new(),
            current: BTreeSet::new(),
            changed: Vec::new(),
            gone,
            deleted_first: Vec::new(),
            deleted: BTreeSet::new(),
            merge_base: None,
            prepared: None,
            stamp: watermark,
            mtimes,
        };
        for ((_, key), settled) in eligible.iter().zip(settled) {
            match settled {
                Settled::Gone => changes.gone = Some(key.clone()),
                Settled::Unreadable { marked } => {
                    changes.unreadable.insert(key.clone());
                    if marked {
                        changes.hashes.insert(key.clone(), UNREADABLE.to_owned());
                    }
                }
                Settled::Kept(hash) => {
                    changes.current.insert(key.clone());
                    changes.hashes.insert(key.clone(), hash);
                }
                Settled::Hashed(hash) => {
                    changes.current.insert(key.clone());
                    let unchanged = !baseline.force && baseline.old.get(key) == Some(&hash);
                    changes.hashes.insert(key.clone(), hash);
                    if !unchanged {
                        let is_new = !baseline.old.contains_key(key)
                            && !baseline.forced.contains(key)
                            && inputs
                                .graph_paths
                                .as_ref()
                                .is_some_and(|paths| !paths.contains(key));
                        changes.changed.push((key.clone(), is_new));
                    }
                }
            }
        }
        if baseline.single {
            settle_single_file(&mut changes, baseline);
        } else {
            let walked: HashSet<&str> = eligible.iter().map(|(_, key)| key.as_str()).collect();
            changes.deleted_first = deleted_first(baseline, &changes, &walked, &inputs.graph_paths);
            changes.deleted = deleted(baseline, &changes, &inputs.graph_paths);
            changes.prepared = Some(changes.hashes.clone());
        }
        Ok(changes)
    }
}

/// `_scan_eligible_file` for one file, without its bytes.
fn settle(path: &Path, key: &str, baseline: &Baseline, single: Option<&Path>) -> Settled {
    if !baseline.force {
        if let Some(old) = baseline.old.get(key) {
            match fs::metadata(path) {
                Err(error) => {
                    if error.kind() == io::ErrorKind::NotFound && target_vanished(path, single) {
                        return Settled::Gone;
                    }
                    return Settled::Unreadable {
                        marked: !vanished(path),
                    };
                }
                Ok(metadata) => {
                    if last_changed(&metadata) <= baseline.cache_mtime && old != UNREADABLE {
                        return Settled::Kept(old.clone());
                    }
                }
            }
        }
    }
    match md5_file(path) {
        Some(hash) => Settled::Hashed(hash),
        None if target_vanished(path, single) => Settled::Gone,
        None => Settled::Unreadable {
            marked: !vanished(path),
        },
    }
}

/// `_single_target_vanished`: the run's single target, its entry gone.
fn target_vanished(path: &Path, single: Option<&Path>) -> bool {
    single == Some(path)
        && fs::symlink_metadata(path).is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
}

/// `_vanished`: the path is gone, or is a link whose target is, rather
/// than there but out of reach.
fn vanished(path: &Path) -> bool {
    match fs::symlink_metadata(path) {
        Err(error) => is_gone(&error),
        Ok(metadata) if Kind::of(metadata.file_type(), || path) == Kind::Link => {
            fs::metadata(path).is_err_and(|error| is_gone(&error))
        }
        Ok(_) => false,
    }
}

/// Windows reports a reparse-point loop as ERROR_CANT_RESOLVE_FILENAME,
/// which Python maps to EINVAL rather than ELOOP.
#[cfg(windows)]
const ERROR_CANT_RESOLVE_FILENAME: i32 = 1921;

/// `_is_gone_error`: the path is absent, runs through a non-folder, or
/// loops.
fn is_gone(error: &io::Error) -> bool {
    if matches!(
        error.kind(),
        io::ErrorKind::NotFound | io::ErrorKind::NotADirectory
    ) {
        return true;
    }
    #[cfg(unix)]
    {
        error.raw_os_error() == Some(libc::ELOOP)
    }
    #[cfg(windows)]
    {
        error.raw_os_error() == Some(ERROR_CANT_RESOLVE_FILENAME)
    }
    #[cfg(not(any(unix, windows)))]
    {
        false
    }
}

/// A single-file run's cache and deletions: its hash merged over the
/// previous cache, published only when there was one, under its mtime; only
/// its own file deleted, when gone.
fn settle_single_file(changes: &mut Changes, baseline: &Baseline) {
    if !baseline.pristine.is_empty() {
        let mut merged = baseline.pristine.clone();
        for (key, hash) in changes.hashes.iter() {
            merged.insert(key.to_owned(), hash.clone());
        }
        if let Some(gone) = &changes.gone {
            merged.remove_all(&BTreeSet::from([gone.clone()]));
        }
        changes.prepared = Some(merged);
    }
    changes.merge_base = Some(baseline.pristine.clone());
    changes.stamp = baseline.cache_mtime;
    changes.deleted = changes.gone.iter().cloned().collect();
}

/// `_keys_deleted_before_parse` on a project run.
fn deleted_first(
    baseline: &Baseline,
    changes: &Changes,
    walked: &HashSet<&str>,
    graph_paths: &Option<HashSet<String>>,
) -> Vec<String> {
    let kept = |key: &&str| !walked.contains(key) && !changes.unreadable.contains(*key);
    let graph_only = graph_paths
        .iter()
        .flatten()
        .map(String::as_str)
        .filter(|key| kept(key));
    let named: BTreeSet<&str> = baseline
        .old
        .keys()
        .chain(baseline.stale.iter().map(String::as_str))
        .chain(baseline.forced.iter().map(String::as_str))
        .chain(graph_only)
        .filter(|key| kept(key))
        .collect();
    named.into_iter().map(str::to_owned).collect()
}

/// `_deleted_keys_for_run` on a project run.
fn deleted(
    baseline: &Baseline,
    changes: &Changes,
    graph_paths: &Option<HashSet<String>>,
) -> BTreeSet<String> {
    baseline
        .old
        .keys()
        .chain(graph_paths.iter().flatten().map(String::as_str))
        .filter(|key| !changes.current.contains(*key) && !changes.unreadable.contains(*key))
        .map(str::to_owned)
        .collect()
}
