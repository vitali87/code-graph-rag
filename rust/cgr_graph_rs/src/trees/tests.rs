//! The walks on trees this crate parses itself. The expected values are what
//! the Python twins return for the same sources; the Python suite
//! (test_native_trees.py) compares the two on py-tree-sitter's own trees.

use tree_sitter::{Node, Parser, Tree};

use super::*;

fn set(items: &[&str]) -> NodeTypes {
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

// ------------------------------------------------------------------ scopes

/// The constants `native_trees._scope_rules()` hands over, as they stand.
fn scope_rules() -> scopes::ScopeRules {
    scopes::ScopeRules {
        identifier: b"identifier".to_vec(),
        nested_scopes: set(&["function_definition", "class_definition", "lambda"]),
        left_binders: set(&["assignment", "augmented_assignment", "for_statement"]),
        name_binders: set(&["named_expression", "function_definition", "class_definition"]),
        imports: set(&["import_statement", "import_from_statement"]),
        patterns: set(&[
            "pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern", "as_pattern_target", "tuple",
            "list",
        ]),
        declarations: set(&["global_statement", "nonlocal_statement"]),
        comprehensions: set(&["list_comprehension", "set_comprehension", "dictionary_comprehension", "generator_expression"]),
        nonlocal_statement: b"nonlocal_statement".to_vec(),
        with_statement: b"with_statement".to_vec(),
        except_clause: b"except_clause".to_vec(),
        case_pattern: b"case_pattern".to_vec(),
        as_pattern: b"as_pattern".to_vec(),
        aliased_import: b"aliased_import".to_vec(),
        dotted_name: b"dotted_name".to_vec(),
        wildcard: "_".into(),
        function_definition: b"function_definition".to_vec(),
        class_definition: b"class_definition".to_vec(),
        lambda: b"lambda".to_vec(),
        for_in_clause: b"for_in_clause".to_vec(),
        attribute: b"attribute".to_vec(),
        keyword_argument: b"keyword_argument".to_vec(),
        separator: ".".into(),
        left_field: b"left".to_vec(),
        name_field: b"name".to_vec(),
        value_field: b"value".to_vec(),
        alias_field: b"alias".to_vec(),
        module_field: b"module_name".to_vec(),
        parameters_field: b"parameters".to_vec(),
        body_field: b"body".to_vec(),
        attribute_field: b"attribute".to_vec(),
    }
}

const SCOPES: &str = "\
def outer(p, q=1, *args, key=None, **kw):
    global g
    import os.path as osp
    try:
        from .pkg import helpers
    except ImportError:
        helpers = None
    a, (b, *c) = p
    with open(p) as fh:
        for i in args:
            d = [k for k in kw]
    sel = lambda item: key(item)
    def middle(v):
        nonlocal q, v
    return [e for e in args], e
";

fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

#[test]
fn scope_facts_match_the_python_twin() {
    let tree = parse(SCOPES);
    let outer = first_function(&tree);
    let facts = scopes::scope_facts(outer, SCOPES.as_bytes(), &scope_rules());
    assert_eq!(
        facts,
        scopes::ScopeFacts {
            params: strings(&["p", "q", "args", "key", "kw"]),
            plain: strings(&["a", "b", "c", "d", "fh", "i", "middle", "sel"]),
            conditional: vec![
                ("helpers".into(), strings(&["pkg.helpers"])),
                ("osp".into(), strings(&["os.path"])),
            ],
            declared: strings(&["g"]),
            nonlocals: Vec::new(),
        }
    );
    let body = outer.child_by_field_name("body").unwrap();
    let mut cursor = body.walk();
    let middle = body.children(&mut cursor).find(|n| n.kind() == "function_definition").unwrap();
    let facts = scopes::scope_facts(middle, SCOPES.as_bytes(), &scope_rules());
    assert_eq!(facts.params, strings(&["v"]));
    assert_eq!(facts.declared, strings(&["q", "v"]));
    assert_eq!(facts.nonlocals, strings(&["q", "v"]));
    assert!(facts.plain.is_empty() && facts.conditional.is_empty());
}

#[test]
fn inner_scope_only_names_match_the_python_twin() {
    let tree = parse(SCOPES);
    // `e` is the comprehension's, but read again after it: the module's `e`.
    assert_eq!(
        scopes::inner_scope_only_names(first_function(&tree), SCOPES.as_bytes(), &scope_rules()),
        strings(&["item", "k"])
    );
}

// --------------------------------------------------------------- interface

fn names(items: &[&str]) -> std::collections::HashSet<String> {
    items.iter().map(|s| (*s).to_owned()).collect()
}

/// `interface_fingerprint.bridge_rules()` as it stands, with Python its only
/// language.
fn interface_rules() -> interface::InterfaceRules {
    let python = interface::LanguageRules {
        flavor: interface::Flavor::Python,
        functions: set(&["function_definition"]),
        blocks: set(&["block"]),
        definitions: set(&["class_definition", "function_definition"]),
        blockers: set(&[
            "delete_statement", "future_import_statement", "global_statement", "import_from_statement",
            "import_statement", "nonlocal_statement", "yield",
        ]),
        kept_names: names(&[]),
        calls: set(&["call"]),
        returns: set(&["return_statement"]),
    };
    let function_binds = list(&["name", "parameters", "parameter"]);
    let js_binder_fields: NodeMap<Vec<Vec<u8>>> = [
        ("variable_declarator", list(&["name"])),
        ("for_in_statement", list(&["left"])),
        ("catch_clause", list(&["parameter"])),
        ("class_declaration", list(&["name"])),
        ("function_declaration", function_binds.clone()),
        ("generator_function_declaration", function_binds.clone()),
        ("generator_function", function_binds.clone()),
        ("function_expression", function_binds.clone()),
        ("arrow_function", function_binds.clone()),
        ("method_definition", function_binds),
    ]
    .into_iter()
    .map(|(node_type, fields)| (node_type.as_bytes().to_vec(), fields))
    .collect();
    interface::InterfaceRules {
        languages: [("python".to_owned(), python)].into_iter().collect(),
        version: b"interface-fingerprint/3".to_vec(),
        open: b"\x01".to_vec(),
        close: b"\x02".to_vec(),
        leaf: b"\x03".to_vec(),
        missing: b"\x04".to_vec(),
        elided_body: b"\x05".to_vec(),
        returned: b"\x06".to_vec(),
        call: b"\x07".to_vec(),
        argument: b"\x08".to_vec(),
        local: b"\x09".to_vec(),
        comment: b"comment".to_vec(),
        dunder: "__".into(),
        body_field: b"body".to_vec(),
        name_field: "name".into(),
        left_field: b"left".to_vec(),
        arguments_field: b"arguments".to_vec(),
        value_field: b"value".to_vec(),
        key_field: b"key".to_vec(),
        function_field: b"function".to_vec(),
        property_field: b"property".to_vec(),
        object_field: b"object".to_vec(),
        parameters_field: b"parameters".to_vec(),
        parameter_field: b"parameter".to_vec(),
        pattern_field: b"pattern".to_vec(),
        alias_field: b"alias".to_vec(),
        callee_fields: list(&["function", "constructor"]),
        identifier: b"identifier".to_vec(),
        keyword_argument: b"keyword_argument".to_vec(),
        name_arguments: set(&["attribute", "identifier", "member_expression"]),
        cast_wrappers: set(&["as_expression", "non_null_expression", "satisfies_expression"]),
        call_expression: b"call_expression".to_vec(),
        member_expression: b"member_expression".to_vec(),
        prototype_methods: names(&["apply", "bind", "call"]),
        js_binder_fields,
        js_bound_names: set(&["identifier", "shorthand_property_identifier_pattern"]),
        js_return: b"return_statement".to_vec(),
        js_pair: b"pair".to_vec(),
        js_computed_property_name: b"computed_property_name".to_vec(),
        js_require: "require".into(),
        js_typed_parameters: set(&["optional_parameter", "required_parameter"]),
        py_identifier: b"identifier".to_vec(),
        py_call: b"call".to_vec(),
        py_attribute: b"attribute".to_vec(),
        py_return: b"return_statement".to_vec(),
        py_read_returns: set(&["attribute", "call", "identifier"]),
        receivers: names(&["self", "cls"]),
        py_target_holders: set(&["assignment", "augmented_assignment", "for_statement"]),
        py_target_wrappers: set(&["as_pattern_target"]),
        py_member_targets: set(&["attribute", "subscript"]),
        py_caller_scope: b"function_definition".to_vec(),
        py_named_parameters: set(&["default_parameter", "typed_default_parameter"]),
        py_typed_parameter: b"typed_parameter".to_vec(),
        py_scope_boundaries: set(&["class_definition", "decorated_definition", "function_definition"]),
        py_decorated_definition: b"decorated_definition".to_vec(),
        py_decorated_definitions: set(&["class_definition", "function_definition"]),
        py_shadow_binders: set(&["assignment", "for_statement"]),
        py_as_pattern_target: b"as_pattern_target".to_vec(),
        py_imports: set(&["import_from_statement", "import_statement"]),
        py_global: b"global_statement".to_vec(),
        py_case_pattern: b"case_pattern".to_vec(),
        py_dotted_name: b"dotted_name".to_vec(),
        py_aliased_import: b"aliased_import".to_vec(),
        python_scan: interface::ScanRules {
            call: b"call".to_vec(),
            identifier: b"identifier".to_vec(),
            closures: set(&["function_definition", "lambda"]),
            opaque: set(&["class_definition"]),
        },
        js_scan: interface::ScanRules {
            call: b"call_expression".to_vec(),
            identifier: b"identifier".to_vec(),
            closures: set(&["arrow_function", "function_declaration", "function_expression"]),
            opaque: set(&["class_declaration"]),
        },
    }
}

/// `setup`'s body is elided (its calls hand names on, its returns read no
/// local), `calls` keeps its body (it calls its parameter).
const INTERFACE: &str = "\
import os

class Widget:
    def area(self) -> int:
        return self.size * 2

def setup(spec, cb):
    handler = make()
    relay(handler, key=cb.run)  # a comment
    if spec:
        return spec.build()
    return [handler]

def calls(cb):
    cb()
";

fn interface_of(source: &str) -> String {
    let rules = interface_rules();
    let tree = parse(source);
    interface::interface_fingerprint(tree.root_node(), source.as_bytes(), &rules.languages["python"], &rules, &scope_rules())
}

#[test]
fn interface_fingerprint_matches_the_python_twin() {
    assert_eq!(interface_of(INTERFACE), "93f33e79336cce183143a8dd312ea511d4b40b1ac17afed7c4ce75437100be93");
}

#[test]
fn interface_fingerprint_ignores_what_no_caller_reads() {
    let base = interface_of(INTERFACE);
    for edit in [
        INTERFACE.replace("# a comment", "# another comment"),
        INTERFACE.replace("import os\n", "import os\n\n\n"),
        INTERFACE.replace("handler = make()", "handler = make(2)"),
    ] {
        assert_eq!(interface_of(&edit), base, "{edit}");
    }
}

#[test]
fn interface_fingerprint_sees_what_callers_read() {
    let base = interface_of(INTERFACE);
    for edit in [
        INTERFACE.replace("    cb()\n", "    cb(1)\n"),
        INTERFACE.replace("key=cb.run", "other=cb.run"),
        INTERFACE.replace("return [handler]", "return handler"),
        INTERFACE.replace("return spec.build()", "return spec.make()"),
    ] {
        assert_ne!(interface_of(&edit), base, "{edit}");
    }
}
