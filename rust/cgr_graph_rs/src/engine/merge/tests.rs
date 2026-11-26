//! The module-name plan on small trees, each rule of section 10 with its
//! files on disk. `codebase_rag/tests/test_engine_merge.py` holds the plan
//! to Python's module map on these corners and more.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use super::super::ignore::{IgnoreRules, PatternError};
use super::super::tests::Scratch;
use super::super::Engine;
use super::{FileSummary, NamingInputs, NamingRules, ProjectIndex};

const PROJECT_NAME: &str = "proj";

fn strings(values: &[&str]) -> Vec<String> {
    values.iter().map(|value| (*value).to_owned()).collect()
}

/// The language tables as cgr's constants and language specs hold them.
fn naming() -> NamingRules {
    let languages = [
        (".py", "python"),
        (".pyi", "python"),
        (".js", "javascript"),
        (".jsx", "javascript"),
        (".mjs", "javascript"),
        (".cjs", "javascript"),
        (".ts", "typescript"),
        (".mts", "typescript"),
        (".cts", "typescript"),
        (".tsx", "tsx"),
        (".rs", "rust"),
        (".go", "go"),
        (".c", "c"),
        (".h", "cpp"),
        (".cpp", "cpp"),
    ];
    let families = [
        strings(&["javascript", "typescript", "tsx"]),
        strings(&["c", "cpp"]),
        strings(&["java", "scala"]),
    ];
    NamingRules::new(
        languages
            .iter()
            .map(|(ext, language)| ((*ext).to_owned(), (*language).to_owned()))
            .collect(),
        &families,
        strings(&[
            ".d.mts", ".d.cts", ".d.ts", ".tsx", ".mts", ".cts", ".ts", ".jsx", ".mjs", ".cjs",
            ".js",
        ]),
        ".d.",
        strings(&["__init__.py", "__init__.pyi", "mod.rs"]),
        ".ipynb".to_owned(),
    )
}

fn no_rules() -> IgnoreRules {
    IgnoreRules::new([""; 0], [""; 0])
}

fn touch(root: &Path, rel: &str) {
    let path = root.join(rel);
    fs::create_dir_all(path.parent().unwrap()).unwrap();
    fs::write(&path, rel.as_bytes()).unwrap();
}

/// The language each file is read as, by its suffix; a notebook's is
/// Python.
fn language_of(rel: &str) -> &'static str {
    match rel.rsplit('.').next() {
        Some("py" | "pyi" | "ipynb") => "python",
        Some("js" | "mjs") => "javascript",
        Some("ts" | "mts") => "typescript",
        Some("rs") => "rust",
        Some("c") => "c",
        Some("h") => "cpp",
        _ => "",
    }
}

/// A summary for each file the walk finds that some language reads, at
/// its place in the walk.
fn summaries(root: &Path, rules: &IgnoreRules) -> Vec<FileSummary> {
    let plan = Engine::new(1)
        .unwrap()
        .walk(root.as_os_str(), rules)
        .unwrap();
    plan.files
        .iter()
        .enumerate()
        .filter(|(_, file)| !language_of(&file.rel_path).is_empty())
        .map(|(i, file)| {
            FileSummary::new(
                i,
                file.rel_path.clone(),
                language_of(&file.rel_path).to_owned(),
            )
        })
        .collect()
}

fn merge_on(
    threads: usize,
    root: &Path,
    summaries: Vec<FileSummary>,
    rules: &IgnoreRules,
) -> Result<ProjectIndex, PatternError> {
    let naming = naming();
    let inputs = NamingInputs {
        root,
        project_name: PROJECT_NAME,
        rules,
        naming: &naming,
        dot_suffix: false,
    };
    Engine::new(threads).unwrap().merge(summaries, &inputs)
}

/// Each file's module name, by its path.
fn names_under(root: &Path, rules: &IgnoreRules) -> BTreeMap<String, String> {
    let index = merge_on(2, root, summaries(root, rules), rules).unwrap();
    index
        .modules
        .into_iter()
        .map(|(qn, module)| (module.path, qn))
        .collect()
}

fn names(root: &Path) -> BTreeMap<String, String> {
    names_under(root, &no_rules())
}

fn expected(pairs: &[(&str, &str)]) -> BTreeMap<String, String> {
    pairs
        .iter()
        .map(|(path, qn)| ((*path).to_owned(), (*qn).to_owned()))
        .collect()
}

fn tree(name: &str, rels: &[&str]) -> Scratch {
    let scratch = Scratch::new(name);
    for rel in rels {
        touch(&scratch.0, rel);
    }
    scratch
}

#[test]
fn names_come_from_the_path_less_the_module_extension() {
    let scratch = tree(
        "merge-path",
        &[
            "top.py",
            "a/b/c.py",
            "lib/a.b.js",
            "types/api.d.ts",
            "src/main.rs",
        ],
    );

    assert_eq!(
        names(&scratch.0),
        expected(&[
            ("a/b/c.py", "proj.a.b.c"),
            ("lib/a.b.js", "proj.lib.a.b"),
            ("src/main.rs", "proj.src.main"),
            ("top.py", "proj.top"),
            // `.d.ts` is one extension, dropped whole (#1720).
            ("types/api.d.ts", "proj.types.api"),
        ])
    );
}

#[test]
fn a_package_s_init_and_a_mod_rs_take_their_folder_s_name() {
    let scratch = tree(
        "merge-package",
        &[
            "__init__.py",
            "pkg/__init__.py",
            "stubs/__init__.pyi",
            "src/net/mod.rs",
        ],
    );

    assert_eq!(
        names(&scratch.0),
        expected(&[
            ("__init__.py", "proj"),
            ("pkg/__init__.py", "proj.pkg"),
            ("src/net/mod.rs", "proj.src.net"),
            ("stubs/__init__.pyi", "proj.stubs"),
        ])
    );
}

#[test]
fn a_notebook_keeps_its_extension() {
    let scratch = tree(
        "merge-notebook",
        &["analysis.ipynb", "analysis.py", "lone.ipynb"],
    );

    // The notebook walks first, and leaves the bare name to the module.
    assert_eq!(
        names(&scratch.0),
        expected(&[
            ("analysis.ipynb", "proj.analysis.ipynb"),
            ("analysis.py", "proj.analysis"),
            ("lone.ipynb", "proj.lone.ipynb"),
        ])
    );
}

#[test]
fn a_same_stem_file_of_another_family_makes_both_keep_their_extensions() {
    let scratch = tree(
        "merge-family",
        &[
            "util.py",
            "util.js",
            "types.py",
            "types.d.ts",
            "pkg/__init__.pyi",
            "pkg/__init__.js",
            "mod.py",
            "mod.rs",
        ],
    );

    assert_eq!(
        names(&scratch.0),
        expected(&[
            // A package's files are named after their folder, never their
            // stem, so they have no same-stem sibling either way.
            ("mod.py", "proj.mod"),
            ("mod.rs", "proj"),
            ("pkg/__init__.js", "proj.pkg.__init__"),
            ("pkg/__init__.pyi", "proj.pkg"),
            // A declaration alone is as much a sibling as its source.
            ("types.d.ts", "proj.types.d.ts"),
            ("types.py", "proj.types.py"),
            ("util.js", "proj.util.js"),
            ("util.py", "proj.util.py"),
        ])
    );
}

#[test]
fn a_sibling_the_walk_would_not_index_renames_nothing() {
    let scratch = tree(
        "merge-not-indexed",
        &["a.py", "a.md", "b.py", "b.js", "c.min.py", "c.min.js"],
    );
    fs::create_dir_all(scratch.0.join("d.js")).unwrap();
    touch(&scratch.0, "d.py");
    #[cfg(unix)]
    {
        touch(&scratch.0, "e.py");
        touch(&scratch.0, "target.js");
        std::os::unix::fs::symlink(scratch.0.join("target.js"), scratch.0.join("e.js")).unwrap();
    }
    let rules = IgnoreRules::new(["b.js"], [""; 0]);

    let found = names_under(&scratch.0, &rules);

    // No language, excluded, minified, a folder, a link.
    for (path, qn) in [
        ("a.py", "proj.a"),
        ("b.py", "proj.b"),
        ("c.min.py", "proj.c.min"),
        ("d.py", "proj.d"),
    ] {
        assert_eq!(found[path], qn);
    }
    #[cfg(unix)]
    assert_eq!(found["e.py"], "proj.e");
}

#[test]
fn a_declaration_beside_its_implementation_keeps_its_extension() {
    let scratch = tree(
        "merge-declaration",
        &[
            "foo.d.ts",
            "foo.ts",
            "bar.d.ts",
            "baz.d.ts",
            "baz.ts",
            "qux.d.mts",
            "qux.mjs",
        ],
    );
    let rules = IgnoreRules::new(["baz.ts"], [""; 0]);

    // `foo.d.ts` walks first but yields the bare name to `foo.ts`; one with
    // no implementation, or only an excluded one, keeps it.
    assert_eq!(
        names_under(&scratch.0, &rules),
        expected(&[
            ("bar.d.ts", "proj.bar"),
            ("baz.d.ts", "proj.baz"),
            ("foo.d.ts", "proj.foo.d.ts"),
            ("foo.ts", "proj.foo"),
            ("qux.d.mts", "proj.qux.d.mts"),
            ("qux.mjs", "proj.qux"),
        ])
    );
}

#[cfg(unix)]
#[test]
fn a_linked_implementation_is_no_implementation() {
    let scratch = tree("merge-linked-implementation", &["foo.d.ts", "real.ts"]);
    std::os::unix::fs::symlink(scratch.0.join("real.ts"), scratch.0.join("foo.ts")).unwrap();

    assert_eq!(names(&scratch.0)["foo.d.ts"], "proj.foo");
}

#[test]
fn the_first_file_in_walk_order_claims_the_bare_name() {
    // Each pair shares a name. The walk takes a folder's files before its
    // subfolders and subfolders by name, so `pkg.py` comes before
    // `pkg/__init__.py`, which sorted part by part comes first, and
    // `a/b.py` before `a.b/__init__.py`, which sorted as a string comes
    // first: a fold in either sorted order gives the other file the name.
    let scratch = tree(
        "merge-walk-order",
        &[
            "x.c",
            "x.h",
            "pkg.py",
            "pkg/__init__.py",
            "a/b.py",
            "a.b/__init__.py",
        ],
    );

    assert_eq!(
        names(&scratch.0),
        expected(&[
            ("a.b/__init__.py", "proj.a.b.py"),
            ("a/b.py", "proj.a.b"),
            ("pkg.py", "proj.pkg"),
            ("pkg/__init__.py", "proj.pkg.py"),
            ("x.c", "proj.x"),
            ("x.h", "proj.x.h"),
        ])
    );
}

#[test]
fn the_fold_takes_summaries_in_walk_order_whatever_order_they_come_in() {
    let scratch = tree(
        "merge-arrival",
        &["pkg.py", "pkg/__init__.py", "x.c", "x.h"],
    );
    let in_order = summaries(&scratch.0, &no_rules());
    let mut reversed = in_order.clone();
    reversed.reverse();

    let first = merge_on(2, &scratch.0, in_order, &no_rules()).unwrap();
    let second = merge_on(2, &scratch.0, reversed, &no_rules()).unwrap();

    assert_eq!(first, second);
    assert_eq!(first.modules["proj.pkg"].path, "pkg.py");
}

#[test]
fn a_later_claim_appends_its_last_suffix() {
    // Two declarations with no implementation strip to one name; the
    // second gets `.ts`, its last suffix, not its module extension.
    let scratch = tree("merge-later", &["foo.d.mts", "foo.d.ts"]);

    assert_eq!(
        names(&scratch.0),
        expected(&[("foo.d.mts", "proj.foo"), ("foo.d.ts", "proj.foo.ts")])
    );
}

#[test]
fn a_suffixed_claim_overwrites_what_holds_that_name_as_python_s_map_does() {
    // `d.x.h.c` claims `proj.d.x.h` first; `d/x.h` loses `proj.d.x` to
    // `d/x.c` and is suffixed to `proj.d.x.h`, which is not asked again.
    let scratch = tree("merge-overwrite", &["d.x.h.c", "d/x.c", "d/x.h"]);

    assert_eq!(
        names(&scratch.0),
        expected(&[("d/x.c", "proj.d.x"), ("d/x.h", "proj.d.x.h")])
    );
}

#[test]
fn the_index_is_the_same_on_any_number_of_threads() {
    let mut rels = Vec::new();
    for i in 0..40 {
        rels.push(format!("dir{}/m{i}.py", i % 7));
        rels.push(format!("dir{}/m{i}.js", i % 5));
        rels.push(format!("dir{}/h{i}.c", i % 3));
        rels.push(format!("dir{}/h{i}.h", i % 3));
    }
    let refs: Vec<&str> = rels.iter().map(String::as_str).collect();
    let scratch = tree("merge-threads", &refs);
    let all = summaries(&scratch.0, &no_rules());

    let one = merge_on(1, &scratch.0, all.clone(), &no_rules()).unwrap();
    for threads in [2, 0] {
        assert_eq!(
            merge_on(threads, &scratch.0, all.clone(), &no_rules()).unwrap(),
            one
        );
    }
    assert_eq!(one.modules.len(), all.len());
}

#[test]
fn a_pattern_python_refuses_is_refused() {
    let scratch = tree("merge-refused", &["util.py", "util.js"]);
    let all = summaries(&scratch.0, &no_rules());
    let rules = IgnoreRules::new(["[z-a]"], [""; 0]);

    assert!(merge_on(1, &scratch.0, all, &rules).is_err());
}

#[test]
fn nothing_fills_the_other_sections_yet() {
    let scratch = tree("merge-sections", &["a.py"]);

    let index = merge_on(
        1,
        &scratch.0,
        summaries(&scratch.0, &no_rules()),
        &no_rules(),
    )
    .unwrap();

    assert_eq!(index.modules.len(), 1);
    assert!(index.definitions.is_empty());
    assert!(index.parents.is_empty());
    assert!(index.imports.is_empty());
    assert!(index.type_locations.is_empty());
}
