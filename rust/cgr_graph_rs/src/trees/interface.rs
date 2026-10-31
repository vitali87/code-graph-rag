//! The interface fingerprint of a source file, on the tree the indexer parsed.
//!
//! A port of `codebase_rag/parsers/interface_fingerprint.py` (issue #2531),
//! with the callable-parameter scan it asks (`callable_parameter_indices` in
//! `codebase_rag/parsers/utils.py`). It hashes what other files can see of a
//! file: its definitions, every statement outside a function body, and the
//! parts of function bodies cgr's cross-file readers look into; a body none
//! of them reads is replaced by a projection of it. `interface_fingerprint`
//! returns exactly the Python twin's digest.
//!
//! Node types, fields and tokens are Python's own constants
//! (`InterfaceRules`, one `LanguageRules` per language), as in `super`. The
//! Python scope walks it needs (`names_bound_within`, and the facts behind
//! `local_names`) are `scopes`'.
//!
//! It never asks a node for its parent: tree-sitter answers that from the
//! root down, scanning every level's children, which in a wide module or
//! class costs more than the rest of the walk. The defs around a call are
//! tracked on the way down instead.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

use sha2::{Digest, Sha256};
use tree_sitter::{Node, TreeCursor};

use super::scopes::{add_locals, inner_scope_only_names, names_bound_within, scope_facts, ScopeFacts, ScopeRules};
use super::{children, contains, hex, kind, named_children, NodeMap, NodeTypes, Preorder};

/// Which of the Python twin's language branches a language takes: Python's
/// readers, the JS/TS ones, or neither. The values of `InterfaceFlavor`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Flavor {
    Python,
    Js,
    Other,
}

/// One language's `_Rules`.
pub struct LanguageRules {
    pub flavor: Flavor,
    /// Function nodes whose body may be replaced by a projection.
    pub functions: NodeTypes,
    /// The body node types a projection may replace.
    pub blocks: NodeTypes,
    pub definitions: NodeTypes,
    /// Node types that keep a whole body wherever they appear in it.
    pub blockers: NodeTypes,
    /// Function names whose body is always kept (a JS `constructor`).
    pub kept_names: HashSet<String>,
    /// Calls whose named arguments a cross-file binding reads.
    pub calls: NodeTypes,
    /// Returns whose value a cross-file reader resolves.
    pub returns: NodeTypes,
}

/// `_CallableScanConfig`.
pub struct ScanRules {
    pub call: Vec<u8>,
    pub identifier: Vec<u8>,
    /// Scopes that capture an enclosing function's parameters.
    pub closures: NodeTypes,
    /// Scopes the scan never enters.
    pub opaque: NodeTypes,
}

/// The constants the Python twins read, under `InterfaceRules` in
/// `codebase_rag/types_defs.py`.
pub struct InterfaceRules {
    pub languages: HashMap<String, LanguageRules>,
    pub version: Vec<u8>,
    pub open: Vec<u8>,
    pub close: Vec<u8>,
    pub leaf: Vec<u8>,
    pub missing: Vec<u8>,
    pub elided_body: Vec<u8>,
    pub returned: Vec<u8>,
    pub call: Vec<u8>,
    pub argument: Vec<u8>,
    pub local: Vec<u8>,
    pub comment: Vec<u8>,
    pub dunder: String,
    pub body_field: Vec<u8>,
    pub name_field: String,
    pub left_field: Vec<u8>,
    pub arguments_field: Vec<u8>,
    pub value_field: Vec<u8>,
    pub key_field: Vec<u8>,
    pub function_field: Vec<u8>,
    pub property_field: Vec<u8>,
    pub object_field: Vec<u8>,
    pub parameters_field: Vec<u8>,
    pub parameter_field: Vec<u8>,
    pub pattern_field: Vec<u8>,
    pub alias_field: Vec<u8>,
    pub callee_fields: Vec<Vec<u8>>,
    pub identifier: Vec<u8>,
    pub keyword_argument: Vec<u8>,
    pub name_arguments: NodeTypes,
    pub cast_wrappers: NodeTypes,
    pub call_expression: Vec<u8>,
    pub member_expression: Vec<u8>,
    pub prototype_methods: HashSet<String>,
    pub js_binder_fields: NodeMap<Vec<Vec<u8>>>,
    pub js_bound_names: NodeTypes,
    pub js_return: Vec<u8>,
    pub js_pair: Vec<u8>,
    pub js_computed_property_name: Vec<u8>,
    pub js_require: String,
    pub js_typed_parameters: NodeTypes,
    pub py_identifier: Vec<u8>,
    pub py_call: Vec<u8>,
    pub py_attribute: Vec<u8>,
    pub py_return: Vec<u8>,
    pub py_read_returns: NodeTypes,
    /// `self` and `cls`.
    pub receivers: HashSet<String>,
    pub py_target_holders: NodeTypes,
    pub py_target_wrappers: NodeTypes,
    pub py_member_targets: NodeTypes,
    pub py_caller_scope: Vec<u8>,
    pub py_named_parameters: NodeTypes,
    pub py_typed_parameter: Vec<u8>,
    pub py_scope_boundaries: NodeTypes,
    pub py_decorated_definition: Vec<u8>,
    pub py_decorated_definitions: NodeTypes,
    pub py_shadow_binders: NodeTypes,
    pub py_as_pattern_target: Vec<u8>,
    pub py_imports: NodeTypes,
    pub py_global: Vec<u8>,
    pub py_case_pattern: Vec<u8>,
    pub py_dotted_name: Vec<u8>,
    pub py_aliased_import: Vec<u8>,
    pub python_scan: ScanRules,
    pub js_scan: ScanRules,
}

/// What the twin's stack holds: a node still to expand, a bare token, or a
/// length-prefixed one (a token kind and its text). `CloseDef` is the
/// closing token of a Python def, which also leaves it.
enum Item<'a> {
    Node(Node<'a>),
    Raw(&'a [u8]),
    Token(&'a [u8], &'a [u8]),
    Owned(&'a [u8], Vec<u8>),
    CloseDef,
}

/// What one fingerprint carries along its walk.
#[derive(Default)]
struct State<'a> {
    /// The Python defs the walk is inside, outermost first.
    defs: Vec<Node<'a>>,
    /// `scope_facts` per def.
    facts: HashMap<usize, ScopeFacts>,
    /// `local_names(def, {})` per def, which `_caller_locals` caches.
    locals: HashMap<usize, HashSet<String>>,
}

/// `interface_fingerprint(root_node, language)`: the hex digest of the
/// interface of the file `root` is the root of.
pub fn interface_fingerprint(
    root: Node,
    source: &[u8],
    language: &LanguageRules,
    rules: &InterfaceRules,
    scopes: &ScopeRules,
) -> String {
    let walk = Walk { source, rules, language, scopes };
    let mut state = State::default();
    let mut digest = Sha256::new();
    Digest::update(&mut digest, &rules.version);
    let mut cursor = root.walk();
    let mut stack = vec![Item::Node(root)];
    while let Some(item) = stack.pop() {
        match item {
            Item::Raw(bytes) => Digest::update(&mut digest, bytes),
            Item::Token(token_kind, text) => token(&mut digest, token_kind, text),
            Item::Owned(token_kind, text) => token(&mut digest, token_kind, &text),
            Item::CloseDef => {
                Digest::update(&mut digest, &rules.close);
                state.defs.pop();
            }
            Item::Node(node) => {
                if !walk.is_comment(node) {
                    walk.expand(node, &mut digest, &mut stack, &mut cursor, &mut state);
                }
            }
        }
    }
    hex(&digest.finalize())
}

/// `_token(kind, text)`: length-prefixed, so no leaf text can imitate a
/// token boundary. tree-sitter's byte offsets are 32-bit, so every length
/// fits the 4-byte big-endian prefix `python::interface_rules_from` checks.
fn token(digest: &mut Sha256, token_kind: &[u8], text: &[u8]) {
    Digest::update(digest, token_kind);
    Digest::update(digest, (text.len() as u32).to_be_bytes());
    Digest::update(digest, text);
}

/// The read-only part of one fingerprint: the tree's source and the rules.
struct Walk<'a> {
    source: &'a [u8],
    rules: &'a InterfaceRules,
    language: &'a LanguageRules,
    scopes: &'a ScopeRules,
}

impl<'a> Walk<'a> {
    /// `node.text or b""`, clamped to the source as a Python slice is.
    fn text(&self, node: Node) -> &'a [u8] {
        let end = node.end_byte().min(self.source.len());
        let start = node.start_byte().min(end);
        &self.source[start..end]
    }

    /// `safe_decode_text(node)`: decoded with `errors="replace"`.
    fn decoded(&self, node: Node) -> Cow<'a, str> {
        String::from_utf8_lossy(self.text(node))
    }

    fn is_comment(&self, node: Node) -> bool {
        contains(kind(node), &self.rules.comment)
    }

    fn is(&self, node: Node, node_type: &[u8]) -> bool {
        kind(node) == node_type
    }

    /// `_expand`. The twin queues an opening token, the children (the
    /// elided body swapped for its projection) and a closing token; the
    /// opening token is the very next item it takes, so it is fed here.
    /// The children are queued in order and the run reversed, so the first
    /// is taken first, as the twin's `reversed` queues them.
    fn expand(
        &self,
        node: Node<'a>,
        digest: &mut Sha256,
        stack: &mut Vec<Item<'a>>,
        cursor: &mut TreeCursor<'a>,
        state: &mut State<'a>,
    ) {
        let rules = self.rules;
        let node_type = kind(node);
        if node.child_count() == 0 {
            let token_kind = if node.is_missing() { &rules.missing } else { &rules.leaf };
            token(digest, token_kind, node_type);
            token(digest, token_kind, self.text(node));
            return;
        }
        token(digest, &rules.open, node_type);
        let elided = self.elidable_body(node);
        let opens_def = self.is_caller_scope(node);
        stack.push(if opens_def { Item::CloseDef } else { Item::Raw(&rules.close) });
        let start = stack.len();
        for child in node.children(cursor) {
            if elided.is_some_and(|body| body.id() == child.id()) {
                stack.extend(self.projection(child, node, state));
            } else {
                stack.push(Item::Node(child));
            }
        }
        stack[start..].reverse();
        if opens_def {
            state.defs.push(node);
        }
    }

    /// A def `_caller_locals` reads a Python call's arguments in (a
    /// lambda's calls are its enclosing def's).
    fn is_caller_scope(&self, node: Node) -> bool {
        self.language.flavor == Flavor::Python && self.is(node, &self.rules.py_caller_scope)
    }

    /// `_elidable_body`: the body of a function no cross-file reader looks
    /// into, or None.
    fn elidable_body(&self, node: Node<'a>) -> Option<Node<'a>> {
        let (rules, language) = (self.rules, self.language);
        if !language.functions.contains(kind(node)) {
            return None;
        }
        let body = node.child_by_field_name(&rules.body_field)?;
        if !language.blocks.contains(kind(body)) {
            return None;
        }
        let name = node.child_by_field_name(&rules.name_field).map(|name| self.decoded(name)).unwrap_or_default();
        let dunder = language.flavor == Flavor::Python
            && name.starts_with(rules.dunder.as_str())
            && name.ends_with(rules.dunder.as_str());
        if language.kept_names.contains(name.as_ref()) || dunder {
            return None;
        }
        if self.invokes_a_parameter(node) || self.body_is_read(body) {
            return None;
        }
        Some(body)
    }

    // ------------------------------------------------- whole-body readers

    /// `_body_is_read`. Nested definitions are searched too, as the twin
    /// says why.
    fn body_is_read(&self, body: Node<'a>) -> bool {
        let language = self.language;
        let mut nodes = Preorder::new(body);
        // The twin starts from the body's children.
        nodes.next();
        let mut readers = Vec::new();
        for node in nodes {
            let node_type = kind(node);
            if language.blockers.contains(node_type) || self.is_read(node) {
                return true;
            }
            if language.calls.contains(node_type) || language.returns.contains(node_type) {
                readers.push(node);
            }
        }
        self.resolves_through_a_local(body, &readers)
    }

    /// `_resolves_through_a_local`: whether what a reader resolves names
    /// anything `body` binds.
    fn resolves_through_a_local(&self, body: Node<'a>, readers: &[Node<'a>]) -> bool {
        let references: Vec<Node> = readers.iter().flat_map(|reader| self.references(*reader)).collect();
        if references.is_empty() {
            return false;
        }
        let bound = self.names_bound_within(body);
        references.iter().any(|reference| self.names_in(*reference).iter().any(|name| bound.contains(name.as_ref())))
    }

    /// `_references`: a returned call's callee or a returned member; or,
    /// for a call handing names on, its callee and each member it hands on.
    fn references(&self, node: Node<'a>) -> Vec<Node<'a>> {
        let rules = self.rules;
        if self.is(node, &rules.py_return) {
            return match self.returned_value(node) {
                Some(value) if self.is(value, &rules.py_call) => self.callee(value).into_iter().collect(),
                Some(value) if self.is(value, &rules.py_attribute) => vec![value],
                _ => Vec::new(),
            };
        }
        let arguments = self.named_arguments(node);
        if arguments.is_empty() {
            return Vec::new();
        }
        let mut references: Vec<Node> = arguments
            .iter()
            .filter(|(_, value)| !self.is(*value, &rules.identifier))
            .map(|(_, value)| *value)
            .collect();
        references.extend(self.callee(node));
        references
    }

    /// `_names_in`: every identifier under `node`.
    fn names_in(&self, node: Node<'a>) -> Vec<Cow<'a, str>> {
        Preorder::new(node)
            .filter(|current| self.is(*current, &self.rules.identifier))
            .map(|current| self.decoded(current))
            .filter(|name| !name.is_empty())
            .collect()
    }

    /// `_names_bound_within`.
    fn names_bound_within(&self, body: Node<'a>) -> HashSet<String> {
        if self.language.flavor == Flavor::Python {
            return names_bound_within(body, self.source, self.scopes);
        }
        let mut names = HashSet::new();
        for node in Preorder::new(body) {
            if let Some(fields) = self.rules.js_binder_fields.get(kind(node)) {
                for field in fields {
                    if let Some(target) = node.child_by_field_name(field) {
                        self.js_bound_names(target, &mut names);
                    }
                }
            }
        }
        names
    }

    /// `_js_bound_names`: every name inside a binding pattern.
    fn js_bound_names(&self, target: Node<'a>, names: &mut HashSet<String>) {
        for node in Preorder::new(target) {
            if self.rules.js_bound_names.contains(kind(node)) {
                self.insert_name(node, names);
            }
        }
    }

    fn callee(&self, call: Node<'a>) -> Option<Node<'a>> {
        self.rules.callee_fields.iter().find_map(|field| call.child_by_field_name(field))
    }

    /// `_named_arguments`: (position or keyword, value) of each argument of
    /// `call` that names something, every non-keyword child of the argument
    /// list counted, a comment included.
    fn named_arguments(&self, call: Node<'a>) -> Vec<(String, Node<'a>)> {
        let rules = self.rules;
        let Some(arguments) = call.child_by_field_name(&rules.arguments_field) else {
            return Vec::new();
        };
        let mut named = Vec::new();
        let mut position = 0usize;
        for child in named_children(arguments) {
            let (key, value) = if self.is(child, &rules.keyword_argument) {
                (
                    child.child_by_field_name(&rules.name_field).map(|name| self.decoded(name).into_owned()),
                    child.child_by_field_name(&rules.value_field),
                )
            } else {
                let key = position.to_string();
                position += 1;
                (Some(key), Some(child))
            };
            if let (Some(key), Some(value)) = (key, value) {
                let value = self.unwrapped_reference(value);
                if rules.name_arguments.contains(kind(value)) {
                    named.push((key, value));
                }
            }
        }
        named
    }

    /// `_unwrapped_reference`: `a` through `a as any` and through
    /// `a.bind(this)` (or `.call` / `.apply`), to a fixpoint.
    fn unwrapped_reference(&self, value: Node<'a>) -> Node<'a> {
        let mut node = value;
        loop {
            while self.rules.cast_wrappers.contains(kind(node)) && node.named_child_count() > 0 {
                match named_children(node).next() {
                    Some(first) => node = first,
                    None => break,
                }
            }
            match self.bound_function(node) {
                Some(bound) => node = bound,
                None => return node,
            }
        }
    }

    fn bound_function(&self, node: Node<'a>) -> Option<Node<'a>> {
        let rules = self.rules;
        if !self.is(node, &rules.call_expression) {
            return None;
        }
        let member = node.child_by_field_name(&rules.function_field)?;
        if !self.is(member, &rules.member_expression) {
            return None;
        }
        let method = self.decoded(member.child_by_field_name(&rules.property_field)?);
        if !rules.prototype_methods.contains(method.as_ref()) {
            return None;
        }
        member.child_by_field_name(&rules.object_field)
    }

    fn is_read(&self, node: Node<'a>) -> bool {
        match self.language.flavor {
            Flavor::Python => self.python_is_read(node),
            Flavor::Js => self.js_is_read(node),
            Flavor::Other => false,
        }
    }

    fn python_is_read(&self, node: Node<'a>) -> bool {
        let rules = self.rules;
        let node_type = kind(node);
        if rules.py_target_holders.contains(node_type) {
            return node.child_by_field_name(&rules.left_field).is_some_and(|target| self.holds_member(target));
        }
        if rules.py_target_wrappers.contains(node_type) {
            return self.holds_member(node);
        }
        if node_type == rules.py_return.as_slice() {
            // A returned local is typed from every binding in the body.
            return self.returned_value(node).is_some_and(|value| {
                self.is(value, &rules.py_identifier) && !rules.receivers.contains(self.decoded(value).as_ref())
            });
        }
        false
    }

    fn js_is_read(&self, node: Node<'a>) -> bool {
        let rules = self.rules;
        let node_type = kind(node);
        if node_type == rules.js_return.as_slice() {
            return self.returned_value(node).is_some();
        }
        if node_type == rules.call_expression.as_slice() {
            return node.child_by_field_name(&rules.function_field).is_some_and(|callee| {
                self.is(callee, &rules.identifier) && self.decoded(callee) == rules.js_require.as_str()
            });
        }
        if node_type == rules.js_pair.as_slice() {
            return self.installs_under_a_symbol(node);
        }
        false
    }

    /// `_installs_under_a_symbol`: `{ [kX]: value }`.
    fn installs_under_a_symbol(&self, pair: Node<'a>) -> bool {
        let rules = self.rules;
        let Some(key) = pair.child_by_field_name(&rules.key_field) else {
            return false;
        };
        if !self.is(key, &rules.js_computed_property_name) {
            return false;
        }
        named_children(key)
            .find(|child| !self.is_comment(*child))
            .is_some_and(|index| self.is(index, &rules.identifier))
    }

    fn holds_member(&self, target: Node<'a>) -> bool {
        Preorder::new(target).any(|node| self.rules.py_member_targets.contains(kind(node)))
    }

    fn returned_value(&self, return_node: Node<'a>) -> Option<Node<'a>> {
        named_children(return_node).find(|child| !self.is_comment(*child))
    }

    // --------------------------------------------------------- projection

    /// `_projection`: what the elided body of `function` still contributes.
    fn projection(&self, body: Node<'a>, function: Node<'a>, state: &mut State<'a>) -> Vec<Item<'a>> {
        let rules = self.rules;
        let mut items = vec![Item::Raw(&rules.elided_body)];
        for statement in named_children(body) {
            if self.is_comment(statement) {
                continue;
            }
            if self.holds_definition(statement) {
                items.push(Item::Node(statement));
            } else if self.language.flavor == Flavor::Python {
                self.python_returns(statement, &mut items);
            }
        }
        self.call_bindings(body, function, &mut items, state);
        items
    }

    fn holds_definition(&self, statement: Node<'a>) -> bool {
        Preorder::new(statement).any(|node| self.language.definitions.contains(kind(node)))
    }

    /// `_python_returns`: the shape of each return, in source order.
    fn python_returns(&self, statement: Node<'a>, items: &mut Vec<Item<'a>>) {
        let rules = self.rules;
        let mut nodes = Preorder::new(statement);
        while let Some(node) = nodes.next() {
            if !self.is(node, &rules.py_return) {
                continue;
            }
            nodes.skip_children();
            match self.returned_value(node) {
                None => items.push(Item::Raw(&rules.returned)),
                Some(value) if rules.py_read_returns.contains(kind(value)) => {
                    items.push(Item::Raw(&rules.returned));
                    items.push(Item::Node(value));
                }
                Some(value) => items.push(Item::Token(&rules.returned, kind(value))),
            }
        }
    }

    /// `_call_bindings`: each call in the elided body of `function` that
    /// names an argument, in source order.
    fn call_bindings(&self, body: Node<'a>, function: Node<'a>, items: &mut Vec<Item<'a>>, state: &mut State<'a>) {
        let rules = self.rules;
        if self.language.calls.is_empty() {
            return;
        }
        // The defs between `body` and the node the walk is at, with their
        // depth below `body`, innermost last.
        let mut inner: Vec<(usize, Node)> = Vec::new();
        let mut nodes = Preorder::new(body);
        // The twin starts from the body's children.
        nodes.next();
        while let Some(node) = nodes.next() {
            let depth = nodes.depth();
            while inner.last().is_some_and(|(above, _)| *above >= depth) {
                inner.pop();
            }
            if self.is_caller_scope(node) {
                inner.push((depth, node));
                continue;
            }
            if !self.language.calls.contains(kind(node)) {
                continue;
            }
            let arguments = self.named_arguments(node);
            if arguments.is_empty() {
                continue;
            }
            let called = self.callee(node).map(|callee| self.text(callee)).filter(|text| !text.is_empty());
            items.push(Item::Token(&rules.call, called.unwrap_or(kind(node))));
            let own = (self.language.flavor == Flavor::Python).then(|| self.caller_locals(function, &inner, state));
            for (key, value) in arguments {
                items.push(Item::Owned(&rules.argument, key.into_bytes()));
                items.push(Item::Token(&rules.argument, self.text(value)));
                let is_local = || own.is_some_and(|own| own.contains(self.decoded(value).as_ref()));
                if self.is(value, &rules.identifier) && is_local() {
                    items.push(Item::Raw(&rules.local));
                }
            }
        }
    }

    /// `_caller_locals`: `local_names(scope, {})` for the innermost def
    /// around a call, the last of `inner` or else `function`. The defs
    /// around that one are the rest of `inner`, `function` and the defs
    /// the walk is inside: every def above it, as the twin's walk up finds.
    fn caller_locals<'s>(
        &self,
        function: Node<'a>,
        inner: &[(usize, Node<'a>)],
        state: &'s mut State<'a>,
    ) -> &'s HashSet<String> {
        let (scope, between) = match inner.split_last() {
            Some(((_, scope), rest)) => (*scope, rest),
            None => (function, &[][..]),
        };
        if !state.locals.contains_key(&scope.id()) {
            let mut enclosing: Vec<Node> = state.defs.clone();
            if scope.id() != function.id() {
                enclosing.push(function);
            }
            enclosing.extend(between.iter().map(|(_, def)| *def));
            let mut names = HashSet::new();
            for def in std::iter::once(scope).chain(enclosing) {
                let facts = state.facts.entry(def.id()).or_insert_with(|| scope_facts(def, self.source, self.scopes));
                add_locals(facts, &mut names);
            }
            names.extend(inner_scope_only_names(scope, self.source, self.scopes));
            state.locals.insert(scope.id(), names);
        }
        &state.locals[&scope.id()]
    }

    // ---------------------------------------- callable-parameter scan

    /// Whether `callable_parameter_indices(func, language)` is non-empty:
    /// the function calls one of its own parameters.
    fn invokes_a_parameter(&self, func: Node<'a>) -> bool {
        let rules = self.rules;
        let (names, scan) = match self.language.flavor {
            Flavor::Python => (self.python_parameter_names(func), &rules.python_scan),
            Flavor::Js => (self.js_parameter_names(func), &rules.js_scan),
            Flavor::Other => return false,
        };
        let Some(body) = func.child_by_field_name(&rules.body_field) else {
            return false;
        };
        let candidates: HashSet<String> = names.into_iter().collect();
        self.scan_invoked(body, &candidates, scan)
    }

    /// `_scan_invoked_parameters`, stopping at the first invocation.
    fn scan_invoked(&self, scope: Node<'a>, candidates: &HashSet<String>, scan: &ScanRules) -> bool {
        if candidates.is_empty() {
            return false;
        }
        let mut nodes = Preorder::new(scope);
        // The scope's own node is not one of its calls; its children are.
        nodes.next();
        while let Some(child) = nodes.next() {
            if self.bare_call_name(child, scan).is_some_and(|name| candidates.contains(name.as_ref())) {
                return true;
            }
            let child_type = kind(child);
            if scan.closures.contains(child_type) {
                nodes.skip_children();
                let bound = self.scope_bound_names(child);
                let inner: HashSet<String> = candidates.iter().filter(|name| !bound.contains(*name)).cloned().collect();
                if self.scan_invoked(child, &inner, scan) {
                    return true;
                }
            } else if scan.opaque.contains(child_type) {
                nodes.skip_children();
            }
        }
        false
    }

    /// `_bare_call_name`: the callee of a call by bare identifier.
    fn bare_call_name(&self, node: Node<'a>, scan: &ScanRules) -> Option<Cow<'a, str>> {
        if !self.is(node, &scan.call) {
            return None;
        }
        let function = node.child_by_field_name(&self.rules.function_field)?;
        self.is(function, &scan.identifier).then(|| self.decoded(function))
    }

    /// The names a nested closure binds itself, which shadow a captured
    /// parameter of the same name.
    fn scope_bound_names(&self, scope: Node<'a>) -> HashSet<String> {
        if self.language.flavor == Flavor::Python {
            return self.python_scope_bound_names(scope);
        }
        self.js_parameter_names(scope).into_iter().collect()
    }

    /// `python_parameter_names`: a leading `self` / `cls` dropped.
    fn python_parameter_names(&self, func: Node<'a>) -> Vec<String> {
        let Some(parameters) = func.child_by_field_name(&self.rules.parameters_field) else {
            return Vec::new();
        };
        let mut names: Vec<String> =
            named_children(parameters).filter_map(|parameter| self.python_parameter_name(parameter)).collect();
        if names.first().is_some_and(|first| self.rules.receivers.contains(first)) {
            names.remove(0);
        }
        names
    }

    fn python_parameter_name(&self, parameter: Node<'a>) -> Option<String> {
        let rules = self.rules;
        let node_type = kind(parameter);
        if node_type == rules.py_identifier.as_slice() {
            return Some(self.decoded(parameter).into_owned());
        }
        if rules.py_named_parameters.contains(node_type) {
            let name = parameter.child_by_field_name(rules.name_field.as_bytes())?;
            return self.is(name, &rules.py_identifier).then(|| self.decoded(name).into_owned());
        }
        if node_type == rules.py_typed_parameter.as_slice() {
            return children(parameter)
                .into_iter()
                .find(|child| self.is(*child, &rules.py_identifier))
                .map(|child| self.decoded(child).into_owned());
        }
        None
    }

    /// `_python_scope_bound_names`: a nested def's or lambda's parameters,
    /// its body's targets and its nested defs' and classes' names.
    fn python_scope_bound_names(&self, scope: Node<'a>) -> HashSet<String> {
        let rules = self.rules;
        let mut bound = HashSet::new();
        if let Some(parameters) = scope.child_by_field_name(&rules.parameters_field) {
            bound.extend(named_children(parameters).filter_map(|parameter| self.python_parameter_name(parameter)));
        }
        if let Some(body) = scope.child_by_field_name(&rules.body_field) {
            self.collect_bound_targets(body, &mut bound);
        }
        bound
    }

    fn collect_bound_targets(&self, node: Node<'a>, out: &mut HashSet<String>) {
        let mut nodes = Preorder::new(node);
        nodes.next();
        while let Some(child) = nodes.next() {
            if self.rules.py_scope_boundaries.contains(kind(child)) {
                // A nested def's or class's name binds here; its body is a
                // scope of its own.
                nodes.skip_children();
                if let Some(name) = self.boundary_name(child).filter(|name| !name.is_empty()) {
                    out.insert(name.into_owned());
                }
                continue;
            }
            self.collect_direct_bindings(child, out);
        }
    }

    /// `_python_scope_boundary_name`: a decorated definition's name is on
    /// the definition it wraps.
    fn boundary_name(&self, child: Node<'a>) -> Option<Cow<'a, str>> {
        let rules = self.rules;
        let named = if self.is(child, &rules.py_decorated_definition) {
            children(child)
                .into_iter()
                .find(|inner| rules.py_decorated_definitions.contains(kind(*inner)))
                .unwrap_or(child)
        } else {
            child
        };
        named.child_by_field_name(rules.name_field.as_bytes()).map(|name| self.decoded(name))
    }

    fn collect_direct_bindings(&self, child: Node<'a>, out: &mut HashSet<String>) {
        let rules = self.rules;
        let child_type = kind(child);
        if rules.py_shadow_binders.contains(child_type) {
            if let Some(left) = child.child_by_field_name(&rules.left_field) {
                self.collect_target_identifiers(left, out);
            }
        } else if child_type == rules.py_as_pattern_target.as_slice() {
            self.collect_target_identifiers(child, out);
        } else if rules.py_imports.contains(child_type) {
            self.collect_import_bound_names(child, out);
        } else if child_type == rules.py_global.as_slice() {
            for declared in named_children(child) {
                if self.is(declared, &rules.py_identifier) {
                    self.insert_name(declared, out);
                }
            }
        } else if child_type == rules.py_case_pattern.as_slice() {
            self.collect_case_pattern_bindings(child, out);
        }
    }

    fn insert_name(&self, node: Node<'a>, out: &mut HashSet<String>) {
        let name = self.decoded(node);
        if !name.is_empty() {
            out.insert(name.into_owned());
        }
    }

    fn collect_target_identifiers(&self, node: Node<'a>, out: &mut HashSet<String>) {
        let mut nodes = Preorder::new(node);
        while let Some(current) = nodes.next() {
            if self.is(current, &self.rules.py_identifier) {
                nodes.skip_children();
                self.insert_name(current, out);
            }
        }
    }

    /// `_python_collect_import_bound_names`: the names an import binds.
    fn collect_import_bound_names(&self, node: Node<'a>, out: &mut HashSet<String>) {
        let rules = self.rules;
        let mut cursor = node.walk();
        for item in node.children_by_field_name(&rules.name_field, &mut cursor) {
            let bound = if self.is(item, &rules.py_aliased_import) {
                item.child_by_field_name(&rules.alias_field)
            } else if self.is(item, &rules.py_dotted_name) {
                named_children(item).find(|child| self.is(*child, &rules.py_identifier))
            } else {
                None
            };
            if let Some(bound) = bound {
                self.insert_name(bound, out);
            }
        }
    }

    /// `_python_collect_case_pattern_bindings`: a `case` binds its capture
    /// patterns, single names, and nothing else.
    fn collect_case_pattern_bindings(&self, node: Node<'a>, out: &mut HashSet<String>) {
        let rules = self.rules;
        let mut nodes = Preorder::new(node);
        while let Some(current) = nodes.next() {
            if self.is(current, &rules.py_dotted_name) {
                nodes.skip_children();
                let identifiers: Vec<Node> =
                    named_children(current).filter(|child| self.is(*child, &rules.py_identifier)).collect();
                if let [only] = identifiers.as_slice() {
                    self.insert_name(*only, out);
                }
            }
        }
    }

    /// `js_ts_parameter_names`: destructuring patterns bind no callable
    /// name and are skipped.
    fn js_parameter_names(&self, func: Node<'a>) -> Vec<String> {
        let rules = self.rules;
        if let Some(parameters) = func.child_by_field_name(&rules.parameters_field) {
            return named_children(parameters)
                .filter_map(|parameter| self.js_parameter_name(parameter))
                .filter(|name| !name.is_empty())
                .collect();
        }
        match func.child_by_field_name(&rules.parameter_field) {
            Some(single) if self.is(single, &rules.identifier) => {
                let name = self.decoded(single);
                if name.is_empty() {
                    Vec::new()
                } else {
                    vec![name.into_owned()]
                }
            }
            _ => Vec::new(),
        }
    }

    fn js_parameter_name(&self, parameter: Node<'a>) -> Option<String> {
        let rules = self.rules;
        if self.is(parameter, &rules.identifier) {
            return Some(self.decoded(parameter).into_owned());
        }
        if rules.js_typed_parameters.contains(kind(parameter)) {
            let pattern = parameter.child_by_field_name(&rules.pattern_field)?;
            return self.is(pattern, &rules.identifier).then(|| self.decoded(pattern).into_owned());
        }
        None
    }
}
