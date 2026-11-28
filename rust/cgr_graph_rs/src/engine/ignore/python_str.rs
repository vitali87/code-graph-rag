//! What Python does with the strings the ignore rules and the Go readers
//! handle, where Rust would do something else: the code points a str may
//! hold, the str it builds for a file name, which characters `str.strip()`
//! removes and `str.split()` splits at, where `str.splitlines()` ends a
//! line, and how `re` reads a bracket expression.

use std::borrow::Cow;
use std::ffi::OsStr;
use std::fmt::Write;
use std::ops::RangeInclusive;

/// The lone surrogates a Python str may hold and a Rust string cannot.
const SURROGATES: RangeInclusive<u32> = 0xD800..=0xDFFF;
/// The private-use character the first surrogate stands as; the rest
/// follow in order, up to U+10FFFF.
const SURROGATE_STAND_IN: u32 = 0x10_F800;

/// A Python str's code points as the rules hold them: one char each, a
/// lone surrogate U+D800..U+DFFF as the private-use char U+10F800 + (its
/// code point - U+D800), every other code point as itself. `None` for a
/// number no str holds.
///
/// On POSIX a file name with a byte that is not UTF-8 reaches Python as a
/// str with that byte as a surrogate (`os.fsdecode`'s surrogateescape:
/// `b"\xff"` is `"\udcff"`). Python's walk asks the ignore rules about such
/// names before it reports them as undecodable, and a pattern may hold one
/// too, so every string crossing from Python goes through here. One char
/// per code point, `?`, `*` and `[...]` read a surrogate as one character,
/// as `re` and `fnmatch` do; a stand-in, like a surrogate, is no space and
/// has no case. Two rare readings differ from Python's: a bracket range
/// from a surrogate to a character that is not one, or back (the
/// surrogates sort before U+E000, their stand-ins after every other
/// character), and a real U+10F800..U+10FFFF, the end of plane 16's private
/// use area, which reads as the surrogate it stands for.
pub fn string_from_code_points<I>(code_points: I) -> Option<String>
where
    I: IntoIterator<Item = u32>,
{
    code_points
        .into_iter()
        .map(|code_point| {
            if SURROGATES.contains(&code_point) {
                Some(stand_in(code_point))
            } else {
                char::from_u32(code_point)
            }
        })
        .collect()
}

/// The private-use char a lone surrogate stands as.
fn stand_in(surrogate: u32) -> char {
    char::from_u32(SURROGATE_STAND_IN + (surrogate - SURROGATES.start()))
        .unwrap_or(char::REPLACEMENT_CHARACTER)
}

/// A file name as the rules hold it: `string_from_code_points` of the str
/// Python's walk builds for the same name, so the walk asks the rules about
/// a name that is not UTF-8 (or, on Windows, not UTF-16) exactly as Python
/// asks them. A valid name is itself.
///
/// On POSIX Python decodes a name's bytes with surrogateescape: each byte
/// `b` that is not part of valid UTF-8 becomes the surrogate U+DC00 + b,
/// whatever splits the invalid run into sequences, since each byte is
/// escaped alone. Its stand-in is U+10FC00 + b. On Windows a name is UTF-16,
/// and an unpaired surrogate unit is kept as that surrogate.
pub fn string_from_os_str(name: &OsStr) -> Cow<'_, str> {
    match name.to_str() {
        Some(text) => Cow::Borrowed(text),
        None => Cow::Owned(undecodable_name(name)),
    }
}

#[cfg(unix)]
fn undecodable_name(name: &OsStr) -> String {
    use std::os::unix::ffi::OsStrExt;

    /// surrogateescape's first surrogate: byte `b` becomes U+DC00 + b.
    const ESCAPED_BYTE: u32 = 0xDC00;
    let mut text = String::with_capacity(name.len() * 4);
    for chunk in name.as_bytes().utf8_chunks() {
        text.push_str(chunk.valid());
        for &byte in chunk.invalid() {
            text.push(stand_in(ESCAPED_BYTE + u32::from(byte)));
        }
    }
    text
}

#[cfg(windows)]
fn undecodable_name(name: &OsStr) -> String {
    use std::os::windows::ffi::OsStrExt;

    char::decode_utf16(name.encode_wide())
        .map(|unit| unit.unwrap_or_else(|lone| stand_in(u32::from(lone.unpaired_surrogate()))))
        .collect()
}

#[cfg(not(any(unix, windows)))]
fn undecodable_name(name: &OsStr) -> String {
    name.to_string_lossy().into_owned()
}

/// Python's `str.isspace()` for one character.
///
/// Rust's `char::is_whitespace` is Unicode's White_Space property. Python
/// also counts the four information separators U+001C to U+001F, whose
/// bidirectional class is B or S.
pub(crate) fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// Python's `s.strip()`.
pub(crate) fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// Python's `s.rstrip()`.
pub(crate) fn rstrip(s: &str) -> &str {
    s.trim_end_matches(is_space)
}

/// Python's `s.split()`: the runs of characters between spaces.
pub(crate) fn split(s: &str) -> impl Iterator<Item = &str> {
    s.split(is_space).filter(|part| !part.is_empty())
}

/// Whether `str.splitlines()` ends a line at `c`: the line feed, the
/// carriage return, the line and form feeds of the C0 range, the file,
/// group and record separators, NEL and the line and paragraph separators.
fn is_line_break(c: char) -> bool {
    matches!(
        c,
        '\n' | '\r'
            | '\u{b}'
            | '\u{c}'
            | '\u{1c}'
            | '\u{1d}'
            | '\u{1e}'
            | '\u{85}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

/// Python's `s.splitlines()`: each line without its break, `\r\n` one
/// break, and no empty line after a final break.
pub(crate) fn splitlines(s: &str) -> impl Iterator<Item = &str> {
    let mut rest = s;
    std::iter::from_fn(move || {
        if rest.is_empty() {
            return None;
        }
        let Some((end, c)) = rest.char_indices().find(|&(_, c)| is_line_break(c)) else {
            return Some(std::mem::take(&mut rest));
        };
        let line = &rest[..end];
        let after = &rest[end + c.len_utf8()..];
        rest = match c {
            '\r' => after.strip_prefix('\n').unwrap_or(after),
            _ => after,
        };
        Some(line)
    })
}

/// Why a bracket expression is refused.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum BracketError {
    /// Python's `re.error("bad character range ...")`: a range that ends
    /// before it starts, such as `[z-a]`.
    BadRange,
    /// Something neither pathspec nor `fnmatch` ever hands `re`: an escape
    /// Python reads as a class or a code (`\d`, `\x41`), or a set that does
    /// not end at the expression's last `]`.
    Unexpected,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Member {
    Char(char),
    Range(char, char),
}

/// A bracket expression as Python's `re` reads it (`re._parser`, the
/// `elif this == "["` branch of `_parse`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct Bracket {
    negated: bool,
    members: Vec<Member>,
}

impl Bracket {
    /// `expr`, from its `[` to its closing `]`, as `re` parses it: a `^`
    /// first negates, a `]` first is a member, `\` escapes the next
    /// character, and `a-z` is a range unless the `-` comes last.
    pub(crate) fn parse(expr: &str) -> Result<Bracket, BracketError> {
        let mut chars = expr.chars().peekable();
        if chars.next() != Some('[') {
            return Err(BracketError::Unexpected);
        }
        let negated = chars.next_if_eq(&'^').is_some();
        let mut members = Vec::new();
        loop {
            let this = chars.next().ok_or(BracketError::Unexpected)?;
            if this == ']' && !members.is_empty() {
                break;
            }
            let low = if this == '\\' {
                escaped(chars.next())?
            } else {
                this
            };
            if chars.next_if_eq(&'-').is_none() {
                members.push(Member::Char(low));
                continue;
            }
            let that = chars.next().ok_or(BracketError::Unexpected)?;
            if that == ']' {
                members.push(Member::Char(low));
                members.push(Member::Char('-'));
                break;
            }
            let high = if that == '\\' {
                escaped(chars.next())?
            } else {
                that
            };
            if high < low {
                return Err(BracketError::BadRange);
            }
            members.push(Member::Range(low, high));
        }
        if chars.next().is_some() {
            return Err(BracketError::Unexpected);
        }
        Ok(Bracket { negated, members })
    }

    /// Whether `c` is in the set.
    pub(crate) fn contains(&self, c: char) -> bool {
        let member = self.members.iter().any(|member| match *member {
            Member::Char(one) => one == c,
            Member::Range(low, high) => (low..=high).contains(&c),
        });
        member != self.negated
    }

    /// The same set in the `regex` crate's syntax, every member spelled as
    /// a code point so no character is read as syntax.
    pub(crate) fn to_regex(&self) -> String {
        let mut out = String::from("[");
        if self.negated {
            out.push('^');
        }
        for member in &self.members {
            match *member {
                Member::Char(one) => push_code_point(&mut out, one),
                Member::Range(low, high) => {
                    push_code_point(&mut out, low);
                    out.push('-');
                    push_code_point(&mut out, high);
                }
            }
        }
        out.push(']');
        out
    }
}

/// The character `\c` stands for inside a set. Python reads `\` before a
/// letter or a digit as a class or a code; pathspec doubles every `\` and
/// `fnmatch` escapes only punctuation, so neither gives `re` one.
fn escaped(c: Option<char>) -> Result<char, BracketError> {
    match c {
        Some(c) if !c.is_ascii_alphanumeric() => Ok(c),
        _ => Err(BracketError::Unexpected),
    }
}

fn push_code_point(out: &mut String, c: char) {
    // Writing to a String cannot fail.
    let _ = write!(out, "\\x{{{:X}}}", u32::from(c));
}
