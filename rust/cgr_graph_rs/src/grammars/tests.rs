//! The compiled grammars on this crate's runtime. That py-tree-sitter reads
//! them alike, and that they are the grammars cgr's Python packages pin, is
//! test_grammar_pins.py's to check.

use tree_sitter::{Node, Parser, Tree, LANGUAGE_VERSION, MIN_COMPATIBLE_LANGUAGE_VERSION};

use super::bounded_scanner::{bounded, RawLanguage, Serialize, ABI, SERIALIZATION_BUFFER};
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

// `bounded_scanner`, on tree-sitter-python 0.25.0, the grammar it repairs
// (issue #3831).

extern "C" {
    fn tree_sitter_python_external_scanner_serialize(
        payload: *mut std::ffi::c_void,
        buffer: *mut std::ffi::c_char,
    ) -> std::ffi::c_uint;
}

fn python_raw() -> *const () {
    // SAFETY: a grammar's entry point takes nothing and returns its static
    // language.
    unsafe { tree_sitter_python::LANGUAGE.into_raw()() }
}

fn python_bounded() -> *const () {
    // SAFETY: tree-sitter-python's static language.
    unsafe { bounded(python_raw()) }.expect("an ABI 15 grammar with a scanner")
}

fn raw_language(raw: *const ()) -> RawLanguage {
    // SAFETY: an ABI 15 grammar, laid out as `RawLanguage`.
    unsafe { std::ptr::read(raw.cast::<RawLanguage>()) }
}

fn as_language(raw: *const ()) -> tree_sitter::Language {
    // SAFETY: a static language, never freed.
    unsafe { tree_sitter::Language::from_raw(raw.cast()) }
}

fn parse_with(raw: *const (), source: &str) -> Tree {
    let mut parser = Parser::new();
    parser.set_language(&as_language(raw)).expect("ABI 15");
    parser.parse(source, None).expect("a finished parse")
}

/// A function `depth` `if`s deep, one space a level, whose innermost line
/// holds a string: the scanner saves its state inside it with one delimiter
/// open, an odd count ahead of the levels.
fn nested(depth: usize) -> String {
    let mut source = String::from("def deep(x):\n");
    for level in 0..depth {
        source += &format!("{}if x == {level}:\n", " ".repeat(level + 1));
    }
    source + &" ".repeat(depth + 1) + "print(\"deep\")\n return 0\n"
}

/// A saved Python scanner state: no f-string, one delimiter (a plain `"`),
/// then `levels` indentation levels of 1, 2, 3, ... columns.
fn python_state(levels: u16) -> Vec<u8> {
    let mut state = vec![0, 1, 0];
    for level in 1..=levels {
        state.extend(level.to_le_bytes());
    }
    state
}

/// What `serialize` writes for a scanner restored from `state`, into a
/// buffer of `SERIALIZATION_BUFFER` bytes followed by a canary: the length
/// it returns and whether the canary is intact.
fn serialize_from(raw: *const (), state: &[u8]) -> (usize, Vec<u8>, bool) {
    let scanner = raw_language(raw).external_scanner;
    let mut buffer = vec![0xAAu8; SERIALIZATION_BUFFER + 64];
    // SAFETY: the grammar's own create, deserialize and destroy on the
    // scanner it made, and a buffer larger than any `serialize` here writes.
    let length = unsafe {
        let payload = scanner.create.unwrap()();
        scanner.deserialize.unwrap()(payload, state.as_ptr().cast(), state.len() as _);
        let length = scanner.serialize.unwrap()(payload, buffer.as_mut_ptr().cast());
        scanner.destroy.unwrap()(payload);
        length as usize
    };
    let canary = buffer[SERIALIZATION_BUFFER..].iter().all(|&b| b == 0xAA);
    buffer.truncate(length.min(SERIALIZATION_BUFFER));
    (length, buffer, canary)
}

#[test]
fn the_mirrored_layout_reads_python_as_the_runtime_does() {
    let raw = raw_language(python_raw());
    let language = as_language(python_raw());
    assert_eq!(raw.abi_version, ABI);
    assert_eq!(raw.abi_version as usize, language.abi_version());
    assert_eq!(raw.state_count as usize, language.parse_state_count());
    assert_eq!(raw.field_count as usize, language.field_count());
    // SAFETY: the grammar's static, NUL-terminated name.
    let name = unsafe { std::ffi::CStr::from_ptr(raw.name) };
    assert_eq!(name.to_str(), Ok("python"));
    let metadata = language.metadata().expect("ABI 15 records its version");
    assert_eq!(
        raw.metadata,
        [
            metadata.major_version,
            metadata.minor_version,
            metadata.patch_version
        ]
    );
    assert_eq!(
        raw.external_scanner.serialize.map(|f| f as usize),
        Some(tree_sitter_python_external_scanner_serialize as Serialize as usize)
    );
}

#[test]
fn pythons_own_scanner_writes_past_the_buffer() {
    // The bug: from 511 levels behind one open string, 1,025 bytes.
    let (length, _, canary) = serialize_from(python_raw(), &python_state(600));
    assert_eq!(length, SERIALIZATION_BUFFER + 1);
    assert!(!canary);
}

#[test]
fn the_bounded_scanner_writes_only_the_buffer() {
    let (length, kept, canary) = serialize_from(python_bounded(), &python_state(600));
    assert_eq!(length, SERIALIZATION_BUFFER);
    assert!(canary);
    let (_, own, _) = serialize_from(python_raw(), &python_state(600));
    assert_eq!(kept, own);
}

#[test]
fn below_the_buffers_end_the_bounded_scanner_saves_the_grammars_bytes() {
    for levels in [0, 1, 100, 509, 510] {
        let own = serialize_from(python_raw(), &python_state(levels));
        let bounded = serialize_from(python_bounded(), &python_state(levels));
        assert!(own.2 && bounded.2, "{levels}");
        assert_eq!(bounded, own, "{levels}");
    }
}

#[test]
fn the_bounded_copy_parses_a_string_open_past_the_buffer() {
    // The parse finishes, over the whole source. Past the buffer the grammar
    // has lost the nesting (it does without a string open too), so the tree
    // is an error; what matters is that it is a tree.
    for depth in [510, 1200] {
        let source = nested(depth);
        let tree = parse_with(python_bounded(), &source);
        assert_eq!(tree.root_node().end_byte(), source.len(), "{depth}");
    }
}

#[test]
fn the_bounded_copy_parses_as_the_grammar_does() {
    let sources = [
        nested(2),
        nested(509),
        "x = f\"{y!r:>{w}}\" + \"\"\"a\n  b\"\"\" + rb\"\\d\"\n".to_owned(),
        "class A:\n\tdef f(self):\n\t\treturn [\n  1]\n".to_owned(),
    ];
    for source in &sources {
        let own = parse_with(python_raw(), source).root_node().to_sexp();
        assert_eq!(
            parse_with(python_bounded(), source).root_node().to_sexp(),
            own
        );
    }
}

#[test]
fn the_bounded_copy_is_one_static_with_the_grammars_structure() {
    let copy = python_bounded();
    assert_eq!(python_bounded(), copy);
    assert_ne!(copy, python_raw());
    assert_eq!(
        fingerprint(&as_language(copy)),
        fingerprint(&as_language(python_raw()))
    );
    // Bounding the copy again is no second repair.
    // SAFETY: the copy is a static language.
    assert_eq!(unsafe { bounded(copy) }, None);
}

#[test]
fn bounded_declines_what_it_cannot_copy() {
    // SAFETY: null, and the compiled grammars' static languages.
    unsafe {
        assert_eq!(bounded(std::ptr::null()), None);
        // Go's grammar has no external scanner; the C++ fork is ABI 14.
        assert_eq!(raw_language(grammar("go").unwrap().raw()).abi_version, ABI);
        assert_eq!(bounded(grammar("go").unwrap().raw()), None);
        assert_ne!(
            grammar("cpp").unwrap().language().abi_version(),
            ABI as usize
        );
        assert_eq!(bounded(grammar("cpp").unwrap().raw()), None);
    }
}

#[test]
fn only_the_grammar_bounded_first_is_bounded() {
    let copy = python_bounded();
    // Another ABI 15 grammar with a scanner: the bounded `serialize` could
    // call only Python's.
    let other: &'static RawLanguage = Box::leak(Box::new(raw_language(python_raw())));
    // SAFETY: a static ABI 15 language.
    assert_eq!(
        unsafe { bounded((other as *const RawLanguage).cast()) },
        None
    );
    assert_eq!(python_bounded(), copy);
}
