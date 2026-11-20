// SPDX-License-Identifier: MIT
//! Fuzz target: the Cypher lexer and parser on any query text.
//!
//! The text is untrusted: an LLM, an MCP client or a user writes the
//! queries the embedded engine parses. A syntax error and a deferral to the
//! Python parser (`Error::Defer`) are both expected outcomes; a panic, a
//! stack overflow or a hang is a bug. The input is the query, up to 64 KiB,
//! with each bad UTF-8 sequence replaced.
#![no_main]

use cgr_graph_rs::cypher::error::SyntaxErrorKind;
use cgr_graph_rs::cypher::lexer::{tokenize, Token, TokenKind};
use cgr_graph_rs::cypher::vocab::{BACKTICK, BLOCK_COMMENT_OPEN, PARAM_SIGIL, QUOTES};
use cgr_graph_rs::cypher::{parse, DeferReason, Error};
use cgr_graph_rs_fuzz::require;
use libfuzzer_sys::fuzz_target;

const MAX_QUERY_BYTES: usize = 64 * 1024;

fuzz_target!(|data: &[u8]| {
    let text = String::from_utf8_lossy(&data[..data.len().min(MAX_QUERY_BYTES)]);
    let chars: Vec<char> = text.chars().collect();
    let lexed = tokenize(&chars);
    match &lexed {
        Ok(tokens) => check_tokens(tokens, &chars),
        Err(error) => check_lexer_error(error, &chars),
    }
    let parsed = parse(&text);
    match (&lexed, &parsed) {
        // `parse` lexes first, so a lexer error is the parse error,
        // unchanged.
        // Mutant: in parser.rs `parse`, map tokenize's error to an
        // UnterminatedString at 0. Input: "~" aborts.
        (Err(lexed), _) => require!(
            parsed.as_ref().err() == Some(lexed),
            "tokenize failed with {lexed:?} but parse returned {parsed:?}"
        ),
        // Once the text lexes, only the grammar can reject it: an error
        // the lexer makes cannot come from the parser, and the position is
        // a character offset into the query, or its end.
        // Mutant: in parser.rs `Parser::error`, report `tok.pos + 1`.
        // Input: the empty query aborts (position 1 > 0).
        // Mutant: in parser.rs, defer deep nesting as `NonAscii`. Input:
        // "RETURN " then 40 "(", "1" and 40 ")" aborts.
        (Ok(_), Err(error)) => {
            require!(
                !is_lexer_error(error),
                "parse invented a lexer error {error:?}"
            );
            require!(
                position(error) <= chars.len(),
                "{error:?} is past the end ({})",
                chars.len()
            );
            if let Error::Syntax(syntax) = error {
                // The message the Python binding raises, so it runs here.
                let _ = syntax.to_string();
            }
        }
        (Ok(_), Ok(_)) => {}
    }
});

fn position(error: &Error) -> usize {
    match error {
        Error::Syntax(e) => e.pos,
        Error::Defer(d) => d.pos,
    }
}

fn is_lexer_error(error: &Error) -> bool {
    match error {
        Error::Syntax(e) => matches!(
            e.kind,
            SyntaxErrorKind::UnterminatedString
                | SyntaxErrorKind::UnterminatedComment
                | SyntaxErrorKind::UnterminatedIdent
                | SyntaxErrorKind::UnexpectedChar(_)
        ),
        Error::Defer(d) => matches!(
            d.reason,
            DeferReason::NonAscii | DeferReason::BigInt | DeferReason::Escape
        ),
    }
}

/// The token list covers the text in order: tokens do not overlap, each
/// spans at least one character, and one `Eof` closes the list at the end.
/// Each token's span holds what its kind says it does.
///
/// Mutant: in lexer.rs `tokenize`, end an identifier at `j + 1`. Input:
/// "d" aborts. Mutant: give a parameter the value `text[i..j]`, its `$`
/// included. Input: "$" aborts. Mutant: put Eof at `n + 1`. Input: the
/// empty query aborts.
fn check_tokens(tokens: &[Token], chars: &[char]) {
    let n = chars.len();
    let (eof, body) = tokens.split_last().expect("tokenize returns at least Eof");
    require!(
        eof.kind == TokenKind::Eof && eof.pos == n && eof.end == n,
        "the last token is {eof:?}, not Eof at {n}"
    );
    let mut previous_end = 0;
    for token in body {
        require!(
            token.kind != TokenKind::Eof,
            "Eof before the end: {token:?}"
        );
        require!(
            previous_end <= token.pos && token.pos < token.end && token.end <= n,
            "{token:?} overlaps the previous token (ending {previous_end}) or leaves the text ({n})"
        );
        previous_end = token.end;
        let span = &chars[token.pos..token.end];
        let source: String = span.iter().collect();
        let first = span[0];
        let last = span[span.len() - 1];
        let ok = match token.kind {
            // A bare word is its own name, and starts with a letter or `_`.
            TokenKind::Ident(_) => {
                token.value == source && (first.is_ascii_alphabetic() || first == '_')
            }
            TokenKind::QuotedIdent => first == BACKTICK && last == BACKTICK && span.len() >= 2,
            TokenKind::Str => QUOTES.contains(&first) && last == first && span.len() >= 2,
            TokenKind::Param => first == PARAM_SIGIL && token.value == source[1..],
            // An integer token is ASCII digits naming the value it carries.
            TokenKind::Int(v) => {
                span.iter().all(char::is_ascii_digit) && source.parse::<i64>() == Ok(v)
            }
            TokenKind::Float(_) => first.is_ascii_digit() || first == '.',
            TokenKind::Punct(_) => span.len() <= 2,
            TokenKind::Eof => false,
        };
        require!(ok, "{token:?} does not match its source {source:?}");
        // Python's `Token.text`, which the parser uses for projections.
        let _ = token.text(chars);
    }
}

/// A lexer error points at the character that caused it, as the Python
/// lexer's `CypherSyntaxError` offset does, and a deferral at the
/// character Python must judge.
///
/// Mutant: in lexer.rs `tokenize`, report UnexpectedChar at `i + 1`.
/// Input: "~" aborts. Mutant: in lexer.rs `unicode_escape`, decline at
/// `at + 1`. Input: "'\u" aborts.
fn check_lexer_error(error: &Error, chars: &[char]) {
    let at = |i: usize| chars.get(i).copied();
    let ok = match error {
        Error::Syntax(e) => match &e.kind {
            // The lexer only reaches this for an ASCII character: every
            // other one is whitespace or deferred before.
            SyntaxErrorKind::UnexpectedChar(c) => at(e.pos) == Some(*c) && c.is_ascii(),
            SyntaxErrorKind::UnterminatedString => at(e.pos).is_some_and(|c| QUOTES.contains(&c)),
            SyntaxErrorKind::UnterminatedIdent => at(e.pos) == Some(BACKTICK),
            SyntaxErrorKind::UnterminatedComment => {
                chars.get(e.pos..e.pos + 2) == Some(&BLOCK_COMMENT_OPEN[..])
            }
            _ => false,
        },
        Error::Defer(d) => match d.reason {
            // Never for ASCII text, and never for whitespace, which the
            // lexer classifies exactly.
            DeferReason::NonAscii => at(d.pos).is_some_and(|c| !c.is_ascii() && !c.is_whitespace()),
            DeferReason::BigInt => at(d.pos).is_some_and(|c| c.is_ascii_digit()),
            DeferReason::Escape => at(d.pos) == Some('\\') && at(d.pos + 1) == Some('u'),
            DeferReason::Nesting => false,
        },
    };
    require!(
        ok,
        "tokenize returned {error:?}, which does not point at its cause"
    );
}
