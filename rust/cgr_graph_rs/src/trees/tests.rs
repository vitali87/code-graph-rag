//! The walks on trees this crate parses itself. The expected values are what
//! the Python twins return for the same sources; the Python suite
//! (test_native_trees.py) compares the two on py-tree-sitter's own trees.

use std::collections::HashSet;

use tree_sitter::{Node, Parser, Tree};

use super::*;

fn set(items: &[&str]) -> HashSet<Vec<u8>> {
    items.iter().map(|s| s.as_bytes().to_vec()).collect()
}

fn list(items: &[&str]) -> Vec<Vec<u8>> {
    items.iter().map(|s| s.as_bytes().to_vec()).collect()
}

/// The constants `native_trees.rules()` hands over, as they stand.
fn rules() -> Rules {
    Rules {
        anchor_version: "ah1:".into(),
        comment: b"comment".to_vec(),
        punct: set(&["(", ")", "{", "}", "[", "]", ",", ";", ":", ".", "->", "=>"]),
        id_token: b"\x01ID".to_vec(),
        lit_token: b"\x02LIT".to_vec(),
        id_substring: b"identifier".to_vec(),
        id_extras: set(&["name", "word", "variable_name", "simple_identifier", "label"]),
        lit_substrings: list(&["literal", "string", "number"]),
        lit_extras: set(&[
            "integer", "float", "true", "false", "null", "nil", "none", "char", "escape_sequence",
            "heredoc_body",
        ]),
        block_substrings: list(&["block", "body"]),
        block_extras: set(&["statement_list", "compound_statement", "macro_definition", "macro_rule", "token_tree"]),
        min_branch_nodes: 5,
        wrapped_def_substring: b"function".to_vec(),
        expression_body_types: set(&["arrow_expression_clause"]),
        self_body_types: set(&["macro_definition"]),
        hash_open: b"(".to_vec(),
        hash_close: b")".to_vec(),
        body_field: b"body".to_vec(),
        dart_signature_types: set(&[]),
        dart_signature_wrappers: set(&[]),
        dart_function_body: b"function_body".to_vec(),
    }
}

fn parse(source: &str) -> Tree {
    let mut parser = Parser::new();
    parser.set_language(&tree_sitter_python::LANGUAGE.into()).unwrap();
    parser.parse(source, None).unwrap()
}

fn first_function(tree: &Tree) -> Node<'_> {
    let root = tree.root_node();
    let mut cursor = root.walk();
    let found = root.children(&mut cursor).find(|n| n.kind() == "function_definition");
    found.expect("a function")
}

const SOURCE: &str = "\
def greet(name, times=2):
    # say hello
    for _ in range(times):
        print(f\"hello {name}\")
    if name:
        return name.upper()
    return None
";

#[test]
fn anchor_hash_matches_the_python_twin() {
    let tree = parse(SOURCE);
    let source = Source { bytes: Some(SOURCE.as_bytes()) };
    assert_eq!(
        anchor_hash(first_function(&tree), &[], source, &rules()),
        "ah1:e490f4cd6d4fb7b231419aa51aeba3e5dae1866d57d23c1556029c0a59fe64c8"
    );
    assert_eq!(
        anchor_hash(first_function(&tree), &["property".into()], source, &rules()),
        "ah1:29061900baa329be4acb50915959d876e4f3bde0e30973cb1c0a414eb51c93e5"
    );
}

#[test]
fn fingerprint_matches_the_python_twin() {
    let tree = parse(SOURCE);
    let expected = Fingerprint {
        fingerprint: "7c201f240d4101ac".into(),
        node_count: 29,
        branch_fingerprints: ["740d5113158dc4eb", "a416d0016945f16e", "f929b3d1b979d6a5", "fd3f30fcac269362"]
            .map(String::from)
            .to_vec(),
    };
    assert_eq!(ast_fingerprint(first_function(&tree), &rules()), Some(expected));
}

#[test]
fn anchor_hash_ignores_comments_and_layout_but_not_names() {
    let r = rules();
    let hash = |text: &str| {
        let tree = parse(text);
        anchor_hash(first_function(&tree), &[], Source { bytes: Some(text.as_bytes()) }, &r)
    };
    let base = hash(SOURCE);
    assert_eq!(base, hash(&SOURCE.replace("    # say hello\n", "")));
    assert_eq!(base, hash(&SOURCE.replace("range(times)", "range( times )")));
    assert_ne!(base, hash(&SOURCE.replace("upper", "lower")));
}

#[test]
fn fingerprint_ignores_renames_and_literals() {
    let r = rules();
    let fingerprint = |text: &str| ast_fingerprint(first_function(&parse(text)), &r).unwrap();
    let base = fingerprint(SOURCE);
    let renamed = fingerprint(&SOURCE.replace("name", "who").replace("hello", "bye"));
    assert_eq!(base, renamed);
    assert_ne!(base, fingerprint(&SOURCE.replace("if name:", "while name:")));
    assert_eq!(base.fingerprint.len(), 16);
    assert!(base.branch_fingerprints.windows(2).all(|w| w[0] < w[1]));
}

#[test]
fn fingerprint_needs_a_body() {
    let tree = parse("x = 1\n");
    let root = tree.root_node();
    assert_eq!(ast_fingerprint(root.child(0).unwrap(), &rules()), None);
}

#[test]
fn text_clamps_like_a_python_slice() {
    let tree = parse(SOURCE);
    let node = first_function(&tree);
    assert_eq!(Source { bytes: Some(b"def") }.text(node), b"def");
    assert_eq!(Source { bytes: None }.text(node), b"");
}
