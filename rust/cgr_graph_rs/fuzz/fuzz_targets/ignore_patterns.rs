// SPDX-License-Identifier: MIT
//! Fuzz target: `engine::ignore`, the walk's ignore rules: pathspec's
//! gitignore patterns ported onto the `regex` crate, the ordered
//! `.gitignore` blocks, `fnmatch` and the walk's decisions.
//!
//! Patterns come from `.gitignore`, `.cgrignore` and `--exclude`, and paths
//! from the disk, so any text reaches them. A refusal is an expected answer
//! where pathspec raises too; a panic, a hang, a refusal the port should
//! never make, or an answer that contradicts another is a bug. The input is
//! up to six patterns of at most 256 bytes and six paths of at most 128,
//! each raw text or a run of the fragments the translation reads specially,
//! so every pattern compiles far below the regex crate's size limit and
//! every run ends quickly.
#![no_main]

use cgr_graph_rs::engine::ignore::{
    fnmatchcase, Flavor, IgnoreRules, Pattern, PatternError, PatternSet,
};
use cgr_graph_rs_fuzz::{require, Input};
use libfuzzer_sys::fuzz_target;

const MAX_PATTERNS: usize = 6;
const MAX_PATTERN_BYTES: usize = 256;
const MAX_PATHS: usize = 6;
const MAX_PATH_BYTES: usize = 128;
const MAX_FRAGMENTS: usize = 12;
const FRAGMENTS: &[&str] = &[
    "*", "**", "/", "?", "[", "]", "!", "^", "\\", "-", " ", "\n", "#", ".git", "a", "b", "bin",
    "src", "é", ".min.js",
];
const GLOB_MAGIC: [char; 3] = ['*', '?', '['];

fn text(input: &mut Input, max: usize) -> String {
    if input.bool() {
        return input.text(max);
    }
    let mut out = String::new();
    for _ in 0..input.below(MAX_FRAGMENTS + 1) {
        let fragment: &&str = input.pick(FRAGMENTS);
        out.push_str(fragment);
    }
    out
}

fn refused_for_a_reason_python_has(error: &PatternError) -> bool {
    !matches!(
        error,
        PatternError::TooLarge { .. } | PatternError::Unexpected { .. }
    )
}

fuzz_target!(|data: &[u8]| {
    let mut input = Input::new(data);
    let patterns: Vec<String> = (0..=input.below(MAX_PATTERNS))
        .map(|_| text(&mut input, MAX_PATTERN_BYTES))
        .collect();
    let paths: Vec<String> = (0..=input.below(MAX_PATHS))
        .map(|_| text(&mut input, MAX_PATH_BYTES))
        .collect();
    let split = input.below(patterns.len() + 1);
    let (exclude, unignore) = patterns.split_at(split);

    // Each pattern alone, in both of pathspec's translations. When every
    // one compiles both ways and none is a block (whose empty lines pathspec
    // refuses), so do both sets.
    let mut sets_compile = true;
    for pattern in &patterns {
        sets_compile &= !pattern.contains('\n');
        for flavor in [Flavor::Basic, Flavor::Spec] {
            match Pattern::new(pattern, flavor) {
                Ok(compiled) => {
                    for path in &paths {
                        let _ = compiled.is_match(path);
                    }
                }
                Err(error) => {
                    sets_compile = false;
                    require!(
                        refused_for_a_reason_python_has(&error),
                        "{flavor:?} refused {pattern:?}: {error}"
                    );
                }
            }
        }
        // fnmatch never refuses; a name without wildcards matches itself.
        for path in &paths {
            require!(
                fnmatchcase(path, pattern).is_ok(),
                "fnmatch refused {pattern:?}"
            );
        }
        if !pattern.contains(GLOB_MAGIC) {
            require!(
                fnmatchcase(pattern, pattern) == Ok(true),
                "{pattern:?} does not match itself"
            );
        }
    }

    // Excludes alone, none negated and none a block: the set matches a
    // path exactly when one of its patterns does.
    let plain: Vec<&String> = exclude
        .iter()
        .filter(|pattern| !pattern.starts_with('!') && !pattern.contains('\n'))
        .collect();
    let singles: Option<Vec<PatternSet>> = plain
        .iter()
        .map(|pattern| PatternSet::new([pattern.as_str()]).ok())
        .collect();
    if let (Ok(set), Some(singles)) = (PatternSet::new(&plain), singles) {
        for path in &paths {
            let any = singles.iter().any(|single| single.match_file(path));
            require!(
                set.match_file(path) == any,
                "{plain:?} on {path:?}: the set and its patterns disagree"
            );
        }
    }

    // The decisions: answered, or refused only when a set cannot compile,
    // and then for a reason pathspec has too.
    let rules = IgnoreRules::new(exclude, unignore);
    for path in &paths {
        let parts: Vec<&str> = path.split('/').collect();
        let (dir_parts, name) = parts.split_at(parts.len() - 1);
        let prefix: String = dir_parts.iter().map(|part| format!("{part}/")).collect();
        let excluded = rules.matches_exclude(path);
        require!(
            excluded == rules.matches_exclude(path),
            "two answers for {path:?}"
        );
        let decisions = [
            rules.matches_unignore(path),
            rules.should_skip_rel_file(path, dir_parts),
            rules.should_skip_rel_path(path, true),
            rules.should_skip_rel_path(path, false),
            rules.should_keep_dir(name[0], &prefix),
            rules.unignore_names_vcs_dir(path),
        ];
        for decision in std::iter::once(&excluded).chain(&decisions) {
            match decision {
                Ok(_) => {}
                Err(error) => {
                    require!(
                        refused_for_a_reason_python_has(error),
                        "{path:?}: refused with {error}"
                    );
                    require!(!sets_compile, "{path:?}: {error} from sets that compile");
                }
            }
        }
        // Excludes beat everything: an excluded file is skipped, and an
        // excluded directory is not entered.
        if excluded == Ok(true) {
            require!(
                decisions[1] == Ok(true) && decisions[2] == Ok(true),
                "{path:?} is excluded but kept"
            );
        }
        if rules.matches_exclude(&format!("{path}/")) == Ok(true) {
            require!(
                decisions[4] == Ok(false),
                "{path:?}/ is excluded but entered"
            );
        }
        let _ = rules.unignore_names_this_file(path);
        let _ = rules.unignore_could_match_within(path);
    }
});
