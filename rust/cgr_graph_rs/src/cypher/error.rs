//! Why a parse returned no statement: a syntax error the Python parser
//! raises identically, or a query this port declines so that the Python
//! parser, the reference, decides it.

use std::fmt;

use super::vocab::{self, Kw, Punct};

#[derive(Clone, Debug, PartialEq)]
pub enum Error {
    Syntax(SyntaxError),
    Defer(Defer),
}

impl From<SyntaxError> for Error {
    fn from(err: SyntaxError) -> Self {
        Error::Syntax(err)
    }
}

impl From<Defer> for Error {
    fn from(defer: Defer) -> Self {
        Error::Defer(defer)
    }
}

/// A query whose meaning rests on something Python defines and Rust does
/// not reproduce exactly. Declining costs only speed; guessing would cost
/// parity.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Defer {
    pub pos: usize,
    pub reason: DeferReason,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DeferReason {
    /// A non-ASCII character where the lexer classifies characters. Python's
    /// `isalpha`/`isdigit`/`isalnum` follow its own Unicode tables (and
    /// `int()` accepts any decimal digit), which Rust's `char` methods do
    /// not match; inside strings, backtick names and comments nothing is
    /// classified, so those stay here.
    NonAscii,
    /// An integer literal beyond `i64`, which Python keeps exactly.
    BigInt,
    /// A `\u` escape that is not four hex digits naming a scalar value:
    /// Python's `int(code, 16)` and `chr()` accept or reject such text by
    /// rules of their own (signs, `0x`, `_`, lone surrogates).
    Escape,
    /// Nesting or operator chains deep enough to approach Python's
    /// recursion limit, or this thread's stack, when either walks the tree.
    Nesting,
}

/// What `CypherSyntaxError(pos, message)` carries.
#[derive(Clone, Debug, PartialEq)]
pub struct SyntaxError {
    pub pos: usize,
    pub kind: SyntaxErrorKind,
}

#[derive(Clone, Debug, PartialEq)]
pub enum SyntaxErrorKind {
    UnterminatedString,
    UnterminatedComment,
    UnterminatedIdent,
    /// A character no token starts with; the message is its `repr`.
    UnexpectedChar(char),
    /// A token the grammar has no place for. `found` is its text, or "end
    /// of query" when that text is empty.
    Unexpected {
        found: String,
        expected: Expected,
    },
    /// `shortestPath` and friends; the message is the word as written.
    Unsupported(String),
}

/// What the parser was looking for, as its error message names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Expected {
    Expression,
    Name,
    Int,
    Clause,
    Pattern,
    SetItem,
    Schema,
    Word(Kw),
    Punct(Punct),
}

impl SyntaxError {
    pub fn new(pos: usize, kind: SyntaxErrorKind) -> Self {
        SyntaxError { pos, kind }
    }

    /// The message, rendering quoted text with `repr`. The Python binding
    /// passes Python's own `repr()`, whose escaping of non-ASCII text
    /// depends on Unicode tables; `py_repr` here is exact for ASCII.
    pub fn message(&self, repr: impl Fn(&str) -> String) -> String {
        match &self.kind {
            SyntaxErrorKind::UnterminatedString => vocab::ERR_UNTERMINATED_STRING.to_owned(),
            SyntaxErrorKind::UnterminatedComment => vocab::ERR_UNTERMINATED_COMMENT.to_owned(),
            SyntaxErrorKind::UnterminatedIdent => vocab::ERR_UNTERMINATED_IDENT.to_owned(),
            SyntaxErrorKind::UnexpectedChar(ch) => repr(ch.encode_utf8(&mut [0; 4])),
            SyntaxErrorKind::Unexpected { found, expected } => {
                let expected = match expected {
                    Expected::Expression => vocab::EXPECT_EXPRESSION.to_owned(),
                    Expected::Name => vocab::EXPECT_NAME.to_owned(),
                    Expected::Int => vocab::EXPECT_INT.to_owned(),
                    Expected::Clause => vocab::EXPECT_CLAUSE.to_owned(),
                    Expected::Pattern => vocab::EXPECT_PATTERN.to_owned(),
                    Expected::SetItem => vocab::EXPECT_SET_ITEM.to_owned(),
                    Expected::Schema => vocab::EXPECT_SCHEMA.to_owned(),
                    Expected::Word(kw) => kw.as_str().to_owned(),
                    Expected::Punct(p) => repr(p.as_str()),
                };
                vocab::unexpected(&repr(found), &expected)
            }
            SyntaxErrorKind::Unsupported(word) => word.clone(),
        }
    }
}

impl fmt::Display for SyntaxError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&vocab::syntax_error(self.pos, &self.message(py_repr)))
    }
}

impl std::error::Error for SyntaxError {}

/// Python's `repr()` of a `str`: single quotes unless the text holds a `'`
/// and no `"`, with backslash escapes for the quote, backslashes and
/// control characters. Outside ASCII, Python keeps a character as is when
/// `str.isprintable()` says so; this approximates that with Rust's notion
/// of control and whitespace characters.
pub fn py_repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for ch in text.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\t' => out.push_str("\\t"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            ' '..='~' => out.push(ch),
            c if c.is_ascii() || c.is_control() || (c.is_whitespace() && c != ' ') => {
                let code = u32::from(c);
                if code <= 0xff {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code <= 0xffff {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_picks_quotes_and_escapes_like_python() {
        assert_eq!(py_repr("abc"), "'abc'");
        assert_eq!(py_repr("it's"), "\"it's\"");
        assert_eq!(py_repr("'a\"b'"), "'\\'a\"b\\''");
        assert_eq!(py_repr("a\\b"), "'a\\\\b'");
        assert_eq!(py_repr("\u{0}\u{7f}\n"), "'\\x00\\x7f\\n'");
        assert_eq!(py_repr("é"), "'é'");
        assert_eq!(py_repr("\u{a0}"), "'\\xa0'");
    }

    #[test]
    fn messages_follow_the_python_templates() {
        let err = SyntaxError::new(
            9,
            SyntaxErrorKind::Unexpected {
                found: "RETURN".into(),
                expected: Expected::Punct(Punct::RParen),
            },
        );
        assert_eq!(
            err.to_string(),
            "Cypher syntax error at offset 9: unexpected 'RETURN', expected ')'"
        );
        let err = SyntaxError::new(7, SyntaxErrorKind::UnexpectedChar('#'));
        assert_eq!(err.to_string(), "Cypher syntax error at offset 7: '#'");
    }
}
