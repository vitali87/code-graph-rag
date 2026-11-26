//! The structure pass on small trees. `codebase_rag/tests/test_engine_structure.py`
//! holds the rows and the map to Python's own on these corners and more.

use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

use super::super::ignore::{IgnoreRules, PatternError};
use super::super::rows::{ContainedRow, FILE, FOLDER, PACKAGE, PROJECT};
use super::super::state::resolve;
use super::super::tests::Scratch;
use super::super::Engine;
use super::{suffix, CaseTable, PathRules, Structure};

const PROJECT_NAME: &str = "proj";

fn markers() -> Vec<String> {
    ["__init__.py", "Cargo.toml", "*.vcxproj", "CMakeLists.txt"]
        .iter()
        .map(|marker| (*marker).to_owned())
        .collect()
}

fn no_rules() -> IgnoreRules {
    IgnoreRules::new([""; 0], [""; 0])
}

fn touch(root: &Path, rel: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, rel.as_bytes()).unwrap();
}

fn structure_on(threads: usize, root: &Path, rules: &IgnoreRules) -> Structure {
    Engine::new(threads)
        .unwrap()
        .structure(
            root.as_os_str(),
            PROJECT_NAME,
            &markers(),
            rules,
            &PathRules::default(),
        )
        .unwrap()
}

fn structure(root: &Path) -> Structure {
    structure_on(2, root, &no_rules())
}

fn keys(structure: &Structure) -> Vec<&str> {
    structure
        .elements
        .iter()
        .map(|(key, _)| key.as_str())
        .collect()
}

fn property<'a>(row: &'a ContainedRow, key: &str) -> &'a str {
    row.node
        .properties
        .iter()
        .find(|(name, _)| *name == key)
        .and_then(|(_, value)| value.to_str())
        .unwrap()
}

fn absolute(path: &Path) -> String {
    resolve(path).to_str().unwrap().replace('\\', "/")
}

#[test]
fn folders_come_in_python_s_path_order_part_by_part() {
    let scratch = Scratch::new("structure-order");
    let root = &scratch.0;
    for rel in [
        "a-b/x/", "a/b/", "a.b/", "B/", "a_b/", "é/", "z/", "中文/", "🎉/", "a/b/c/",
    ] {
        fs::create_dir_all(root.join(rel)).unwrap();
    }

    let found = structure(root);

    // `a/b` before `a-b`, though `a-b` sorts first as a string; upper case
    // before lower, compared exactly; by code point; the root first, as `.`.
    let expected = [
        ".", "B", "a", "a/b", "a/b/c", "a-b", "a-b/x", "a.b", "a_b", "z", "é", "中文", "🎉",
    ];
    assert_eq!(keys(&found), expected);
    // The root gets no Folder row; every other plain folder gets one.
    assert_eq!(found.rows.len(), expected.len() - 1);
    assert!(found.rows.iter().all(|row| row.node.label == FOLDER));
}

#[test]
fn nested_packages_take_their_parents_names_and_the_root_may_be_one() {
    let scratch = Scratch::new("structure-packages");
    let root = &scratch.0;
    touch(root, "__init__.py");
    touch(root, "pkg/__init__.py");
    touch(root, "pkg/sub/Cargo.toml");
    touch(root, "pkg/plain/x.txt");
    touch(root, "pkg/plain/deep/CMakeLists.txt");

    let found = structure(root);

    assert_eq!(
        found.elements,
        [
            (".".to_owned(), Some("proj".to_owned())),
            ("pkg".to_owned(), Some("proj.pkg".to_owned())),
            ("pkg/plain".to_owned(), None),
            (
                "pkg/plain/deep".to_owned(),
                Some("proj.pkg.plain.deep".to_owned())
            ),
            ("pkg/sub".to_owned(), Some("proj.pkg.sub".to_owned())),
        ]
    );
    let labels: Vec<&str> = found.rows.iter().map(|row| row.node.label).collect();
    assert_eq!(labels, [PACKAGE, PACKAGE, FOLDER, PACKAGE, PACKAGE]);
    // The root package and anything at the root hang off the Project; a
    // package's children off it; a plain folder's children off its Folder.
    let parents: Vec<(&str, String)> = found
        .rows
        .iter()
        .map(|row| {
            let start = &row.relationship.start;
            (start.label, start.value.to_str().unwrap().to_owned())
        })
        .collect();
    assert_eq!(
        parents,
        [
            (PROJECT, "proj".to_owned()),
            (PROJECT, "proj".to_owned()),
            (PACKAGE, "proj.pkg".to_owned()),
            (FOLDER, absolute(&root.join("pkg/plain"))),
            (PACKAGE, "proj.pkg".to_owned()),
        ]
    );
    let root_row = &found.rows[0];
    assert_eq!(property(root_row, "path"), ".");
    assert_eq!(
        property(root_row, "name"),
        root.file_name().unwrap().to_str().unwrap()
    );
}

#[test]
fn a_vcxproj_marker_is_a_literal_name_never_a_pattern() {
    let scratch = Scratch::new("structure-vcxproj");
    let root = &scratch.0;
    touch(root, "globbed/app.vcxproj");
    if !cfg!(windows) {
        touch(root, "literal/*.vcxproj");
    }

    let found = structure(root);

    let packages: Vec<&str> = found
        .elements
        .iter()
        .filter(|(_, qn)| qn.is_some())
        .map(|(key, _)| key.as_str())
        .collect();
    let expected: &[&str] = if cfg!(windows) { &[] } else { &["literal"] };
    assert_eq!(packages, expected);
}

#[test]
fn skipped_and_linked_folders_are_left_out_and_never_entered() {
    let scratch = Scratch::new("structure-skipped");
    let root = &scratch.0;
    touch(root, "node_modules/dep/__init__.py");
    touch(root, "kept/__init__.py");
    touch(root, "excluded/inner/x.py");
    fs::create_dir_all(root.join("empty")).unwrap();
    #[cfg(unix)]
    std::os::unix::fs::symlink(root.join("kept"), root.join("link")).unwrap();
    let rules = IgnoreRules::new(["excluded"], [""; 0]);

    let found = structure_on(2, root, &rules);

    // A folder with no file to index is still a Folder.
    assert_eq!(keys(&found), [".", "empty", "kept"]);
}

#[test]
fn a_pattern_python_refuses_is_refused() {
    let scratch = Scratch::new("structure-refused");
    fs::create_dir_all(scratch.0.join("a")).unwrap();
    let rules = IgnoreRules::new(["[z-a]"], [""; 0]);

    let found = Engine::new(1).unwrap().structure(
        scratch.0.as_os_str(),
        PROJECT_NAME,
        &markers(),
        &rules,
        &PathRules::default(),
    );

    assert!(matches!(found, Err(PatternError::BadRange { .. })));
}

#[test]
fn any_number_of_threads_gives_the_same_structure() {
    let scratch = Scratch::new("structure-threads");
    let root = &scratch.0;
    for i in 0..40 {
        touch(root, &format!("d{i}/e{}/f.py", i % 7));
        if i % 3 == 0 {
            touch(root, &format!("d{i}/__init__.py"));
        }
    }

    let one = structure_on(1, root, &no_rules());
    for threads in [2, 0] {
        assert_eq!(structure_on(threads, root, &no_rules()), one);
    }
}

fn file_rows(root: &Path, found: &Structure, paths: &[PathBuf]) -> Vec<Option<ContainedRow>> {
    file_rows_under(root, found, paths, &PathRules::default())
}

fn file_rows_under(
    root: &Path,
    found: &Structure,
    paths: &[PathBuf],
    rules: &PathRules,
) -> Vec<Option<ContainedRow>> {
    Engine::new(2)
        .unwrap()
        .file_rows(root, PROJECT_NAME, &found.elements, paths, rules)
}

#[test]
fn a_file_row_names_its_folder_s_package_or_folder_or_the_project() {
    let scratch = Scratch::new("structure-files");
    let root = &scratch.0;
    touch(root, "top.yaml");
    touch(root, "pkg/__init__.py");
    touch(root, "pkg/data.json");
    touch(root, "plain/readme");
    let found = structure(root);
    let paths = [
        root.join("top.yaml"),
        root.join("pkg/data.json"),
        root.join("plain/readme"),
    ];

    let rows = file_rows(root, &found, &paths);

    let summary: Vec<(String, String, String, &str, String)> = rows
        .iter()
        .map(|row| {
            let row = row.as_ref().unwrap();
            assert_eq!(row.node.label, FILE);
            let start = &row.relationship.start;
            (
                property(row, "path").to_owned(),
                property(row, "extension").to_owned(),
                property(row, "absolute_path").to_owned(),
                start.label,
                start.value.to_str().unwrap().to_owned(),
            )
        })
        .collect();
    assert_eq!(
        summary,
        [
            (
                "top.yaml".to_owned(),
                ".yaml".to_owned(),
                absolute(&root.join("top.yaml")),
                PROJECT,
                "proj".to_owned()
            ),
            (
                "pkg/data.json".to_owned(),
                ".json".to_owned(),
                absolute(&root.join("pkg/data.json")),
                PACKAGE,
                "proj.pkg".to_owned()
            ),
            (
                "plain/readme".to_owned(),
                String::new(),
                absolute(&root.join("plain/readme")),
                FOLDER,
                absolute(&root.join("plain"))
            ),
        ]
    );
}

#[test]
fn a_path_outside_the_root_has_no_row() {
    let scratch = Scratch::new("structure-outside");
    let root = scratch.0.join("repo");
    fs::create_dir_all(&root).unwrap();
    touch(&scratch.0, "other.py");

    let rows = file_rows(&root, &Structure::default(), &[scratch.0.join("other.py")]);

    assert_eq!(rows, [None]);
    // Nor is an absolute path under a relative root.
    let rows = file_rows(Path::new("."), &Structure::default(), &[root.join("a.py")]);
    assert_eq!(rows, [None]);
}

#[cfg(unix)]
#[test]
fn a_linked_file_is_named_by_the_link_not_its_target() {
    let scratch = Scratch::new("structure-link-file");
    let root = &scratch.0;
    touch(root, "real.md");
    std::os::unix::fs::symlink(root.join("real.md"), root.join("alias.md")).unwrap();

    let rows = file_rows(root, &Structure::default(), &[root.join("alias.md")]);

    let row = rows[0].as_ref().unwrap();
    let expected = format!("{}/alias.md", absolute(root));
    assert_eq!(property(row, "absolute_path"), expected);
    assert_eq!(
        row.relationship.end.value,
        OsString::from(expected.as_str())
    );
}

#[test]
fn the_suffix_is_python_s_before_and_from_3_14() {
    let cases = [
        // name, before 3.14, from 3.14
        ("a.py", ".py", ".py"),
        ("a.tar.gz", ".gz", ".gz"),
        ("Makefile", "", ""),
        (".bashrc", "", ""),
        ("a.", "", "."),
        ("a..", "", "."),
        ("..a", ".a", ""),
        (".a.b", ".b", ".b"),
        ("...", "", ""),
        ("a. ", ". ", ". "),
        ("é.ü", ".ü", ".ü"),
    ];
    for (name, before, after) in cases {
        assert_eq!(suffix(name, false), before, "{name}");
        assert_eq!(suffix(name, true), after, "{name}");
    }
}

/// A few lines of `str.lower`'s table, as a Python whose Unicode lowers
/// fewer letters than Rust's would hold it: U+A7CB is a capital Rust's
/// Unicode lowers and Python 3.13's does not.
fn case_table() -> CaseTable {
    CaseTable::new(
        [
            ('B', "b"),
            ('\u{c4}', "\u{e4}"),
            ('\u{391}', "\u{3b1}"),
            ('\u{3a3}', "\u{3c3}"),
            ('\u{130}', "i\u{307}"),
        ]
        .map(|(upper, lower)| (upper, lower.to_owned())),
    )
}

#[test]
fn lowering_follows_python_s_table_and_a_sigma_its_context() {
    let case = case_table();

    assert_eq!(case.lower("ABC"), "abc");
    assert_eq!(case.lower("B\u{c4}"), "b\u{e4}");
    // Not in the table: kept, though Rust's own Unicode lowers it.
    assert_eq!(case.lower("\u{a7cb}x"), "\u{a7cb}x");
    assert_eq!(case.lower("\u{130}B"), "i\u{307}b");
    // A sigma ending a word takes its final form, one inside it does not.
    assert_eq!(case.lower("\u{391}\u{3a3}"), "\u{3b1}\u{3c2}");
    assert_eq!(case.lower("\u{391}\u{3a3}\u{391}"), "\u{3b1}\u{3c3}\u{3b1}");
    assert_eq!(case.lower("\u{3a3}"), "\u{3c3}");
    // A letter after a case-ignorable dot keeps the word going.
    assert_eq!(case.lower("\u{130}\u{3a3}.b"), "i\u{307}\u{3c3}.b");
    assert_eq!(case.lower("\u{130}\u{3a3}."), "i\u{307}\u{3c2}.");
}

#[test]
fn folders_compared_without_case_sort_by_their_lowered_names() {
    let keys: Vec<String> = [
        ".", "a_b", "B", "a", "\u{c4}", "z", "\u{e9}", "-x", "a/B", "a/b0",
    ]
    .iter()
    .map(|key| (*key).to_owned())
    .collect();
    let case = case_table();

    let folded = Engine::folder_order(Path::new("/repo"), &keys, Some(&case));
    let exact = Engine::folder_order(Path::new("/repo"), &keys, None);
    let dot = Engine::folder_order(Path::new("."), &keys, Some(&case));

    assert_eq!(
        folded,
        [".", "-x", "a", "a/B", "a/b0", "a_b", "B", "z", "\u{c4}", "\u{e9}"]
    );
    assert_eq!(
        exact,
        [".", "-x", "B", "a", "a/B", "a/b0", "a_b", "z", "\u{c4}", "\u{e9}"]
    );
    assert_eq!(
        dot,
        ["-x", ".", "a", "a/B", "a/b0", "a_b", "B", "z", "\u{c4}", "\u{e9}"]
    );
}

#[cfg(unix)]
#[test]
fn a_folder_turned_into_a_loop_of_links_has_no_row_where_python_raises() {
    let scratch = Scratch::new("structure-loop");
    let root = &scratch.0;
    touch(root, "a.py");
    std::os::unix::fs::symlink("loop", root.join("loop")).unwrap();
    let paths = [root.join("a.py"), root.join("loop").join("b.py")];

    let raising = file_rows_under(
        root,
        &Structure::default(),
        &paths,
        &PathRules {
            loop_raises: true,
            ..PathRules::default()
        },
    );
    let resolving = file_rows(root, &Structure::default(), &paths);

    assert!(raising[0].is_some());
    assert_eq!(raising[1], None);
    // From 3.13 the loop is left as it is, the folder before it resolved.
    let row = resolving[1].as_ref().unwrap();
    let expected = format!("{}/loop/b.py", absolute(root));
    assert_eq!(property(row, "absolute_path"), expected);
}
