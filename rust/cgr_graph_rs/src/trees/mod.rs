//! Walks of tree-sitter subtrees, computed on the trees the indexer parsed.
//!
//! The indexer parses each file once, in Python, with py-tree-sitter, and
//! its passes walk that tree node by node: every `node.children`,
//! `node.type` and `node.text` crosses into C and allocates a Python object.
//! The functions here walk the same tree in Rust instead. They are given the
//! very `TSNode` the Python `Node` holds (`python` says how it is borrowed),
//! so nothing is parsed twice and no grammar is compiled into this crate.
//!
//! Each function is a port of the Python function of the same name and
//! returns the same value, byte for byte:
//!
//! - `anchor_hash`: `codebase_rag/parsers/anchor_hash.py`;
//! - `ast_fingerprint`: `compute_ast_fingerprint` in
//!   `codebase_rag/parsers/ast_fingerprint.py`;
//! - `scopes`: the Python scope walks in
//!   `codebase_rag/parsers/py/ast_analyzer.py`;
//! - `interface`: `codebase_rag/parsers/interface_fingerprint.py`, for every
//!   language it has rules for.
//!
//! What they classify (which node types are punctuation, identifiers,
//! literals, blocks, bindings, ...) is not spelled here: Python hands over
//! its own constants as `Rules`, `scopes::ScopeRules` and
//! `interface::InterfaceRules`, so the two twins cannot disagree on them.

use std::collections::{HashMap, HashSet};
use std::ffi::CStr;
use std::hash::{BuildHasherDefault, Hasher};

use blake2::digest::consts::U8;
use blake2::Blake2b;
use sha2::{Digest, Sha256};
use tree_sitter::{ffi, Node};

pub mod interface;
#[cfg(feature = "python")]
pub mod python;
pub mod scopes;

/// FNV-1a. A set of node types is asked about nearly every node a walk
/// visits, and its keys are a grammar's own type names, never text from the
/// file, so it needs no defence against chosen collisions and can skip
/// SipHash's cost. Sets of names read from the source keep the default.
pub struct Fnv(u64);

impl Default for Fnv {
    fn default() -> Fnv {
        Fnv(0xcbf2_9ce4_8422_2325)
    }
}

impl Hasher for Fnv {
    fn write(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 = (self.0 ^ u64::from(*byte)).wrapping_mul(0x0100_0000_01b3);
        }
    }

    fn finish(&self) -> u64 {
        self.0
    }
}

/// A set of node types (or field names), as the rules hold them.
pub type NodeTypes = HashSet<Vec<u8>, BuildHasherDefault<Fnv>>;
/// A map keyed by node type.
pub type NodeMap<V> = HashMap<Vec<u8>, V, BuildHasherDefault<Fnv>>;

/// The constants the Python twins read, as UTF-8 bytes.
pub struct Rules {
    /// `ANCHOR_HASH_VERSION`, the prefix of every anchor hash.
    pub anchor_version: String,
    /// `AST_FP_COMMENT_SUBSTRING`: a node whose type holds it is dropped.
    pub comment: Vec<u8>,
    /// `AST_FP_PUNCT_TYPES`: unnamed tokens that are layout only.
    pub punct: NodeTypes,
    pub id_token: Vec<u8>,
    pub lit_token: Vec<u8>,
    pub id_substring: Vec<u8>,
    pub id_extras: NodeTypes,
    pub lit_substrings: Vec<Vec<u8>>,
    pub lit_extras: NodeTypes,
    pub block_substrings: Vec<Vec<u8>>,
    pub block_extras: NodeTypes,
    pub min_branch_nodes: u64,
    pub wrapped_def_substring: Vec<u8>,
    pub expression_body_types: NodeTypes,
    pub self_body_types: NodeTypes,
    pub hash_open: Vec<u8>,
    pub hash_close: Vec<u8>,
    /// `FIELD_BODY`, the field `_resolve_body` asks for first.
    pub body_field: Vec<u8>,
    /// `DART_SIGNATURE_TYPES`, `DART_SIGNATURE_WRAPPERS` and
    /// `TS_DART_FUNCTION_BODY`, for `dart_body_node`.
    pub dart_signature_types: NodeTypes,
    pub dart_signature_wrappers: NodeTypes,
    pub dart_function_body: Vec<u8>,
}

/// The source a tree was parsed from, for the text of its nodes.
#[derive(Clone, Copy)]
pub struct Source<'a> {
    /// None when the tree keeps no source (py-tree-sitter's `node.text` is
    /// then None).
    pub bytes: Option<&'a [u8]>,
}

impl<'a> Source<'a> {
    /// `node.text or b""`: the node's bytes, clamped to the source as a
    /// Python slice is.
    fn text(&self, node: Node) -> &'a [u8] {
        let Some(bytes) = self.bytes else {
            return &[];
        };
        let end = node.end_byte().min(bytes.len());
        let start = node.start_byte().min(end);
        &bytes[start..end]
    }
}

/// `node.type` as bytes. py-tree-sitter decodes the same C string.
pub fn kind(node: Node) -> &'static [u8] {
    // SAFETY: ts_node_type returns a string the language owns for as long
    // as it is loaded, or NULL for a symbol the language does not have
    // (where py-tree-sitter would fail too).
    let name = unsafe { ffi::ts_node_type(node.into_raw()) };
    if name.is_null() {
        return &[];
    }
    unsafe { CStr::from_ptr(name) }.to_bytes()
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    needle.is_empty() || haystack.windows(needle.len()).any(|w| w == needle)
}

/// `node.children`, in order, as py-tree-sitter lists them (a cursor's
/// first child, then each next sibling).
fn children<'t>(node: Node<'t>) -> Vec<Node<'t>> {
    let mut cursor = node.walk();
    node.children(&mut cursor).collect()
}

fn named_children<'t>(node: Node<'t>) -> impl Iterator<Item = Node<'t>> {
    children(node).into_iter().filter(|child| child.is_named())
}

/// `root`'s subtree, `root` first, in source order: the order a Python walk
/// that pops a stack it refills with `reversed(node.children)` visits it in.
/// One cursor serves the whole walk, where `children` allocates one (and a
/// list) per node; `skip_children` leaves the last node's subtree unvisited.
pub struct Preorder<'t> {
    cursor: tree_sitter::TreeCursor<'t>,
    depth: usize,
    started: bool,
    descend: bool,
    done: bool,
}

impl<'t> Preorder<'t> {
    pub fn new(root: Node<'t>) -> Preorder<'t> {
        Preorder { cursor: root.walk(), depth: 0, started: false, descend: true, done: false }
    }

    /// Leave the children of the node `next` returned last unvisited.
    pub fn skip_children(&mut self) {
        self.descend = false;
    }

    /// How far below the root the node `next` returned last is.
    pub fn depth(&self) -> usize {
        self.depth
    }
}

impl<'t> Iterator for Preorder<'t> {
    type Item = Node<'t>;

    fn next(&mut self) -> Option<Node<'t>> {
        if self.done {
            return None;
        }
        if !self.started {
            self.started = true;
            return Some(self.cursor.node());
        }
        if std::mem::replace(&mut self.descend, true) && self.cursor.goto_first_child() {
            self.depth += 1;
            return Some(self.cursor.node());
        }
        while self.depth > 0 {
            if self.cursor.goto_next_sibling() {
                return Some(self.cursor.node());
            }
            self.cursor.goto_parent();
            self.depth -= 1;
        }
        self.done = true;
        None
    }
}

pub fn hex(bytes: &[u8]) -> String {
    const DIGITS: &[u8; 16] = b"0123456789abcdef";
    let mut out = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        out.push(DIGITS[(b >> 4) as usize] as char);
        out.push(DIGITS[(b & 0xf) as usize] as char);
    }
    out
}

// ------------------------------------------------------------------- dart

/// `dart_body_node`: the sibling `function_body` completing a captured Dart
/// signature, or None.
pub fn dart_body_node<'t>(node: Node<'t>, rules: &Rules) -> Option<Node<'t>> {
    if !rules.dart_signature_types.contains(kind(node)) {
        return None;
    }
    let mut base = node;
    if let Some(parent) = node.parent() {
        if rules.dart_signature_wrappers.contains(kind(parent)) {
            base = parent;
        }
    }
    base.next_named_sibling()
        .filter(|following| kind(*following) == rules.dart_function_body.as_slice())
}

// ------------------------------------------------------------ anchor hash

const SEPARATOR: &[u8] = b"\x00";
const OPEN: &[u8] = b"\x01";
const CLOSE: &[u8] = b"\x02";

/// `anchor_hash(definition, decorators)`: the versioned SHA-256 of the
/// definition's named-node tree with leaf text, decorators first.
pub fn anchor_hash(definition: Node, decorators: &[String], source: Source, rules: &Rules) -> String {
    let mut digest = Sha256::new();
    for decorator in decorators {
        Digest::update(&mut digest, decorator.as_bytes());
        Digest::update(&mut digest, SEPARATOR);
    }
    anchor_walk(definition, source, rules, &mut digest);
    if let Some(body) = dart_body_node(definition, rules) {
        anchor_walk(body, source, rules, &mut digest);
    }
    format!("{}{}", rules.anchor_version, hex(&digest.finalize()))
}

/// Feeds `root`'s subtree to `digest` in the order the Python twin's stack
/// does: a node, then its children in source order, then its close marker.
/// A cursor walk visits the same nodes in the same order without a stack;
/// every node it descends into was opened, so each climb closes one.
fn anchor_walk(root: Node, source: Source, rules: &Rules, digest: &mut Sha256) {
    let mut cursor = root.walk();
    let mut depth = 0usize;
    loop {
        let node = cursor.node();
        if anchor_open(node, source, rules, digest) {
            if cursor.goto_first_child() {
                depth += 1;
                continue;
            }
            Digest::update(digest, CLOSE);
        }
        loop {
            if depth == 0 {
                return;
            }
            if cursor.goto_next_sibling() {
                break;
            }
            cursor.goto_parent();
            depth -= 1;
            Digest::update(digest, CLOSE);
        }
    }
}

/// What one node adds before its children; true when it was opened, so its
/// children follow and a close marker ends it.
fn anchor_open(node: Node, source: Source, rules: &Rules, digest: &mut Sha256) -> bool {
    let node_type = kind(node);
    if contains(node_type, &rules.comment) {
        return false;
    }
    if !node.is_named() {
        // Keywords and operators stay; punctuation is layout.
        if !rules.punct.contains(node_type) {
            Digest::update(digest, node_type);
            Digest::update(digest, SEPARATOR);
        }
        return false;
    }
    Digest::update(digest, OPEN);
    Digest::update(digest, node_type);
    Digest::update(digest, SEPARATOR);
    if node.child_count() == 0 {
        Digest::update(digest, source.text(node));
        Digest::update(digest, SEPARATOR);
    }
    true
}

// -------------------------------------------------------- AST fingerprint

/// `AstFingerprintResult`: the skeleton's digest, its node count and the
/// sorted digests of its statement-level branches.
#[derive(Debug, PartialEq)]
pub struct Fingerprint {
    pub fingerprint: String,
    pub node_count: u64,
    pub branch_fingerprints: Vec<String>,
}

/// `compute_ast_fingerprint(func_node)`, or None for a bodiless node.
pub fn ast_fingerprint(func_node: Node, rules: &Rules) -> Option<Fingerprint> {
    let body = resolve_body(func_node, rules)?;
    fingerprint_subtree(body, rules)
}

/// `_resolve_body`: the subtree holding a definition's logic.
fn resolve_body<'t>(func_node: Node<'t>, rules: &Rules) -> Option<Node<'t>> {
    if rules.self_body_types.contains(kind(func_node)) {
        return Some(func_node);
    }
    if let Some(body) = func_node.child_by_field_name(&rules.body_field) {
        return Some(body);
    }
    if let Some(body) = dart_body_node(func_node, rules) {
        return Some(body);
    }
    for child in named_children(func_node) {
        if contains(kind(child), &rules.wrapped_def_substring) {
            if let Some(inner) = resolve_body(child, rules) {
                return Some(inner);
            }
        }
    }
    named_children(func_node).find(|child| rules.expression_body_types.contains(kind(*child)))
}

/// `_token_for`: the skeleton token a node contributes, or None to drop it
/// with its subtree.
fn token<'r>(node: Node, rules: &'r Rules) -> Option<&'r [u8]> {
    let node_type = kind(node);
    if contains(node_type, &rules.comment) {
        return None;
    }
    if !node.is_named() {
        return (!rules.punct.contains(node_type)).then_some(node_type);
    }
    if contains(node_type, &rules.id_substring) || rules.id_extras.contains(node_type) {
        return Some(&rules.id_token);
    }
    if rules.lit_extras.contains(node_type)
        || rules.lit_substrings.iter().any(|marker| contains(node_type, marker))
    {
        return Some(&rules.lit_token);
    }
    Some(node_type)
}

fn is_leaf_token(token: &[u8], rules: &Rules) -> bool {
    token == rules.id_token.as_slice() || token == rules.lit_token.as_slice()
}

fn is_block_parent(token: &[u8], rules: &Rules) -> bool {
    rules.block_extras.contains(token)
        || rules.block_substrings.iter().any(|marker| contains(token, marker))
}

/// `AST_FP_DIGEST_SIZE`, which `python::rules_from` checks the constant
/// still is: the hasher's output size is part of its type.
pub const DIGEST_SIZE: usize = 8;
type NodeHasher = Blake2b<U8>;
type NodeDigest = [u8; DIGEST_SIZE];

/// A kept node whose children are still being folded into its digest.
struct Frame {
    hasher: NodeHasher,
    count: u64,
    branch_root: bool,
    block_parent: bool,
}

/// A folded node: its digest, its skeleton-node count and whether its
/// parent is block-ish.
type Closed = (NodeDigest, u64, bool);

impl Frame {
    fn open(token: &[u8], branch_root: bool, rules: &Rules) -> Frame {
        let mut hasher = NodeHasher::new();
        Digest::update(&mut hasher, token);
        Digest::update(&mut hasher, &rules.hash_open);
        Frame { hasher, count: 1, branch_root, block_parent: is_block_parent(token, rules) }
    }

    fn close(mut self, rules: &Rules) -> Closed {
        Digest::update(&mut self.hasher, &rules.hash_close);
        (self.hasher.finalize().into(), self.count, self.branch_root)
    }
}

/// `_fingerprint_subtree`. The Python twin collects the skeleton in pre-order
/// and folds it in reverse; a node's digest depends only on its token and
/// its children's digests in source order, so folding each node as the
/// cursor leaves it gives the same digests, counts and branch set.
fn fingerprint_subtree(body: Node, rules: &Rules) -> Option<Fingerprint> {
    let root_token = token(body, rules)?;
    let mut branches: HashSet<NodeDigest> = HashSet::new();
    let mut stack = vec![Frame::open(root_token, false, rules)];
    let mut cursor = body.walk();
    let mut at_child = !is_leaf_token(root_token, rules) && cursor.goto_first_child();
    loop {
        if at_child {
            let node = cursor.node();
            if let Some(tok) = token(node, rules) {
                let branch_root = stack.last().is_some_and(|parent| parent.block_parent);
                let frame = Frame::open(tok, branch_root, rules);
                if !is_leaf_token(tok, rules) && cursor.goto_first_child() {
                    stack.push(frame);
                    continue;
                }
                fold(frame.close(rules), &mut stack, &mut branches, rules);
            }
            if cursor.goto_next_sibling() {
                continue;
            }
            cursor.goto_parent();
        }
        // Every child of the top frame is folded: close it into its parent.
        let closed = stack.pop().expect("a frame per open node").close(rules);
        if stack.is_empty() {
            let (digest, node_count, _) = closed;
            let mut branch_fingerprints: Vec<String> = branches.iter().map(|d| hex(d)).collect();
            branch_fingerprints.sort();
            return Some(Fingerprint { fingerprint: hex(&digest), node_count, branch_fingerprints });
        }
        fold(closed, &mut stack, &mut branches, rules);
        at_child = cursor.goto_next_sibling();
        if !at_child {
            cursor.goto_parent();
        }
    }
}

fn fold(closed: Closed, stack: &mut [Frame], branches: &mut HashSet<NodeDigest>, rules: &Rules) {
    let (digest, count, branch_root) = closed;
    let parent = stack.last_mut().expect("a closed child has a parent");
    Digest::update(&mut parent.hasher, digest);
    parent.count += count;
    if branch_root && count >= rules.min_branch_nodes {
        branches.insert(digest);
    }
}

#[cfg(test)]
mod tests;
