//! pathspec's gitignore patterns, ported onto the `regex` crate.
//!
//! cgr matches ignore patterns with pathspec 1.1.1, the version `uv.lock`
//! pins. Each pattern becomes a regular expression
//! (`pathspec/patterns/gitignore/basic.py` and `spec.py`), searched for in
//! the path relative to the root, with a trailing `/` for a directory. This
//! builds the same expression for the `regex` crate: pathspec's use no
//! look-around and no back-reference, and the two engines report the same
//! match and the same groups for them (leftmost first). Two pieces of
//! Python's `re` differ and are spelled out: a bracket expression is read as
//! Python reads it (`python_str::Bracket`), and `$`, which in Python also
//! matches before a final newline, becomes `(?P<ps_nl>\n)?\z`, the newline
//! it consumes kept out of the match's end.

use std::borrow::Cow;

use regex::{Regex, RegexBuilder};

use super::python_str::{rstrip, Bracket, BracketError};
use super::PatternError;

/// The largest compiled program one pattern may need, the `regex` crate's
/// own default. pathspec has no such limit; a line this large is refused
/// here rather than compiled.
pub const REGEX_SIZE_LIMIT: usize = 10 * (1 << 20);
/// The lazy DFA's cache for one pattern, the `regex` crate's own default.
pub const REGEX_DFA_SIZE_LIMIT: usize = 2 * (1 << 20);

/// pathspec's directory marker (`_DIR_MARK_CG` in `spec.py`): the `/` after
/// a directory the pattern matched ABOVE the path, rather than the path.
const DIR_MARK: &str = "(?P<ps_d>/)";
/// Python's `$`: the end, or just before a newline that ends the text.
const END: &str = "(?P<ps_nl>\\n)?\\z";
/// The group named in `DIR_MARK` (`cs.PATHSPEC_DIR_MARK_GROUP`).
const DIR_MARK_GROUP: &str = "ps_d";
/// The group `END` puts a final newline in.
const NEWLINE_GROUP: &str = "ps_nl";
const DOUBLE_STAR: &str = "**";

/// Which of pathspec's two gitignore translations a pattern goes through.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Flavor {
    /// `GitIgnoreBasicPattern`, registered as "gitignore"
    /// (`cs.GITWILDMATCH_STYLE`): `PathSpec.from_lines("gitignore", ...)`,
    /// which compiles the exclude and unignore sets.
    Basic,
    /// `GitIgnoreSpecPattern`, what `GitIgnoreSpec.from_lines` compiles:
    /// the lines of an ordered `.gitignore` block. It marks a directory
    /// match with `ps_d`, and drops a pattern whose `[` never closes.
    Spec,
}

/// One compiled pattern: pathspec's `RegexPattern`.
#[derive(Debug, Clone)]
pub struct Pattern {
    /// `include`: true for an exclude, false for a `!` line, `None` for a
    /// line that does nothing (blank, `#`, and a few more).
    include: Option<bool>,
    regex: Option<Regex>,
    /// Whether the regex is pathspec's `(?P<ps_d>/)`, which it gives `*/`
    /// and `**/` (`cs.PATHSPEC_ANY_DIR_REGEX`).
    any_dir: bool,
}

/// Where a pattern matched: `RegexPattern.match_file`'s match.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Found {
    /// Where the `ps_d` directory marker matched, if it took part.
    pub(crate) dir_mark: Option<usize>,
    /// The match's end, as Python reports it.
    pub(crate) end: usize,
}

impl Pattern {
    /// `pattern` compiled as pathspec compiles it: `RegexPattern(pattern)`
    /// with `pattern_to_regex` of the flavor's class, then `re.compile`.
    pub fn new(pattern: &str, flavor: Flavor) -> Result<Pattern, PatternError> {
        match pattern_to_regex(pattern, flavor)? {
            Translation::NoOp => Ok(Pattern {
                include: None,
                regex: None,
                any_dir: false,
            }),
            Translation::Regex { source, include } => Ok(Pattern {
                include: Some(include),
                any_dir: flavor == Flavor::Spec && source == DIR_MARK,
                regex: Some(compile(&source, pattern)?),
            }),
        }
    }

    pub fn include(&self) -> Option<bool> {
        self.include
    }

    /// Whether the pattern matches `file` (`match_file(file) is not None`).
    pub fn is_match(&self, file: &str) -> bool {
        self.regex
            .as_ref()
            .is_some_and(|regex| regex.is_match(file))
    }

    /// `RegexPattern.match_file`: the pattern's regex searched for in
    /// `file`, as it is, with no normalisation.
    pub(crate) fn search(&self, file: &str) -> Option<Found> {
        let captures = self.regex.as_ref()?.captures(file)?;
        let whole = captures.get(0)?;
        let newline = captures.name(NEWLINE_GROUP).is_some();
        Some(Found {
            dir_mark: captures.name(DIR_MARK_GROUP).map(|mark| mark.start()),
            end: whole.end() - usize::from(newline),
        })
    }

    pub(crate) fn is_any_dir(&self) -> bool {
        self.any_dir
    }
}

enum Translation {
    NoOp,
    Regex { source: String, include: bool },
}

/// Why a segment could not be translated.
enum SegmentError {
    /// `_RangeError`: a `[` that never closes, under `Flavor::Spec`.
    Range,
    /// A `\` that ends the segment, with nothing to escape.
    TrailingEscape,
}

/// `pattern_to_regex` of `GitIgnoreBasicPattern` or `GitIgnoreSpecPattern`,
/// with the regex in the `regex` crate's syntax.
fn pattern_to_regex(pattern: &str, flavor: Flavor) -> Result<Translation, PatternError> {
    let invalid = || PatternError::Invalid {
        pattern: pattern.to_owned(),
    };
    // Trailing whitespace goes, unless the pattern ends in an escaped space.
    let mut rest = if pattern.ends_with("\\ ") {
        pattern
    } else {
        rstrip(pattern)
    };
    if rest.is_empty() || rest.starts_with('#') {
        return Ok(Translation::NoOp);
    }
    if flavor == Flavor::Spec && rest == "/" {
        return Ok(Translation::NoOp);
    }
    let include = match rest.strip_prefix('!') {
        Some(negated) => {
            rest = negated;
            false
        }
        None => true,
    };
    let mut segments: Vec<&str> = rest.split('/').collect();
    let mut is_dir_pattern = segments.last().is_some_and(|last| last.is_empty());
    if flavor == Flavor::Basic && rest == "/" {
        // Basic reads a lone `/` as `**`, not as `**/`.
        is_dir_pattern = false;
    }

    // `__normalize_segments`.
    if segments[0].is_empty() {
        // A leading `/` anchors the pattern to the root.
        segments.remove(0);
    } else if segments.len() == 1 || (segments.len() == 2 && segments[1].is_empty()) {
        // One segment, with or without a trailing `/`, matches at any depth.
        if segments[0] != DOUBLE_STAR {
            segments.insert(0, DOUBLE_STAR);
        }
    }
    let Some(last) = segments.last_mut() else {
        // "Pattern normalized to nothing": a lone `!`.
        return Err(invalid());
    };
    if last.is_empty() {
        *last = DOUBLE_STAR;
    }
    for index in (1..segments.len()).rev() {
        if segments[index - 1] == DOUBLE_STAR && segments[index] == DOUBLE_STAR {
            segments.remove(index);
        }
    }
    let every_dir = match flavor {
        Flavor::Basic => "/",
        Flavor::Spec => DIR_MARK,
    };
    let special = match segments.as_slice() {
        [DOUBLE_STAR] if is_dir_pattern => Some(every_dir),
        [DOUBLE_STAR] | [DOUBLE_STAR, "*"] => Some("."),
        [DOUBLE_STAR, "*", DOUBLE_STAR] if is_dir_pattern => Some(every_dir),
        [DOUBLE_STAR, "*", DOUBLE_STAR] => Some("/"),
        _ => None,
    };
    if let Some(source) = special {
        return Ok(Translation::Regex {
            source: source.to_owned(),
            include,
        });
    }

    let mut refused = None;
    let source = match translate_segments(&segments, is_dir_pattern, flavor, &mut refused) {
        Ok(source) => source,
        // Git drops a pattern with a `[` that never closes.
        Err(SegmentError::Range) => return Ok(Translation::NoOp),
        Err(SegmentError::TrailingEscape) => return Err(invalid()),
    };
    // `re.compile` refuses the expression only once it is whole, so a
    // pattern Spec drops above never gets that far.
    match refused {
        None => Ok(Translation::Regex { source, include }),
        Some(BracketError::BadRange) => Err(PatternError::BadRange {
            pattern: pattern.to_owned(),
        }),
        Some(BracketError::Unexpected) => Err(PatternError::Unexpected {
            pattern: pattern.to_owned(),
            reason: "a bracket expression Python's re would not read as a set".to_owned(),
        }),
    }
}

/// `__translate_segments` of either class.
fn translate_segments(
    segments: &[&str],
    is_dir_pattern: bool,
    flavor: Flavor,
    refused: &mut Option<BracketError>,
) -> Result<String, SegmentError> {
    let mut out = String::new();
    let mut need_slash = false;
    let end = segments.len() - 1;
    for (index, segment) in segments.iter().enumerate() {
        if *segment == DOUBLE_STAR {
            if index == 0 {
                // Any leading directories.
                out.push_str("^(?:.+/)?");
            } else if index < end {
                // Any directories in between, or none.
                out.push_str("(?:/.+)?");
                need_slash = true;
            } else if flavor == Flavor::Spec && is_dir_pattern {
                out.push_str(DIR_MARK);
            } else {
                // Everything beneath.
                out.push('/');
            }
            continue;
        }
        if index == 0 {
            out.push('^');
        }
        if need_slash {
            out.push('/');
        }
        if *segment == "*" {
            out.push_str("[^/]+");
        } else {
            let raise_range = flavor == Flavor::Spec;
            out.push_str(&segment_glob(segment, raise_range, refused)?);
        }
        if index == end {
            match flavor {
                // `foo/*` matches `foo/a` and `foo/a/`, not `foo/a/b`.
                Flavor::Basic if *segment == "*" => {
                    out.push_str("/?");
                    out.push_str(END);
                }
                // A file, or a directory and everything beneath it.
                Flavor::Basic => {
                    out.push_str("(?:/|");
                    out.push_str(END);
                    out.push(')');
                }
                Flavor::Spec => {
                    out.push_str("(?:");
                    out.push_str(DIR_MARK);
                    out.push('|');
                    out.push_str(END);
                    out.push(')');
                }
            }
        }
        need_slash = true;
    }
    Ok(out)
}

/// `_translate_segment_glob`: one segment's `*`, `?`, `[...]` and `\`
/// escapes. `raise_range` is Spec's `range_error="raise"`: a `[` that never
/// closes fails the pattern; Basic's `"literal"` reads it as a `[`. A set
/// `re` would refuse is noted in `refused`, the first one only, for after
/// the whole pattern is translated.
fn segment_glob(
    segment: &str,
    raise_range: bool,
    refused: &mut Option<BracketError>,
) -> Result<String, SegmentError> {
    let chars: Vec<char> = segment.chars().collect();
    let end = chars.len();
    let mut out = String::new();
    let mut escape = false;
    let mut i = 0;
    while i < end {
        let c = chars[i];
        i += 1;
        if escape {
            escape = false;
            push_literal(&mut out, c);
        } else if c == '\\' {
            escape = true;
        } else if c == '*' {
            out.push_str("[^/]*");
        } else if c == '?' {
            out.push_str("[^/]");
        } else if c == '[' {
            let mut j = i;
            if j < end && (chars[j] == '!' || chars[j] == '^') {
                j += 1;
            }
            if j < end && chars[j] == ']' {
                j += 1;
            }
            while j < end && chars[j] != ']' {
                j += 1;
            }
            if j < end {
                j += 1;
                // The expression pathspec hands `re`: `!` and `^` negate,
                // and each `\` is doubled to stand for itself.
                let mut expr = String::from("[");
                if chars[i] == '!' || chars[i] == '^' {
                    expr.push('^');
                    i += 1;
                }
                for &member in &chars[i..j] {
                    if member == '\\' {
                        expr.push_str("\\\\");
                    } else {
                        expr.push(member);
                    }
                }
                match Bracket::parse(&expr) {
                    Ok(bracket) => out.push_str(&bracket.to_regex()),
                    Err(error) => {
                        refused.get_or_insert(error);
                    }
                }
                i = j;
            } else if raise_range {
                return Err(SegmentError::Range);
            } else {
                out.push_str("\\[");
            }
        } else {
            push_literal(&mut out, c);
        }
    }
    if escape {
        return Err(SegmentError::TrailingEscape);
    }
    Ok(out)
}

fn push_literal(out: &mut String, c: char) {
    let mut buffer = [0u8; 4];
    out.push_str(&regex::escape(c.encode_utf8(&mut buffer)));
}

fn compile(source: &str, pattern: &str) -> Result<Regex, PatternError> {
    RegexBuilder::new(source)
        .size_limit(REGEX_SIZE_LIMIT)
        .dfa_size_limit(REGEX_DFA_SIZE_LIMIT)
        .build()
        .map_err(|error| match error {
            regex::Error::CompiledTooBig(_) => PatternError::TooLarge {
                pattern: pattern.to_owned(),
            },
            other => PatternError::Unexpected {
                pattern: pattern.to_owned(),
                reason: other.to_string(),
            },
        })
}

/// `util.normalize_file`: the path with `/` separators and no leading `/`
/// or `./`, as `PathSpec.match_file` and `GitIgnoreSpec.match_file` read it.
pub(crate) fn normalize_file(file: &str) -> Cow<'_, str> {
    // `NORMALIZE_PATH_SEPS`: `os.sep` where it is not `/`.
    #[cfg(windows)]
    let file: Cow<'_, str> = Cow::Owned(file.replace('\\', "/"));
    #[cfg(not(windows))]
    let file: Cow<'_, str> = Cow::Borrowed(file);
    if let Some(rest) = file.strip_prefix('/') {
        return Cow::Owned(rest.to_owned());
    }
    if let Some(rest) = file.strip_prefix("./") {
        return Cow::Owned(rest.to_owned());
    }
    file
}

/// `PathSpec.match_file` on a normalised path (the simple backend,
/// `SimplePsBackend`): the last pattern that matches decides.
pub(crate) fn path_spec_include(patterns: &[Pattern], file: &str) -> Option<bool> {
    patterns
        .iter()
        .rev()
        .find_map(|pattern| match pattern.include {
            Some(include) if pattern.is_match(file) => Some(include),
            _ => None,
        })
}

/// `GitIgnoreSpec.match_file` on a normalised path (`SimpleGiBackend`,
/// which reads the patterns last first): the last pattern that matches the
/// path itself decides, and one that matches only a directory above it
/// decides when no pattern matches the path itself.
pub(crate) fn git_ignore_spec_include(patterns: &[Pattern], file: &str) -> Option<bool> {
    let mut out = None;
    let mut out_priority = 0;
    for pattern in patterns.iter().rev() {
        let Some(include) = pattern.include else {
            continue;
        };
        let Some(found) = pattern.search(file) else {
            continue;
        };
        let priority = if found.dir_mark.is_some() { 1 } else { 2 };
        if priority > out_priority {
            out = Some(include);
            out_priority = priority;
        }
        if priority == 2 {
            break;
        }
    }
    out
}
