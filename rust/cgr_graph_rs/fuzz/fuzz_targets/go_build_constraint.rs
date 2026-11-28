// SPDX-License-Identifier: MIT
//! Fuzz target: `engine::go::builds`, the Go build-constraint reader and
//! `decide`, and `engine::go::package`, the package clause, on any file name
//! and head.
//!
//! A Go file's name and its first 64 KiB come from the disk, so any bytes
//! reach them. A panic, a run that does not end quickly, a head read two
//! ways, or a constraint past the size and nesting caps is a bug; so is a
//! constraint whose canonical text, read back as a `//go:build` line, is
//! another constraint (the text may be refused only for the reader's
//! limits: its groups can nest one level deeper, and hold more tokens, than
//! the line it came from), and a decision that no build holds a constraint
//! whose negation is not then held by every build. The input is a name of
//! at most 64 bytes and a head of at most 2,048, each raw bytes or a run of
//! the fragments the reader reads specially, now and then padded to a whole
//! scan so the cut-off is reached.
#![no_main]

use cgr_graph_rs::engine::go::builds::{
    all_of, canonical_text, decide, head_constraint, name_constraint, negate, parse_go_build, Expr,
    Refusal, HEADER_SCAN_BYTES, MAX_EXPR_DEPTH, MAX_EXPR_SIZE,
};
use cgr_graph_rs::engine::go::package::root_package_clause;
use cgr_graph_rs_fuzz::{require, Input};
use libfuzzer_sys::fuzz_target;

const MAX_NAME_BYTES: usize = 64;
const MAX_HEAD_BYTES: usize = 2048;
const MAX_FRAGMENTS: usize = 64;
/// One input in this many is padded to a whole scan: the one whose byte
/// reads as the last, so an input that ran out, which reads as zeros, is
/// not.
const PADDED_ONE_IN: usize = 16;
const NAME_FRAGMENTS: &[&str] = &[
    "_", "linux", "windows", "android", "amd64", "arm64", "test", "x", ".go", "/", ".", "unix",
];
const HEAD_FRAGMENTS: &[&[u8]] = &[
    b"//go:build ",
    b"// +build ",
    b"//go:build",
    b"\n",
    b"\n\n",
    b"\r\n",
    b"\r",
    "\u{85}".as_bytes(),
    "\u{2028}".as_bytes(),
    b"/*",
    b"*/",
    b"//",
    b" ",
    b"\t",
    "\u{3000}".as_bytes(),
    b"\x1f",
    b"package p",
    b"func f() {}",
    b"linux",
    b"amd64",
    b"unix",
    b"cgo",
    b"t1",
    b"go1.21",
    b"!",
    b"(",
    b")",
    b"&&",
    b"||",
    b"&",
    b",",
    b"$",
    b"\xff",
    b"\xc3",
];
const PADDING: &[&[u8]] = &[b"\n", b"// filler\n"];

fn fragments<T: AsRef<[u8]>>(input: &mut Input, pieces: &[T]) -> Vec<u8> {
    let mut out = Vec::new();
    for _ in 0..input.below(MAX_FRAGMENTS + 1) {
        out.extend_from_slice(input.pick(pieces).as_ref());
    }
    out
}

/// Whether `expr` reads back from its canonical text as itself.
fn reads_back(expr: &Expr) -> bool {
    if expr.is_always() || expr.is_never() || expr.is_unknown() {
        return true;
    }
    match parse_go_build(&canonical_text(expr)) {
        Ok(back) => back == *expr,
        Err(refusal) => refusal != Refusal::Malformed,
    }
}

fuzz_target!(|data: &[u8]| {
    let mut input = Input::new(data);
    let name = if input.bool() {
        input.text(MAX_NAME_BYTES)
    } else {
        String::from_utf8_lossy(&fragments(&mut input, NAME_FRAGMENTS)).into_owned()
    };
    let mut head = if input.bool() {
        input.bytes(MAX_HEAD_BYTES).to_vec()
    } else {
        fragments(&mut input, HEAD_FRAGMENTS)
    };
    if input.below(PADDED_ONE_IN) == PADDED_ONE_IN - 1 {
        let padding = input.pick(PADDING);
        while head.len() < HEADER_SCAN_BYTES {
            head.extend_from_slice(padding);
        }
        head.truncate(HEADER_SCAN_BYTES);
    }

    let named = name_constraint(&name);
    let read = head_constraint(&head);
    require!(read == head_constraint(&head), "{head:?}: read two ways");
    let constraint = all_of([named.clone(), read.clone()]);
    for expr in [&named, &read, &constraint] {
        require!(
            expr.size() <= MAX_EXPR_SIZE && expr.depth() <= MAX_EXPR_DEPTH,
            "{expr:?}: past the caps"
        );
        require!(reads_back(expr), "{expr:?} does not read back");
    }

    let decided = decide([constraint.clone()]);
    require!(
        decide([constraint.clone(), constraint.clone()]) == decided,
        "{constraint:?}: decided two ways"
    );
    if constraint.is_unknown() {
        require!(decided.is_none(), "{constraint:?}: unknown but decided");
    }
    if decided == Some(false) {
        let negated = negate(constraint.clone());
        require!(
            negated.is_unknown() || decide([negated]) == Some(true),
            "{constraint:?}: held by no build, its negation not by every one"
        );
    }

    if let Some(clause) = root_package_clause(&String::from_utf8_lossy(&head)) {
        require!(
            !clause.contains(char::is_whitespace) && !clause.ends_with(';'),
            "{head:?}: clause {clause:?}"
        );
    }
});
