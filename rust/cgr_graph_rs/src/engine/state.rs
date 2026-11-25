//! Change detection, the file half (docs/reports/PHASE2_FRAME.md, section
//! 6): the checkout's state folder, the hash cache and the folder times as
//! the sync reads and writes them, the in-sync check, and each run's new,
//! changed and deleted files (`changes`). A copy of
//! `codebase_rag/checkout_state.py` and of `codebase_rag/graph_updater.py`'s
//! change detection, rule for rule, reading and writing the same files byte
//! for byte, so an upgrade keeps users' state.
//!
//! - **The state folder** is `$CGR_HOME/state/<name>__<digest>`, named as
//!   `derive_project_name` names the checkout: the resolved folder's last
//!   name with each run of characters outside `[A-Za-z0-9_-]` made `_`, the
//!   `_` and `-` at its ends dropped (`repo` when nothing is left), and the
//!   first eight hex digits of the sha256 of its resolved path. On first
//!   use the state files an older cgr left in the checkout are moved in,
//!   never replacing one already there.
//! - **The hash cache** maps each file to its md5 or `unreadable`, written
//!   as `json.dump(indent=2)` writes it, to a temporary file stamped with
//!   the run's watermark, forced to disk, then renamed. **The folder times**
//!   map each folder (the root as `.`) to its mtime as Python's float,
//!   `sec + nsec * 1e-9`, compared exactly. Both are read as `json.loads`
//!   reads them (`json`); an entry whose name holds a lone surrogate, as a
//!   cache from before #3722 can, is dropped.
//! - **Prepared, not published.** `changes` returns the cache and folder
//!   times a run would publish; Python publishes the ones the run used, at
//!   its commit point, after the graph's writes are flushed.

mod changes;
pub mod json;
#[cfg(test)]
mod tests;

pub use changes::{
    baseline, Baseline, BaselineInputs, ChangeInputs, Changes, SingleTarget, SyncInputs,
};

use std::collections::hash_map::RandomState;
use std::collections::{BTreeSet, HashMap};
use std::ffi::OsString;
use std::fs::{self, File, FileTimes, Metadata, OpenOptions};
use std::hash::BuildHasher;
use std::io::{self, ErrorKind, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};

use super::discover::STATE_FILENAMES;
use json::{Text, Value};

/// The folder under CGR_HOME holding each checkout's state
/// (`cs.CGR_STATE_DIRNAME`).
pub const STATE_DIRNAME: &str = "state";
/// What joins a state folder's name to its digest
/// (`cs.PROJECT_NAME_DIGEST_MARKER`), and how many hex digits the digest
/// keeps (`cs.PROJECT_NAME_DIGEST_LEN`).
pub const DIGEST_MARKER: &str = "__";
pub const DIGEST_LEN: usize = 8;
/// A state folder's name when the checkout's own leaves nothing.
pub const FALLBACK_NAME: &str = "repo";
/// The locks an older cgr takes in the checkout, which stay there
/// (`cs.CGR_STATE_LOCK_FILENAMES`).
pub const LOCK_FILENAMES: &[&str] = &[".cgr-edit-lock", ".cgr-sync-lock"];
/// The edit history, which Python moves in under the edit lock, adding an
/// older cgr's entries to the history there (`cs.EDIT_HISTORY_FILENAME`).
pub const EDIT_HISTORY_FILENAME: &str = ".cgr-edit-history.json";
/// `cs.HASH_CACHE_FILENAME`, `cs.DIR_MTIMES_FILENAME`.
pub const HASH_CACHE_FILENAME: &str = ".cgr-hash-cache.json";
pub const DIR_MTIMES_FILENAME: &str = ".cgr-dir-mtimes.json";
/// The hash cache's mark for a file the run could not read
/// (`cs.HASH_CACHE_UNREADABLE`).
pub const UNREADABLE: &str = "unreadable";
/// `cs.TMP_EXTENSION`, and how many temporary names a publish tries.
const TMP_EXTENSION: &str = ".tmp";
const TEMP_NAME_ATTEMPTS: usize = 8;
/// The permissions of a temporary state file (`cs.EDIT_TEMP_FILE_MODE`).
#[cfg(unix)]
const TEMP_FILE_MODE: u32 = 0o600;
const NANOS_PER_SEC: f64 = 1e9;
const SLASH: &str = "/";

/// A `dict` of str keys: insertion order kept, a key set again keeping
/// its place and taking the new value.
#[derive(Debug, Clone, PartialEq)]
pub struct Entries<V> {
    items: Vec<(String, V)>,
    index: HashMap<String, usize>,
}

impl<V> Default for Entries<V> {
    fn default() -> Self {
        Entries {
            items: Vec::new(),
            index: HashMap::new(),
        }
    }
}

impl<V> Entries<V> {
    pub fn new() -> Self {
        Self::default()
    }

    /// `d[key] = value`.
    pub fn insert(&mut self, key: String, value: V) {
        match self.index.get(&key) {
            Some(&at) => self.items[at].1 = value,
            None => {
                self.index.insert(key.clone(), self.items.len());
                self.items.push((key, value));
            }
        }
    }

    pub fn get(&self, key: &str) -> Option<&V> {
        self.index.get(key).map(|&at| &self.items[at].1)
    }

    pub fn contains_key(&self, key: &str) -> bool {
        self.index.contains_key(key)
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &V)> {
        self.items.iter().map(|(key, value)| (key.as_str(), value))
    }

    pub fn keys(&self) -> impl Iterator<Item = &str> {
        self.items.iter().map(|(key, _)| key.as_str())
    }

    /// `d.pop(key, None)` for each of `keys`, the rest keeping their order.
    pub fn remove_all(&mut self, keys: &BTreeSet<String>) {
        if keys.iter().any(|key| self.index.contains_key(key)) {
            self.items.retain(|(key, _)| !keys.contains(key));
            self.reindex();
        }
    }

    pub fn clear(&mut self) {
        self.items.clear();
        self.index.clear();
    }

    pub fn into_items(self) -> Vec<(String, V)> {
        self.items
    }

    fn reindex(&mut self) {
        self.index = self
            .items
            .iter()
            .enumerate()
            .map(|(at, (key, _))| (key.clone(), at))
            .collect();
    }
}

impl<V> FromIterator<(String, V)> for Entries<V> {
    fn from_iter<I: IntoIterator<Item = (String, V)>>(items: I) -> Self {
        let mut entries = Entries::new();
        for (key, value) in items {
            entries.insert(key, value);
        }
        entries
    }
}

// --- times ------------------------------------------------------------------

/// A file time as `os.stat_result`'s float holds it: `sec + nsec * 1e-9`,
/// as CPython's `fill_time` computes it.
pub fn python_time(sec: i64, nsec: i64) -> f64 {
    sec as f64 + nsec as f64 * 1e-9
}

/// A system time as seconds and nanoseconds from the epoch, the
/// nanoseconds never negative, as `stat` reports them.
fn epoch_parts(time: SystemTime) -> (i64, i64) {
    match time.duration_since(UNIX_EPOCH) {
        Ok(after) => (after.as_secs() as i64, i64::from(after.subsec_nanos())),
        Err(before) => {
            let before = before.duration();
            let (sec, nsec) = (before.as_secs() as i64, i64::from(before.subsec_nanos()));
            if nsec == 0 {
                (-sec, 0)
            } else {
                (-sec - 1, 1_000_000_000 - nsec)
            }
        }
    }
}

/// `st_mtime`.
#[cfg(unix)]
pub fn python_mtime(metadata: &Metadata) -> f64 {
    use std::os::unix::fs::MetadataExt;
    python_time(metadata.mtime(), metadata.mtime_nsec())
}

/// `st_mtime`.
#[cfg(not(unix))]
pub fn python_mtime(metadata: &Metadata) -> f64 {
    let (sec, nsec) = metadata.modified().map_or((0, 0), epoch_parts);
    python_time(sec, nsec)
}

/// `_last_changed`: when a file's bytes last changed as far as the cache's
/// watermark can trust, the later of its mtime and its inode change time,
/// which no copy can backdate. Windows has no such time, so a cached file
/// there is always hashed.
#[cfg(unix)]
pub fn last_changed(metadata: &Metadata) -> f64 {
    use std::os::unix::fs::MetadataExt;
    let changed = python_time(metadata.ctime(), metadata.ctime_nsec());
    python_mtime(metadata).max(changed)
}

/// `_last_changed`: when a file's bytes last changed as far as the cache's
/// watermark can trust, the later of its mtime and its inode change time,
/// which no copy can backdate. Windows has no such time, so a cached file
/// there is always hashed.
#[cfg(not(unix))]
pub fn last_changed(_metadata: &Metadata) -> f64 {
    f64::INFINITY
}

/// `time.time()`.
pub fn now() -> f64 {
    let (sec, nsec) = epoch_parts(SystemTime::now());
    (sec as f64 * NANOS_PER_SEC + nsec as f64) / NANOS_PER_SEC
}

/// The instant `os.utime` sets for `seconds`: its nanoseconds rounded down,
/// as CPython's `_PyTime_ObjectToTimespec` rounds them.
pub fn utime_instant(seconds: f64) -> Option<SystemTime> {
    let whole = seconds.trunc();
    let mut fraction = ((seconds - whole) * NANOS_PER_SEC).floor();
    let mut sec = whole;
    if fraction >= NANOS_PER_SEC {
        fraction -= NANOS_PER_SEC;
        sec += 1.0;
    } else if fraction < 0.0 {
        fraction += NANOS_PER_SEC;
        sec -= 1.0;
    }
    if !sec.is_finite() || sec.abs() >= i64::MAX as f64 {
        return None;
    }
    let nanos = Duration::from_nanos(fraction as u64);
    let sec = sec as i64;
    if sec >= 0 {
        UNIX_EPOCH.checked_add(Duration::from_secs(sec as u64) + nanos)
    } else {
        UNIX_EPOCH
            .checked_sub(Duration::from_secs(sec.unsigned_abs()))?
            .checked_add(nanos)
    }
}

/// `_trustworthy_cache_mtime`: the cache's own mtime, which every file at
/// or under it is checked against, or 0 when it is ahead of `now`, where it
/// would vouch for every edit (#1665), or cannot be read.
pub fn trustworthy_cache_mtime(cache_path: &Path, now: f64) -> f64 {
    match fs::metadata(cache_path) {
        Ok(metadata) => {
            let stamped = python_mtime(&metadata);
            if stamped > now {
                0.0
            } else {
                stamped
            }
        }
        Err(_) => 0.0,
    }
}

// --- the state folder -------------------------------------------------------

/// `Path.resolve()`: absolute, links followed, `.` and `..` gone, and for a
/// path that does not exist, the part that does resolved and the rest
/// joined to it. On Windows the `\\?\` prefix `canonicalize` adds is
/// dropped, as Python's `realpath` drops it.
pub fn resolve(path: &Path) -> PathBuf {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|cwd| cwd.join(path))
            .unwrap_or_else(|_| path.to_path_buf())
    };
    let components: Vec<Component<'_>> = absolute.components().collect();
    for split in (0..=components.len()).rev() {
        let head: PathBuf = components[..split].iter().collect();
        let Ok(mut resolved) = fs::canonicalize(&head) else {
            continue;
        };
        for component in &components[split..] {
            match component {
                Component::CurDir => {}
                Component::ParentDir => {
                    resolved.pop();
                }
                other => resolved.push(other),
            }
        }
        return without_verbatim_prefix(resolved);
    }
    absolute
}

#[cfg(windows)]
fn without_verbatim_prefix(path: PathBuf) -> PathBuf {
    const VERBATIM_UNC: &str = r"\\?\UNC\";
    const VERBATIM: &str = r"\\?\";
    const UNC: &str = r"\\";
    match path.to_str() {
        Some(text) if text.starts_with(VERBATIM_UNC) => {
            PathBuf::from(format!("{UNC}{}", &text[VERBATIM_UNC.len()..]))
        }
        Some(text) if text.starts_with(VERBATIM) => PathBuf::from(&text[VERBATIM.len()..]),
        _ => path,
    }
}

#[cfg(not(windows))]
fn without_verbatim_prefix(path: PathBuf) -> PathBuf {
    path
}

fn is_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_' || c == '-'
}

/// `derive_project_name(repo)`: the state folder's name. `None` for a path
/// that is not UTF-8, which Python cannot encode to hash either.
pub fn state_dir_name(repo: &Path) -> Option<String> {
    let resolved = resolve(repo);
    let digest = Sha256::digest(resolved.to_str()?.as_bytes());
    let name = resolved
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut cleaned = String::with_capacity(name.len());
    let mut in_run = false;
    for c in name.chars() {
        if is_name_char(c) {
            cleaned.push(c);
            in_run = false;
        } else if !in_run {
            cleaned.push('_');
            in_run = true;
        }
    }
    let base = cleaned.trim_matches(|c| c == '_' || c == '-');
    let base = if base.is_empty() { FALLBACK_NAME } else { base };
    let hex = super::hex(&digest);
    Some(format!("{base}{DIGEST_MARKER}{}", &hex[..DIGEST_LEN]))
}

/// `state_dir(repo)` under `cgr_home` (CGR_HOME, its `~` expanded).
pub fn state_dir(cgr_home: &Path, repo: &Path) -> Option<PathBuf> {
    Some(cgr_home.join(STATE_DIRNAME).join(state_dir_name(repo)?))
}

/// What preparing a state folder did.
#[derive(Debug, Default)]
pub struct Prepared {
    pub dir: PathBuf,
    /// The state files moved in from the checkout, by name, sorted.
    pub adopted: Vec<String>,
    /// The state files that could not be moved, which stay in the checkout.
    pub failed: Vec<(String, io::Error)>,
    /// Why the folder could not be created; nothing was moved then.
    pub unavailable: Option<io::Error>,
}

/// The state files a first use moves in: all of cgr's but the locks, which
/// an older cgr still running takes in the checkout, and the edit history,
/// which Python adds to the folder's under the edit lock.
pub fn adoptable_names() -> Vec<&'static str> {
    let mut names: Vec<&str> = STATE_FILENAMES
        .iter()
        .copied()
        .filter(|name| !LOCK_FILENAMES.contains(name) && *name != EDIT_HISTORY_FILENAME)
        .collect();
    names.sort_unstable();
    names
}

/// `prepare_state_dir(repo)`, but for the edit history: the state folder,
/// created, with the state files an older cgr left in the checkout moved
/// in. A file already in the folder is never replaced, and the checkout's
/// copy, older than it, is dropped. `None` for a path Python cannot name.
pub fn prepare_state_dir(cgr_home: &Path, repo: &Path) -> Option<Prepared> {
    let dir = state_dir(cgr_home, repo)?;
    let mut prepared = Prepared {
        dir,
        ..Prepared::default()
    };
    if let Err(error) = fs::create_dir_all(&prepared.dir) {
        prepared.unavailable = Some(error);
        return Some(prepared);
    }
    for name in adoptable_names() {
        match adopt(&repo.join(name), &prepared.dir.join(name)) {
            Ok(true) => prepared.adopted.push(name.to_owned()),
            Ok(false) => {}
            Err(error) => prepared.failed.push((name.to_owned(), error)),
        }
    }
    Some(prepared)
}

/// `_is_regular`: only a regular file under a state file's name is cgr's.
fn is_regular(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| metadata.file_type().is_file())
}

/// `_adopt`: whether `legacy` became `target`. A target that exists, or
/// appears meanwhile, is kept and `legacy` dropped; an error keeps both.
fn adopt(legacy: &Path, target: &Path) -> io::Result<bool> {
    if !is_regular(legacy) {
        return Ok(false);
    }
    let placed = if fs::metadata(target).is_ok() {
        false
    } else {
        match place(legacy, target) {
            Ok(placed) => placed,
            Err(error) if error.kind() == ErrorKind::AlreadyExists => false,
            Err(error) => return Err(error),
        }
    };
    // Best effort, as `_discard`: a copy left behind is dropped next time.
    let _ = fs::remove_file(legacy);
    Ok(placed)
}

/// `_place`: `legacy` copied beside `target`, its mtime kept, then linked
/// into place, which fails rather than replaces. False when `legacy` is
/// gone, moved by another process first.
fn place(legacy: &Path, target: &Path) -> io::Result<bool> {
    let (file, temp) = open_exclusive_temp(target)?;
    drop(file);
    let placed = (|| {
        fs::copy(legacy, &temp)?;
        copy_times(legacy, &temp)?;
        link_no_replace(&temp, target)
    })();
    let _ = fs::remove_file(&temp);
    match placed {
        Ok(()) => Ok(true),
        Err(error) if error.kind() == ErrorKind::NotFound && !legacy.exists() => Ok(false),
        Err(error) => Err(error),
    }
}

/// `shutil.copystat`'s times: the access and modification times, to the
/// nanosecond.
fn copy_times(source: &Path, target: &Path) -> io::Result<()> {
    let metadata = fs::metadata(source)?;
    let times = FileTimes::new()
        .set_accessed(metadata.accessed()?)
        .set_modified(metadata.modified()?);
    OpenOptions::new()
        .write(true)
        .open(target)?
        .set_times(times)
}

/// `_link_no_replace`: `source` under the name `target`, failing when it is
/// taken. A hard link where the file system has them, else an exclusive
/// copy.
fn link_no_replace(source: &Path, target: &Path) -> io::Result<()> {
    match fs::hard_link(source, target) {
        Ok(()) => Ok(()),
        Err(error) if matches!(error.kind(), ErrorKind::AlreadyExists | ErrorKind::NotFound) => {
            Err(error)
        }
        Err(_) => create_exclusive(source, target),
    }
}

/// `_create_exclusive`: a new `target` holding `source`'s bytes and times,
/// removed again when the copy fails, unless something else replaced it.
fn create_exclusive(source: &Path, target: &Path) -> io::Result<()> {
    let mut file = exclusive_options().open(target)?;
    let created = fs::symlink_metadata(target)?;
    let copied = (|| {
        io::copy(&mut File::open(source)?, &mut file)?;
        fs::set_permissions(target, fs::metadata(source)?.permissions())?;
        copy_times(source, target)
    })();
    if copied.is_err() && fs::symlink_metadata(target).is_ok_and(|now| same_file(&created, &now)) {
        let _ = fs::remove_file(target);
    }
    copied
}

#[cfg(unix)]
fn same_file(one: &Metadata, other: &Metadata) -> bool {
    use std::os::unix::fs::MetadataExt;
    (one.ino(), one.dev()) == (other.ino(), other.dev())
}

#[cfg(not(unix))]
fn same_file(_one: &Metadata, _other: &Metadata) -> bool {
    true
}

/// A new file opened for writing that fails when the name exists, never
/// following a link, readable by its owner alone.
fn exclusive_options() -> OpenOptions {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(TEMP_FILE_MODE).custom_flags(libc::O_NOFOLLOW);
    }
    options
}

/// `_open_exclusive_temp`: a fresh `<name>.<16 hex digits>.tmp` beside
/// `path`. The name is random, so no one can plant a link there in
/// advance, and the open is exclusive and follows no link, so one planted
/// anyway fails it.
fn open_exclusive_temp(path: &Path) -> io::Result<(File, PathBuf)> {
    let name = path.file_name().unwrap_or_default();
    for attempt in 0..TEMP_NAME_ATTEMPTS {
        let mut candidate = OsString::from(name);
        candidate.push(format!(".{:016x}{TMP_EXTENSION}", random_token(attempt)));
        let candidate = path.with_file_name(candidate);
        match exclusive_options().open(&candidate) {
            Ok(file) => return Ok((file, candidate)),
            Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        ErrorKind::AlreadyExists,
        format!(
            "could not create a temporary file beside {}",
            path.display()
        ),
    ))
}

/// 64 bits no one outside the process can predict: SipHash under the
/// process's random keys, over the time and the attempt.
fn random_token(attempt: usize) -> u64 {
    RandomState::new().hash_one((now().to_bits(), std::process::id(), attempt))
}

// --- the two files ----------------------------------------------------------

/// What reading the hash cache found.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct HashCacheRead {
    pub entries: Entries<String>,
    /// Why the file could not be used, for a file that is there; it then
    /// reads as no cache, as `_load_hash_cache` reads it.
    pub error: Option<String>,
}

/// `_load_hash_cache`: each file's hash, in the file's order. A file that
/// is missing, cannot be read or decoded, or holds no object reads as no
/// cache; only str values count, and an entry whose name or hash holds a
/// lone surrogate is dropped.
pub fn read_hash_cache(path: &Path) -> HashCacheRead {
    let members = match read_members(path) {
        Ok(Some(members)) => members,
        Ok(None) => return HashCacheRead::default(),
        Err(error) => {
            return HashCacheRead {
                entries: Entries::new(),
                error: Some(error),
            }
        }
    };
    let entries = as_dict(members)
        .into_items()
        .into_iter()
        .filter_map(|(name, value)| match value {
            Value::Str(Some(hash)) => Some((name, hash)),
            _ => None,
        })
        .collect();
    HashCacheRead {
        entries,
        error: None,
    }
}

/// `_load_dir_mtimes`: each folder's mtime, in the file's order. A file
/// that is missing or cannot be used reads as none, and so does one with
/// a time too large for a float; only numbers (and the booleans, which
/// are ints to Python) count, and a folder named with a lone surrogate is
/// dropped.
pub fn read_dir_mtimes(path: &Path) -> Entries<f64> {
    let Ok(Some(members)) = read_members(path) else {
        return Entries::new();
    };
    let mut mtimes = Entries::new();
    for (name, value) in as_dict(members).into_items() {
        let mtime = match value {
            Value::Float(mtime) => mtime,
            Value::Int(Some(mtime)) => mtime,
            Value::Bool(flag) => f64::from(u8::from(flag)),
            // `float()` overflows on it, which ends the whole reading.
            Value::Int(None) => return Entries::new(),
            Value::Str(_) | Value::Other => continue,
        };
        mtimes.insert(name, mtime);
    }
    mtimes
}

/// The top-level object's members of the JSON file at `path`; `Ok(None)`
/// for a file that is missing or holds no object, an error for one that
/// cannot be read or decoded.
fn read_members(path: &Path) -> Result<Option<Vec<(Text, Value)>>, String> {
    if !fs::metadata(path).is_ok_and(|metadata| metadata.is_file()) {
        return Ok(None);
    }
    let bytes = fs::read(path).map_err(|error| error.to_string())?;
    let text = std::str::from_utf8(&bytes).map_err(|error| error.to_string())?;
    json::object_members(text).map_err(|error| error.to_string())
}

/// The members folded as a `dict` folds them, those whose name holds a
/// lone surrogate dropped: no other name can equal one, so dropping it
/// first changes nothing else.
fn as_dict(members: Vec<(Text, Value)>) -> Entries<Value> {
    members
        .into_iter()
        .filter_map(|(name, value)| Some((name?, value)))
        .collect()
}

/// The hash cache's text, `json.dumps(hashes, indent=2)`.
pub fn hash_cache_text(hashes: &Entries<String>) -> String {
    json::indented_object(hashes.iter().map(|(name, hash)| (name, hash.as_str())))
}

/// The folder times' text, `json.dumps(mtimes)`.
pub fn dir_mtimes_text(mtimes: &Entries<f64>) -> String {
    json::float_object(mtimes.iter().map(|(name, &mtime)| (name, mtime)))
}

/// What a text-mode write puts on disk: on Windows each `\n` becomes
/// `\r\n`, as Python's text files write it.
pub fn text_mode_bytes(text: &str) -> Vec<u8> {
    if cfg!(windows) {
        text.replace('\n', "\r\n").into_bytes()
    } else {
        text.as_bytes().to_vec()
    }
}

/// `_publish_hash_cache`: the cache written whole to a fresh temporary
/// file beside `path`, stamped with `stamp` (atime and mtime), forced to
/// disk and renamed over `path`, so a reader sees the previous cache or
/// this one, stamped, never one without its mtime. A temporary file
/// replaced meanwhile refuses the publish; on any failure it is removed and
/// the previous cache left as it was.
pub fn publish_hash_cache(path: &Path, hashes: &Entries<String>, stamp: f64) -> io::Result<()> {
    let instant = utime_instant(stamp).ok_or_else(|| {
        io::Error::new(
            ErrorKind::InvalidInput,
            format!("no file time can hold {stamp}"),
        )
    })?;
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let (mut file, temp) = open_exclusive_temp(path)?;
    let published = (|| {
        file.write_all(&text_mode_bytes(&hash_cache_text(hashes)))?;
        file.set_times(FileTimes::new().set_accessed(instant).set_modified(instant))?;
        let created = file.metadata()?;
        file.sync_all()?;
        drop(file);
        let current = fs::symlink_metadata(&temp)?;
        if current.file_type().is_symlink() || !same_file(&created, &current) {
            return Err(io::Error::other(format!(
                "temporary cache path {} was replaced after creation",
                temp.display()
            )));
        }
        fs::rename(&temp, path)
    })();
    if published.is_err() {
        let _ = fs::remove_file(&temp);
    }
    published
}

/// `_save_dir_mtimes`: the folder times written over `path` in place, as
/// Python writes them.
pub fn save_dir_mtimes(path: &Path, mtimes: &Entries<f64>) -> io::Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(path, text_mode_bytes(&dir_mtimes_text(mtimes)))
}

/// `f"{repo}/{key}"`, the path the sync reaches a cached file or folder by.
pub fn child_path(repo: &std::ffi::OsStr, key: &str) -> PathBuf {
    let mut path = repo.to_owned();
    path.push(SLASH);
    path.push(key);
    PathBuf::from(path)
}
