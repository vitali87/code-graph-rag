//! Which Go files one build compiles together (issue #2483): a port of
//! `codebase_rag/parsers/go/builds.py`, answer for answer.
//!
//! A file's constraint is its name's `_GOOS`, `_GOARCH` or `_GOOS_GOARCH`
//! suffix and its `//go:build` line (or, without one, its `// +build`
//! lines), as `go/build` reads them. Files build together when some GOOS,
//! GOARCH and set of other tags satisfies all their constraints; `decide`
//! answers whether one does, or that it cannot tell.
//!
//! Every constraint is built by `all_of`, `any_of` and `negate`, with
//! Python's simplifications in Python's order, so each one is the very
//! `Expr` Python builds, its operands in the same order and its size and
//! depth the same: what stops growing past a limit stops at the same input.
//! Text is read as Python reads a str (`python_str`): its spaces, its line
//! breaks, and a head that is not UTF-8 decoded with `errors="replace"`.

use std::cmp::Ordering;
use std::collections::hash_map::DefaultHasher;
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::hash::{Hash, Hasher};
use std::path::Path;
use std::sync::Arc;
use std::time::SystemTime;

use super::{joined, read_head};
use crate::engine::ignore::python_str::{is_space, rstrip, split, splitlines, strip};

#[cfg(test)]
mod tests;

/// go/build's syslist: the GOOS values a file name or tag names, sorted, as
/// `decide` enumerates them.
pub const KNOWN_OS: [&str; 18] = [
    "aix",
    "android",
    "darwin",
    "dragonfly",
    "freebsd",
    "hurd",
    "illumos",
    "ios",
    "js",
    "linux",
    "nacl",
    "netbsd",
    "openbsd",
    "plan9",
    "solaris",
    "wasip1",
    "windows",
    "zos",
];
/// The GOOS values the `unix` tag holds for.
pub const UNIX_OS: [&str; 12] = [
    "aix",
    "android",
    "darwin",
    "dragonfly",
    "freebsd",
    "hurd",
    "illumos",
    "ios",
    "linux",
    "netbsd",
    "openbsd",
    "solaris",
];
/// go/build's syslist: the GOARCH values, sorted.
pub const KNOWN_ARCH: [&str; 24] = [
    "386",
    "amd64",
    "amd64p32",
    "arm",
    "arm64",
    "arm64be",
    "armbe",
    "loong64",
    "mips",
    "mips64",
    "mips64le",
    "mips64p32",
    "mips64p32le",
    "mipsle",
    "ppc",
    "ppc64",
    "ppc64le",
    "riscv",
    "riscv64",
    "s390",
    "s390x",
    "sparc",
    "sparc64",
    "wasm",
];
/// GOOS=android builds `linux` files and tags too, illumos `solaris`, ios
/// `darwin`.
const IMPLIED_OS: [(&str, &str); 3] = [
    ("android", "linux"),
    ("illumos", "solaris"),
    ("ios", "darwin"),
];
const UNIX_TAG: &str = "unix";
/// The one build `decide` tries when no tag names a GOOS, or a GOARCH.
const DEFAULT_GOOS: &str = "linux";
const DEFAULT_GOARCH: &str = "amd64";
/// A file's head holds its constraints; a generated file can be megabytes.
pub const HEADER_SCAN_BYTES: usize = 64 * 1024;
/// Beyond this many tags of their own (not a GOOS or GOARCH), each both
/// asked for and ruled out, constraints are not enumerated: whether they
/// hold together is unknown.
pub const MAX_FREE_TAGS: usize = 10;
/// A `//go:build` expression nested deeper than this (`!` and parentheses),
/// or longer than this many tokens, is not read: it is UNKNOWN. Go's own
/// reader rejects such expressions too.
pub const MAX_DEPTH: usize = 100;
pub const MAX_TOKENS: usize = 1000;
/// A constraint built up past this many nodes, or nested past this deep, is
/// UNKNOWN too (#3360 review), so every walk over one is bounded.
pub const MAX_EXPR_SIZE: usize = 4096;
pub const MAX_EXPR_DEPTH: usize = 200;

const GO_EXTENSION: &str = ".go";
const NAME_SEPARATOR: char = '_';
const TEST_SUFFIX: &str = "test";
const PATH_SEPARATOR: char = '/';
const CURRENT_DIR: &str = ".";
const GO_BUILD_PREFIX: &str = "//go:build";
const PLUS_BUILD_PREFIX: &str = "// +build";
/// What may follow a directive's prefix.
const DIRECTIVE_GAPS: &[u8; 2] = b" \t";
const LINE_COMMENT: &str = "//";
const BLOCK_OPEN: &str = "/*";
const BLOCK_CLOSE: &str = "*/";
/// `// +build`'s separators: a space between options, a comma between the
/// terms of one, and `!` before a term ruled out.
const PLUS_TERM_SEPARATOR: char = ',';
const NOT: &str = "!";
const AND: &str = "&&";
const OR: &str = "||";
const GROUP_OPEN: &str = "(";
const GROUP_CLOSE: &str = ")";
/// A constraint's canonical text, as the project index hand-off writes it
/// (docs/reports/PHASE3_GO.md, section 6): the `//go:build` grammar, and for
/// the three constraints it has no words for, a word no tag can be.
const TEXT_AND: &str = " && ";
const TEXT_OR: &str = " || ";
const TEXT_ALWAYS: &str = "<always>";
const TEXT_NEVER: &str = "<never>";
const TEXT_UNKNOWN: &str = "<unknown>";

/// What a constraint node is, as `Expr.op` names it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Op {
    Tag,
    Not,
    And,
    Or,
    Unknown,
}

impl Op {
    /// The name `builds.py` gives it.
    pub fn name(self) -> &'static str {
        match self {
            Op::Tag => "tag",
            Op::Not => "not",
            Op::And => "and",
            Op::Or => "or",
            Op::Unknown => "unknown",
        }
    }
}

/// A constraint: a tag (`name` set), its negation, or an all-of (`And`) or
/// any-of (`Or`) of `parts`; `builds.py`'s `Expr`, its fields in its order.
/// An empty all-of holds in every build (ALWAYS), an empty any-of in none
/// (NEVER). Equal when every field is, as the NamedTuple is; shared, so a
/// copy costs nothing however large the constraint.
#[derive(Clone)]
pub struct Expr(Arc<Node>);

struct Node {
    op: Op,
    name: String,
    parts: Vec<Expr>,
    size: usize,
    depth: usize,
    /// A digest of the fields, for the sets and maps constraints go into
    /// and to tell two apart at once; never written anywhere.
    digest: u64,
}

impl Expr {
    fn new(op: Op, name: String, parts: Vec<Expr>, size: usize, depth: usize) -> Expr {
        let mut hasher = DefaultHasher::new();
        op.hash(&mut hasher);
        name.hash(&mut hasher);
        for part in &parts {
            hasher.write_u64(part.0.digest);
        }
        size.hash(&mut hasher);
        depth.hash(&mut hasher);
        let digest = hasher.finish();
        Expr(Arc::new(Node {
            op,
            name,
            parts,
            size,
            depth,
            digest,
        }))
    }

    fn leaf(op: Op, name: &str) -> Expr {
        Expr::new(op, name.to_owned(), Vec::new(), 1, 1)
    }

    /// The tag `name`.
    pub fn tag(name: &str) -> Expr {
        Expr::leaf(Op::Tag, name)
    }

    /// The constraint every build satisfies.
    pub fn always() -> Expr {
        Expr::leaf(Op::And, "")
    }

    /// The constraint no build satisfies.
    pub fn never() -> Expr {
        Expr::leaf(Op::Or, "")
    }

    /// A constraint this cannot read (malformed, nested or long past what it
    /// reads, cut off by the head it scans, or built up past the limits):
    /// neither true nor false in any build, so every decision resting on it
    /// is unknown.
    pub fn unknown() -> Expr {
        Expr::leaf(Op::Unknown, "")
    }

    pub fn op(&self) -> Op {
        self.0.op
    }

    pub fn name(&self) -> &str {
        &self.0.name
    }

    pub fn parts(&self) -> &[Expr] {
        &self.0.parts
    }

    /// Its nodes.
    pub fn size(&self) -> usize {
        self.0.size
    }

    /// Its nesting.
    pub fn depth(&self) -> usize {
        self.0.depth
    }

    pub fn is_always(&self) -> bool {
        self.0.op == Op::And && self.0.parts.is_empty()
    }

    pub fn is_never(&self) -> bool {
        self.0.op == Op::Or && self.0.parts.is_empty()
    }

    pub fn is_unknown(&self) -> bool {
        self.0.op == Op::Unknown
    }

    /// Whether this is the unit of `op`'s compound: ALWAYS for `And`,
    /// NEVER for `Or`.
    fn is_unit_of(&self, op: Op) -> bool {
        self.0.op == op && self.0.parts.is_empty()
    }
}

impl PartialEq for Expr {
    fn eq(&self, other: &Expr) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
            || (self.0.digest == other.0.digest
                && self.0.op == other.0.op
                && self.0.size == other.0.size
                && self.0.depth == other.0.depth
                && self.0.name == other.0.name
                && self.0.parts == other.0.parts)
    }
}

impl Eq for Expr {}

impl Hash for Expr {
    fn hash<H: Hasher>(&self, state: &mut H) {
        state.write_u64(self.0.digest);
    }
}

/// A total order by the fields, for the sets of constraints a decision is
/// memoised by; it decides nothing else.
impl Ord for Expr {
    fn cmp(&self, other: &Expr) -> Ordering {
        if Arc::ptr_eq(&self.0, &other.0) {
            return Ordering::Equal;
        }
        (self.0.op, &self.0.name, self.0.size, self.0.depth)
            .cmp(&(other.0.op, &other.0.name, other.0.size, other.0.depth))
            .then_with(|| self.0.parts.cmp(&other.0.parts))
    }
}

impl PartialOrd for Expr {
    fn partial_cmp(&self, other: &Expr) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl std::fmt::Debug for Expr {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&canonical_text(self))
    }
}

fn compound(op: Op, parts: Vec<Expr>) -> Expr {
    let size = 1 + parts.iter().map(Expr::size).sum::<usize>();
    let depth = 1 + parts.iter().map(Expr::depth).max().unwrap_or(0);
    if size > MAX_EXPR_SIZE || depth > MAX_EXPR_DEPTH {
        return Expr::unknown();
    }
    Expr::new(op, String::new(), parts, size, depth)
}

/// Every one of `parts`.
pub fn all_of<I: IntoIterator<Item = Expr>>(parts: I) -> Expr {
    let mut kept = flat(Op::And, parts);
    if kept.iter().any(Expr::is_never) || complementary(&kept) {
        return Expr::never();
    }
    if kept.iter().any(Expr::is_unknown) {
        return Expr::unknown();
    }
    match kept.len() {
        0 => Expr::always(),
        1 => kept.remove(0),
        _ => compound(Op::And, kept),
    }
}

/// One of `parts`. What every one of them asks for is asked for once beside
/// the rest (`B && linux || B && !linux` is `B`), so the builds an
/// interface's method set merges across embeds stay as small as they are.
pub fn any_of<I: IntoIterator<Item = Expr>>(parts: I) -> Expr {
    let mut kept = flat(Op::Or, parts);
    if kept.iter().any(Expr::is_always) || complementary(&kept) {
        return Expr::always();
    }
    if kept.iter().any(Expr::is_unknown) {
        return Expr::unknown();
    }
    match kept.len() {
        0 => return Expr::never(),
        1 => return kept.remove(0),
        _ => {}
    }
    let shared: Vec<Expr> = conjuncts(&kept[0])
        .iter()
        .filter(|part| {
            kept[1..]
                .iter()
                .all(|other| conjuncts(other).contains(part))
        })
        .cloned()
        .collect();
    if shared.is_empty() {
        return compound(Op::Or, kept);
    }
    let options: Vec<Expr> = kept
        .iter()
        .map(|option| {
            all_of(
                conjuncts(option)
                    .iter()
                    .filter(|part| !shared.contains(part))
                    .cloned(),
            )
        })
        .collect();
    let rest = any_of(options);
    all_of(shared.into_iter().chain(std::iter::once(rest)))
}

fn conjuncts(part: &Expr) -> &[Expr] {
    if part.op() == Op::And && !part.is_always() {
        part.parts()
    } else {
        std::slice::from_ref(part)
    }
}

/// Whether a part and its negation are both among `kept`.
fn complementary(kept: &[Expr]) -> bool {
    let present: HashSet<&Expr> = kept.iter().collect();
    kept.iter()
        .any(|part| part.op() == Op::Not && present.contains(&part.parts()[0]))
}

/// The distinct operands, in order, an operand of the same `op` spliced in:
/// a constraint built up level by level through an interface hierarchy stays
/// as large as its distinct tags (#3360 review).
fn flat<I: IntoIterator<Item = Expr>>(op: Op, parts: I) -> Vec<Expr> {
    let mut kept = Vec::new();
    let mut seen = HashSet::new();
    let mut keep = |operand: &Expr| {
        if !operand.is_unit_of(op) && seen.insert(operand.clone()) {
            kept.push(operand.clone());
        }
    };
    for part in parts {
        if part.op() == op && !part.is_unit_of(op) {
            part.parts().iter().for_each(&mut keep);
        } else {
            keep(&part);
        }
    }
    kept
}

/// Every build `part` rules out.
pub fn negate(part: Expr) -> Expr {
    if part.is_always() {
        return Expr::never();
    }
    if part.is_never() {
        return Expr::always();
    }
    match part.op() {
        Op::Unknown => Expr::unknown(),
        Op::Not => part.parts()[0].clone(),
        _ => compound(Op::Not, vec![part]),
    }
}

/// `expr` written out, the same for the same constraint: its operands in the
/// order it holds them, each compound one of a compound in parentheses. Read
/// back as a `//go:build` line, the text is `expr` again while it stays
/// within what the line's reader reads (`parse_go_build`); the three
/// constraints that grammar cannot spell get words of their own.
pub fn canonical_text(expr: &Expr) -> String {
    if expr.is_always() {
        return TEXT_ALWAYS.to_owned();
    }
    if expr.is_never() {
        return TEXT_NEVER.to_owned();
    }
    match expr.op() {
        Op::Unknown => TEXT_UNKNOWN.to_owned(),
        Op::Tag => expr.name().to_owned(),
        Op::Not => format!("{NOT}{}", operand_text(&expr.parts()[0])),
        Op::And => operands_text(expr, TEXT_AND),
        Op::Or => operands_text(expr, TEXT_OR),
    }
}

fn operands_text(expr: &Expr, separator: &str) -> String {
    expr.parts()
        .iter()
        .map(operand_text)
        .collect::<Vec<_>>()
        .join(separator)
}

fn operand_text(part: &Expr) -> String {
    let text = canonical_text(part);
    let grouped = matches!(part.op(), Op::And | Op::Or) && !part.is_always() && !part.is_never();
    if grouped {
        format!("{GROUP_OPEN}{text}{GROUP_CLOSE}")
    } else {
        text
    }
}

fn is_os(name: &str) -> bool {
    KNOWN_OS.contains(&name)
}

fn is_arch(name: &str) -> bool {
    KNOWN_ARCH.contains(&name)
}

/// What a file's name asks of the build, as `go/build`'s goodOSArchFile
/// reads it: everything before the first `_` is free, a `_test` suffix is
/// dropped, and a trailing GOOS, GOARCH or GOOS_GOARCH constrains. The name
/// is the last part of `rel_path` as `PurePosixPath` reads it.
pub fn name_constraint(rel_path: &str) -> Expr {
    let name = rel_path
        .split(PATH_SEPARATOR)
        .rfind(|part| !part.is_empty() && *part != CURRENT_DIR)
        .unwrap_or_default();
    let stem = name.strip_suffix(GO_EXTENSION).unwrap_or(name);
    let Some(underscore) = stem.find(NAME_SEPARATOR) else {
        return Expr::always();
    };
    let mut parts: Vec<&str> = stem[underscore..].split(NAME_SEPARATOR).collect();
    if parts.last() == Some(&TEST_SUFFIX) {
        parts.pop();
    }
    if let [.., os, arch] = parts[..] {
        if is_os(os) && is_arch(arch) {
            return all_of([Expr::tag(os), Expr::tag(arch)]);
        }
    }
    match parts.last() {
        Some(last) if is_os(last) || is_arch(last) => Expr::tag(last),
        _ => Expr::always(),
    }
}

/// The `//go:build` expression in a file's head, or its `// +build` lines
/// without one, read where `go/build` reads them: before the package clause,
/// past blank lines and line and block comments (a license `/* ... */`
/// included), never inside a block comment. A constraint this cannot read
/// is UNKNOWN, and so is a head that ends inside a block comment, or that
/// was cut off (`truncated`) before its first code line: its constraint may
/// lie past what was read.
pub fn header_constraint(text: &str, truncated: bool) -> Expr {
    let mut header = Header::default();
    header.read(text);
    if header.in_block || (truncated && !header.reached_code) {
        return Expr::unknown();
    }
    match header.go_build {
        Some(line) => parse_go_build(line).unwrap_or_else(|_| Expr::unknown()),
        None => all_of(header.plus_lines.into_iter().map(plus_build)),
    }
}

/// The constraint of a file's head as read from disk, the first
/// `HEADER_SCAN_BYTES` of it: decoded as UTF-8 with each bad sequence
/// replaced, and cut off when it fills the scan.
pub fn head_constraint(head: &[u8]) -> Expr {
    header_constraint(
        &String::from_utf8_lossy(head),
        head.len() >= HEADER_SCAN_BYTES,
    )
}

/// The constraint of the file at `rel_path` under `root`: its name's and its
/// head's, in that order, as `GoBuilds.constraint` reads it. A file that
/// cannot be read has a head that asks nothing; no path has no constraint.
pub fn file_constraint(root: &Path, rel_path: &str) -> Expr {
    if rel_path.is_empty() {
        return Expr::always();
    }
    let head = read_head(&joined(root, rel_path), HEADER_SCAN_BYTES).unwrap_or_default();
    all_of([name_constraint(rel_path), head_constraint(&head)])
}

/// `go/build`'s parseFileHeader (#3360 review): a `//go:build` line counts
/// anywhere in the head outside a block comment; a `// +build` line only
/// before the first line that is no line comment, and only when a blank line
/// follows it there.
#[derive(Default)]
struct Header<'a> {
    go_build: Option<&'a str>,
    plus_lines: Vec<&'a str>,
    pending: Vec<&'a str>,
    /// A line not opening with `//` was seen: no `// +build` line after.
    ended: bool,
    in_block: bool,
    reached_code: bool,
}

impl<'a> Header<'a> {
    fn read(&mut self, text: &'a str) {
        for raw in splitlines(text) {
            if !self.line(strip(raw)) {
                self.reached_code = true;
                return;
            }
        }
    }

    /// False at the first text that is no comment.
    fn line(&mut self, line: &'a str) -> bool {
        if line.is_empty() {
            if !self.ended {
                self.plus_lines.append(&mut self.pending);
            }
            return true;
        }
        if !line.starts_with(LINE_COMMENT) {
            self.ended = true;
        }
        if !self.in_block {
            if self.go_build.is_none() && is_directive(line, GO_BUILD_PREFIX) {
                self.go_build = Some(&line[GO_BUILD_PREFIX.len()..]);
            } else if !self.ended && is_directive(line, PLUS_BUILD_PREFIX) {
                self.pending.push(&line[PLUS_BUILD_PREFIX.len()..]);
            }
        }
        self.only_comments(line)
    }

    /// Whether the rest of `line` is comment, tracking a block comment
    /// across lines (`/* a */ /* b */ package p` is code).
    fn only_comments(&mut self, mut line: &str) -> bool {
        while !line.is_empty() {
            if self.in_block {
                let Some(close) = line.find(BLOCK_CLOSE) else {
                    return true;
                };
                self.in_block = false;
                line = strip(&line[close + BLOCK_CLOSE.len()..]);
            } else if line.starts_with(LINE_COMMENT) {
                return true;
            } else if let Some(rest) = line.strip_prefix(BLOCK_OPEN) {
                self.in_block = true;
                line = strip(rest);
            } else {
                return false;
            }
        }
        true
    }
}

/// Whether `line` is the directive `prefix` followed by a space or a tab.
fn is_directive(line: &str, prefix: &str) -> bool {
    line.starts_with(prefix)
        && line
            .as_bytes()
            .get(prefix.len())
            .is_some_and(|gap| DIRECTIVE_GAPS.contains(gap))
}

/// Whether `term` is a tag: `[A-Za-z0-9_.]+`.
fn is_tag_name(term: &str) -> bool {
    !term.is_empty() && term.bytes().all(is_tag_byte)
}

fn is_tag_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'.'
}

/// `a,!b c`: space-separated options any of which holds, each a
/// comma-separated all-of of tags or negated tags. A term that is no tag
/// makes the line UNKNOWN.
fn plus_build(line: &str) -> Expr {
    let terms: Vec<Vec<&str>> = split(line)
        .map(|option| option.split(PLUS_TERM_SEPARATOR).collect())
        .collect();
    let readable = terms
        .iter()
        .flatten()
        .all(|term| is_tag_name(term.strip_prefix(NOT).unwrap_or(term)));
    if !readable {
        return Expr::unknown();
    }
    if terms.is_empty() {
        return Expr::always();
    }
    any_of(terms.iter().map(|option| {
        all_of(option.iter().map(|term| match term.strip_prefix(NOT) {
            Some(tag) => negate(Expr::tag(tag)),
            None => Expr::tag(term),
        }))
    }))
}

/// Why a `//go:build` expression was not read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// It is no expression the grammar reads.
    Malformed,
    /// It holds more than `MAX_TOKENS` tokens.
    TooManyTokens,
    /// It nests `!` and parentheses deeper than `MAX_DEPTH`.
    TooDeep,
}

/// The expression after `//go:build`: `||` over `&&` over `!`, parentheses
/// and tags, as `_parse_go_build` reads it; an `Err` where it gives UNKNOWN,
/// with the reason.
pub fn parse_go_build(text: &str) -> Result<Expr, Refusal> {
    let tokens = tokens(text)?;
    let mut parser = ExprParser {
        tokens: &tokens,
        index: 0,
        depth: 0,
    };
    match parser.parse_or()? {
        Some(parsed) if parser.index == tokens.len() => Ok(parsed),
        _ => Err(Refusal::Malformed),
    }
}

/// `_TOKEN`'s matches over the expression, rstripped: `(`, `)`, `!`, `&&`,
/// `||` and runs of tag characters, each after any spaces.
fn tokens(text: &str) -> Result<Vec<&str>, Refusal> {
    let mut tokens = Vec::new();
    let mut rest = rstrip(text);
    while !rest.is_empty() {
        if tokens.len() >= MAX_TOKENS {
            return Err(Refusal::TooManyTokens);
        }
        rest = rest.trim_start_matches(is_space);
        let length = match rest.as_bytes() {
            [b'(' | b')' | b'!', ..] => 1,
            [b'&', b'&', ..] | [b'|', b'|', ..] => 2,
            bytes => bytes.iter().take_while(|&&byte| is_tag_byte(byte)).count(),
        };
        if length == 0 {
            return Err(Refusal::Malformed);
        }
        tokens.push(&rest[..length]);
        rest = &rest[length..];
    }
    Ok(tokens)
}

struct ExprParser<'a> {
    tokens: &'a [&'a str],
    index: usize,
    depth: usize,
}

/// A parse step: `Ok(None)` where Python's parser returns None (the line is
/// malformed), `Err` once it nests too deep.
type Parsed = Result<Option<Expr>, Refusal>;

impl<'a> ExprParser<'a> {
    fn peek(&self) -> Option<&'a str> {
        self.tokens.get(self.index).copied()
    }

    fn parse_or(&mut self) -> Parsed {
        self.chain(OR, Self::parse_and, any_of)
    }

    fn parse_and(&mut self) -> Parsed {
        self.chain(AND, Self::parse_not, all_of)
    }

    fn chain(
        &mut self,
        operator: &str,
        operand: fn(&mut Self) -> Parsed,
        combine: fn(Vec<Expr>) -> Expr,
    ) -> Parsed {
        let Some(first) = operand(self)? else {
            return Ok(None);
        };
        let mut parts = vec![first];
        while self.peek() == Some(operator) {
            self.index += 1;
            let Some(part) = operand(self)? else {
                return Ok(None);
            };
            parts.push(part);
        }
        Ok(Some(if parts.len() == 1 {
            parts.remove(0)
        } else {
            combine(parts)
        }))
    }

    fn parse_not(&mut self) -> Parsed {
        let Some(token) = self.peek() else {
            return Ok(None);
        };
        self.index += 1;
        if token != NOT && token != GROUP_OPEN {
            let operator = [GROUP_CLOSE, AND, OR].contains(&token);
            return Ok((!operator).then(|| Expr::tag(token)));
        }
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(Refusal::TooDeep);
        }
        let inner = if token == NOT {
            self.parse_not()?
        } else {
            self.parse_group()?
        };
        self.depth -= 1;
        Ok(inner.map(|inner| if token == NOT { negate(inner) } else { inner }))
    }

    fn parse_group(&mut self) -> Parsed {
        let inner = self.parse_or()?;
        if inner.is_none() || self.peek() != Some(GROUP_CLOSE) {
            return Ok(None);
        }
        self.index += 1;
        Ok(inner)
    }
}

/// Each tag, and whether it is asked for and whether it is ruled out. The
/// walk keeps a stack of its own.
fn polarities(expr: &Expr) -> BTreeMap<&str, (bool, bool)> {
    let mut found: BTreeMap<&str, (bool, bool)> = BTreeMap::new();
    let mut stack = vec![(expr, true)];
    while let Some((node, positive)) = stack.pop() {
        if node.op() == Op::Tag {
            let entry = found.entry(node.name()).or_default();
            if positive {
                entry.0 = true;
            } else {
                entry.1 = true;
            }
        }
        let flips = node.op() == Op::Not;
        stack.extend(node.parts().iter().map(|part| (part, positive != flips)));
    }
    found
}

/// One build `decide` tries: its GOOS and GOARCH, the tags it sets of those
/// asked for and ruled out both (`free`, by bit), and the ones it always
/// sets (`asked`).
struct Build<'a> {
    goos: &'a str,
    goarch: &'a str,
    free: &'a [&'a str],
    chosen: u32,
    asked: &'a BTreeSet<&'a str>,
}

impl Build<'_> {
    fn has(&self, tag: &str) -> bool {
        tag == self.goos
            || tag == self.goarch
            || (tag == UNIX_TAG && UNIX_OS.contains(&self.goos))
            || IMPLIED_OS
                .iter()
                .any(|&(goos, implied)| goos == self.goos && implied == tag)
            || self.asked.contains(tag)
            || self
                .free
                .iter()
                .position(|&free| free == tag)
                .is_some_and(|bit| self.chosen & (1 << bit) != 0)
    }

    /// Whether the build satisfies `expr`; its nesting is at most
    /// `MAX_EXPR_DEPTH`, which bounds the recursion.
    fn holds(&self, expr: &Expr) -> bool {
        match expr.op() {
            Op::Tag => self.has(expr.name()),
            Op::Not => !self.holds(&expr.parts()[0]),
            Op::And => expr.parts().iter().all(|part| self.holds(part)),
            Op::Or | Op::Unknown => expr.parts().iter().any(|part| self.holds(part)),
        }
    }
}

/// Whether one build satisfies every constraint in `exprs`: `Some` when
/// this can tell, `None` when it cannot (an UNKNOWN constraint, or more tags
/// of their own than it enumerates).
pub fn decide<I: IntoIterator<Item = Expr>>(exprs: I) -> Option<bool> {
    let combined = all_of(exprs);
    if combined.is_unknown() {
        return None;
    }
    if combined.is_always() || combined.is_never() {
        return Some(combined.is_always());
    }
    decide_combined(&combined)
}

fn decide_combined(combined: &Expr) -> Option<bool> {
    let polarities = polarities(combined);
    let fixed = |tag: &str| is_os(tag) || is_arch(tag) || tag == UNIX_TAG;
    let os_bound = polarities.keys().any(|&tag| is_os(tag) || tag == UNIX_TAG);
    let arch_bound = polarities.keys().any(|&tag| is_arch(tag));
    // A tag of its own only ever asked for is best set, one only ever ruled
    // out best unset: either way no other part fails for it. Only a tag both
    // asked for and ruled out is enumerated.
    let asked: BTreeSet<&str> = polarities
        .iter()
        .filter(|&(&tag, &(yes, no))| !fixed(tag) && yes && !no)
        .map(|(&tag, _)| tag)
        .collect();
    let free: Vec<&str> = polarities
        .iter()
        .filter(|&(&tag, &(yes, no))| !fixed(tag) && yes && no)
        .map(|(&tag, _)| tag)
        .collect();
    if free.len() > MAX_FREE_TAGS {
        return None;
    }
    let oses: &[&str] = if os_bound { &KNOWN_OS } else { &[DEFAULT_GOOS] };
    let arches: &[&str] = if arch_bound {
        &KNOWN_ARCH
    } else {
        &[DEFAULT_GOARCH]
    };
    let subsets = 1u32 << free.len();
    let holds = oses.iter().any(|&goos| {
        arches.iter().any(|&goarch| {
            (0..subsets).any(|chosen| {
                Build {
                    goos,
                    goarch,
                    free: &free,
                    chosen,
                    asked: &asked,
                }
                .holds(combined)
            })
        })
    });
    Some(holds)
}

/// Whether one build certainly satisfies every one of `exprs`: what a link,
/// an override or a call edge needs. Unknown is no.
pub fn possible<I: IntoIterator<Item = Expr>>(exprs: I) -> bool {
    decide(exprs) == Some(true)
}

/// Whether no build certainly satisfies them all: what telling two
/// declarations apart as build variants needs. Unknown is no.
pub fn impossible<I: IntoIterator<Item = Expr>>(exprs: I) -> bool {
    decide(exprs) == Some(false)
}

/// Whether every build satisfying `scope` certainly satisfies `claim`.
pub fn holds_wherever(scope: Expr, claim: Expr) -> bool {
    impossible([scope, negate(claim)])
}

/// When a file was last changed and its size, or None when it cannot be
/// asked: a file whose stamp changes is read again.
type Stamp = Option<(Option<SystemTime>, u64)>;

fn stamp(path: &Path) -> Stamp {
    let metadata = std::fs::metadata(path).ok()?;
    Some((metadata.modified().ok(), metadata.len()))
}

/// Each Go file's build constraint under one repository, read from its name
/// and its head on disk and read again when the file changes, and which
/// files one build can compile together, each set of constraints decided
/// once: `GoBuilds`.
pub struct GoBuilds<'a> {
    root: &'a Path,
    constraints: HashMap<String, (Stamp, Expr)>,
    decided: HashMap<BTreeSet<Expr>, Option<bool>>,
}

impl<'a> GoBuilds<'a> {
    pub fn new(root: &'a Path) -> GoBuilds<'a> {
        GoBuilds {
            root,
            constraints: HashMap::new(),
            decided: HashMap::new(),
        }
    }

    /// `rel_path`'s constraint (`file_constraint`).
    pub fn constraint(&mut self, rel_path: &str) -> Expr {
        if rel_path.is_empty() {
            return Expr::always();
        }
        let now = stamp(&joined(self.root, rel_path));
        if let Some((then, constraint)) = self.constraints.get(rel_path) {
            if *then == now {
                return constraint.clone();
            }
        }
        let constraint = file_constraint(self.root, rel_path);
        self.constraints
            .insert(rel_path.to_owned(), (now, constraint.clone()));
        constraint
    }

    /// `decide` over the constraints of `rel_paths`, as a set.
    pub fn decide<'p, I: IntoIterator<Item = &'p str>>(&mut self, rel_paths: I) -> Option<bool> {
        let key: BTreeSet<Expr> = rel_paths
            .into_iter()
            .map(|path| self.constraint(path))
            .collect();
        if let Some(&decided) = self.decided.get(&key) {
            return decided;
        }
        let decided = decide(key.iter().cloned());
        self.decided.insert(key, decided);
        decided
    }

    /// Whether one build certainly compiles every file in `rel_paths`.
    pub fn together<'p, I: IntoIterator<Item = &'p str>>(&mut self, rel_paths: I) -> bool {
        self.decide(rel_paths) == Some(true)
    }

    /// Whether no build certainly compiles them all.
    pub fn apart<'p, I: IntoIterator<Item = &'p str>>(&mut self, rel_paths: I) -> bool {
        self.decide(rel_paths) == Some(false)
    }
}
