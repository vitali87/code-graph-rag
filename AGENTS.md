# Notes for agents and contributors

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: its rules apply to every
change. This file holds procedures that are easy to get wrong and that a test
alone cannot explain.

Codex, Cursor, Windsurf and Copilot's coding agent read this file directly;
Claude Code reads it through `CLAUDE.md`, and Copilot chat and code review
through `.github/copilot-instructions.md`.

## Bumping tree-sitter

The Rust extension (`rust/cgr_graph_rs`) walks the syntax trees py-tree-sitter
builds, in place (`codebase_rag/parsers/native_trees.py` says how). Two copies
of the tree-sitter C runtime therefore read the same memory: the one inside
the py-tree-sitter wheel and the one the `tree-sitter` crate compiles into the
extension. They must be the same code, so the two are pinned exactly and move
together, in one pull request:

| Pin | Where |
|---|---|
| py-tree-sitter | `tree-sitter==X` in `pyproject.toml` |
| tree-sitter crate | `tree-sitter = "=Y"` in `rust/cgr_graph_rs/Cargo.toml` |
| the pair the bridge was checked with | `PY_TREE_SITTER` and `TREE_SITTER_CRATE` in `rust/cgr_graph_rs/src/trees/python.rs` |

To move them:

1. Get both sources: py-tree-sitter's sdist from PyPI, and the crate from
   `https://static.crates.io/crates/tree-sitter/tree-sitter-<version>.crate`.
2. Diff the C files a tree walk uses, `node.c`, `tree.c`, `tree_cursor.c`,
   `subtree.h` and `language.c`: py-tree-sitter's
   `tree_sitter/core/lib/src/` against the crate's `src/`. They must be
   byte-identical. Pick the crate release where they are.
3. Check that py-tree-sitter's `tree_sitter/binding/types.h` still lays out
   `Node` as `{PyObject_HEAD; TSNode node; PyObject *children; PyObject *tree}`
   and `Tree` as `{PyObject_HEAD; TSTree *tree; PyObject *source; ...}`. That
   is what the bridge reads.
4. Update both pins and both constants, then bump the crate's own version
   (`rust/cgr_graph_rs/pyproject.toml` and `Cargo.toml`, plus the
   `cgr-graph-rs` floor in `pyproject.toml`), as CI requires for any change
   to the library. If the new crate needs a newer Rust than
   `RUST_TOOLCHAIN` in `.clusterfuzzlite/Dockerfile`, raise that too: the
   fuzz build compiles the crate with it and fails otherwise. The same goes
   for `RUST_FUZZ_TOOLCHAIN`, the nightly the crate's cargo-fuzz targets are
   built with, and for the `rust` image in the root `Dockerfile`, which
   builds the crate into the release image and must be no older than
   `RUST_TOOLCHAIN` (`test_rust_extension_packaging.py` checks this).
5. Run `codebase_rag/tests/test_native_trees.py` and
   `cargo test --manifest-path rust/cgr_graph_rs/Cargo.toml`.

`test_native_trees.py` fails when the pins disagree with the checked pair.
Dependabot bumps the `pyproject.toml` pin on its own; such a pull
request fails those tests until steps 1 to 5 are done.

## Removing, renaming or moving a test

Never delete a test to get a pull request through. A test goes only once a
test that checks the same thing exists, in the same pull request or an
earlier one, and `codebase_rag/tests/removed_tests.toml` names it
(CONTRIBUTING.md, "Removing a test", has the format). A rename, a move to
another file, class or Rust `mod` and a deleted file all count as removals,
Rust `#[test]` functions included (named
`rust/<crate>/<path>.rs::<mod>::<fn>`). Commit, then run
`python3 scripts/test_removals.py check --base origin/main`: it reads the
committed trees, not the working tree, and prints the entry each removal
needs, with the moved test's new id when an added test has the same name.
Write `reason` and `pull_request` yourself. Never delete an entry to quiet
the check; the one exception is a test that is back, whose entry the check
calls stale.

## Bumping a compiled grammar

`rust/cgr_graph_rs/src/grammars` compiles Go's grammar and the patched C and
C++ forks (#555, #2424) into the crate, each at exactly the release or commit
`uv.lock` holds for its Python package, and the parser loader hands
py-tree-sitter the C and C++ ones on every install
(`codebase_rag/crate_grammars.py`). So each grammar's two sides move together,
in one pull request:

| Pin | Where |
|---|---|
| the Python package | its exact pin in `treesitter-full` in `pyproject.toml`, and for a fork its `rev` in `[tool.uv.sources]` |
| the crate | `tree-sitter-<lang>` in `rust/cgr_graph_rs/Cargo.toml`: `=X`, or the fork's `git` and `rev` |
| what the build reports | `pin` in `GRAMMARS` in `rust/cgr_graph_rs/src/grammars/mod.rs` |

To move one:

1. For Go, take the crates.io release with the PyPI release's version. For C
   or C++, rebase the fork, tag the new commit `code-graph-rag-pin` as the
   current one is, and use that commit on both sides. A fork's
   `treesitter-full` pin is the version its `pyproject.toml` declares.
2. Update the three places above, then run `uv lock`, and `cargo update -p
   <crate>` in both `rust/cgr_graph_rs` and `rust/cgr_graph_rs/fuzz`.
3. Bump the crate's own version as for any change to the library (see
   "Bumping tree-sitter", step 4) and regenerate its licences with
   `python scripts/generate_rust_licenses.py`.
4. Run `codebase_rag/tests/test_grammar_pins.py`, `cargo test --manifest-path
   rust/cgr_graph_rs/Cargo.toml` and `scripts/reference_graphs.py check`. A
   new grammar can change a graph: the pull request then records the graphs
   again and says why each line moved.

`test_grammar_pins.py` fails while the two sides disagree, and also when one
version's Python package and crate hold different grammars: it compares their
fingerprints (ABI, node-kind, field and parse-state counts, and a hash of the
kind and field names) and parses its samples with both. Dependabot leaves the
crates and the forks alone. It still bumps `tree-sitter-go` in `pyproject.toml`,
and that pull request fails the test until steps 1 to 4 are done. A grammar
whose crate is named differently from its Python package (SQL's is
`tree-sitter-sequel`, Markdown's `tree-sitter-md`) gets both names in that
test's `COMPILED`.
