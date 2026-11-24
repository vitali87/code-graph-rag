//! Answers pathspec 1.1.1 and path_utils give, recorded from Python. The
//! differential test (`codebase_rag/tests/test_engine_ignore_rules.py`)
//! holds the port to thousands more.

use super::python_str::{is_space, Bracket, BracketError};
use super::*;

fn matched(pattern: &str, path: &str) -> Result<bool, PatternError> {
    IgnoreRules::new([pattern], [""; 0]).matches_exclude(path)
}

fn block(lines: &[&str]) -> String {
    lines.join("\n")
}

#[test]
fn single_patterns_match_as_pathspec_does() {
    let cases = [
        ("*.py", "a/b.py", true),
        ("*.py", "a/b.pyc", false),
        ("/build", "build/x", true),
        ("/build", "src/build/x", false),
        // Basic's `foo/*` matches what is directly inside foo only.
        ("docs/*", "docs/a/b.md", false),
        ("docs/*", "docs/a/", true),
        ("docs/*.md", "docs/sub/a.md", false),
        ("**/fixtures/**", "a/b/fixtures/c/x.py", true),
        ("dist/", "dist", false),
        ("dist/", "dist/", true),
        ("dist/", "dist/x.py", true),
        // A negated set matches `/` too.
        ("a[!b]c", "a/c", true),
        ("a[!b]c", "axc", true),
        ("[a-c]x", "bx", true),
        ("\\*x", "*x", true),
        ("\\*x", "ax", false),
        ("Build", "build/x", false),
        ("é*", "ü/éa.py", true),
        ("a b", "a b", true),
        ("a\\ ", "a ", true),
        ("a   ", "a", true),
        ("/", "x", true),
        ("**/", "x", false),
        ("**/", "a/x", true),
        ("*/", "a/x", true),
        // Python's `$` matches before a final newline.
        ("x", "x\n", true),
        ("x/*", "x/y\n", true),
        ("!x", "x", false),
        // An unclosed `[` is a literal in Basic.
        ("[abc", "[abc", true),
        ("#x", "#x", false),
        ("\\#x", "#x", true),
    ];
    for (pattern, path, expected) in cases {
        assert_eq!(
            matched(pattern, path),
            Ok(expected),
            "{pattern:?} on {path:?}"
        );
    }
}

#[test]
fn patterns_pathspec_refuses_are_refused() {
    assert!(matches!(
        matched("[z-a]", "x"),
        Err(PatternError::BadRange { .. })
    ));
    assert!(matches!(
        matched("a\\", "a"),
        Err(PatternError::Invalid { .. })
    ));
    assert!(matches!(
        matched("!", "x"),
        Err(PatternError::Invalid { .. })
    ));
    assert!(matches!(
        matched("a\n\nb", "x"),
        Err(PatternError::EmptyBlockLine { .. })
    ));
    // Spec drops a pattern whose `[` never closes before `re` sees the bad
    // range earlier in it, so the block compiles and the line does nothing.
    assert_eq!(matched(&block(&["[z-a]/[x", "!y"]), "x"), Ok(false));
}

#[test]
fn an_error_surfaces_only_where_the_set_is_asked() {
    let rules = IgnoreRules::new(["out/"], ["a\\"]);
    // The exclude decides before the unignore set is reached.
    assert_eq!(rules.should_skip_rel_file("out/x.py", &["out"]), Ok(true));
    assert!(rules.should_skip_rel_file("src/x.py", &["src"]).is_err());
}

#[test]
fn ordered_blocks_judge_each_level() {
    let paths = [
        "src/gen/auto.py",
        "src/gen/handwritten.py",
        "gen/keep.py",
        "src/gen/keep.py",
        "src/app.py",
        "app.py",
        "src/notes.txt",
        "docs/api/conf.py",
        "docs/api/index.md",
        "vendor/our-fork/a.go",
        "vendor/other/a.go",
        "src/",
        "src/gen/",
    ];
    let cases: [(&[&str], &[&str]); 6] = [
        (
            &["src/gen/*.py", "!src/gen/handwritten.py"],
            &["src/gen/auto.py", "src/gen/keep.py"],
        ),
        (
            &["gen/", "!gen/keep.py"],
            &[
                "src/gen/auto.py",
                "src/gen/handwritten.py",
                "gen/keep.py",
                "src/gen/keep.py",
                "src/gen/",
            ],
        ),
        (
            &["src/**", "!src/gen/", "!src/gen/keep.py"],
            &[
                "src/gen/auto.py",
                "src/gen/handwritten.py",
                "src/app.py",
                "src/notes.txt",
            ],
        ),
        (
            &["*", "!*/", "!*.py"],
            &[
                "src/notes.txt",
                "docs/api/index.md",
                "vendor/our-fork/a.go",
                "vendor/other/a.go",
            ],
        ),
        (
            &["docs/**", "!docs/**/", "!docs/**/*.py"],
            &["docs/api/index.md"],
        ),
        (&["/vendor/*", "!/vendor/our-fork/"], &["vendor/other/a.go"]),
    ];
    for (lines, expected) in cases {
        let set = PatternSet::new([block(lines)]).unwrap();
        let got: Vec<&str> = paths
            .iter()
            .copied()
            .filter(|path| set.match_file(path))
            .collect();
        assert_eq!(got, expected, "{lines:?}");
    }
}

#[test]
fn fnmatch_reads_sets_as_python_does() {
    let cases = [
        ("abc", "a*c", true),
        ("abc", "a?c", true),
        ("a-c", "a[!b]c", true),
        ("abc", "[!a]bc", false),
        ("b", "[a-c]", true),
        // A range that runs backwards is dropped, not refused.
        ("b", "[c-a]", false),
        ("-", "[a-]", true),
        ("]", "[]]", true),
        ("a", "[!]", false),
        ("[", "[", true),
        ("ab", "*b*", true),
        ("a\nb", "a*b", true),
        ("\\", "[\\]", true),
        ("x", "[z-a-x]", true),
    ];
    for (name, pattern, expected) in cases {
        assert_eq!(
            fnmatchcase(name, pattern),
            Ok(expected),
            "{name:?} {pattern:?}"
        );
    }
}

#[test]
fn a_metadata_dir_is_kept_only_when_a_line_spells_it() {
    let cases: [(&str, &str, bool); 7] = [
        (".git", ".git", true),
        ("a/.git", "/.git", false),
        ("sub/.git", "sub/.git", true),
        ("sub/.git", "*/.git/x", true),
        ("sub/.git/hooks", "sub/.git/**", true),
        ("sub/.git", "**/x", false),
        ("a/b/.git", "a/*/.git/", true),
    ];
    for (dir, unignore, expected) in cases {
        let rules = IgnoreRules::new([""; 0], [unignore]);
        assert_eq!(
            rules.unignore_names_vcs_dir(dir),
            Ok(expected),
            "{dir} {unignore}"
        );
    }
}

#[test]
fn the_walk_decisions_follow_path_utils() {
    let none = IgnoreRules::new([""; 0], [""; 0]);
    assert_eq!(none.should_keep_dir("node_modules", ""), Ok(false));
    assert_eq!(none.should_keep_dir("bin", "src/"), Ok(true));
    assert_eq!(none.should_keep_dir("bin", "tools/"), Ok(false));
    assert_eq!(none.should_keep_dir("lib", "a/"), Ok(true));
    assert_eq!(none.should_skip_rel_file("a/x.pyc", &["a"]), Ok(true));
    assert_eq!(none.should_skip_rel_file("a/x.min.js", &["a"]), Ok(true));
    assert_eq!(
        none.should_skip_rel_file("build/x.py", &["build"]),
        Ok(true)
    );
    assert_eq!(
        none.should_skip_rel_file("src/bin/x.rs", &["src", "bin"]),
        Ok(false)
    );

    let rescue = IgnoreRules::new(["gen"], ["bin/*.py", "build/js/jquery.min.js"]);
    assert_eq!(
        rescue.should_skip_rel_file("bin/tool.py", &["bin"]),
        Ok(false)
    );
    assert_eq!(
        rescue.should_skip_rel_file("bin/data.txt", &["bin"]),
        Ok(true)
    );
    assert_eq!(rescue.should_skip_rel_file("gen/x.py", &["gen"]), Ok(true));
    assert_eq!(rescue.should_keep_dir("bin", ""), Ok(true));
    assert_eq!(rescue.should_keep_dir("gen", ""), Ok(false));
    assert_eq!(
        rescue.should_skip_rel_path("build/js/jquery.min.js", true),
        Ok(false)
    );
    assert_eq!(
        rescue.should_skip_rel_path("build/js/other.min.js", true),
        Ok(true)
    );
    // The directory a rescue reaches into is walked.
    assert_eq!(rescue.should_skip_rel_path("build/js", false), Ok(false));
    assert_eq!(rescue.should_skip_rel_path(".git/hooks", false), Ok(true));
}

#[test]
fn the_unignore_helpers_follow_path_utils() {
    assert!(unignore_could_match_within("keep.py", "node_modules"));
    assert!(unignore_could_match_within("bin/*.py", "bin"));
    assert!(!unignore_could_match_within("tools/bin/x", "bin"));
    assert!(unignore_could_match_within("a/b/c", "a/b"));
    assert!(unignore_could_match_within("a/b", "a/b/c"));
    assert!(unignore_names_this_file(
        "docs/js/jquery.min.js",
        &[" !x", "/docs/js/jquery.min.js/"]
    ));
    assert!(unignore_names_this_file(
        "a/jquery.min.js",
        &["\u{1c}jquery.min.js\u{3000}"]
    ));
    assert!(!unignore_names_this_file("a/jquery.min.js", &["*.min.js"]));
}

#[test]
fn python_whitespace_is_unicode_whitespace_and_the_separators() {
    for c in [
        '\t', '\n', '\u{b}', '\u{c}', '\r', ' ', '\u{1c}', '\u{1f}', '\u{85}', '\u{a0}',
        '\u{2028}', '\u{3000}',
    ] {
        assert!(is_space(c), "{c:?}");
    }
    for c in ['\u{0}', '\u{8}', '\u{200b}', '\u{feff}', 'a'] {
        assert!(!is_space(c), "{c:?}");
    }
}

#[test]
fn brackets_are_read_as_python_re_reads_them() {
    assert_eq!(
        Bracket::parse("[a-]").unwrap().to_regex(),
        "[\\x{61}\\x{2D}]"
    );
    assert_eq!(
        Bracket::parse("[]a]").unwrap().to_regex(),
        "[\\x{5D}\\x{61}]"
    );
    assert_eq!(Bracket::parse("[^]]").unwrap().to_regex(), "[^\\x{5D}]");
    assert_eq!(
        Bracket::parse("[\\\\-a]").unwrap().to_regex(),
        "[\\x{5C}-\\x{61}]"
    );
    // No POSIX classes: `[[:alpha:]` is a set of its characters (the glob's
    // last `]` follows it as a literal).
    assert_eq!(
        Bracket::parse("[[:alpha:]").unwrap().to_regex(),
        "[\\x{5B}\\x{3A}\\x{61}\\x{6C}\\x{70}\\x{68}\\x{61}\\x{3A}]"
    );
    assert_eq!(Bracket::parse("[a]]"), Err(BracketError::Unexpected));
    assert_eq!(Bracket::parse("[z-a]"), Err(BracketError::BadRange));
    assert_eq!(Bracket::parse("[\\d]"), Err(BracketError::Unexpected));
}

#[test]
fn hostile_patterns_compile_within_bounds_or_are_refused() {
    let stars = "*".repeat(20_000);
    let deep = "**/a/".repeat(2_000);
    let sets = "[!a]".repeat(5_000);
    for pattern in [stars.as_str(), deep.as_str(), sets.as_str()] {
        match Pattern::new(pattern, Flavor::Basic) {
            Ok(compiled) => {
                let _ = compiled.is_match(&"a/".repeat(1_000));
            }
            Err(error) => assert!(matches!(error, PatternError::TooLarge { .. }), "{error}"),
        }
    }
}
