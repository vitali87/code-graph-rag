//! The tracked-source rescue: `config.py`'s `_tracked_source_dirs`
//! (docs/reports/PHASE2_FRAME.md, section 5, rule 3).
//!
//! A built-in directory name such as `bin` or `out` holds committed source
//! as often as build output, so each file git tracks under one of the
//! ambiguous names is unignored by its own anchored path. Rescued only when
//! its first ambiguous directory is one of them, no other built-in
//! directory is on its path, and the path holds no pattern character and
//! does not end in whitespace. The same command Python runs, `git ls-files
//! -z` in the root with a 30 s limit, read as Python reads it; any failure
//! rescues nothing.

use std::collections::BTreeSet;
use std::io::Read;
use std::path::Path;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

use super::python_str::is_space;
use super::BUILTIN_IGNORED_DIRS;

/// The ambiguous names (`cs.TRACKED_SOURCE_DIR_NAMES`).
pub const TRACKED_SOURCE_DIR_NAMES: &[&str] = &[
    "bin", "coverage", "env", "obj", "out", "target", "temp", "tmp",
];
/// What `cs.TRACKED_PATH_PATTERN_CHARS` finds in a path.
pub const TRACKED_PATH_PATTERN_CHARS: &[char] = &['*', '?', '[', ']', '\\', '!'];
/// `cs.GIT_LS_FILES_TIMEOUT_S`.
pub const GIT_LS_FILES_TIMEOUT: Duration = Duration::from_secs(30);
/// `cs.SHELL_CMD_GIT` and its arguments.
const GIT: &str = "git";
const LS_FILES: [&str; 2] = ["ls-files", "-z"];
const ENTRY_SEPARATOR: char = '\0';
const SLASH: char = '/';
const LINE_FEED: &str = "\n";
const CARRIAGE_RETURN: char = '\r';
const CRLF: &str = "\r\n";
/// How often to look whether git has exited once its output is read.
const EXIT_POLL: Duration = Duration::from_millis(1);

/// The rescue's unignores, and the folders they keep files in.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TrackedRescue {
    /// `/<path>` for each rescued tracked file.
    pub rescued: BTreeSet<String>,
    /// The first ambiguous folder of each, from the root.
    pub dirs: BTreeSet<String>,
}

/// `_tracked_source_dirs(root)`, with the folders it logs.
pub fn tracked_source_rescue(root: &Path) -> TrackedRescue {
    git_ls_files(root).map_or_else(TrackedRescue::default, |listing| rescue(&listing))
}

/// The rescue for `git ls-files -z`'s output, as Python's text.
pub(crate) fn rescue(listing: &str) -> TrackedRescue {
    let mut found = TrackedRescue::default();
    for entry in listing.split(ENTRY_SEPARATOR) {
        let parts: Vec<&str> = entry.split(SLASH).collect();
        let dirs = &parts[..parts.len() - 1];
        if entry.contains(TRACKED_PATH_PATTERN_CHARS)
            || entry.chars().next_back().is_some_and(is_space)
            || dirs.iter().any(|part| {
                BUILTIN_IGNORED_DIRS.contains(part) && !TRACKED_SOURCE_DIR_NAMES.contains(part)
            })
        {
            continue;
        }
        if let Some(index) = dirs
            .iter()
            .position(|part| TRACKED_SOURCE_DIR_NAMES.contains(part))
        {
            found.rescued.insert(format!("{SLASH}{entry}"));
            found.dirs.insert(parts[..=index].join("/"));
        }
    }
    found
}

/// What `subprocess.run(["git", "ls-files", "-z"], cwd=root,
/// capture_output=True, encoding="utf-8", errors="replace", timeout=30)`
/// gives as `stdout`, or `None` where Python rescues nothing: git missing,
/// failing or too slow.
fn git_ls_files(root: &Path) -> Option<String> {
    let mut child = Command::new(GIT)
        .args(LS_FILES)
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + GIT_LS_FILES_TIMEOUT;
    let mut stdout = child.stdout.take()?;
    // Read on a thread of its own, so a git that never finishes is stopped
    // at the deadline rather than waited for.
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut out = Vec::new();
        let read = stdout.read_to_end(&mut out).map(|_| out);
        let _ = sender.send(read);
    });
    let output = receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .ok()
        .and_then(Result::ok);
    let status = match output {
        Some(_) => exit_status(&mut child, deadline),
        None => None,
    };
    let Some(status) = status else {
        let _ = child.kill();
        let _ = child.wait();
        return None;
    };
    let output = output?;
    status.success().then(|| python_text(&output))
}

/// The child's exit, once it comes before `deadline`.
fn exit_status(child: &mut Child, deadline: Instant) -> Option<ExitStatus> {
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            Ok(None) if Instant::now() < deadline => thread::sleep(EXIT_POLL),
            _ => return None,
        }
    }
}

/// Bytes as `subprocess` hands text back: decoded as UTF-8, each invalid
/// sequence one U+FFFD (Python's "replace" and `from_utf8_lossy` both
/// substitute the maximal subpart), then `\r\n` and `\r` read as `\n`.
pub(crate) fn python_text(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes)
        .replace(CRLF, LINE_FEED)
        .replace(CARRIAGE_RETURN, LINE_FEED)
}
