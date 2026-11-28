use std::fs;
use std::path::{Path, PathBuf};

use super::builds::{self, Expr};
use super::package::{read_package_clause, root_package_clause, PACKAGE_CLAUSE_SCAN_BYTES};
use super::{language, read_head, syntax, Engine};
use crate::engine::tests::Scratch;
use crate::grammars::{grammar, parse_described};
use crate::trees::{describe, Described};

/// Go sources of every shape a parse meets: declarations, generics, a
/// syntax error the grammar recovers from, nothing at all, and text that is
/// not UTF-8.
const SOURCES: [&[u8]; 6] = [
    b"package p\n\nimport \"fmt\"\n\ntype T struct{ A int }\n\nfunc (t *T) M() { fmt.Println(t.A) }\n",
    b"package p\n\ntype Box[T any] struct{ v T }\n\nfunc Get[T any](b Box[T]) T { return b.v }\n",
    b"package p\n\nfunc f( {\n\tx := \n}\n",
    b"",
    b"// caf\xc3\xa9\npackage p\n\nvar s = \"\xff\xfe\"\n",
    b"package p\n\ntype I interface {\n\tio.Reader\n\tM(int) (string, error)\n}\n",
];

fn go_files(scratch: &Scratch, copies: usize) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    for copy in 0..copies {
        for (index, source) in SOURCES.iter().enumerate() {
            let path = scratch.0.join(format!("f{copy:02}_{index}.go"));
            fs::write(&path, source).unwrap();
            paths.push(path);
        }
    }
    // A file that is not there, and a folder: neither can be read.
    paths.push(scratch.0.join("absent.go"));
    paths.push(scratch.0.clone());
    paths
}

fn parsed(engine: &Engine, paths: &[PathBuf]) -> Vec<Option<Vec<Described>>> {
    engine.go_parse_map(paths, |file| {
        file.map(|file| describe(file.tree.root_node()))
    })
}

#[test]
fn files_are_parsed_on_the_pool_as_one_parser_parses_them() {
    let scratch = Scratch::new("go-parse");
    let paths = go_files(&scratch, 6);
    let go = grammar("go").unwrap();
    let expected: Vec<Option<Vec<Described>>> = paths
        .iter()
        .map(|path| {
            let source = fs::read(path).ok()?;
            parse_described(go, &source).unwrap()
        })
        .collect();
    assert!(expected.iter().flatten().count() > 30);

    for threads in [1, 2, 3, 8, 0] {
        assert_eq!(parsed(&Engine::new(threads).unwrap(), &paths), expected);
        assert_eq!(
            parsed(&Engine::always_pooled(threads).unwrap(), &paths),
            expected
        );
    }
    assert_eq!(expected[expected.len() - 2], None);
    assert_eq!(expected[expected.len() - 1], None);
}

#[test]
fn a_parse_hands_over_the_bytes_it_read() {
    let scratch = Scratch::new("go-source");
    let paths = go_files(&scratch, 1);
    let sources = Engine::new(2)
        .unwrap()
        .go_parse_map(&paths, |file| file.map(|file| file.source.clone()));
    for (source, expected) in sources.iter().zip(SOURCES) {
        assert_eq!(source.as_deref(), Some(expected));
    }
}

#[test]
fn every_node_kind_and_field_is_the_compiled_grammars() {
    let go = language();
    for kind in syntax::NODE_KINDS {
        assert_ne!(go.id_for_node_kind(kind, true), 0, "{kind}");
    }
    for token in syntax::TOKENS {
        assert_ne!(go.id_for_node_kind(token, false), 0, "{token}");
    }
    for field in syntax::FIELDS {
        assert!(go.field_id_for_name(field).is_some(), "{field}");
    }
    // The check bites: Python's name for a C function is no Go kind, and
    // Python's `parameters` field is no Go field.
    assert_eq!(go.id_for_node_kind("function_definition", true), 0);
    assert!(go.field_id_for_name("decorator").is_none());
}

#[test]
fn no_name_is_listed_twice() {
    for table in [syntax::NODE_KINDS, syntax::TOKENS, syntax::FIELDS] {
        let mut sorted = table.to_vec();
        sorted.sort_unstable();
        sorted.dedup();
        assert_eq!(sorted.len(), table.len());
    }
}

#[test]
fn the_package_clause_is_the_first_code() {
    for (head, clause) in [
        ("package p\n", Some("p")),
        ("// doc\n\npackage main\n", Some("main")),
        (
            "/*\npackage docs describes why this stub exists.\n*/\npackage main\n",
            Some("main"),
        ),
        ("package main;\n\nfunc main() {}\n", Some("main")),
        ("package main;;\n", Some("main")),
        ("package/*doc*/gen\n", Some("gen")),
        ("/* a */ /* b */ package q // c\n", Some("q")),
        ("//go:build linux\n\npackage p\n", Some("p")),
        ("package p\r\n", Some("p")),
        ("\u{3000}package\u{a0}p\u{85}", Some("p")),
        ("package\n", None),
        ("func f() {}\npackage p\n", None),
        ("/* never closed\npackage p\n", None),
        ("", None),
        // `/*/` opens a comment; its `*` closes nothing.
        ("/*/ package p */ package q\n", Some("q")),
    ] {
        assert_eq!(root_package_clause(head).as_deref(), clause, "{head:?}");
    }
}

#[test]
fn the_package_clause_is_read_from_the_files_head() {
    let scratch = Scratch::new("go-clause");
    let near = scratch.0.join("near.go");
    let far = scratch.0.join("far.go");
    let cut = scratch.0.join("cut.go");
    // The clause line ends on the head's last byte.
    let clause = "package pkg\n";
    let filler = "\n".repeat(PACKAGE_CLAUSE_SCAN_BYTES - clause.len());
    fs::write(&near, format!("{filler}{clause}")).unwrap();
    fs::write(
        &far,
        format!("{filler}{}{clause}", "\n".repeat(clause.len())),
    )
    .unwrap();
    fs::write(&cut, format!("{filler}package pkgname\n")).unwrap();
    assert_eq!(read_package_clause(&near).as_deref(), Some("pkg"));
    assert_eq!(read_package_clause(&far), None);
    // Cut off inside the name, the head holds what it holds of it.
    assert_eq!(read_package_clause(&cut).as_deref(), Some("pkgn"));
    assert_eq!(read_package_clause(&scratch.0.join("absent.go")), None);
    assert_eq!(read_package_clause(&scratch.0), None);
}

#[cfg(unix)]
#[test]
fn a_pipe_is_never_waited_on() {
    let scratch = Scratch::new("go-pipe");
    let pipe = scratch.0.join("p.go");
    let name = std::ffi::CString::new(pipe.as_os_str().as_encoded_bytes()).unwrap();
    // SAFETY: a valid, NUL-terminated path.
    assert_eq!(unsafe { libc::mkfifo(name.as_ptr(), 0o600) }, 0);
    assert_eq!(read_head(&pipe, builds::HEADER_SCAN_BYTES), None);
    assert_eq!(read_package_clause(&pipe), None);
    assert_eq!(
        builds::file_constraint(&scratch.0, "p.go"),
        builds::Expr::always()
    );
}

fn constraint_corpus(root: &Path) -> Vec<String> {
    let heads = [
        "//go:build linux\n\npackage p\n",
        "//go:build !linux\n\npackage p\n",
        "//go:build linux && (amd64 || arm64)\n\npackage p\n",
        "//go:build cgo || !cgo && windows\n\npackage p\n",
        "// +build darwin,!cgo freebsd\n\npackage p\n",
        "//go:build ((linux)\n\npackage p\n",
        "package p\n",
    ];
    let names = [
        "a.go",
        "b_linux.go",
        "c_windows_amd64.go",
        "d_arm64_test.go",
    ];
    let mut rel_paths = Vec::new();
    for (index, head) in heads.iter().enumerate() {
        for name in names {
            let rel_path = format!("d{index}/{name}");
            fs::create_dir_all(root.join(format!("d{index}"))).unwrap();
            fs::write(root.join(&rel_path), head).unwrap();
            rel_paths.push(rel_path);
        }
    }
    rel_paths.push("missing_windows.go".to_owned());
    rel_paths.push(String::new());
    rel_paths
}

#[test]
fn constraints_decisions_and_clauses_keep_their_order_on_any_number_of_threads() {
    let scratch = Scratch::new("go-batches");
    let rel_paths = constraint_corpus(&scratch.0);
    let groups: Vec<Vec<String>> = rel_paths
        .iter()
        .enumerate()
        .flat_map(|(i, first)| {
            rel_paths
                .iter()
                .skip(i)
                .step_by(3)
                .map(move |second| vec![first.clone(), second.clone()])
        })
        .chain([Vec::new(), rel_paths[..5].to_vec()])
        .collect();
    let paths: Vec<PathBuf> = rel_paths.iter().map(|path| scratch.0.join(path)).collect();
    let constraints: Vec<Expr> = rel_paths
        .iter()
        .map(|path| builds::file_constraint(&scratch.0, path))
        .collect();
    let mut one = builds::GoBuilds::new(&scratch.0);
    let decisions: Vec<Option<bool>> = groups
        .iter()
        .map(|group| one.decide(group.iter().map(String::as_str)))
        .collect();
    let clauses: Vec<Option<String>> = paths.iter().map(|path| read_package_clause(path)).collect();
    assert!(decisions.contains(&Some(true)));
    assert!(decisions.contains(&Some(false)));
    assert!(decisions.contains(&None));

    for threads in [1, 2, 3, 0] {
        for engine in [
            Engine::new(threads).unwrap(),
            Engine::always_pooled(threads).unwrap(),
        ] {
            assert_eq!(
                engine.go_build_constraints(&scratch.0, &rel_paths),
                constraints
            );
            assert_eq!(engine.go_build_decisions(&scratch.0, &groups), decisions);
            assert_eq!(engine.go_package_clauses(&paths), clauses);
        }
    }
}
