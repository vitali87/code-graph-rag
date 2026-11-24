//! The ignore rules: which paths a repository walk leaves out
//! (docs/reports/PHASE2_FRAME.md, section 5).
//!
//! A port of the matching in `codebase_rag/utils/path_utils.py`, rule for
//! rule, so the frame's walk keeps exactly the files the Python walk keeps.
//! Each function's doc names the Python function it answers for. Patterns
//! follow pathspec's "gitignore" semantics (`pathspec` below), not git's
//! and not the `ignore` crate's: matching is case-sensitive, a pattern that
//! matches a directory matches everything in it, excludes beat unignores,
//! and an ordered `.gitignore` block is judged level by level, the last
//! matching line winning, so an excluded parent cannot be re-included.
//! `codebase_rag/tests/test_engine_ignore_rules.py` holds every answer here
//! to pathspec's and path_utils' own.
//!
//! Not here yet: reading `.gitignore` and `.cgrignore` (`config.py`'s
//! `load_ignore_patterns`), which waits for #3723 and #3724 to change how
//! Python trims their lines and reads a byte-order mark, so the reader is
//! ported once, after the fix; and what only the walk (step 5) needs: the
//! tracked-source rescue (`git ls-files`), the generated-source roots, and
//! `should_skip_path`'s check that a path resolves inside the root.

mod fnmatch;
mod pathspec;
mod python_str;
#[cfg(test)]
mod tests;

use std::borrow::Cow;
use std::collections::BTreeSet;
use std::fmt;

pub use pathspec::{Flavor, Pattern, REGEX_DFA_SIZE_LIMIT, REGEX_SIZE_LIMIT};

use pathspec::{git_ignore_spec_include, normalize_file, path_spec_include};

/// Directory names skipped wherever they are (`cs.IGNORE_PATTERNS`).
pub const BUILTIN_IGNORED_DIRS: &[&str] = &[
    ".cache",
    ".claude",
    ".cxx",
    ".dart_tool",
    ".eclipse",
    ".eggs",
    ".env",
    ".git",
    ".gradle",
    ".hg",
    ".idea",
    ".ipynb_checkpoints",
    ".maven",
    ".mypy_cache",
    ".nox",
    ".npm",
    ".nyc_output",
    ".pnpm-store",
    ".pytest_cache",
    ".qdrant_code_embeddings",
    ".ruff_cache",
    ".svn",
    ".tmp",
    ".tox",
    ".venv",
    ".vs",
    ".vscode",
    ".yarn",
    "Pods",
    "__pycache__",
    "bin",
    "bower_components",
    "build",
    "coverage",
    "dist",
    "env",
    "htmlcov",
    "node_modules",
    "obj",
    "out",
    "site-packages",
    "target",
    "temp",
    "tmp",
    "vendor",
    "venv",
];
/// Version-control metadata, rescued only by a `!` line that spells a path
/// through it (`cs.VCS_METADATA_DIRS`).
pub const VCS_METADATA_DIRS: &[&str] = &[".git", ".hg", ".svn"];
/// File endings no unignore rescues (`cs.UNCONDITIONAL_IGNORE_SUFFIXES`).
pub const UNCONDITIONAL_IGNORE_SUFFIXES: &[&str] = &[
    ".a", ".class", ".dll", ".o", ".pyc", ".pyo", ".so", ".tmp", "~",
];
/// File endings a `!` line naming the file rescues
/// (`cs.RESCUABLE_IGNORE_SUFFIXES`).
pub const RESCUABLE_IGNORE_SUFFIXES: &[&str] = &[".min.css", ".min.js"];

/// `cs.DIR_BIN`, a built-in directory except directly under `cs.DIR_SRC`.
const DIR_BIN: &str = "bin";
const DIR_SRC: &str = "src";
const SLASH: char = '/';
/// `cs.IGNORE_BLOCK_SEPARATOR`: joins the lines of an ordered block.
const BLOCK_SEPARATOR: char = '\n';
/// `cs.GITIGNORE_NEGATION`.
const NEGATION: char = '!';
/// `cs.GITIGNORE_DIRS_INSIDE_SUFFIX`: `x/**/`, the directories inside x.
const DIRS_INSIDE_SUFFIX: &str = "/**/";
/// `cs.GLOB_DOUBLE_STAR`.
const DOUBLE_STAR: &str = "**";
/// `_GLOB_MAGIC`.
const GLOB_MAGIC: [char; 3] = ['*', '?', '['];

/// Why a set of patterns cannot be used; where the Python code raises.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PatternError {
    /// pathspec's `GitIgnorePatternError`: a pattern ending in an unescaped
    /// `\`, or a lone `!`, which normalises to nothing.
    Invalid { pattern: String },
    /// `re.error` on the regex pathspec builds: a bracket range that runs
    /// backwards, such as `[z-a]`.
    BadRange { pattern: String },
    /// An ordered block with an empty line, which pathspec skips, so
    /// `_ordered_block`'s `zip(strict=True)` raises.
    EmptyBlockLine { block: String },
    /// A pattern whose regex outgrows `REGEX_SIZE_LIMIT`; pathspec has no
    /// limit, so this is the one refusal Python does not make.
    TooLarge { pattern: String },
    /// A translation the port never produces, kept so no input can panic.
    Unexpected { pattern: String, reason: String },
}

impl fmt::Display for PatternError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PatternError::Invalid { pattern } => write!(f, "Invalid git pattern: {pattern:?}"),
            PatternError::BadRange { pattern } => {
                write!(f, "bad character range in pattern {pattern:?}")
            }
            PatternError::EmptyBlockLine { block } => {
                write!(f, "an ordered ignore block with an empty line: {block:?}")
            }
            PatternError::TooLarge { pattern } => write!(
                f,
                "pattern {pattern:?} compiles to more than {REGEX_SIZE_LIMIT} bytes"
            ),
            PatternError::Unexpected { pattern, reason } => {
                write!(f, "pattern {pattern:?}: {reason}")
            }
        }
    }
}

impl std::error::Error for PatternError {}

/// One ordered `.gitignore` block (`_OrderedBlock`): its lines in order,
/// each compiled as `GitIgnoreSpec` compiles it.
#[derive(Debug, Clone)]
struct OrderedBlock {
    lines: Vec<(String, Pattern)>,
    /// The block's excludes alone, so a path none of them reaches costs one
    /// match.
    excludes: Vec<Pattern>,
}

impl OrderedBlock {
    /// `_ordered_block(ordered_block_lines(block))`.
    fn new(block: &str) -> Result<OrderedBlock, PatternError> {
        let lines: Vec<&str> = block.split(BLOCK_SEPARATOR).collect();
        let mut compiled = Vec::with_capacity(lines.len());
        for line in lines.iter().filter(|line| !line.is_empty()) {
            compiled.push(Pattern::new(line, Flavor::Spec)?);
        }
        if compiled.len() != lines.len() {
            return Err(PatternError::EmptyBlockLine {
                block: block.to_owned(),
            });
        }
        let excludes = lines
            .iter()
            .zip(&compiled)
            .filter(|(line, _)| !line.starts_with(NEGATION))
            .map(|(_, pattern)| pattern.clone())
            .collect();
        Ok(OrderedBlock {
            lines: lines
                .iter()
                .map(|line| (*line).to_owned())
                .zip(compiled)
                .collect(),
            excludes,
        })
    }

    /// `_git_ignores`: whether git ignores the path, at the path itself or
    /// at a directory above it, the last line matching that level deciding.
    fn git_ignores(&self, rel_path: &str) -> bool {
        if git_ignore_spec_include(&self.excludes, &normalize_file(rel_path)) != Some(true) {
            return false;
        }
        let is_dir = rel_path.ends_with(SLASH);
        let parts: Vec<&str> = rel_path.trim_end_matches(SLASH).split(SLASH).collect();
        for depth in 1..=parts.len() {
            let level = parts[..depth].join("/");
            let level_is_dir = is_dir || depth < parts.len();
            let mut ignored = false;
            for (line, pattern) in &self.lines {
                if let Some(include) = pattern.include() {
                    if matches_level(line, pattern, &level, level_is_dir) {
                        ignored = include;
                    }
                }
            }
            if ignored {
                return true;
            }
        }
        false
    }
}

/// `_matches_level`: whether a block line matches `level` itself rather
/// than a directory above it, read from where pathspec's `ps_d` marker
/// matched.
fn matches_level(line: &str, pattern: &Pattern, level: &str, is_dir: bool) -> bool {
    let probe: Cow<'_, str> = if is_dir {
        Cow::Owned(format!("{level}/"))
    } else {
        Cow::Borrowed(level)
    };
    let Some(found) = pattern.search(&probe) else {
        return false;
    };
    if pattern.is_any_dir() {
        return is_dir;
    }
    if line.ends_with(DIRS_INSIDE_SUFFIX) {
        return is_dir && found.dir_mark.is_some_and(|start| start < level.len());
    }
    if let Some(start) = found.dir_mark {
        return start == level.len();
    }
    // `x/**` matches what is inside x, which the probe `x/` is not.
    !(is_dir && found.end == probe.len())
}

/// A compiled exclude or unignore set: `compiled_ignore_spec(patterns)`,
/// the `_IgnoreMatcher` that `matches_ignore_patterns` asks.
#[derive(Debug, Clone)]
pub struct PatternSet {
    /// The single patterns, sorted, compiled with `Flavor::Basic`.
    patterns: Vec<Pattern>,
    /// The ordered `.gitignore` blocks, sorted.
    ordered_blocks: Vec<OrderedBlock>,
}

impl PatternSet {
    /// The set compiled as `compiled_ignore_spec` compiles it: an entry
    /// holding a line break is an ordered block, the rest one `PathSpec`
    /// in sorted order, the empty pattern dropped.
    pub fn new<I, S>(patterns: I) -> Result<PatternSet, PatternError>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let unique: BTreeSet<String> = patterns
            .into_iter()
            .map(|pattern| pattern.as_ref().to_owned())
            .collect();
        let (blocks, single): (Vec<&String>, Vec<&String>) = unique
            .iter()
            .partition(|pattern| pattern.contains(BLOCK_SEPARATOR));
        let patterns = single
            .iter()
            .filter(|pattern| !pattern.is_empty())
            .map(|pattern| Pattern::new(pattern, Flavor::Basic))
            .collect::<Result<_, _>>()?;
        let ordered_blocks = blocks
            .iter()
            .map(|block| OrderedBlock::new(block))
            .collect::<Result<_, _>>()?;
        Ok(PatternSet {
            patterns,
            ordered_blocks,
        })
    }

    /// `_IgnoreMatcher.match_file`: whether the relative path (with a
    /// trailing `/` for a directory) is matched.
    pub fn match_file(&self, rel_path: &str) -> bool {
        path_spec_include(&self.patterns, &normalize_file(rel_path)) == Some(true)
            || self
                .ordered_blocks
                .iter()
                .any(|block| block.git_ignores(rel_path))
    }
}

/// One of the two sets the rules take, as the Python functions receive it.
#[derive(Debug, Clone)]
struct RuleSet {
    /// The patterns, each once, sorted.
    patterns: Vec<String>,
    /// The set compiled, or why pathspec would raise on it; Python raises
    /// only once a check reaches the set, and so does this.
    compiled: Result<PatternSet, PatternError>,
}

impl RuleSet {
    /// `None` for an empty set, which the Python checks skip (`if
    /// exclude_paths and ...`).
    fn new<I, S>(patterns: I) -> Option<RuleSet>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let unique: BTreeSet<String> = patterns
            .into_iter()
            .map(|pattern| pattern.as_ref().to_owned())
            .collect();
        if unique.is_empty() {
            return None;
        }
        let patterns: Vec<String> = unique.into_iter().collect();
        Some(RuleSet {
            compiled: PatternSet::new(&patterns),
            patterns,
        })
    }
}

fn matches(set: Option<&RuleSet>, rel_path: &str) -> Result<bool, PatternError> {
    match set {
        None => Ok(false),
        Some(set) => match &set.compiled {
            Ok(compiled) => Ok(compiled.match_file(rel_path)),
            Err(error) => Err(error.clone()),
        },
    }
}

/// The rules over one exclude set and one unignore set: the `exclude_paths`
/// and `unignore_paths` the path_utils functions take.
#[derive(Debug, Clone)]
pub struct IgnoreRules {
    exclude: Option<RuleSet>,
    unignore: Option<RuleSet>,
}

impl IgnoreRules {
    pub fn new<E, U, S, T>(exclude: E, unignore: U) -> IgnoreRules
    where
        E: IntoIterator<Item = S>,
        U: IntoIterator<Item = T>,
        S: AsRef<str>,
        T: AsRef<str>,
    {
        IgnoreRules {
            exclude: RuleSet::new(exclude),
            unignore: RuleSet::new(unignore),
        }
    }

    /// `matches_ignore_patterns(rel_path, exclude_paths)`.
    pub fn matches_exclude(&self, rel_path: &str) -> Result<bool, PatternError> {
        matches(self.exclude.as_ref(), rel_path)
    }

    /// `matches_ignore_patterns(rel_path, unignore_paths)`.
    pub fn matches_unignore(&self, rel_path: &str) -> Result<bool, PatternError> {
        matches(self.unignore.as_ref(), rel_path)
    }

    fn unignore_patterns(&self) -> &[String] {
        self.unignore.as_ref().map_or(&[], |set| &set.patterns)
    }

    /// `should_keep_dir(dirname, dir_prefix, ...)`: whether a walk descends
    /// into the directory `dirname` inside `dir_prefix` (`""` at the root,
    /// else ending in `/`).
    pub fn should_keep_dir(&self, dirname: &str, dir_prefix: &str) -> Result<bool, PatternError> {
        let rel_dir = format!("{dir_prefix}{dirname}");
        // An explicit exclude is never rescued, so the subtree goes.
        if self.matches_exclude(&format!("{rel_dir}/"))? {
            return Ok(false);
        }
        if VCS_METADATA_DIRS.contains(&dirname) {
            return self.unignore_names_vcs_dir(&rel_dir);
        }
        if !BUILTIN_IGNORED_DIRS.contains(&dirname) {
            return Ok(true);
        }
        // Cargo's src/bin/ holds first-party binaries.
        if dirname == DIR_BIN
            && dir_prefix.trim_end_matches(SLASH).rsplit(SLASH).next() == Some(DIR_SRC)
        {
            return Ok(true);
        }
        Ok(self.unignore_could_match_within(&rel_dir))
    }

    /// `should_skip_rel_file(rel_path, dir_parts, ...)`.
    pub fn should_skip_rel_file<S: AsRef<str>>(
        &self,
        rel_path: &str,
        dir_parts: &[S],
    ) -> Result<bool, PatternError> {
        let filename = last_part(rel_path);
        if is_unconditionally_ignored_filename(filename) {
            return Ok(true);
        }
        if self.matches_exclude(rel_path)? {
            return Ok(true);
        }
        if self.has_unrescued_vcs_part(dir_parts)? {
            return Ok(true);
        }
        if is_ignored_filename(filename) {
            return Ok(!self.unignore_names_this_file(rel_path));
        }
        // An unignore rescues only built-in ignores, never an exclude.
        if self.matches_unignore(rel_path)? {
            return Ok(false);
        }
        Ok(has_ignored_dir_part(dir_parts))
    }

    /// `should_skip_path(path, repo_path, ..., is_file)` for the path's
    /// relative POSIX spelling (`path.relative_to(repo_path).as_posix()`),
    /// once it is known to resolve inside the root: the containment check
    /// reads the file system and stays with the caller.
    pub fn should_skip_rel_path(
        &self,
        rel_path: &str,
        is_file: bool,
    ) -> Result<bool, PatternError> {
        let name = last_part(rel_path);
        if is_file && is_unconditionally_ignored_filename(name) {
            return Ok(true);
        }
        let match_path: Cow<'_, str> = if is_file {
            Cow::Borrowed(rel_path)
        } else {
            Cow::Owned(format!("{rel_path}/"))
        };
        if self.matches_exclude(&match_path)? {
            return Ok(true);
        }
        let parts = path_parts(rel_path);
        let dir_parts = if is_file {
            &parts[..parts.len().saturating_sub(1)]
        } else {
            &parts[..]
        };
        if let Some(verdict) = self.vcs_metadata_verdict(dir_parts, rel_path, is_file)? {
            return Ok(verdict);
        }
        if is_file && is_ignored_filename(name) {
            return Ok(!self.unignore_names_this_file(rel_path));
        }
        if self.unignore_rescues(&match_path, rel_path, is_file)? {
            return Ok(false);
        }
        Ok(has_ignored_dir_part(dir_parts))
    }

    /// `_vcs_metadata_verdict`: whether the version-control directories on
    /// the path decide it, and how.
    fn vcs_metadata_verdict(
        &self,
        dir_parts: &[&str],
        rel_path: &str,
        is_file: bool,
    ) -> Result<Option<bool>, PatternError> {
        if self.has_unrescued_vcs_part(dir_parts)? {
            return Ok(Some(true));
        }
        if !is_file
            && dir_parts
                .iter()
                .any(|part| VCS_METADATA_DIRS.contains(part))
            && self.unignore_names_vcs_dir(rel_path)?
        {
            return Ok(Some(false));
        }
        Ok(None)
    }

    /// `_unignore_rescues`.
    fn unignore_rescues(
        &self,
        match_path: &str,
        rel_path: &str,
        is_file: bool,
    ) -> Result<bool, PatternError> {
        if self.unignore.is_none() {
            return Ok(false);
        }
        if self.matches_unignore(match_path)? {
            return Ok(true);
        }
        Ok(!is_file && self.unignore_could_match_within(rel_path))
    }

    /// `has_unrescued_vcs_part(dir_parts, unignore_paths)`.
    pub fn has_unrescued_vcs_part<S: AsRef<str>>(
        &self,
        dir_parts: &[S],
    ) -> Result<bool, PatternError> {
        for (index, part) in dir_parts.iter().enumerate() {
            if VCS_METADATA_DIRS.contains(&part.as_ref()) {
                let rel_dir = dir_parts[..=index]
                    .iter()
                    .map(AsRef::as_ref)
                    .collect::<Vec<&str>>()
                    .join("/");
                if !self.unignore_names_vcs_dir(&rel_dir)? {
                    return Ok(true);
                }
            }
        }
        Ok(false)
    }

    /// `unignore_names_this_file(rel_path, unignore_paths)`.
    pub fn unignore_names_this_file(&self, rel_path: &str) -> bool {
        unignore_names_this_file(rel_path, self.unignore_patterns())
    }

    /// Whether any unignore pattern could match the directory or something
    /// beneath it: `any(unignore_could_match_within(u, rel_dir) for u in
    /// unignore_paths)`.
    pub fn unignore_could_match_within(&self, rel_dir: &str) -> bool {
        self.unignore_patterns()
            .iter()
            .any(|pattern| unignore_could_match_within(pattern, rel_dir))
    }

    /// `unignore_names_vcs_dir(rel_dir, unignore_paths)`: whether an
    /// unignore spells out a path through this directory.
    pub fn unignore_names_vcs_dir(&self, rel_dir: &str) -> Result<bool, PatternError> {
        let dir_parts: Vec<&str> = rel_dir.split(SLASH).collect();
        for pattern in self.unignore_patterns() {
            let bare = pattern.trim_matches(SLASH);
            if !bare.contains(SLASH) {
                // `!.git/` names a `.git` at any depth, `!/.git/` the root's.
                let anchored = pattern.starts_with(SLASH);
                if dir_parts.last() == Some(&bare) && (!anchored || dir_parts.len() == 1) {
                    return Ok(true);
                }
                continue;
            }
            let pattern_parts: Vec<&str> = bare.split(SLASH).collect();
            if pattern_reaches_dir(&pattern_parts, &dir_parts)? {
                return Ok(true);
            }
        }
        Ok(false)
    }
}

/// The last `/`-separated part (`rel_path.rsplit("/", 1)[-1]`).
fn last_part(rel_path: &str) -> &str {
    rel_path.rsplit(SLASH).next().unwrap_or(rel_path)
}

/// `PurePosixPath(rel_path).parts` for a relative path: its names, without
/// the empty and `.` ones pathlib drops.
fn path_parts(rel_path: &str) -> Vec<&str> {
    rel_path
        .split(SLASH)
        .filter(|part| !part.is_empty() && *part != ".")
        .collect()
}

/// `is_ignored_filename(name)`: a generated file, by its ending.
pub fn is_ignored_filename(name: &str) -> bool {
    UNCONDITIONAL_IGNORE_SUFFIXES
        .iter()
        .chain(RESCUABLE_IGNORE_SUFFIXES)
        .any(|suffix| name.ends_with(suffix))
}

/// `is_unconditionally_ignored_filename(name)`: compiled output and editor
/// droppings, which nothing rescues.
pub fn is_unconditionally_ignored_filename(name: &str) -> bool {
    UNCONDITIONAL_IGNORE_SUFFIXES
        .iter()
        .any(|suffix| name.ends_with(suffix))
}

/// `has_ignored_dir_part(dir_parts)`: a built-in directory on the path,
/// `bin` directly under `src` excepted.
pub fn has_ignored_dir_part<S: AsRef<str>>(dir_parts: &[S]) -> bool {
    dir_parts.iter().enumerate().any(|(index, part)| {
        let part = part.as_ref();
        BUILTIN_IGNORED_DIRS.contains(&part)
            && !(part == DIR_BIN && index > 0 && dir_parts[index - 1].as_ref() == DIR_SRC)
    })
}

/// `unignore_could_match_within(pattern, rel_dir)`: whether the unignore
/// could match the directory or anything beneath it, read from the pattern's
/// literal head.
pub fn unignore_could_match_within(pattern: &str, rel_dir: &str) -> bool {
    if !pattern.trim_end_matches(SLASH).contains(SLASH) {
        // Slash-free patterns match at any depth.
        return true;
    }
    let head = match pattern.find(GLOB_MAGIC) {
        // The glob may complete the last segment; keep whole segments.
        Some(at) => {
            let before = &pattern[..at];
            before.rsplit_once(SLASH).map_or(before, |(head, _)| head)
        }
        None => pattern,
    };
    let head = head.trim_matches(SLASH);
    head.is_empty()
        || head == rel_dir
        || head
            .strip_prefix(rel_dir)
            .is_some_and(|rest| rest.starts_with(SLASH))
        || rel_dir
            .strip_prefix(head)
            .is_some_and(|rest| rest.starts_with(SLASH))
}

/// `unignore_names_this_file(rel_path, patterns)`: whether a pattern, with
/// no glob, names this file by its path or its bare name.
pub fn unignore_names_this_file<S: AsRef<str>>(rel_path: &str, patterns: &[S]) -> bool {
    let filename = last_part(rel_path);
    patterns.iter().any(|pattern| {
        let candidate = python_str::strip(pattern.as_ref()).trim_end_matches(SLASH);
        if candidate.is_empty() || candidate.contains(GLOB_MAGIC) {
            return false;
        }
        let candidate = candidate.trim_start_matches(SLASH);
        candidate == rel_path || candidate == filename
    })
}

/// `fnmatch.fnmatchcase(name, pattern)`.
pub fn fnmatchcase(name: &str, pattern: &str) -> Result<bool, PatternError> {
    fnmatch::fnmatchcase(name, pattern).map_err(|error| PatternError::Unexpected {
        pattern: pattern.to_owned(),
        reason: format!("fnmatch: {error:?}"),
    })
}

/// `_pattern_reaches_dir`: whether the pattern's leading segments match the
/// whole directory path, any segment a glob or `**`, except a metadata
/// directory ending the path, which must be spelled out. One directory
/// segment at a time over the set of pattern positions that can stand
/// there, so no position pair is visited twice.
fn pattern_reaches_dir(pattern_parts: &[&str], dir_parts: &[&str]) -> Result<bool, PatternError> {
    let last = dir_parts.len().saturating_sub(1);
    let mut positions = past_empty_double_stars(BTreeSet::from([0]), pattern_parts);
    for (index, part) in dir_parts.iter().enumerate() {
        let literal = index == last && VCS_METADATA_DIRS.contains(part);
        let advanced = positions_after(&positions, pattern_parts, part, literal)?;
        positions = past_empty_double_stars(advanced, pattern_parts);
        if positions.is_empty() {
            return Ok(false);
        }
    }
    Ok(true)
}

/// `_positions_after`: the pattern positions that can follow `positions`
/// once the directory segment `part` is consumed.
fn positions_after(
    positions: &BTreeSet<usize>,
    pattern_parts: &[&str],
    part: &str,
    literal: bool,
) -> Result<BTreeSet<usize>, PatternError> {
    let mut advanced = BTreeSet::new();
    for &position in positions {
        let Some(&segment) = pattern_parts.get(position) else {
            continue;
        };
        if segment == DOUBLE_STAR {
            if !literal {
                advanced.insert(position);
            }
            continue;
        }
        let consumed = if literal {
            segment == part
        } else {
            fnmatchcase(part, segment)?
        };
        if consumed {
            advanced.insert(position + 1);
        }
    }
    Ok(advanced)
}

/// `_past_empty_double_stars`: a `**` may match no segment, so the position
/// after it is reachable wherever it is.
fn past_empty_double_stars(positions: BTreeSet<usize>, pattern_parts: &[&str]) -> BTreeSet<usize> {
    let mut pending: Vec<usize> = positions.iter().copied().collect();
    let mut reachable = positions;
    while let Some(position) = pending.pop() {
        if pattern_parts.get(position) == Some(&DOUBLE_STAR) && reachable.insert(position + 1) {
            pending.push(position + 1);
        }
    }
    reachable
}
