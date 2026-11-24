//! What Python does with the strings the ignore rules handle, where Rust
//! would do something else: which characters `str.strip()` removes, and how
//! `re` reads a bracket expression.

use std::fmt::Write;

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
