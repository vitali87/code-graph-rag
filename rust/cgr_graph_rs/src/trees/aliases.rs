//! What each local of a Python scope was assigned, reduced to the
//! expressions its value can come from.
//!
//! A port of `python_local_aliases` in
//! `codebase_rag/parsers/py/variable_analyzer.py`: type inference types a
//! local from the leaves of its assignment (`resolved = target or Path(x)`
//! reads `target` and `Path(x)`), and the twin finds those assignments by
//! walking the whole body node by node. `local_aliases` returns what the
//! twin returns, in its order, with each leaf given as its descendant index
//! under the scope (`TreeCursor.goto_descendant`), so Python can hand on the
//! very nodes the twin would.

use std::borrow::Cow;
use std::collections::{HashMap, HashSet};

use tree_sitter::{Node, TreeCursor};

use super::{kind, named_children, NodeTypes};

/// The constants the twin reads, under `AliasRules` in
/// `codebase_rag/types_defs.py`.
pub struct AliasRules {
    /// Nested scopes whose assignments are not this scope's.
    pub boundaries: NodeTypes,
    pub assignment: Vec<u8>,
    pub identifier: Vec<u8>,
    pub attribute: Vec<u8>,
    pub call: Vec<u8>,
    pub binary_operator: Vec<u8>,
    pub boolean_operator: Vec<u8>,
    pub conditional_expression: Vec<u8>,
    pub parenthesized_expression: Vec<u8>,
    pub left_field: Vec<u8>,
    pub right_field: Vec<u8>,
    pub operator_field: Vec<u8>,
    /// `PY_OP_AND`: an `and` yields its right operand when the left is truthy.
    pub and_operator: String,
    /// `PY_BINARY_OPERATOR_DUNDERS`: an operator token's forward method.
    pub dunders: HashMap<String, String>,
    /// `_MAX_ALIAS_DEPTH`, the deepest `_value_leaves` descends.
    pub max_depth: usize,
}

/// One `_Alias`, its leaves as descendant indices under the scope.
pub struct Alias {
    pub local: String,
    pub candidates: Vec<usize>,
    pub dunder: Option<String>,
    pub reflected: Vec<usize>,
}

struct Walk<'a> {
    source: &'a [u8],
    rules: &'a AliasRules,
}

/// `python_local_aliases(caller)`: each local's first alias in the order
/// the twin meets them.
pub fn local_aliases(caller: Node, source: &[u8], rules: &AliasRules) -> Vec<Alias> {
    let walk = Walk { source, rules };
    let mut aliases = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut cursor = caller.walk();
    // The twin pops a stack it refills with each node's children in order,
    // so it takes a node's last child first: the same stack is kept here.
    let mut stack = Vec::new();
    push_children(caller, &mut cursor, &mut stack);
    while let Some(node) = stack.pop() {
        let node_type = kind(node);
        if rules.boundaries.contains(node_type) {
            continue;
        }
        if node_type == rules.assignment.as_slice() {
            if let Some(alias) = walk.alias_of(node, caller, &seen) {
                seen.insert(alias.local.clone());
                aliases.push(alias);
            }
        }
        push_children(node, &mut cursor, &mut stack);
    }
    aliases
}

fn push_children<'t>(node: Node<'t>, cursor: &mut TreeCursor<'t>, stack: &mut Vec<Node<'t>>) {
    cursor.reset(node);
    if cursor.goto_first_child() {
        stack.push(cursor.node());
        while cursor.goto_next_sibling() {
            stack.push(cursor.node());
        }
    }
}

/// `node`'s index in `root`'s subtree in a cursor's pre-order, `root` 0:
/// what `TreeCursor.goto_descendant` takes.
fn descendant_index(root: Node, node: Node) -> usize {
    let mut index = 0;
    let mut current = root;
    while current.id() != node.id() {
        let Some(child) = current.child_with_descendant(node) else {
            break;
        };
        let mut cursor = current.walk();
        for sibling in current.children(&mut cursor) {
            if sibling.id() == child.id() {
                break;
            }
            index += sibling.descendant_count();
        }
        index += 1;
        current = child;
    }
    index
}

impl<'a> Walk<'a> {
    /// `safe_decode_text(node)`: decoded with `errors="replace"`.
    fn decoded(&self, node: Node) -> Cow<'a, str> {
        let end = node.end_byte().min(self.source.len());
        let start = node.start_byte().min(end);
        String::from_utf8_lossy(&self.source[start..end])
    }

    fn is(&self, node: Node, node_type: &[u8]) -> bool {
        kind(node) == node_type
    }

    /// The alias `assignment` records, when its target is a plain local not
    /// aliased yet and its value types something.
    fn alias_of(&self, assignment: Node, caller: Node, seen: &HashSet<String>) -> Option<Alias> {
        let rules = self.rules;
        let left = assignment.child_by_field_name(&rules.left_field)?;
        let right = assignment.child_by_field_name(&rules.right_field)?;
        if !self.is(left, &rules.identifier) {
            return None;
        }
        let local = self.decoded(left);
        if local.is_empty() || seen.contains(local.as_ref()) {
            return None;
        }
        let (candidates, dunder, reflected) = self.assignment_alias(right)?;
        let indices = |nodes: Vec<Node>| nodes.into_iter().map(|leaf| descendant_index(caller, leaf)).collect();
        Some(Alias { local: local.into_owned(), candidates: indices(candidates), dunder, reflected: indices(reflected) })
    }

    /// `assignment_alias(right)`.
    #[allow(clippy::type_complexity)]
    fn assignment_alias<'t>(&self, right: Node<'t>) -> Option<(Vec<Node<'t>>, Option<String>, Vec<Node<'t>>)> {
        let rules = self.rules;
        if self.is(right, &rules.call) {
            return None;
        }
        let mut candidates = Vec::new();
        let mut reflected = Vec::new();
        let mut dunder = None;
        if self.is(right, &rules.binary_operator) {
            let token = right.child_by_field_name(&rules.operator_field).map(|op| self.decoded(op));
            dunder = Some(rules.dunders.get(token.as_deref().unwrap_or(""))?.clone());
            if let Some(left) = right.child_by_field_name(&rules.left_field) {
                self.value_leaves(left, 1, &mut candidates);
            }
            if let Some(operand) = right.child_by_field_name(&rules.right_field) {
                self.value_leaves(operand, 1, &mut reflected);
            }
        } else {
            self.value_leaves(right, 0, &mut candidates);
        }
        if candidates.is_empty() && reflected.is_empty() {
            return None;
        }
        Some((candidates, dunder, reflected))
    }

    /// `_value_leaves(node, depth)`, appended to `out`.
    fn value_leaves<'t>(&self, node: Node<'t>, depth: usize, out: &mut Vec<Node<'t>>) {
        let rules = self.rules;
        if depth > rules.max_depth {
            return;
        }
        let node_type = kind(node);
        if [&rules.identifier, &rules.attribute, &rules.call].iter().any(|leaf| node_type == leaf.as_slice()) {
            out.push(node);
            return;
        }
        let picks: Vec<Node<'t>> = if node_type == rules.boolean_operator.as_slice() {
            self.boolean_value_operands(node)
        } else if node_type == rules.conditional_expression.as_slice() {
            // `a if cond else b`: the first and last named children, which
            // carry no field name.
            let kids: Vec<Node<'t>> = named_children(node).collect();
            match (kids.first(), kids.last()) {
                (Some(first), Some(last)) => vec![*first, *last],
                _ => Vec::new(),
            }
        } else if node_type == rules.binary_operator.as_slice() {
            node.child_by_field_name(&rules.left_field).into_iter().collect()
        } else if node_type == rules.parenthesized_expression.as_slice() {
            named_children(node).take(1).collect()
        } else {
            return;
        };
        for pick in picks {
            self.value_leaves(pick, depth + 1, out);
        }
    }

    /// `_boolean_value_operands(node)`: `a or b` may yield either operand,
    /// `a and b` only the right one.
    fn boolean_value_operands<'t>(&self, node: Node<'t>) -> Vec<Node<'t>> {
        let rules = self.rules;
        let is_and = node
            .child_by_field_name(&rules.operator_field)
            .is_some_and(|op| self.decoded(op) == rules.and_operator.as_str());
        let fields: &[&Vec<u8>] = if is_and { &[&rules.right_field] } else { &[&rules.left_field, &rules.right_field] };
        fields.iter().filter_map(|field| node.child_by_field_name(field.as_slice())).collect()
    }
}
