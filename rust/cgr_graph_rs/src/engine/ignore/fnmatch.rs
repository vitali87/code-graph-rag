//! Python's `fnmatch.fnmatchcase`, which `_pattern_reaches_dir` matches a
//! directory name against one segment of an unignore pattern with.
//!
//! `fnmatch.translate` (Python 3.12 and 3.13 agree) turns the pattern into
//! a regex: `*` any run of characters, `?` any one, `[...]` a set with
//! fnmatch's own reading of `!`, `-` and backslashes, everything else
//! itself; no `{}` alternation and no escaping. Every piece but `*` stands
//! for exactly one character, so the whole name matches by the usual
//! wildcard walk, which accepts exactly what Python's regex accepts.

use super::python_str::{Bracket, BracketError};

#[derive(Debug, Clone, PartialEq, Eq)]
enum Piece {
    /// `*`, any run of characters, newlines and `/` included.
    Star,
    /// `?`, and `[!]` once its stuff is `!` alone: any one character.
    Any,
    /// A set that came out empty, `(?!)`: matches nothing.
    Never,
    Set(Bracket),
    Char(char),
}

impl Piece {
    fn matches(&self, c: char) -> bool {
        match self {
            Piece::Star | Piece::Any => true,
            Piece::Never => false,
            Piece::Set(bracket) => bracket.contains(c),
            Piece::Char(one) => *one == c,
        }
    }
}

/// `fnmatch.fnmatchcase(name, pattern)`.
pub(crate) fn fnmatchcase(name: &str, pattern: &str) -> Result<bool, BracketError> {
    let pieces = translate(pattern)?;
    let name: Vec<char> = name.chars().collect();
    Ok(whole_match(&pieces, &name))
}

/// `fnmatch._translate`, kept as pieces rather than regex text.
fn translate(pattern: &str) -> Result<Vec<Piece>, BracketError> {
    let pat: Vec<char> = pattern.chars().collect();
    let n = pat.len();
    let mut out: Vec<Piece> = Vec::new();
    let mut i = 0;
    while i < n {
        let c = pat[i];
        i += 1;
        match c {
            '*' => {
                // Consecutive `*` are one.
                if out.last() != Some(&Piece::Star) {
                    out.push(Piece::Star);
                }
            }
            '?' => out.push(Piece::Any),
            '[' => {
                let mut j = i;
                if j < n && pat[j] == '!' {
                    j += 1;
                }
                if j < n && pat[j] == ']' {
                    j += 1;
                }
                while j < n && pat[j] != ']' {
                    j += 1;
                }
                if j >= n {
                    out.push(Piece::Char('['));
                    continue;
                }
                let stuff = set_stuff(&pat, &mut i, j);
                i = j + 1;
                if stuff.is_empty() {
                    out.push(Piece::Never);
                } else if stuff == "!" {
                    out.push(Piece::Any);
                } else {
                    let stuff = if let Some(rest) = stuff.strip_prefix('!') {
                        format!("^{rest}")
                    } else if stuff.starts_with(['^', '[']) {
                        format!("\\{stuff}")
                    } else {
                        stuff
                    };
                    out.push(Piece::Set(Bracket::parse(&format!("[{stuff}]"))?));
                }
            }
            other => out.push(Piece::Char(other)),
        }
    }
    Ok(out)
}

/// The inside of the set `pat[*i..j]` as fnmatch writes it for `re`:
/// backslashes doubled, ranges that run backwards dropped, a `-` that is not
/// a range escaped, and `&`, `~` and `|` escaped. Moves `*i` as Python's
/// loop does.
fn set_stuff(pat: &[char], i: &mut usize, j: usize) -> String {
    let content = &pat[*i..j];
    let stuff = if !content.contains(&'-') {
        double_backslashes(content)
    } else {
        let mut chunks: Vec<Vec<char>> = Vec::new();
        let mut k = if pat.get(*i) == Some(&'!') {
            *i + 2
        } else {
            *i + 1
        };
        // `pat.find('-', k, j)` until there is none.
        while let Some(found) = (k..j).find(|&at| pat[at] == '-') {
            chunks.push(pat[*i..found].to_vec());
            *i = found + 1;
            k = found + 3;
        }
        let chunk = pat[*i..j].to_vec();
        if !chunk.is_empty() {
            chunks.push(chunk);
        } else if let Some(last) = chunks.last_mut() {
            last.push('-');
        }
        // Remove empty ranges -- invalid in RE.
        for k in (1..chunks.len()).rev() {
            let backwards = match (chunks[k - 1].last(), chunks[k].first()) {
                (Some(low), Some(high)) => low > high,
                _ => false,
            };
            if backwards {
                let next = chunks.remove(k);
                let previous = &mut chunks[k - 1];
                previous.pop();
                previous.extend_from_slice(next.get(1..).unwrap_or_default());
            }
        }
        chunks
            .iter()
            .map(|chunk| {
                let mut escaped = String::new();
                for &c in chunk {
                    match c {
                        '\\' => escaped.push_str("\\\\"),
                        '-' => escaped.push_str("\\-"),
                        other => escaped.push(other),
                    }
                }
                escaped
            })
            .collect::<Vec<_>>()
            .join("-")
    };
    // `re.sub(r'([&~|])', r'\\\1', stuff)`.
    let mut out = String::with_capacity(stuff.len());
    for c in stuff.chars() {
        if matches!(c, '&' | '~' | '|') {
            out.push('\\');
        }
        out.push(c);
    }
    out
}

fn double_backslashes(chars: &[char]) -> String {
    let mut out = String::new();
    for &c in chars {
        if c == '\\' {
            out.push_str("\\\\");
        } else {
            out.push(c);
        }
    }
    out
}

/// Whether the pieces match all of `name`: the wildcard walk, going back to
/// the last `*` on a mismatch.
fn whole_match(pieces: &[Piece], name: &[char]) -> bool {
    let (mut p, mut n) = (0, 0);
    let mut last_star: Option<(usize, usize)> = None;
    while n < name.len() {
        match pieces.get(p) {
            Some(Piece::Star) => {
                last_star = Some((p, n));
                p += 1;
            }
            Some(piece) if piece.matches(name[n]) => {
                p += 1;
                n += 1;
            }
            _ => match last_star {
                Some((star, from)) => {
                    // The `*` takes one more character.
                    last_star = Some((star, from + 1));
                    p = star + 1;
                    n = from + 1;
                }
                None => return false,
            },
        }
    }
    pieces[p..].iter().all(|piece| *piece == Piece::Star)
}
