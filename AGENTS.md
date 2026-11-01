# Notes for agents and contributors

Read [CONTRIBUTING.md](CONTRIBUTING.md) first: its rules apply to every
change. This file holds procedures that are easy to get wrong and that a test
alone cannot explain.

Codex, Cursor, Windsurf and Copilot's coding agent read this file directly;
Claude Code reads it through `CLAUDE.md`, and Copilot chat and code review
through `.github/copilot-instructions.md`. Those files only point here, so
edit this one.

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
   built with.
5. Run `codebase_rag/tests/test_native_trees.py` and
   `cargo test --manifest-path rust/cgr_graph_rs/Cargo.toml`.

If only one side moves, nothing breaks for users: the bridge sees a
py-tree-sitter it was not checked against, turns itself off, and the Python
code runs, only slower. `test_native_trees.py` fails in that case, and when
the pins disagree with the checked pair, so the slowdown cannot slip in
unnoticed. Dependabot bumps the `pyproject.toml` pin on its own; such a pull
request fails those tests until steps 1 to 5 are done.
