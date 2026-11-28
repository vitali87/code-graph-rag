//! The build constraints, case for case with
//! `codebase_rag/tests/test_go_build_constraints.py` and the canonical text's
//! cases in `test_handoff_go_sections.py`; `test_engine_go.py` holds the
//! port to `builds.py` itself over a generated corpus.

use std::fs;

use super::*;
use crate::engine::tests::Scratch;

fn header(text: &str) -> Expr {
    header_constraint(text, false)
}

fn go_build(expression: &str) -> Expr {
    header(&format!("//go:build {expression}\n"))
}

fn tag(name: &str) -> Expr {
    Expr::tag(name)
}

#[test]
fn a_file_name_constrains_its_build() {
    for (first, second, together) in [
        ("i_linux.go", "i_windows.go", false),
        ("i_linux.go", "i_amd64.go", true),
        ("i_linux_amd64.go", "i_linux_arm64.go", false),
        ("i_windows_amd64.go", "i_amd64.go", true),
        ("i_linux_test.go", "i_windows.go", false),
        // GOOS=android builds `_linux` files too.
        ("i_android.go", "i_linux.go", true),
        // Only a trailing GOOS or GOARCH counts, and not without a `_`.
        ("linux.go", "windows.go", true),
        ("i_linux_extra.go", "i_windows.go", true),
    ] {
        assert_eq!(
            decide([name_constraint(first), name_constraint(second)]),
            Some(together),
            "{first} {second}"
        );
    }
}

#[test]
fn a_file_name_is_its_paths_last_part() {
    assert_eq!(name_constraint("a/b/x_linux.go"), tag("linux"));
    assert_eq!(name_constraint("a/x_linux.go/"), tag("linux"));
    assert_eq!(name_constraint("a/x_linux.go/."), tag("linux"));
    assert_eq!(name_constraint("x_linux/y.go"), Expr::always());
    assert_eq!(
        name_constraint("x_windows_arm64_test.go"),
        all_of([tag("windows"), tag("arm64")])
    );
    // `_linux` alone: the part before the `_` is empty, and free.
    assert_eq!(name_constraint("_linux.go"), tag("linux"));
    assert_eq!(name_constraint(""), Expr::always());
    assert_eq!(name_constraint("x_Linux.go"), Expr::always());
}

#[test]
fn a_build_line_constrains_its_build() {
    for (first, second, together) in [
        ("//go:build linux\n", "//go:build !linux\n", false),
        (
            "//go:build linux || darwin\n",
            "//go:build windows\n",
            false,
        ),
        ("//go:build unix\n", "//go:build windows\n", false),
        ("//go:build unix\n", "//go:build darwin\n", true),
        (
            "//go:build linux && !(arm64 || cgo)\n",
            "//go:build arm64\n",
            false,
        ),
        // A tag no GOOS or GOARCH fixes may be either.
        ("//go:build foo\n", "//go:build !foo\n", false),
        ("//go:build foo\n", "//go:build bar\n", true),
        ("//go:build go1.21\n", "//go:build !go1.22\n", true),
        // The legacy form, used only without a //go:build line.
        (
            "// +build linux,amd64 darwin\n\n",
            "// +build windows\n\n",
            false,
        ),
        ("// +build linux\n\n", "//go:build linux\n", true),
    ] {
        assert_eq!(
            decide([header(first), header(second)]),
            Some(together),
            "{first:?} {second:?}"
        );
    }
}

#[test]
fn what_is_no_constraint_holds_in_every_build() {
    // Not a constraint: no blank line ends the legacy block, the line
    // follows the package clause, or it is no directive.
    for text in [
        "// +build linux\npackage p\n",
        "package p\n\n//go:build linux\n",
        "//go:buildlinux\n\npackage p\n",
    ] {
        assert_eq!(header(text), Expr::always(), "{text:?}");
    }
}

const UNREADABLE: [&str; 8] = [
    "//go:build ((linux)\n",
    "//go:build linux))\n",
    "//go:build ) linux (\n",
    "//go:build linux && || windows\n",
    "//go:build linux $ windows\n",
    "//go:build \u{0}\n",
    "//go:build linux &&\n",
    "// +build ,,, !\n\n",
];

#[test]
fn a_constraint_this_cannot_read_is_unknown() {
    // Malformed, or nested past what this reads: never a panic, and neither
    // true nor false in any build, so the file is neither known to build
    // with another nor known apart from it.
    let deep = format!("//go:build {}linux{}\n", "(".repeat(300), ")".repeat(300));
    let negated = format!("//go:build {}linux\n", "!".repeat(5000));
    let long = format!("//go:build {}\n", vec!["linux"; 900].join(" && "));
    let scratch = Scratch::new("go-unreadable");
    for text in UNREADABLE
        .iter()
        .copied()
        .chain([deep.as_str(), negated.as_str(), long.as_str()])
    {
        assert_eq!(header(text), Expr::unknown(), "{text:?}");
        fs::write(scratch.0.join("a.go"), format!("{text}\npackage p\n")).unwrap();
        let mut builds = GoBuilds::new(&scratch.0);
        for other in ["b_windows.go", "b_linux.go"] {
            assert!(!builds.together(["a.go", other]), "{text:?}");
            assert!(!builds.apart(["a.go", other]), "{text:?}");
        }
    }
}

#[test]
fn the_line_reader_says_why_it_refuses() {
    assert_eq!(parse_go_build(" linux &&"), Err(Refusal::Malformed));
    assert_eq!(parse_go_build(" linux & amd64"), Err(Refusal::Malformed));
    let tokens = format!("{} linux", "!".repeat(MAX_TOKENS));
    assert_eq!(parse_go_build(&tokens), Err(Refusal::TooManyTokens));
    let deep = format!("{}linux", "!".repeat(MAX_DEPTH + 1));
    assert_eq!(parse_go_build(&deep), Err(Refusal::TooDeep));
}

#[test]
fn the_reader_stops_exactly_at_its_limits() {
    // Nesting: `MAX_DEPTH` levels of `(` or `!` are read, one more is not.
    for depth in [MAX_DEPTH, MAX_DEPTH + 1] {
        let groups = format!("{}linux{}", "(".repeat(depth), ")".repeat(depth));
        let negations = format!("{}linux", "!".repeat(depth));
        let mixed = format!("{}linux{}", "!(".repeat(depth / 2), ")".repeat(depth / 2));
        let read = depth <= MAX_DEPTH;
        assert_eq!(parse_go_build(&groups).is_ok(), read, "{depth}");
        assert_eq!(parse_go_build(&negations).is_ok(), read, "{depth}");
        assert!(parse_go_build(&mixed).is_ok(), "{depth}");
    }
    assert_eq!(
        parse_go_build(&format!("{}linux", "!".repeat(MAX_DEPTH))),
        Ok(tag("linux"))
    );
    // Tokens: `!t0 && t1 && ... && t499` is exactly `MAX_TOKENS`.
    let terms: Vec<String> = (0..500).map(|k| format!("t{k}")).collect();
    let exact = format!("!{}", terms.join(" && "));
    let over = format!("!{exact}");
    assert!(parse_go_build(&exact).is_ok());
    assert_eq!(parse_go_build(&over), Err(Refusal::TooManyTokens));
}

#[test]
fn a_head_cut_off_before_its_package_clause_is_unknown() {
    // The constraint may lie past the bytes read.
    let scratch = Scratch::new("go-cut-off");
    let filler = "// filler\n".repeat(8000);
    fs::write(
        scratch.0.join("a.go"),
        format!("{filler}//go:build linux\n\npackage p\n"),
    )
    .unwrap();
    let mut builds = GoBuilds::new(&scratch.0);
    assert_eq!(builds.constraint("a.go"), Expr::unknown());
    assert!(!builds.apart(["a.go", "b_windows.go"]));
}

#[test]
fn a_head_is_cut_off_when_it_fills_the_scan() {
    // A head of exactly `HEADER_SCAN_BYTES` may go on past them: unknown
    // unless it reached code; one byte less was read whole.
    let line = "//go:build linux\n";
    for (size, expected) in [
        (HEADER_SCAN_BYTES - 1, tag("linux")),
        (HEADER_SCAN_BYTES, Expr::unknown()),
    ] {
        let mut head = line.as_bytes().to_vec();
        head.resize(size, b'\n');
        assert_eq!(head_constraint(&head), expected, "{size}");
        // A code line at the very end, even cut short, was reached.
        *head.last_mut().unwrap() = b'p';
        assert_eq!(head_constraint(&head), tag("linux"), "{size}");
    }
}

#[test]
fn more_tags_than_this_enumerates_is_undecided() {
    // Each tag both asked for and ruled out: only enumerating tells.
    let chain = |links: usize| {
        (0..links)
            .map(|k| format!("(t{k} || !t{})", k + 1))
            .collect::<Vec<_>>()
            .join(" && ")
    };
    assert_eq!(decide([go_build(&chain(12))]), None);
    // `links` links hold `links + 1` tags, the two ends asked for or ruled
    // out alone: `MAX_FREE_TAGS` free tags are enumerated, one more is not.
    assert_eq!(decide([go_build(&chain(MAX_FREE_TAGS + 1))]), Some(true));
    assert_eq!(decide([go_build(&chain(MAX_FREE_TAGS + 2))]), None);
}

#[test]
fn a_tag_only_asked_for_or_only_ruled_out_is_never_enumerated() {
    let ruled_out: Vec<Expr> = (0..40).map(|k| go_build(&format!("!t{k}"))).collect();
    let asked: Vec<Expr> = (0..40).map(|k| go_build(&format!("u{k}"))).collect();
    let linux = name_constraint("x_linux.go");
    let windows = name_constraint("y_windows.go");
    let both = || ruled_out.iter().chain(&asked).cloned();
    assert_eq!(decide(both().chain([linux.clone()])), Some(true));
    assert_eq!(decide(both().chain([linux, windows])), Some(false));
    assert_eq!(
        decide(ruled_out.iter().cloned().chain([go_build("t3")])),
        Some(false)
    );
}

#[test]
fn a_constraint_nested_within_the_limit_is_read() {
    let nested = format!("//go:build {}linux{}\n", "(".repeat(50), ")".repeat(50));
    assert_eq!(
        decide([header(&nested), name_constraint("x_windows.go")]),
        Some(false)
    );
}

#[test]
fn a_constraint_is_read_where_go_reads_it() {
    // Each verdict is `go list`'s under GOOS=windows: true where the file
    // builds there.
    let windows = name_constraint("x_windows.go");
    for (text, on_windows) in [
        ("/* license */\n\n//go:build linux\n\npackage p\n", false),
        (
            "/*\n * Copyright 2026\n */\n//go:build linux\n\npackage p\n",
            false,
        ),
        (
            "// Copyright\n/* more */\n\n//go:build linux\npackage p\n",
            false,
        ),
        (
            "/* //go:build windows */\n//go:build linux\n\npackage p\n",
            false,
        ),
        ("/*\n//go:build linux\n*/\n\npackage p\n", true),
        ("//go:build linux\n/* x */ package p\n", false),
        ("/* x */\n\n// +build linux\n\npackage p\n", true),
        ("// +build linux\n\n/* x */\npackage p\n", false),
    ] {
        assert_eq!(
            decide([header(text), windows.clone()]),
            Some(on_windows),
            "{text:?}"
        );
    }
}

#[test]
fn a_head_ending_inside_a_block_comment_is_unknown() {
    assert_eq!(
        header("/* unterminated\n//go:build linux\n"),
        Expr::unknown()
    );
}

#[test]
fn lines_end_where_python_ends_them() {
    // `\r\n`, a lone `\r`, a form feed and the Unicode separators each end
    // a line; a space Python strips may sit anywhere.
    for text in [
        "//go:build linux\r\n\r\npackage p\r\n",
        "//go:build linux\rpackage p",
        "//go:build linux\u{c}package p",
        "//go:build linux\u{2028}package p",
        "\u{3000}//go:build \u{a0}linux\u{1f}\n\u{85}package p",
    ] {
        assert_eq!(header(text), tag("linux"), "{text:?}");
    }
    // U+001F is a space but no line break; U+200B is neither.
    assert_eq!(header("//go:build linux\u{1f}package p"), Expr::unknown());
    assert_eq!(
        header("//go:build linux\u{200b}\npackage p"),
        Expr::unknown()
    );
}

#[test]
fn nested_conjunctions_stay_as_large_as_their_distinct_parts() {
    let tags: Vec<Expr> = (0..40).map(|k| go_build(&format!("t{k}"))).collect();
    let (mut left, mut right) = (Expr::always(), Expr::always());
    for tag in tags {
        (left, right) = (
            all_of([tag.clone(), left.clone(), right.clone()]),
            all_of([tag, right, left]),
        );
    }
    assert_eq!(left.op(), Op::And);
    assert_eq!(left.parts().len(), 40);
}

#[test]
fn merged_builds_along_a_chain_stay_small() {
    let linux = name_constraint("x_linux.go");
    let mut builds = go_build("cgo");
    for _ in 0..5000 {
        builds = any_of([
            all_of([builds.clone(), linux.clone()]),
            all_of([builds, negate(linux.clone())]),
        ]);
    }
    assert_eq!(builds, go_build("cgo"));
}

#[test]
fn a_constraint_past_the_size_or_depth_cap_is_unknown() {
    // Python's same loop first gives UNKNOWN at k = 100, the step before at
    // depth 199 and size 397.
    let mut nested = go_build("t0");
    let mut first_unknown = None;
    for k in 1..5000 {
        let tag = go_build(&format!("t{}", k % 7));
        let next = any_of([all_of([nested.clone(), tag]), go_build(&format!("u{k}"))]);
        if next.is_unknown() && first_unknown.is_none() {
            first_unknown = Some(k);
            assert_eq!((nested.depth(), nested.size()), (199, 397));
        }
        nested = next;
    }
    assert_eq!(first_unknown, Some(100));
    assert_eq!(nested, Expr::unknown());
    assert_eq!(decide([nested]), None);
    // Size: an all-of or any-of of 4,095 tags is 4,096 nodes, of 4,096
    // more than the cap.
    let tags: Vec<Expr> = (0..MAX_EXPR_SIZE).map(|k| tag(&format!("t{k}"))).collect();
    let combines: [fn(Vec<Expr>) -> Expr; 2] = [all_of, any_of];
    for combine in combines {
        let within = combine(tags[..MAX_EXPR_SIZE - 1].to_vec());
        assert_eq!(within.size(), MAX_EXPR_SIZE);
        assert_eq!(combine(tags.clone()), Expr::unknown());
    }
}

#[test]
fn negations_cancel_and_complements_decide() {
    let linux = name_constraint("x_linux.go");
    let mut negated = linux.clone();
    for _ in 0..5001 {
        negated = negate(negated);
    }
    assert_eq!(negated, negate(linux.clone()));
    assert_eq!(
        any_of([linux.clone(), negate(linux.clone())]),
        Expr::always()
    );
    assert_eq!(all_of([linux.clone(), negate(linux)]), Expr::never());
}

#[test]
fn units_and_unknown_absorb_as_pythons_do() {
    let linux = tag("linux");
    assert_eq!(all_of([Expr::unknown(), Expr::never()]), Expr::never());
    assert_eq!(any_of([Expr::unknown(), Expr::always()]), Expr::always());
    assert_eq!(all_of([Expr::unknown(), linux.clone()]), Expr::unknown());
    assert_eq!(all_of([Expr::always(), linux.clone()]), linux);
    assert_eq!(any_of([Expr::never(), linux.clone()]), linux);
    assert_eq!(negate(Expr::unknown()), Expr::unknown());
    assert_eq!(decide([Expr::unknown()]), None);
    assert_eq!(decide(Vec::new()), Some(true));
    assert!(holds_wherever(tag("linux"), tag("unix")));
    assert!(!holds_wherever(tag("unix"), tag("linux")));
    assert!(possible([tag("android"), tag("linux")]));
    assert!(impossible([tag("windows"), tag("unix")]));
}

// Constraints as `//go:build` lines write them, each with its canonical
// text: operands in the order written, compound operands of a compound
// grouped.
const CONSTRAINTS: [(&str, &str); 11] = [
    ("linux", "linux"),
    ("!windows", "!windows"),
    ("linux && amd64", "linux && amd64"),
    ("linux || darwin", "linux || darwin"),
    (
        "!windows && (amd64 || arm64)",
        "!windows && (amd64 || arm64)",
    ),
    ("(linux && cgo) || go1.21", "(linux && cgo) || go1.21"),
    ("linux && cgo || go1.21", "(linux && cgo) || go1.21"),
    ("!(linux && amd64)", "!(linux && amd64)"),
    ("((linux))", "linux"),
    ("linux && linux", "linux"),
    ("custom_tag && !other.tag", "custom_tag && !other.tag"),
];

fn go_build_file(expression: &str) -> String {
    format!("//go:build {expression}\n\npackage p\n")
}

#[test]
fn the_canonical_text_reads_back_as_the_same_constraint() {
    for (expression, text) in CONSTRAINTS {
        let constraint = header(&go_build_file(expression));
        assert_eq!(canonical_text(&constraint), text, "{expression}");
        assert_eq!(header(&go_build_file(text)), constraint, "{expression}");
    }
}

#[test]
fn a_file_name_reads_as_its_build() {
    assert_eq!(
        canonical_text(&name_constraint("x/poll_linux_arm64.go")),
        "linux && arm64"
    );
    assert_eq!(
        canonical_text(&name_constraint("x/poll_windows_test.go")),
        "windows"
    );
}

#[test]
fn what_the_grammar_cannot_spell_has_words_no_tag_can_be() {
    let texts: Vec<String> = [Expr::always(), Expr::never(), Expr::unknown()]
        .iter()
        .map(canonical_text)
        .collect();
    assert_eq!(texts, ["<always>", "<never>", "<unknown>"]);
    assert_eq!(
        canonical_text(&header(&go_build_file("linux && !linux"))),
        "<never>"
    );
    assert_eq!(
        canonical_text(&header(&go_build_file("linux &&"))),
        "<unknown>"
    );
    assert_eq!(canonical_text(&header("package p\n")), "<always>");
}

#[test]
fn a_canonical_text_past_the_readers_limits_is_refused_for_them() {
    // Grouping can nest the text deeper than the line it came from: each
    // `&&` inside a `||` takes parentheses the line did without.
    let flat: Vec<String> = (0..200).map(|k| format!("a{k} && b{k}")).collect();
    let line = flat.join(" || ");
    let constraint = parse_go_build(&line).unwrap();
    let text = canonical_text(&constraint);
    assert_eq!(parse_go_build(&text), Err(Refusal::TooManyTokens));
    let within: Vec<String> = (0..100).map(|k| format!("a{k} && b{k}")).collect();
    let constraint = parse_go_build(&within.join(" || ")).unwrap();
    assert_eq!(parse_go_build(&canonical_text(&constraint)), Ok(constraint));
}

#[test]
fn a_changed_file_is_read_again() {
    let scratch = Scratch::new("go-changed");
    let path = scratch.0.join("a.go");
    fs::write(&path, "//go:build linux\n\npackage p\n").unwrap();
    let mut builds = GoBuilds::new(&scratch.0);
    assert!(!builds.together(["a.go", "x_windows.go"]));
    fs::write(&path, "//go:build windows\n\npackage p\n\n// grown\n").unwrap();
    assert!(builds.together(["a.go", "x_windows.go"]));
}

#[test]
fn variants_are_the_files_no_build_compiles_together() {
    let scratch = Scratch::new("go-variants");
    fs::write(scratch.0.join("a.go"), "//go:build linux\n\npackage p\n").unwrap();
    fs::write(scratch.0.join("b.go"), "//go:build !linux\n\npackage p\n").unwrap();
    fs::write(scratch.0.join("c.go"), "package p\n").unwrap();
    let mut builds = GoBuilds::new(&scratch.0);
    assert!(builds.apart(["a.go", "b.go"]));
    assert!(builds.together(["a.go", "c.go"]));
    assert!(builds.together(["b.go", "c.go"]));
    assert!(builds.apart(["a.go", "x_windows.go"]));
    // A file that cannot be read asks nothing beyond its name, and no path
    // asks nothing at all.
    assert_eq!(builds.constraint("gone_linux.go"), tag("linux"));
    assert_eq!(builds.constraint(""), Expr::always());
}

#[test]
fn a_path_is_joined_as_pathlib_joins_it() {
    // `Path("d/./x_linux.go/")` is `d/x_linux.go`: the file is read.
    let scratch = Scratch::new("go-joined");
    fs::create_dir_all(scratch.0.join("d")).unwrap();
    fs::write(
        scratch.0.join("d/x_linux.go"),
        "//go:build cgo\n\npackage p\n",
    )
    .unwrap();
    let read = all_of([tag("linux"), tag("cgo")]);
    for rel_path in ["d/x_linux.go", "d/./x_linux.go/", "d//x_linux.go"] {
        assert_eq!(file_constraint(&scratch.0, rel_path), read, "{rel_path}");
    }
}
