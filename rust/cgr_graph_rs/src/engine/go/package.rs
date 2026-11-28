//! A Go file's `package` clause, read without a parse: a port of
//! `read_package_clause` in `codebase_rag/parsers/go/module_paths.py`, which
//! `editing/rename.py` asks of every Go file a rename may touch.

use std::path::Path;

use super::{normalized, read_head};
use crate::engine::ignore::python_str::{split, splitlines};

/// A package clause follows only comments and build constraints, so a
/// file's head holds it; a generated file can run to megabytes.
pub const PACKAGE_CLAUSE_SCAN_BYTES: usize = 64 * 1024;

const PACKAGE_KEYWORD: &str = "package";
/// `package main;` is a valid explicit-semicolon clause form.
const STATEMENT_END: char = ';';
const LINE_COMMENT: &str = "//";
const BLOCK_OPEN: &str = "/*";
const BLOCK_CLOSE: &str = "*/";
/// What a removed block comment leaves: a token separator, never a splice
/// (`package/*doc*/gen` stays two tokens).
const COMMENT_GAP: char = ' ';

/// The `package` clause of the Go file at `path`, spelled as a `Path` spells
/// it (`normalized`); None when the file is unreadable or its head holds no
/// clause.
pub fn read_package_clause(path: &Path) -> Option<String> {
    let head = read_head(&normalized(path), PACKAGE_CLAUSE_SCAN_BYTES)?;
    root_package_clause(&String::from_utf8_lossy(&head))
}

/// The package clause is the first code in a valid Go file, so only
/// comments can precede it: line and block comments (which do not nest, and
/// no string literal can come before the clause) are stripped and the first
/// code line read. Anything other than a package clause there means a
/// malformed file: None rather than a guess.
pub fn root_package_clause(text: &str) -> Option<String> {
    let mut in_block = false;
    for line in splitlines(text) {
        let code;
        (code, in_block) = strip_go_comments(line, in_block);
        let mut parts = split(&code);
        let Some(first) = parts.next() else {
            continue;
        };
        return match parts.next() {
            Some(name) if first == PACKAGE_KEYWORD => {
                Some(name.trim_end_matches(STATEMENT_END).to_owned())
            }
            _ => None,
        };
    }
    None
}

/// `line`'s code with its comments removed, and whether a block comment is
/// still open at its end; `in_block` says whether one was at its start.
fn strip_go_comments(line: &str, mut in_block: bool) -> (String, bool) {
    let mut code = String::new();
    let mut i = 0;
    while i < line.len() {
        if in_block {
            let Some(end) = line[i..].find(BLOCK_CLOSE) else {
                return (code, true);
            };
            in_block = false;
            i += end + BLOCK_CLOSE.len();
            code.push(COMMENT_GAP);
            continue;
        }
        let block = line[i..].find(BLOCK_OPEN).map(|at| i + at);
        let comment = line[i..].find(LINE_COMMENT).map(|at| i + at);
        if let Some(comment) = comment.filter(|&at| block.is_none_or(|block| at < block)) {
            code.push_str(&line[i..comment]);
            break;
        }
        let Some(block) = block else {
            code.push_str(&line[i..]);
            break;
        };
        code.push_str(&line[i..block]);
        in_block = true;
        i = block + BLOCK_OPEN.len();
    }
    (code, in_block)
}
