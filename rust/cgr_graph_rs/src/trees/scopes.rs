//! Which names a Python body binds, computed on the trees the indexer parsed.
//!
//! Ports of the scope walks in `codebase_rag/parsers/py/ast_analyzer.py`,
//! which call resolution and type inference ask about every function they
//! read (issue #3473):
//!
//! - `scope_facts`: what `_parameter_names`, `_bindings_in` and
//!   `_declared_non_local` find in one scope, and the import paths that can
//!   make a binding a re-import rather than a shadow (`_excusing_paths`);
//! - `inner_scope_only_names`: `_inner_scope_only_names`;
//! - `names_bound_within`, and `add_locals` for `local_names(caller, {})`,
//!   which the interface fingerprint (`super::interface`) reads.
//!
//! Both return exactly what their Python twin returns. Node types and field
//! names are Python's own constants (`ScopeRules`), as in `super`.

use std::borrow::Cow;
use std::collections::{BTreeSet, HashSet};

use tree_sitter::Node;

use super::{kind, NodeTypes, Preorder};

/// The constants the Python scope walks read, as UTF-8 bytes.
pub struct ScopeRules {
    pub identifier: Vec<u8>,
    /// `PY_NESTED_SCOPE_TYPES`: a body of its own, not entered.
    pub nested_scopes: NodeTypes,
    /// `PY_LEFT_BINDERS`: statements binding their `left` field.
    pub left_binders: NodeTypes,
    /// `PY_NAME_BINDERS`: nodes binding their `name` field.
    pub name_binders: NodeTypes,
    pub imports: NodeTypes,
    /// `PY_BINDING_PATTERN_TYPES`: targets descended into for names.
    pub patterns: NodeTypes,
    /// `global` and `nonlocal` statements.
    pub declarations: NodeTypes,
    pub comprehensions: NodeTypes,
    pub nonlocal_statement: Vec<u8>,
    pub with_statement: Vec<u8>,
    pub except_clause: Vec<u8>,
    pub case_pattern: Vec<u8>,
    pub as_pattern: Vec<u8>,
    pub aliased_import: Vec<u8>,
    pub dotted_name: Vec<u8>,
    /// `_`, the `case` pattern that captures nothing.
    pub wildcard: String,
    pub function_definition: Vec<u8>,
    pub class_definition: Vec<u8>,
    pub lambda: Vec<u8>,
    pub for_in_clause: Vec<u8>,
    pub attribute: Vec<u8>,
    pub keyword_argument: Vec<u8>,
    /// `SEPARATOR_DOT`, which joins an import's module and name.
    pub separator: String,
    pub left_field: Vec<u8>,
    pub name_field: Vec<u8>,
    pub value_field: Vec<u8>,
    pub alias_field: Vec<u8>,
    pub module_field: Vec<u8>,
    pub parameters_field: Vec<u8>,
    pub body_field: Vec<u8>,
    pub attribute_field: Vec<u8>,
}

/// `ScopeFacts` in `codebase_rag/types_defs.py`, sorted so that equal facts
/// compare equal.
#[derive(Debug, Default, PartialEq)]
pub struct ScopeFacts {
    /// `_parameter_names(scope)`, in order.
    pub params: Vec<String>,
    /// Names bound where nothing can make them a re-import.
    pub plain: Vec<String>,
    /// (name, import paths) for each binding a matching import-map entry
    /// would excuse.
    pub conditional: Vec<(String, Vec<String>)>,
    /// `_declared_non_local(scope)`.
    pub declared: Vec<String>,
    /// The names the scope's own `nonlocal` statements declare.
    pub nonlocals: Vec<String>,
}

/// `safe_decode_text(node)` for a tree with its source: the node's bytes,
/// clamped as a Python slice is, decoded with `errors="replace"`.
fn text<'a>(node: Node, source: &'a [u8]) -> Cow<'a, str> {
    let end = node.end_byte().min(source.len());
    let start = node.start_byte().min(end);
    String::from_utf8_lossy(&source[start..end])
}

fn named_children<'t>(node: Node<'t>) -> Vec<Node<'t>> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor).collect()
}

fn is(node: Node, node_type: &[u8]) -> bool {
    kind(node) == node_type
}

/// `_identifiers_in(target)`: the identifier itself, or every one inside a
/// pattern. Iterative, so a deeply nested target cannot exhaust the stack;
/// callers use the names as a set.
fn identifiers_in<'t>(target: Node<'t>, rules: &ScopeRules, out: &mut Vec<Node<'t>>) {
    let mut stack = vec![target];
    while let Some(node) = stack.pop() {
        if is(node, &rules.identifier) {
            out.push(node);
        } else if rules.patterns.contains(kind(node)) {
            stack.extend(named_children(node));
        }
    }
}

/// `_binding_targets(node)`: the targets one statement or expression binds.
fn binding_targets<'t>(node: Node<'t>, source: &[u8], rules: &ScopeRules, out: &mut Vec<Node<'t>>) {
    let node_type = kind(node);
    if rules.left_binders.contains(node_type) {
        out.extend(node.child_by_field_name(&rules.left_field));
    } else if rules.name_binders.contains(node_type) {
        out.extend(node.child_by_field_name(&rules.name_field));
    } else if node_type == rules.with_statement.as_slice() {
        // `_with_aliases`
        for clause in named_children(node) {
            for item in named_children(clause) {
                let Some(value) = item.child_by_field_name(&rules.value_field) else {
                    continue;
                };
                if is(value, &rules.as_pattern) {
                    out.extend(value.child_by_field_name(&rules.alias_field));
                }
            }
        }
    } else if node_type == rules.except_clause.as_slice() {
        // `_except_aliases`
        for child in named_children(node) {
            if is(child, &rules.as_pattern) {
                out.extend(child.child_by_field_name(&rules.alias_field));
            }
        }
    } else if rules.imports.contains(node_type) {
        // `_import_targets`
        let module = node.child_by_field_name(&rules.module_field);
        for child in named_children(node) {
            if module.is_some_and(|module| module.id() == child.id()) {
                continue;
            }
            if is(child, &rules.aliased_import) {
                out.extend(child.child_by_field_name(&rules.alias_field));
            } else if is(child, &rules.dotted_name) {
                out.extend(named_children(child).into_iter().next());
            }
        }
    } else if node_type == rules.case_pattern.as_slice() {
        // `_capture_targets`
        for child in named_children(node) {
            if !is(child, &rules.dotted_name) {
                continue;
            }
            let segments = named_children(child);
            if segments.len() == 1 && text(child, source) != rules.wildcard.as_str() {
                out.push(segments[0]);
            }
        }
    }
}

/// `_parameter_names(function)`: every name its parameter list binds.
fn parameter_names(function: Node, source: &[u8], rules: &ScopeRules) -> Vec<String> {
    let Some(parameters) = function.child_by_field_name(&rules.parameters_field) else {
        return Vec::new();
    };
    let mut names = Vec::new();
    for parameter in named_children(parameters) {
        let first = if is(parameter, &rules.identifier) {
            Some(parameter)
        } else {
            named_children(parameter).into_iter().find(|child| is(*child, &rules.identifier))
        };
        if let Some(first) = first {
            let name = text(first, source);
            if !name.is_empty() {
                names.push(name.into_owned());
            }
        }
    }
    names
}

/// `_import_bound_leaf(item, name, from_import)`.
fn import_bound_leaf(item: Node, name: &str, from_import: bool, source: &[u8], rules: &ScopeRules) -> Option<String> {
    if is(item, &rules.aliased_import) {
        let alias = item.child_by_field_name(&rules.alias_field)?;
        let target = item.child_by_field_name(&rules.name_field)?;
        if text(alias, source) != name {
            return None;
        }
        let target = text(target, source);
        return (!target.is_empty()).then(|| target.into_owned());
    }
    if !is(item, &rules.dotted_name) {
        return None;
    }
    let full = text(item, source);
    let bound = if from_import {
        full.as_ref()
    } else {
        full.split(rules.separator.as_str()).next().unwrap_or("")
    };
    (bound == name).then(|| bound.to_owned())
}

/// `_import_binding_path(statement, name)`: the dotted path an import binds
/// `name` to, leading dots dropped, or None when it binds another name.
fn import_binding_path(statement: Node, name: &str, source: &[u8], rules: &ScopeRules) -> Option<String> {
    let module = statement.child_by_field_name(&rules.module_field);
    let module_text = module.map(|module| text(module, source));
    let prefix = module_text.as_deref().map_or("", |text| text.trim_start_matches(rules.separator.as_str()));
    for item in named_children(statement) {
        if module.is_some_and(|module| module.id() == item.id()) {
            continue;
        }
        if let Some(leaf) = import_bound_leaf(item, name, module.is_some(), source, rules) {
            if leaf.is_empty() {
                continue;
            }
            let parts: Vec<&str> = [prefix, leaf.as_str()].into_iter().filter(|part| !part.is_empty()).collect();
            return Some(parts.join(rules.separator.as_str()));
        }
    }
    None
}

/// `_guarded_try_body`'s except clause for a scope's own binders, seen from
/// the scope: the first one above `scope` before a def, if any.
fn clause_above<'t>(scope: Node<'t>, rules: &ScopeRules) -> Option<Node<'t>> {
    let mut node = scope;
    loop {
        if is(node, &rules.function_definition) {
            return None;
        }
        if is(node, &rules.except_clause) {
            return Some(node);
        }
        node = node.parent()?;
    }
}

/// `_excusing_paths(binder, name)`: the import paths that make `name` a
/// re-import when the import map holds one of them, sorted and unique.
///
/// `inner` is the nearest except clause between the scope and `binder`,
/// which the walk carries down; `above` caches `clause_above(scope)`, read
/// only when nothing nearer guards the binder.
fn excusing_paths<'t>(
    binder: Node<'t>,
    name: &str,
    inner: Option<Node<'t>>,
    scope: Node<'t>,
    above: &mut Option<Option<Node<'t>>>,
    source: &[u8],
    rules: &ScopeRules,
) -> Vec<String> {
    let mut paths = Vec::new();
    if rules.imports.contains(kind(binder)) {
        paths.extend(import_binding_path(binder, name, source, rules));
    }
    // `_guarded_try_body` walks up from the binder itself and stops at a def.
    let clause = if is(binder, &rules.function_definition) {
        None
    } else if is(binder, &rules.except_clause) {
        Some(binder)
    } else if inner.is_some() {
        inner
    } else {
        *above.get_or_insert_with(|| clause_above(scope, rules))
    };
    let body = clause
        .and_then(|clause| clause.parent())
        .and_then(|statement| statement.child_by_field_name(&rules.body_field));
    if let Some(body) = body {
        for statement in named_children(body) {
            if rules.imports.contains(kind(statement)) {
                paths.extend(import_binding_path(statement, name, source, rules));
            }
        }
    }
    paths.sort();
    paths.dedup();
    paths
}

/// `ScopeFacts` of `scope`: its parameters, what its own body binds (a
/// nested def, class or lambda is not entered), and its `global` and
/// `nonlocal` declarations.
pub fn scope_facts(scope: Node, source: &[u8], rules: &ScopeRules) -> ScopeFacts {
    let mut plain: BTreeSet<String> = BTreeSet::new();
    let mut conditional: BTreeSet<(String, Vec<String>)> = BTreeSet::new();
    let mut above = None;
    let mut targets = Vec::new();
    let mut identifiers = Vec::new();
    // `_bindings_in`: each node with the nearest except clause above it,
    // from the clauses on the path down to the node, kept with their depth.
    let mut clauses: Vec<(usize, Node)> = Vec::new();
    let mut nodes = Preorder::new(scope);
    nodes.next();
    while let Some(node) = nodes.next() {
        let depth = nodes.depth();
        while clauses.last().is_some_and(|(above, _)| *above >= depth) {
            clauses.pop();
        }
        let inner = clauses.last().map(|(_, clause)| *clause);
        targets.clear();
        binding_targets(node, source, rules, &mut targets);
        identifiers.clear();
        for target in &targets {
            identifiers_in(*target, rules, &mut identifiers);
        }
        for identifier in &identifiers {
            let name = text(*identifier, source);
            if name.is_empty() {
                continue;
            }
            let paths = excusing_paths(node, &name, inner, scope, &mut above, source, rules);
            if paths.is_empty() {
                plain.insert(name.into_owned());
            } else {
                conditional.insert((name.into_owned(), paths));
            }
        }
        if rules.nested_scopes.contains(kind(node)) {
            nodes.skip_children();
        } else if is(node, &rules.except_clause) {
            clauses.push((depth, node));
        }
    }
    let (declared, nonlocals) = declarations(scope, source, rules);
    ScopeFacts {
        params: parameter_names(scope, source, rules),
        plain: plain.into_iter().collect(),
        conditional: conditional.into_iter().collect(),
        declared,
        nonlocals,
    }
}

/// `_declared_non_local(scope)`, and the identifiers of the scope's own
/// `nonlocal` statements, which `_nonlocal_declarations` reads; both sorted.
fn declarations(scope: Node, source: &[u8], rules: &ScopeRules) -> (Vec<String>, Vec<String>) {
    let mut declared: BTreeSet<String> = BTreeSet::new();
    let mut nonlocals: BTreeSet<String> = BTreeSet::new();
    let mut nodes = Preorder::new(scope);
    nodes.next();
    while let Some(child) = nodes.next() {
        let child_type = kind(child);
        // The twin walks named children only: an anonymous node, and
        // anything an alias may hang under one, is never read.
        if !child.is_named() || rules.nested_scopes.contains(child_type) {
            nodes.skip_children();
            continue;
        }
        if !rules.declarations.contains(child_type) {
            continue;
        }
        nodes.skip_children();
        let is_nonlocal = child_type == rules.nonlocal_statement.as_slice();
        for declared_name in named_children(child) {
            let name = text(declared_name, source);
            if name.is_empty() {
                continue;
            }
            if is_nonlocal && is(declared_name, &rules.identifier) {
                nonlocals.insert(name.clone().into_owned());
            }
            declared.insert(name.into_owned());
        }
    }
    (declared.into_iter().collect(), nonlocals.into_iter().collect())
}

/// `_inner_scope_bindings(scope)`: what a comprehension's `for` targets or
/// a lambda's parameters bind.
fn inner_scope_bindings<'a>(scope: Node, source: &'a [u8], rules: &ScopeRules) -> Vec<Cow<'a, str>> {
    if is(scope, &rules.lambda) {
        return parameter_names(scope, source, rules).into_iter().map(Cow::Owned).collect();
    }
    let mut names = Vec::new();
    let mut identifiers = Vec::new();
    for clause in named_children(scope) {
        if !is(clause, &rules.for_in_clause) {
            continue;
        }
        if let Some(target) = clause.child_by_field_name(&rules.left_field) {
            identifiers.clear();
            identifiers_in(target, rules, &mut identifiers);
            names.extend(identifiers.iter().map(|identifier| text(*identifier, source)).filter(|name| !name.is_empty()));
        }
    }
    names
}

/// `names_bound_within(node)`: every name anything under `node` binds, and,
/// unlike a scope's facts, everything its nested defs, lambdas and
/// comprehensions bind, their parameters included.
pub fn names_bound_within(node: Node, source: &[u8], rules: &ScopeRules) -> HashSet<String> {
    let mut names = HashSet::new();
    let mut targets = Vec::new();
    let mut identifiers = Vec::new();
    for current in Preorder::new(node) {
        targets.clear();
        binding_targets(current, source, rules, &mut targets);
        identifiers.clear();
        for target in &targets {
            identifiers_in(*target, rules, &mut identifiers);
        }
        for identifier in &identifiers {
            let name = text(*identifier, source);
            if !name.is_empty() {
                names.insert(name.into_owned());
            }
        }
        if is(current, &rules.function_definition) {
            names.extend(parameter_names(current, source, rules));
        } else if is(current, &rules.lambda) || rules.comprehensions.contains(kind(current)) {
            names.extend(inner_scope_bindings(current, source, rules).into_iter().map(Cow::into_owned));
        }
    }
    names
}

/// What one scope adds to `local_names(caller, {})`, read for the caller
/// and each def around it (`_locally_bound_names`): its parameters and the
/// names it binds but declares `global` or `nonlocal`. With no import map,
/// no binding is a re-import.
pub fn add_locals(facts: &ScopeFacts, names: &mut HashSet<String>) {
    let local = |name: &&String| facts.declared.binary_search(name).is_err();
    names.extend(facts.params.iter().cloned());
    names.extend(facts.plain.iter().filter(local).cloned());
    names.extend(facts.conditional.iter().map(|(name, _paths)| name).filter(local).cloned());
}

/// `_is_name_read(identifier)`: not the attribute of `x.name` and not the
/// keyword of `f(name=...)`. `parent` is the identifier's parent.
fn is_name_read(identifier: Node, parent: Node, rules: &ScopeRules) -> bool {
    let field = if is(parent, &rules.attribute) {
        &rules.attribute_field
    } else if is(parent, &rules.keyword_argument) {
        &rules.name_field
    } else {
        return true;
    };
    parent.child_by_field_name(field).is_none_or(|named| named.id() != identifier.id())
}

/// `_inner_scope_only_names(caller)`: names a comprehension or lambda in the
/// caller binds that the caller never reads outside such a scope; sorted.
pub fn inner_scope_only_names(caller: Node, source: &[u8], rules: &ScopeRules) -> Vec<String> {
    let mut bound: HashSet<Cow<str>> = HashSet::new();
    let mut read_outside: HashSet<Cow<str>> = HashSet::new();
    // The names each enclosing comprehension or lambda binds; 0 is none.
    let mut enclosing: Vec<HashSet<Cow<str>>> = vec![HashSet::new()];
    // Each node on the path down from `caller`, with the comprehension or
    // lambda scope its children are read in.
    let mut path: Vec<(Node, usize)> = vec![(caller, 0)];
    let mut nodes = Preorder::new(caller);
    nodes.next();
    while let Some(node) = nodes.next() {
        let depth = nodes.depth();
        path.truncate(depth);
        let (parent, outer) = path[depth - 1];
        if is(node, &rules.function_definition) || is(node, &rules.class_definition) {
            nodes.skip_children();
            continue;
        }
        let mut scope = outer;
        if rules.comprehensions.contains(kind(node)) || is(node, &rules.lambda) {
            let own = inner_scope_bindings(node, source, rules);
            bound.extend(own.iter().cloned());
            let mut names = enclosing[outer].clone();
            names.extend(own);
            enclosing.push(names);
            scope = enclosing.len() - 1;
        } else if is(node, &rules.identifier) && is_name_read(node, parent, rules) {
            let name = text(node, source);
            if !name.is_empty() && !enclosing[scope].contains(&name) {
                read_outside.insert(name);
            }
        }
        path.push((node, scope));
    }
    let mut names: Vec<String> = bound
        .into_iter()
        .filter(|name| !read_outside.contains(name))
        .map(Cow::into_owned)
        .collect();
    names.sort();
    names
}
