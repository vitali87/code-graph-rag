//! Tokenizer: a port of `codebase_rag/embedded_graph/lexer.py`, rule for
//! rule. Offsets count characters (code points), as Python's `str` indices
//! do, so error positions and the source text that names an unaliased
//! projection match the reference.

use super::error::{Defer, DeferReason, Error, SyntaxError, SyntaxErrorKind};
use super::vocab::{self, Kw, Punct};

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum TokenKind {
    /// A bare word, with the keyword it spells, if any.
    Ident(Option<Kw>),
    QuotedIdent,
    Int(i64),
    Float(f64),
    Str,
    Param,
    Punct(Punct),
    Eof,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Token {
    pub kind: TokenKind,
    /// The name of an identifier or parameter, or a string's value;
    /// empty for other tokens.
    pub value: String,
    pub pos: usize,
    /// One past the token's last character in the source.
    pub end: usize,
}

impl Token {
    fn new(kind: TokenKind, value: String, pos: usize, end: usize) -> Self {
        Token {
            kind,
            value,
            pos,
            end,
        }
    }

    /// Python's `Token.text`: the name for identifiers (backtick ones
    /// unescaped), the source for literals and parameters.
    pub fn text(&self, source: &[char]) -> String {
        match &self.kind {
            TokenKind::Ident(_) | TokenKind::QuotedIdent => self.value.clone(),
            TokenKind::Punct(p) => p.as_str().to_owned(),
            TokenKind::Eof => String::new(),
            TokenKind::Int(_) | TokenKind::Float(_) | TokenKind::Str | TokenKind::Param => {
                source[self.pos..self.end].iter().collect()
            }
        }
    }

    pub fn is_word(&self, word: Kw) -> bool {
        self.kind == TokenKind::Ident(Some(word))
    }

    pub fn is_punct(&self, punct: Punct) -> bool {
        self.kind == TokenKind::Punct(punct)
    }

    pub fn is_name(&self) -> bool {
        matches!(self.kind, TokenKind::Ident(_) | TokenKind::QuotedIdent)
    }
}

/// Python's `str.isspace()`, exact for every code point: Unicode
/// White_Space plus the ASCII information separators U+001C..U+001F, which
/// Python counts by their bidirectional class.
fn is_space(ch: char) -> bool {
    ch.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&ch)
}

/// Classifies `ch` as one of Python's `str` predicates would, or declines.
/// Every ASCII answer is exact, and so is "no" for whitespace, which is
/// never a letter or digit; any other non-ASCII character is the Python
/// parser's to judge.
fn classify(ch: char, pos: usize, ascii_test: fn(&char) -> bool) -> Result<bool, Defer> {
    if ch.is_ascii() {
        Ok(ascii_test(&ch))
    } else if is_space(ch) {
        Ok(false)
    } else {
        Err(Defer {
            pos,
            reason: DeferReason::NonAscii,
        })
    }
}

fn is_word_char(ch: char, pos: usize) -> Result<bool, Defer> {
    classify(ch, pos, |c| {
        c.is_ascii_alphanumeric() || *c == vocab::IDENT_EXTRA
    })
}

fn is_digit(ch: char, pos: usize) -> Result<bool, Defer> {
    classify(ch, pos, char::is_ascii_digit)
}

fn starts_with(text: &[char], at: usize, prefix: &[char]) -> bool {
    text.get(at..at + prefix.len()) == Some(prefix)
}

fn find(text: &[char], from: usize, needle: &[char]) -> Option<usize> {
    text.get(from..)?
        .windows(needle.len())
        .position(|w| w == needle)
        .map(|i| i + from)
}

pub fn tokenize(text: &[char]) -> Result<Vec<Token>, Error> {
    let n = text.len();
    let mut tokens: Vec<Token> = Vec::new();
    let mut i = 0;
    while i < n {
        let ch = text[i];
        if is_space(ch) {
            i += 1;
            continue;
        }
        if starts_with(text, i, &vocab::LINE_COMMENT) {
            i = text[i..]
                .iter()
                .position(|&c| c == vocab::NEWLINE)
                .map_or(n, |end| i + end + 1);
            continue;
        }
        if starts_with(text, i, &vocab::BLOCK_COMMENT_OPEN) {
            let Some(end) = find(text, i + 2, &vocab::BLOCK_COMMENT_CLOSE) else {
                return Err(SyntaxError::new(i, SyntaxErrorKind::UnterminatedComment).into());
            };
            i = end + 2;
            continue;
        }
        if vocab::QUOTES.contains(&ch) {
            let (value, end) = read_string(text, i)?;
            tokens.push(Token::new(TokenKind::Str, value, i, end));
            i = end;
            continue;
        }
        if ch == vocab::BACKTICK {
            let (value, end) = read_backtick(text, i)?;
            tokens.push(Token::new(TokenKind::QuotedIdent, value, i, end));
            i = end;
            continue;
        }
        if ch == vocab::PARAM_SIGIL {
            let mut j = i + 1;
            while j < n && is_word_char(text[j], j)? {
                j += 1;
            }
            tokens.push(Token::new(
                TokenKind::Param,
                text[i + 1..j].iter().collect(),
                i,
                j,
            ));
            i = j;
            continue;
        }
        if is_digit(ch, i)?
            || (ch == '.' && i + 1 < n && is_digit(text[i + 1], i + 1)? && !after_value(&tokens))
        {
            let token = read_number(text, i)?;
            i = token.end;
            tokens.push(token);
            continue;
        }
        if ch.is_ascii_alphabetic() || ch == vocab::IDENT_EXTRA {
            let mut j = i + 1;
            while j < n && is_word_char(text[j], j)? {
                j += 1;
            }
            let name: String = text[i..j].iter().collect();
            tokens.push(Token::new(TokenKind::Ident(Kw::lookup(&name)), name, i, j));
            i = j;
            continue;
        }
        if let Some(punct) = text.get(i + 1).and_then(|&next| Punct::two(ch, next)) {
            tokens.push(Token::new(TokenKind::Punct(punct), String::new(), i, i + 2));
            i += 2;
            continue;
        }
        if let Some(punct) = Punct::one(ch) {
            tokens.push(Token::new(TokenKind::Punct(punct), String::new(), i, i + 1));
            i += 1;
            continue;
        }
        // `is_digit` above already declined every non-ASCII character that
        // is not whitespace, so this one is ASCII.
        return Err(SyntaxError::new(i, SyntaxErrorKind::UnexpectedChar(ch)).into());
    }
    tokens.push(Token::new(TokenKind::Eof, String::new(), n, n));
    Ok(tokens)
}

/// `n.prop` lexes `.` as an accessor, `1..2` as a range; only a `.` that
/// cannot continue a value starts a number such as `.5`.
fn after_value(tokens: &[Token]) -> bool {
    match tokens.last() {
        None => false,
        Some(Token {
            kind: TokenKind::Punct(p),
            ..
        }) => p.closes_value(),
        Some(_) => true,
    }
}

fn read_number(text: &[char], start: usize) -> Result<Token, Error> {
    let n = text.len();
    let mut j = start;
    let mut is_float = false;
    while j < n && is_digit(text[j], j)? {
        j += 1;
    }
    // `1..3` is a range, not the float `1.`
    if j < n && text[j] == '.' && !starts_with(text, j, &['.', '.']) {
        is_float = true;
        j += 1;
        while j < n && is_digit(text[j], j)? {
            j += 1;
        }
    }
    if j < n && vocab::EXPONENT.contains(&text[j]) {
        let mut k = j + 1;
        if k < n && vocab::SIGNS.contains(&text[k]) {
            k += 1;
        }
        if k < n && is_digit(text[k], k)? {
            is_float = true;
            j = k;
            while j < n && is_digit(text[j], j)? {
                j += 1;
            }
        }
    }
    let raw: String = text[start..j].iter().collect();
    let kind = if is_float {
        // Rust's float parsing is correctly rounded, as Python's `float()`
        // is, and both saturate to infinity or zero.
        TokenKind::Float(
            raw.parse()
                .expect("the lexer only accepts decimal float syntax"),
        )
    } else {
        match raw.parse() {
            Ok(value) => TokenKind::Int(value),
            Err(_) => {
                return Err(Defer {
                    pos: start,
                    reason: DeferReason::BigInt,
                }
                .into())
            }
        }
    };
    Ok(Token::new(kind, String::new(), start, j))
}

fn read_string(text: &[char], start: usize) -> Result<(String, usize), Error> {
    let quote = text[start];
    let n = text.len();
    let mut out = String::new();
    let mut i = start + 1;
    while i < n {
        let ch = text[i];
        if ch == vocab::BACKSLASH && i + 1 < n {
            let next = text[i + 1];
            if next == vocab::UNICODE_ESCAPE {
                out.push(unicode_escape(text, i)?);
                i += 2 + vocab::UNICODE_ESCAPE_LEN;
                continue;
            }
            out.push(vocab::string_escape(next));
            i += 2;
            continue;
        }
        if ch == quote {
            return Ok((out, i + 1));
        }
        out.push(ch);
        i += 1;
    }
    Err(SyntaxError::new(start, SyntaxErrorKind::UnterminatedString).into())
}

/// The character a `\uXXXX` escape at `at` names, when it is exactly four
/// hex digits naming a Unicode scalar value.
fn unicode_escape(text: &[char], at: usize) -> Result<char, Defer> {
    let decline = Defer {
        pos: at,
        reason: DeferReason::Escape,
    };
    let digits = text
        .get(at + 2..at + 2 + vocab::UNICODE_ESCAPE_LEN)
        .ok_or(decline)?;
    let mut code = 0u32;
    for ch in digits {
        code = code * vocab::HEX_BASE + ch.to_digit(vocab::HEX_BASE).ok_or(decline)?;
    }
    char::from_u32(code).ok_or(decline)
}

fn read_backtick(text: &[char], start: usize) -> Result<(String, usize), Error> {
    let n = text.len();
    let mut out = String::new();
    let mut i = start + 1;
    while i < n {
        if text[i] == vocab::BACKTICK {
            if starts_with(text, i, &[vocab::BACKTICK, vocab::BACKTICK]) {
                out.push(vocab::BACKTICK);
                i += 2;
                continue;
            }
            return Ok((out, i + 1));
        }
        out.push(text[i]);
        i += 1;
    }
    Err(SyntaxError::new(start, SyntaxErrorKind::UnterminatedIdent).into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lex(text: &str) -> Result<Vec<Token>, Error> {
        tokenize(&text.chars().collect::<Vec<_>>())
    }

    fn kinds(text: &str) -> Vec<TokenKind> {
        lex(text)
            .expect("lexes")
            .into_iter()
            .map(|t| t.kind)
            .collect()
    }

    fn syntax(text: &str) -> SyntaxError {
        match lex(text) {
            Err(Error::Syntax(err)) => err,
            other => panic!("expected a syntax error, got {other:?}"),
        }
    }

    fn deferred(text: &str) -> DeferReason {
        match lex(text) {
            Err(Error::Defer(defer)) => defer.reason,
            other => panic!("expected a deferral, got {other:?}"),
        }
    }

    #[test]
    fn numbers_ranges_and_accessors() {
        use TokenKind::*;
        assert_eq!(
            kinds("1..3"),
            [Int(1), Punct(super::Punct::Range), Int(3), Eof]
        );
        assert_eq!(kinds(".5"), [Float(0.5), Eof]);
        assert_eq!(
            kinds("1.5e-3 2E3 1e 7."),
            [
                Float(1.5e-3),
                Float(2000.0),
                Int(1),
                Ident(None),
                Float(7.0),
                Eof
            ]
        );
        // After a value, `.` is an accessor: `x.5` is a property, `[1].5` too.
        assert_eq!(
            kinds("x.5"),
            [Ident(None), Punct(super::Punct::Dot), Int(5), Eof]
        );
        assert_eq!(
            kinds(").5"),
            [
                Punct(super::Punct::RParen),
                Punct(super::Punct::Dot),
                Int(5),
                Eof
            ]
        );
        assert_eq!(kinds("(.5"), [Punct(super::Punct::LParen), Float(0.5), Eof]);
        assert_eq!(kinds("1e999"), [Float(f64::INFINITY), Eof]);
    }

    #[test]
    fn strings_unescape_and_keep_their_source_span() {
        let tokens = lex(r#"'a\'b\n\u00e9\q' "x""#).expect("lexes");
        assert_eq!(tokens[0].value, "a'b\n\u{e9}q");
        assert_eq!((tokens[0].pos, tokens[0].end), (0, 16));
        assert_eq!(tokens[1].value, "x");
    }

    #[test]
    fn backtick_names_double_their_backticks() {
        let tokens = lex("`a``b` ``").expect("lexes");
        assert_eq!(tokens[0].kind, TokenKind::QuotedIdent);
        assert_eq!(tokens[0].value, "a`b");
        assert_eq!(tokens[0].end, 6);
        assert_eq!(tokens[1].value, "");
    }

    #[test]
    fn comments_and_whitespace_vanish() {
        use TokenKind::*;
        assert_eq!(
            kinds("a // x\n/* y */ b\u{1c}\u{3000}c"),
            [Ident(None), Ident(None), Ident(None), Eof]
        );
        assert_eq!(kinds("a // trailing"), [Ident(None), Eof]);
    }

    #[test]
    fn keywords_and_parameters() {
        let tokens = lex("match $p1 $ $_").expect("lexes");
        assert_eq!(tokens[0].kind, TokenKind::Ident(Some(Kw::Match)));
        assert_eq!(tokens[1].value, "p1");
        assert_eq!(tokens[2].value, "");
        assert_eq!(tokens[3].value, "_");
    }

    #[test]
    fn errors_name_the_offset() {
        assert_eq!(syntax("RETURN 'abc").pos, 7);
        assert_eq!(
            syntax("RETURN 'abc").kind,
            SyntaxErrorKind::UnterminatedString
        );
        assert_eq!(syntax("a /* b").kind, SyntaxErrorKind::UnterminatedComment);
        assert_eq!(syntax("a /*/").kind, SyntaxErrorKind::UnterminatedComment);
        assert_eq!(syntax("`abc").kind, SyntaxErrorKind::UnterminatedIdent);
        assert_eq!(
            syntax("RETURN #").kind,
            SyntaxErrorKind::UnexpectedChar('#')
        );
        // A trailing backslash escapes nothing and the string never ends.
        assert_eq!(syntax("'ab\\").kind, SyntaxErrorKind::UnterminatedString);
    }

    #[test]
    fn python_specific_text_is_deferred() {
        assert_eq!(deferred("RETURN café"), DeferReason::NonAscii);
        assert_eq!(deferred("RETURN 1²"), DeferReason::NonAscii);
        assert_eq!(deferred("RETURN 99999999999999999999"), DeferReason::BigInt);
        assert_eq!(deferred(r"RETURN '\u12'"), DeferReason::Escape);
        assert_eq!(deferred(r"RETURN '\ud800'"), DeferReason::Escape);
        assert_eq!(deferred(r"RETURN '\u+0ff'"), DeferReason::Escape);
        // Unicode inside strings, names and comments is not classified.
        assert!(lex("RETURN 'café', `ñ` // ü").is_ok());
        // Nor is Unicode whitespace, which is never a letter or digit.
        assert!(lex("RETURN\u{a0}a\u{2003}").is_ok());
    }
}
