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

/// `text` with each `~` the lone surrogate `surrogate`, as
/// `string_from_code_points` hands such a Python str over.
fn with_surrogate(text: &str, surrogate: u32) -> String {
    let code_points = text
        .chars()
        .map(|c| if c == '~' { surrogate } else { u32::from(c) });
    string_from_code_points(code_points).unwrap()
}

#[test]
fn a_python_str_is_one_char_per_code_point() {
    // Each lone surrogate stands as a private-use char of its own, every
    // other code point as itself.
    let stand_ins: BTreeSet<char> = (0xD800..=0xDFFF)
        .map(|surrogate| {
            let text = string_from_code_points([surrogate]).unwrap();
            let mut chars = text.chars();
            let stand_in = chars.next().unwrap();
            assert_eq!(chars.next(), None);
            stand_in
        })
        .collect();
    assert_eq!(stand_ins.len(), 0x800);
    assert_eq!(stand_ins.first(), Some(&'\u{10F800}'));
    assert_eq!(stand_ins.last(), Some(&'\u{10FFFF}'));
    let plain = [0, 0x61, 0xD7FF, 0xE000, 0xFFFF, 0x1_F389];
    let text = string_from_code_points(plain).unwrap();
    assert!(text.chars().map(u32::from).eq(plain));
    assert_eq!(string_from_code_points([0x61, 0x11_0000]), None);
    // A stand-in, like a surrogate, is no space.
    assert!(!stand_ins.iter().any(|&c| is_space(c)));
}

#[test]
fn names_with_surrogates_match_as_python_matches_them() {
    let ff = |text: &str| with_surrogate(text, 0xDCFF);
    let fe = |text: &str| with_surrogate(text, 0xDCFE);
    // `bad\udcff.o` is the name `b"bad\xff.o"` reaches Python as.
    let rules = IgnoreRules::new(["*.c"], [""; 0]);
    assert_eq!(
        rules.should_skip_rel_file(&ff("bad~.o"), &[""; 0]),
        Ok(true)
    );
    assert_eq!(
        rules.should_skip_rel_file(&ff("bad~.c"), &[""; 0]),
        Ok(true)
    );
    assert_eq!(
        rules.should_skip_rel_file(&ff("bad~.h"), &[""; 0]),
        Ok(false)
    );
    assert_eq!(rules.should_keep_dir(&ff("x~"), ""), Ok(true));
    // `?` takes one surrogate, not two.
    assert_eq!(matched("a?b", &ff("a~b")), Ok(true));
    assert_eq!(matched("a?b", &ff("a~~b")), Ok(false));
    assert_eq!(fnmatchcase(&ff("a~b"), "a?b"), Ok(true));
    assert_eq!(fnmatchcase(&ff("a~~b"), "a?b"), Ok(false));
    // A pattern holding a surrogate matches that surrogate only.
    assert_eq!(matched(&ff("bad~*"), &ff("bad~.c")), Ok(true));
    assert_eq!(matched(&ff("bad~*"), &fe("bad~.c")), Ok(false));
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

#[test]
fn a_name_is_held_as_python_holds_it_for_the_rules() {
    use std::ffi::OsStr;

    // A valid name is itself, borrowed.
    assert!(matches!(
        string_from_os_str(OsStr::new("é/中.py")),
        Cow::Borrowed("é/中.py")
    ));
    #[cfg(unix)]
    {
        use std::os::unix::ffi::OsStrExt;
        // surrogateescape gives each stray byte b the surrogate U+DC00 + b,
        // however the decoder splits the invalid run: the str Python builds
        // for each name, as code points, and what the binding makes of it.
        let cases: [(&[u8], &[u32]); 5] = [
            (b"bad\xff.py", &[0x62, 0x61, 0x64, 0xDCFF, 0x2E, 0x70, 0x79]),
            (b"\xe2\x82A", &[0xDCE2, 0xDC82, 0x41]),
            (b"\xed\xa0\x80", &[0xDCED, 0xDCA0, 0xDC80]),
            (b"\xf0\x9f\x98", &[0xDCF0, 0xDC9F, 0xDC98]),
            (b"\xc3\xa9\xff\xc3\xa9", &[0xE9, 0xDCFF, 0xE9]),
        ];
        for (bytes, python) in cases {
            assert_eq!(
                string_from_os_str(OsStr::from_bytes(bytes)),
                string_from_code_points(python.iter().copied()).unwrap(),
                "{bytes:?}"
            );
        }
    }
    #[cfg(windows)]
    {
        use std::ffi::OsString;
        use std::os::windows::ffi::OsStringExt;
        // A name that is not UTF-16 keeps its unpaired surrogate unit.
        let units = [0x61, 0xD800, 0x62, 0xDFFF];
        assert_eq!(
            string_from_os_str(&OsString::from_wide(&units)),
            string_from_code_points(units.iter().map(|&unit| u32::from(unit))).unwrap()
        );
    }
}

#[test]
fn an_ignore_line_is_trimmed_as_python_trims_it() {
    // Recorded from `config.trim_ignore_line`.
    let cases = [
        ("  a.py  ", "a.py"),
        ("t.py\\ ", "t.py\\ "),
        ("t.py\\\\ ", "t.py\\\\"),
        ("t.py\\  ", "t.py\\ "),
        ("\tx\u{0b}", "x"),
        ("x\u{1c}", "x"),
        ("\u{3000}y\u{3000}", "y"),
        ("x\\", "x\\"),
        ("a b", "a b"),
        ("   ", ""),
        ("x\u{a0}", "x"),
        ("\\ ", "\\ "),
        ("x\u{85}", "x"),
    ];
    for (line, trimmed) in cases {
        assert_eq!(files::trim_ignore_line(line), trimmed, "{line:?}");
    }
}

#[test]
fn a_text_file_ends_its_lines_at_any_line_end() {
    assert_eq!(
        files::universal_lines("a\r\nb\rc\nd\u{0b}e\u{2028}f"),
        ["a", "b", "c", "d\u{0b}e\u{2028}f"]
    );
    assert_eq!(files::universal_lines("a\n"), ["a", ""]);
}

/// A root with these two ignore files, outside any git checkout.
fn ignore_root(name: &str, cgrignore: &[u8], gitignore: &[u8]) -> std::path::PathBuf {
    let root = std::env::temp_dir().join(format!("cgr-ignore-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    std::fs::write(root.join(CGRIGNORE_FILENAME), cgrignore).unwrap();
    std::fs::write(root.join(GITIGNORE_FILENAME), gitignore).unwrap();
    root
}

fn set(items: &[&str]) -> BTreeSet<String> {
    items.iter().map(|item| (*item).to_owned()).collect()
}

#[test]
fn the_ignore_files_give_python_s_sets() {
    let root = ignore_root(
        "sets",
        b"\xef\xbb\xbfgen/\n# note\n\n!a\n!  keep.py\nt.py\\ \n",
        b"a\nb/\n!b/keep\nc\n!c\n!d\n",
    );

    let found = load_ignore_patterns(&root);
    let _ = std::fs::remove_dir_all(&root);

    // As `config.load_ignore_patterns` gives them: `a` cancelled by the
    // `.cgrignore` unignore, `c` by the `!c` after it, and the surviving
    // `!` lines make `.gitignore` one ordered block.
    assert_eq!(
        found.exclude,
        set(&["b/\n!b/keep\n!c\n!d", "gen/", "t.py\\ "])
    );
    assert_eq!(found.unignore, set(&["a", "b/keep", "c", "d", "keep.py"]));
    assert!(found.refused_lines.is_empty());
    assert!(found.unreadable.is_empty());
}

#[test]
fn a_line_pathspec_refuses_is_left_out_alone() {
    let root = ignore_root("refused", b"ok.py\nbad\\\n[z-a]\n!\n!!\n", b"x\r\n\xff\n");

    let found = load_ignore_patterns(&root);
    let _ = std::fs::remove_dir_all(&root);

    assert_eq!(found.exclude, set(&["ok.py"]));
    assert!(found.unignore.is_empty());
    assert_eq!(
        found.refused_lines,
        [
            (CGRIGNORE_FILENAME, 2),
            (CGRIGNORE_FILENAME, 3),
            (CGRIGNORE_FILENAME, 4),
            (CGRIGNORE_FILENAME, 5)
        ]
    );
    // Not UTF-8: the whole file is skipped.
    assert_eq!(found.unreadable, [GITIGNORE_FILENAME]);
}

#[test]
fn tracked_files_under_an_ambiguous_name_are_rescued_by_their_own_path() {
    let listing = [
        "bin/main.dart",
        "tools/out/gen.go",
        "vendor/bin/x.js",
        "src/bin/[x].rs",
        "tmp/space ",
        "pkg/target/a/b.rs",
        "README.md",
        "",
    ]
    .join("\0");

    let found = tracked::rescue(&listing);

    assert_eq!(
        found.rescued,
        set(&["/bin/main.dart", "/pkg/target/a/b.rs", "/tools/out/gen.go"])
    );
    assert_eq!(found.dirs, set(&["bin", "pkg/target", "tools/out"]));
}

#[test]
fn git_s_output_is_read_as_python_reads_it() {
    // Recorded from `bytes.decode("utf-8", "replace")`, then subprocess's
    // newline translation.
    let cases: [(&[u8], &str); 6] = [
        (b"a\xffb", "a\u{FFFD}b"),
        (b"\xe2\x82A", "\u{FFFD}A"),
        (b"\xf0\x80\x80A", "\u{FFFD}\u{FFFD}\u{FFFD}A"),
        (b"\xed\xa0\x80", "\u{FFFD}\u{FFFD}\u{FFFD}"),
        (b"\xc0\xafx", "\u{FFFD}\u{FFFD}x"),
        (b"a\r\nb\rc", "a\nb\nc"),
    ];
    for (bytes, text) in cases {
        assert_eq!(tracked::python_text(bytes), text, "{bytes:?}");
    }
}
