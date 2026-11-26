//! The compiled grammars on this crate's runtime. That py-tree-sitter reads
//! them alike, and that they are the grammars cgr's Python packages pin, is
//! test_grammar_pins.py's to check.

use tree_sitter::{Node, Parser, Tree, LANGUAGE_VERSION, MIN_COMPATIBLE_LANGUAGE_VERSION};

use super::*;

/// The shape of issue #555: a block comment between two tokens of a macro's
/// value. Upstream tree-sitter-c has one `preproc_arg` for the value, so the
/// text after the comment matches no rule and the definition that follows is
/// lost in an ERROR; the forks repeat `preproc_arg`.
const COMMENTED_MACRO: &str = "#define TEN(x)   \\\n  do {           \\\n    f(x);        \\\n    /*comment*/; \\\n  } while (0)\n\nvoid after(void) {}\n";

fn parse(name: &str, source: &str) -> Tree {
    let mut parser = Parser::new();
    parser
        .set_language(&grammar(name).expect("compiled in").language())
        .expect("a language this runtime loads");
    parser.parse(source, None).expect("a finished parse")
}

fn named_kinds(node: Node) -> Vec<String> {
    let mut cursor = node.walk();
    node.named_children(&mut cursor)
        .map(|child| child.kind().to_owned())
        .collect()
}

#[test]
fn every_grammar_loads_on_this_runtime() {
    for grammar in &GRAMMARS {
        let abi = grammar.language().abi_version();
        assert!(
            (MIN_COMPATIBLE_LANGUAGE_VERSION..=LANGUAGE_VERSION).contains(&abi),
            "{} is ABI {abi}",
            grammar.name
        );
        // A grammar generated before ABI 15 records no name (the C++ fork's).
        let language = grammar.language();
        let name = language.name();
        assert!(name.is_none() || name == Some(grammar.name), "{name:?}");
        assert!(!grammar.highlights.is_empty(), "{}", grammar.name);
    }
}

#[test]
fn grammars_are_found_by_name_only() {
    let names: Vec<&str> = GRAMMARS.iter().map(|grammar| grammar.name).collect();
    assert_eq!(names, ["c", "cpp", "go"]);
    for name in names {
        assert_eq!(grammar(name).map(|grammar| grammar.name), Some(name));
    }
    assert!(grammar("python").is_none());
    assert!(grammar("C").is_none());
}

#[test]
fn the_raw_language_is_the_grammars_own() {
    for grammar in &GRAMMARS {
        assert_eq!(grammar.raw(), grammar.raw());
        assert_eq!(
            grammar.raw().cast::<tree_sitter::ffi::TSLanguage>(),
            grammar.language().into_raw()
        );
    }
}

#[test]
fn the_c_and_cpp_forks_keep_what_follows_a_commented_macro() {
    for name in ["c", "cpp"] {
        let tree = parse(name, COMMENTED_MACRO);
        let root = tree.root_node();
        assert!(!root.has_error(), "{name}: {}", root.to_sexp());
        assert_eq!(
            named_kinds(root),
            ["preproc_function_def", "function_definition"],
            "{name}"
        );
        let value = named_kinds(root.named_child(0).unwrap());
        assert_eq!(
            value,
            [
                "identifier",
                "preproc_params",
                "preproc_arg",
                "comment",
                "preproc_arg"
            ],
            "{name}"
        );
    }
}

#[test]
fn go_parses_without_errors() {
    let tree = parse(
        "go",
        "package p\n\nfunc Map[T, U any](xs []T, f func(T) U) []U {\n\tout := make([]U, 0, len(xs))\n\tfor _, x := range xs {\n\t\tout = append(out, f(x))\n\t}\n\treturn out\n}\n",
    );
    let root = tree.root_node();
    assert!(!root.has_error(), "{}", root.to_sexp());
    assert_eq!(
        named_kinds(root),
        ["package_clause", "function_declaration"]
    );
}

#[test]
fn a_fingerprint_reads_the_grammar_it_is_given() {
    let c = fingerprint(&grammar("c").unwrap().language());
    assert_eq!(c, fingerprint(&grammar("c").unwrap().language()));
    assert_eq!(c.name.as_deref(), Some("c"));
    assert_eq!(c.digest.len(), 64);
    assert!(c
        .digest
        .bytes()
        .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()));
    assert!(c.node_kind_count > 0 && c.field_count > 0 && c.parse_state_count > 0);
    for other in ["cpp", "go"] {
        let found = fingerprint(&grammar(other).unwrap().language());
        assert_ne!(found.digest, c.digest, "{other}");
    }
}

#[test]
fn a_fingerprint_digest_is_the_documented_encoding() {
    // The first kind of every grammar is the end of input, "end", hidden and
    // anonymous; a field's ids start at 1. Rebuilt by hand, byte for byte.
    let language = grammar("go").unwrap().language();
    let mut expected = Sha256::new();
    for id in 0..language.node_kind_count() as u16 {
        expected.update([
            u8::from(language.node_kind_is_named(id)),
            u8::from(language.node_kind_is_visible(id)),
        ]);
        let name = language.node_kind_for_id(id).unwrap();
        expected.update((name.len() as u32).to_be_bytes());
        expected.update(name.as_bytes());
    }
    for id in 1..=language.field_count() as u16 {
        let name = language.field_name_for_id(id).unwrap();
        expected.update((name.len() as u32).to_be_bytes());
        expected.update(name.as_bytes());
    }
    assert_eq!(language.node_kind_for_id(0), Some("end"));
    assert_eq!(fingerprint(&language).digest, hex(&expected.finalize()));
}

#[test]
fn a_missing_name_is_its_own_marker() {
    let mut missing = Sha256::new();
    name_into(&mut missing, None);
    let mut empty = Sha256::new();
    name_into(&mut empty, Some(""));
    assert_ne!(hex(&missing.finalize()), hex(&empty.finalize()));
}

#[test]
fn parse_described_is_the_tree_this_runtime_parses() {
    let source = "int f(int a) { return a; }\n";
    let described = parse_described(grammar("c").unwrap(), source.as_bytes())
        .unwrap()
        .unwrap();
    assert_eq!(described, describe(parse("c", source).root_node()));
    let root = &described[0];
    assert_eq!(
        (root.1.as_str(), root.2, root.3, root.4),
        ("translation_unit", true, 0, source.len())
    );
    assert!(described.iter().any(|node| node.1 == "return_statement"));
}
