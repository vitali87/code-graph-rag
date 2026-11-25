//! Reading the root `.cgrignore` and `.gitignore`: `config.py`'s
//! `load_ignore_patterns`, line for line (docs/reports/PHASE2_FRAME.md,
//! section 5, rules 1 to 3).
//!
//! Only the root's two files are read, as UTF-8, a byte-order mark first
//! skipped (#3724). A line keeps the whitespace a backslash escapes at its
//! end (#3723); blank lines and `#` lines are dropped; a line pathspec
//! refuses is left out on its own rather than failing the run. `!x` is an
//! unignore. In `.gitignore`, an exclude that a later `!` line of the same
//! string, or a `.cgrignore` `!` line, cancels is dropped, and a surviving
//! `!` line turns the file into one ordered block. The tracked-source
//! rescue (`tracked`) joins the unignores.
//!
//! What Python logs about the files (a line left out, a file it could not
//! read, the tracked folders it kept) comes back as data, for the caller to
//! report.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use super::pathspec::{Flavor, Pattern};
use super::python_str::is_space;
use super::tracked::tracked_source_rescue;
use super::{PatternError, BLOCK_SEPARATOR, NEGATION};

/// `config.CGRIGNORE_FILENAME`.
pub const CGRIGNORE_FILENAME: &str = ".cgrignore";
/// `config.GITIGNORE_FILENAME`.
pub const GITIGNORE_FILENAME: &str = ".gitignore";
/// What `utf-8-sig` skips at the start of a file.
const BYTE_ORDER_MARK: &[u8] = b"\xef\xbb\xbf";
const COMMENT: char = '#';
const BACKSLASH: char = '\\';
const LINE_FEED: &str = "\n";
const CARRIAGE_RETURN: char = '\r';
const CRLF: &str = "\r\n";

/// The patterns the two files give, and what reading them found.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct IgnorePatterns {
    /// `load_ignore_patterns(root).exclude`.
    pub exclude: BTreeSet<String>,
    /// `load_ignore_patterns(root).unignore`, the tracked-source rescue
    /// included.
    pub unignore: BTreeSet<String>,
    /// Each line left out because pathspec refuses it, as the file's name
    /// and the line's number from 1, in the order Python warns of them.
    pub refused_lines: Vec<(&'static str, usize)>,
    /// Each file that exists but could not be read whole as UTF-8.
    pub unreadable: Vec<&'static str>,
    /// The folders the tracked-source rescue keeps files in.
    pub tracked_dirs: BTreeSet<String>,
}

/// `load_ignore_patterns(root)`: the root `.cgrignore`'s and `.gitignore`'s
/// excludes, and their unignores with the tracked-source rescue's.
pub fn load_ignore_patterns(root: &Path) -> IgnorePatterns {
    let mut found = IgnorePatterns::default();
    let cgr = read_ignore_lines(root, CGRIGNORE_FILENAME, &mut found);
    let git = read_ignore_lines(root, GITIGNORE_FILENAME, &mut found);
    let (cgr_exclude, cgr_unignore) = split(&cgr);
    let (_, git_unignore) = split(&git);
    let tracked = tracked_source_rescue(root);
    found.exclude = cgr_exclude
        .into_iter()
        .chain(gitignore_excludes(&git, &cgr_unignore))
        .collect();
    found.unignore = cgr_unignore
        .into_iter()
        .chain(git_unignore)
        .chain(tracked.rescued)
        .collect();
    found.tracked_dirs = tracked.dirs;
    found
}

/// `_read_ignore_lines(root / name)`: the file's pattern lines in order.
fn read_ignore_lines(root: &Path, name: &'static str, found: &mut IgnorePatterns) -> Vec<String> {
    let path = root.join(name);
    // `Path.is_file()`, which follows a link and is false for a pipe.
    if !fs::metadata(&path).is_ok_and(|metadata| metadata.is_file()) {
        return Vec::new();
    }
    let Some(text) = fs::read(&path).ok().and_then(decode_utf8_sig) else {
        found.unreadable.push(name);
        return Vec::new();
    };
    let mut lines = Vec::new();
    for (index, line) in universal_lines(&text).iter().enumerate() {
        let line = trim_ignore_line(line);
        if line.is_empty() || line.starts_with(COMMENT) {
            continue;
        }
        if refuses(line) || (line.starts_with(NEGATION) && refuses(unignore_pattern(line))) {
            found.refused_lines.push((name, index + 1));
            continue;
        }
        lines.push(line.to_owned());
    }
    lines
}

/// The text `open(encoding="utf-8-sig")` reads: a leading byte-order mark
/// skipped, and nothing at all for bytes that are not UTF-8.
fn decode_utf8_sig(bytes: Vec<u8>) -> Option<String> {
    let start = if bytes.starts_with(BYTE_ORDER_MARK) {
        BYTE_ORDER_MARK.len()
    } else {
        0
    };
    std::str::from_utf8(&bytes[start..]).ok().map(str::to_owned)
}

/// The lines a text-mode file yields: `\r\n` and `\r` read as `\n`, which
/// alone ends a line. The piece after a final `\n` is empty, so it is no
/// pattern either way.
pub(crate) fn universal_lines(text: &str) -> Vec<String> {
    text.replace(CRLF, LINE_FEED)
        .replace(CARRIAGE_RETURN, LINE_FEED)
        .split(LINE_FEED)
        .map(str::to_owned)
        .collect()
}

/// `trim_ignore_line`: trailing whitespace goes unless a backslash escapes
/// it, as git trims it; leading whitespace goes.
pub(crate) fn trim_ignore_line(line: &str) -> &str {
    let mut end = 0;
    let mut escaped = false;
    for (index, c) in line.char_indices() {
        if escaped || !is_space(c) {
            end = index + c.len_utf8();
        }
        escaped = !escaped && c == BACKSLASH;
    }
    line[..end].trim_start_matches(is_space)
}

/// `_unignore_pattern`: a `!` line's pattern, only the whitespace after the
/// `!` stripped.
fn unignore_pattern(line: &str) -> &str {
    line.strip_prefix(NEGATION)
        .unwrap_or(line)
        .trim_start_matches(is_space)
}

/// `ignore_line_error(line) is not None`: whether pathspec refuses the
/// line among the plain patterns or in an ordered block. A pattern too
/// large for this port's regexes is one Python takes.
fn refuses(line: &str) -> bool {
    [Flavor::Basic, Flavor::Spec].into_iter().any(|flavor| {
        matches!(
            Pattern::new(line, flavor),
            Err(PatternError::Invalid { .. } | PatternError::BadRange { .. })
        )
    })
}

/// `_ignore_patterns`: the excludes, and the `!` lines' unignores.
fn split(lines: &[String]) -> (BTreeSet<String>, BTreeSet<String>) {
    let (unignore, exclude): (Vec<&String>, Vec<&String>) =
        lines.iter().partition(|line| line.starts_with(NEGATION));
    (
        exclude.into_iter().cloned().collect(),
        unignore
            .into_iter()
            .map(|line| unignore_pattern(line).to_owned())
            .collect(),
    )
}

/// `_gitignore_excludes(lines, cancelled)`: the `.gitignore` excludes as
/// git reads them, each its own pattern, or one ordered block once a `!`
/// line survives.
fn gitignore_excludes(lines: &[String], cancelled: &BTreeSet<String>) -> BTreeSet<String> {
    let mut kept: Vec<&str> = Vec::new();
    let mut seen_exclude = false;
    for (index, line) in lines.iter().enumerate() {
        if line.starts_with(NEGATION) {
            if seen_exclude {
                kept.push(line);
            }
        } else if !cancelled.contains(line)
            && !lines[index + 1..]
                .iter()
                .any(|later| later.strip_prefix(NEGATION) == Some(line.as_str()))
        {
            kept.push(line);
            seen_exclude = true;
        }
    }
    if kept.iter().all(|line| !line.starts_with(NEGATION)) {
        return kept.into_iter().map(str::to_owned).collect();
    }
    BTreeSet::from([kept.join(&BLOCK_SEPARATOR.to_string())])
}
