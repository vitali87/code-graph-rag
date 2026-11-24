# Rust Rewrite, Phase 2: The Engine's Frame

[RUST_REWRITE_PLAN.md](RUST_REWRITE_PLAN.md) names phase 2 in one line:
"The Rust engine's frame, a crate beside `cgr-graph-rs`: discovery with the
ignore rules, change detection, the all-cores scheduler, tree-sitter 0.27
and the grammars, the merge, the writer behind the same ingestor interface."
This document decides how each of those works, corrects the parts of that
line the code has overtaken, lists the pull requests in order, and says when
the phase is done.

No language's analysis moves in phase 2. The frame takes over, for every
language at once, the work that does not depend on language: finding the
files, hashing them, telling what changed, the folder structure and the File
nodes. So the frame runs on every index users make for a whole phase before
any language's graph depends on it.

It was written against `main` at 0.7.111 (November 2026), after phase 1
([PHASE1_SEAMS.md](PHASE1_SEAMS.md)) was complete.

## Where phase 2 starts

- **The seams.** `CGR_INDEX_ENGINE` and its resolution, `GraphUpdater`'s
  `engines` and `rust_languages`, `_route()` as the one place a file's tier
  is decided, the `python`, `rust` and `both` test modes, the hand-off files
  (`files.txt`, `index.txt`) and the removed-tests map all landed in phase 1.
- **The crate.** `cgr-graph-rs` 0.1.8: 13,909 lines (the embedded graph, the
  Cypher parser, the TOML and XML readers, the tree bridge), pyo3 0.30 with
  `abi3-py312`, so one wheel per platform: Linux gnu and musl on x86_64 and
  aarch64, macOS universal2, Windows x64. It starts no threads; it releases
  the interpreter lock around its long calls. `publish.yml` uploads a new
  crate version before the package that needs it, so a crate change and the
  floor that requires it land in one pull request.
- **tree-sitter.** The bridge reads py-tree-sitter's trees with the crate's
  own runtime, so the two are pinned as a pair (`AGENTS.md`): py-tree-sitter
  0.26.0 and the crate at `=0.26.8`. py-tree-sitter 0.27.0 was published on
  2026-11-23. Its whole C runtime (`core/lib/src`) is byte-identical to the
  crate's 0.27.2, and its `Node` and `Tree` objects still start the way the
  bridge reads them.
- **Where time goes on a sync.** On cgr's own package (495 files), a full
  index takes 222 s. A sync with nothing changed takes 1.9 s: about 1.1 s of
  interpreter start-up and imports, 0.1 to 0.3 s re-hashing cgr's own
  sources for the parser fingerprint, and 2 ms checking the files. Finding
  and checking files is not where users wait today. On a large repository
  the first index hashes every file on one core.
- **Bugs found while specifying.** Seven, none filed before, each confirmed
  with a reproduction: #3721 (a named pipe hangs the index), #3722 (one file
  name that is not UTF-8 crashes it), #3723 (an escaped trailing space in
  `.gitignore` crashes it), #3724 (a byte-order mark hides the first ignore
  pattern), #3725 (the parser fingerprint leaves out the engine and the
  crate version), #3726 (`reingest` saves the hash cache in place) and #3727
  (a reused updater re-parses files a fresh one does not).

## The decisions

| Question | Decision | Instead of |
|---|---|---|
| Where the engine lives | Inside `cgr-graph-rs`, as `src/engine/` and `src/grammars/` (section 1) | A second crate and package beside it, as the plan said |
| Which tree-sitter | Move the pair to py-tree-sitter 0.27.0 and the crate 0.27.2 first (section 2) | A second runtime for the engine |
| What moves for real | Discovery, file hashes, the file half of change detection, the structure pass and the File nodes, for every language (section 3) | Nothing until Go moves |
| How it is switched on | An internal flag, then a check that runs Python's answer beside it in production and falls back to it on any difference (section 4) | `CGR_INDEX_ENGINE`, which chooses per language |
| The ignore rules | A port of today's rules and their `pathspec` semantics, checked against `pathspec` itself (section 5) | The `ignore` crate's own gitignore rules |
| The writer | Rust builds the rows; today's ingestor writes them (section 9) | Rust writing to the store |
| Merge order | Walk order (section 10) | Sorted path order, as the plan said |
| Grammars | Compiled in when their language moves, Go first, one version on both engines, checked (section 11) | All sixteen at once |
| Today's bugs | The seven above are fixed in Python first (section 13, step 0) | Copied, then fixed twice |

## 1. One crate

The plan put the engine in a crate of its own so that it could use a newer
tree-sitter than the bridge. That reason is gone: py-tree-sitter 0.27.0 and
the crate 0.27.2 share one runtime, and the pair moves first (section 2).
What remains are the costs of two packages:

- **Cargo allows one tree-sitter per build.** Every `tree-sitter` crate
  declares `links = "tree-sitter"`; a workspace holding the bridge on one
  version and the engine on another is refused. A separate package works only
  as a separate build.
- **A second package doubles the release machinery:** six more wheels and
  seven more test jobs in `rust-wheels.yml`, the PyPI check and publish jobs
  in `publish.yml`, the version floor and its tests in
  `test_rust_extension_packaging.py`, the uv source, the Docker image, the
  PyInstaller spec, the licence report and Dependabot.
- **It would load a third copy of SQLite.** `rusqlite` bundles SQLite, the
  engine's writer would need it, and `native.py` already manages the hazard
  of two copies writing one file (one owner per file).
- **It gains nothing at run time.** Two extension modules can each carry a
  tree-sitter runtime without clashing (each exports only its `PyInit_`
  symbol), but nothing needs that once the pair has moved.

So the engine goes into `rust/cgr_graph_rs`:

| Path | Holds |
|---|---|
| `src/engine/discover.rs` | The walk, the ignore rules' matching, regular files only (section 5) |
| `src/engine/ignore.rs` | Reading `.gitignore` and `.cgrignore`, and the pattern matcher |
| `src/engine/state.rs` | The state folder, the hash cache, the folder times, the in-sync check (section 6) |
| `src/engine/structure.rs` | Packages, folders, `structural_elements`, File rows (section 7) |
| `src/engine/schedule.rs` | The thread pool and the order rules (section 8) |
| `src/engine/rows.rs` | Rows handed to the ingestor (section 9) |
| `src/engine/merge.rs` | Per-file summaries and the walk-order fold (section 10) |
| `src/grammars/` | The compiled grammars and their fingerprints (section 11) |

Python reaches it through one internal class, `cgr_graph_rs.Engine`, wrapped
by a new `codebase_rag/engine_frame.py`. Neither is part of the public
surface. The module keeps its name: renaming it would touch every install for
no gain.

Two things to keep right under one crate: `build.rs` and any non-Rust
sources must join the uv rebuild keys and the version-bump check in
`rust-wheels.yml`, and grammar crates come from crates.io or git, never as
vendored path crates, which the licence report skips.

## 2. tree-sitter 0.27 first

Moved by the procedure in `AGENTS.md`, as the first step:

- The five files a tree walk uses, and the rest of `core/lib/src`
  (`parser.c` and `lib.c` included), are byte-identical between
  py-tree-sitter 0.27.0 and the crate 0.27.2.
- `binding/types.h` keeps `Node` as
  `{PyObject_HEAD; TSNode node; PyObject *children; PyObject *tree}` and
  `Tree` as `{PyObject_HEAD; TSTree *tree; PyObject *source; ...}`. `Tree`
  gained a `uint32_t busy` at its end, so the bridge's size check grows by
  it; the fields the bridge reads have not moved.
- `Node::child_count` now returns `u32`.
- All sixteen grammars (eighteen languages) load and parse on both runtimes.
- One reference graph changes, by one line: in cJSON's
  `tests/unity/src/unity.c`, an `else` that follows an `#endif` cannot be
  parsed, and 0.27 recovers from it differently (an expression statement
  where 0.26 had a type identifier). `UnityPrint`'s AST fingerprint changes;
  no edge does. Every other graph, and every recorded answer, is unchanged.

## 3. What the frame takes over

| Job | Today | Phase 2 | Stays in Python until |
|---|---|---|---|
| The walk and the ignore rules | `walk_eligible_files` (`utils/path_utils.py`), `config.py` | Rust | Phase 5 removes Python's |
| File hashes | One file at a time in `_scan_eligible_file` | Rust, on every core | Phase 5 |
| Change detection, the file half: state folder, hash cache, folder times, the in-sync check, new, changed and deleted files | `graph_updater.py` | Rust, given the graph's paths by Python | Phase 5 |
| Change detection, the graph half: graph-lost checks, dependents, subtree deletion, sync time, unfinished-run markers | `graph_updater.py` | Python | Each language's dependents move with it; Go's same-folder rule in phase 3 |
| The route of each file | `_route` | Python | Phase 3, when the frame drives parsing; `_route` stays the one place it is decided |
| The structure pass: Package and Folder nodes, `structural_elements` | `structure_processor.py` (311 lines) | Rust builds the rows and the map; Python writes them at today's point | Phase 5 |
| File nodes | Python | Rust builds the rows | Phase 5 |
| The module-name plan (which file claims a bare module name) | `definition_processor.py` | Rust computes it and it is compared; Python's is used | Phase 3 uses Rust's |
| The Project node | Python | Python | Phase 8 |

What users get in phase 2 is mostly safety, not speed: finding and checking
files takes milliseconds today. Hashing on every core shortens the first
index of a large repository a little. The gain is that the frame has run on
every index for a release before Go's analysis depends on it.

## 4. Switching it on, and the safety net

`CGR_INDEX_ENGINE` chooses an engine per language. The frame is not per
language, so it gets its own switch:

- **An internal flag**, `engine_frame.ENABLED`, like `native_trees.ENABLED`
  for the bridge. It is not a setting and not part of the public surface. It
  stays off until the last step of the phase.
- **A shadow check in production.** Once the flag is on, Python's walk,
  hashes and structure still run beside Rust's, at today's cost. On any
  difference cgr logs one warning naming the first differing paths, uses
  Python's answer for that run, and carries on. A user can never get a worse
  index than today's. The shadow goes in phase 5 with Python's walk.
- **In tests the shadow is strict.** The `both` cells (Ubuntu, 3.13) compare
  the frame with Python on every index, whether or not any language is on
  Rust, and fail with the differing lines. This needs no second index: the
  frame is compared at the end of stage 3 and after the structure pass. The
  `rust` cells (Windows and macOS, 3.13) turn the flag on with the strict
  shadow, so Windows and macOS paths are checked too.
- **On the pinned repositories**, `scripts/handoff.py check` compares the
  frame's file plan with Python's, in the reference-graphs job.

## 5. Discovery: what the Rust walk copies

Today's walk is not git's, and it is not the `ignore` crate's. The Rust walk
copies it rule for rule.

1. **Inputs.** Excludes: the `--exclude` values, `.cgrignore` lines and root
   `.gitignore` lines. Unignores: `!` lines from both files, the
   tracked-source rescue, and `<root>/**` for each generated-source root.
2. **Reading the ignore files.** Only the root `.gitignore` and the root
   `.cgrignore`, as UTF-8. Blank lines and `#` lines are dropped; `!x` is an
   unignore. In `.gitignore`, an exclude that a later `!` line or a
   `.cgrignore` `!` line cancels is dropped, and surviving `!` lines turn the
   file into one ordered block. (#3723 and #3724 change the trimming and the
   byte-order mark first.)
3. **Tracked-source rescue.** `git ls-files -z`, with a 30 s timeout;
   any failure rescues nothing. A tracked file is rescued only when its first
   ambiguous directory is one of `bin`, `coverage`, `env`, `obj`, `out`,
   `target`, `temp` or `tmp`, no other built-in directory is on its path, and
   its path has no pattern characters. The Rust walk runs the same command.
4. **Order.** In each directory, its files sorted by name, then each kept
   subdirectory in name order, recursively. Names compare by code point,
   which for UTF-8 is byte order. This is not sorted path order: `a/b.py`
   comes before `a-b/x.py`, and the root's files come first.
5. **Directories** are pruned by a matching exclude; `.git`, `.hg` and
   `.svn` are kept only when an unignore spells a path through them;
   built-in directory names (`constants/languages.py`) are kept only for
   `src/bin` or when an unignore could match inside. Links are never
   followed; the root is followed when it is itself a link.
6. **Files**, in order: compiled and temporary suffixes are skipped; a
   matching exclude skips; files inside version-control directories not
   kept are skipped; `.min.js` and `.min.css` are skipped unless an unignore
   names the file; a matching unignore keeps; a built-in directory on the
   path skips; anything else is kept. Links are dropped. After #3721 only
   regular files are kept, and after #3722 a name that is not UTF-8 is
   skipped with a warning.
7. **Matching** follows `pathspec`'s "gitignore" patterns on the path
   relative to the root, with a trailing `/` for directories, and a pattern
   that matches a directory matches everything in it. In an ordered block the
   last matching line wins, level by level, and an excluded parent cannot be
   re-included. Excludes beat unignores. Matching is case-sensitive.

How it is built:

- The walk lists directories with `std::fs::read_dir` on the pool
  (section 8) and sorts as rule 4 says. The `ignore` crate's walker is not
  used: by default it skips hidden files, reads nested `.gitignore` files,
  `.git/info/exclude` and the global excludes file, needs a git repository,
  and does not sort in parallel. Each default would change which files users
  get.
- The matcher is a port of `pathspec`'s pattern-to-regex translation onto
  the `regex` crate (`pathspec`'s regexes use no look-arounds). A
  differential test runs a corpus of patterns and paths through `pathspec`
  and through the Rust matcher and requires the same answers, and a fuzz
  target checks the matcher for panics and runaway time. `pathspec` is
  pinned exactly while both engines exist, so its behaviour cannot drift
  under the comparison.
- Folder identity uses the resolved path; on Windows the Rust side strips
  the `\\?\` prefix that `canonicalize` adds.

Kept as they are, because users see them and changing them is a behaviour
change for after the rewrite: only the root `.gitignore` is read (#2454),
`.gitignore` excludes beat the generated-source rescue (#3129), no shebang
detection (#3210), `.h` files are C++ (#2696), no detection of generated
files (#3213). Two more are copied and recorded for the same decision later:
a `.env` file and a submodule's `.git` file get File nodes, and a bare `!`
line walks every built-in directory.

## 6. Change detection: the file half

The Rust side reads and writes the same files Python does, so an upgrade
keeps users' state:

- **The state folder** is `$CGR_HOME/state/<cleaned folder name>__<first 8
  hex digits of sha256(resolved path)>`. On first use, `.cgr-*` files an
  older cgr left in the checkout are moved in, and a file already there is
  never replaced.
- **The hash cache** is JSON mapping each path to its md5 or `unreadable`.
  It is written to a temporary file, indented by 2, with its mtime set to
  the run's watermark, forced to disk, then renamed. A cache is never
  rewritten without its mtime restored: a fresh mtime would hide edits.
- **The folder times** are JSON mapping each folder (the root as `.`) to its
  mtime as Python's float, `st_mtime = sec + nsec * 1e-9`, compared exactly.
  The Rust side computes the same float and reads it back with exact
  rounding (`serde_json`'s `float_roundtrip`).
- **Old names.** A cache written before #3722 can hold a name with `\udcXX`
  escapes. The reader accepts and drops such entries rather than refusing
  the whole cache.
- **The watermark** is taken before hashing starts. A cached file at or
  under it keeps its hash unread; any other file is hashed as raw bytes,
  with no line-ending or byte-order-mark normalisation.
- **The in-sync check.** Each recorded folder's mtime matches exactly, or
  its listing shows no indexable file added or removed. Each cached file is
  not `unreadable` and not a link, and either `max(mtime, ctime)` is at or
  under the cache's own mtime or its md5 matches. On Windows it always
  hashes. A cache mtime in the future counts as 0.
- **Deleted files** are the cached, forced and graph-only paths minus those
  walked and those unreadable. Python passes the graph-only paths in.
- **The sync lock** (`.cgr-sync-lock`) stays Python's: it is held around the
  whole run.

The parser fingerprint gains the resolved engine of each language and the
`cgr-graph-rs` version (#3725) before any language moves, so a user who
switches engine, or upgrades the crate alone, gets a re-parse rather than the
other engine's stale output. It does not gain the frame flag: the frame's
output is compared to be identical.

## 7. The structure pass and File nodes

Rust builds the Package and Folder rows, the `structural_elements` map that
55 places read, and the File rows. Python writes them through the ingestor
at today's point, so the call order that 267 test files assert on does not
change. What a port must keep:

- Directories are emitted in Python's path order, which compares part by
  part (`a/b` before `a-b`), and case-insensitively on Windows.
- `*.vcxproj` is matched as a literal file name and never expanded.
- The repository root gets no Folder node.
- Package markers come from the per-language tables in `constants/`, passed
  in, not copied into Rust.

## 8. The scheduler

- **Threads:** `rayon` on a pool of the engine's own, sized by an internal
  parameter that defaults to the available cores. Tests run with 1, 2 and
  every core and require identical output.
- **Parsers:** one per language per worker (`map_init`), from phase 3 on.
- **The interpreter lock** is released for the whole engine call.
- **Order:** results are collected by position and replayed in walk order,
  never in the order threads finish. Nothing iterates a hash map into
  output; maps that reach output are ordered, or sorted when written.
- **External analysers** (Go's type checker, from phase 3) run as child
  processes, at most one per core.

## 9. The writer

Rust builds node and relationship rows; the existing ingestor writes them,
through the `FilteringIngestor` and the capture selection. Measured on the
leveldb reference repository: 3,100 node calls and 11,007 relationship
calls replay in 0.44 s, of which 0.33 s is SQLite inside the crate's own
writer, about 0.1 s Python grouping and 0.05 s building the row objects.
The Python side costs about 10 µs a row against 23 µs for the store; writing
is about 7% of a 6.8 s index.

So the ingestor stays the writer in phases 2 and 3:

- 267 test files assert on mock-ingestor calls, and the protobuf sink,
  Memgraph and Neo4j all need the calls.
- The writer's rules (rows split by merge key, rows without a call site,
  missing end nodes) stay in one place.
- Writing straight to SQLite would not remove the cost that dominates.

If phase 3's profiles show the row objects matter, the engine hands encoded
batches straight to the crate's own writer when the store is the embedded
one. One crate makes that a function call.

Order matters for the rows: the same node or edge is emitted more than once
with different values, and the last write wins (leveldb re-emits 278 Method
nodes, javapoet 349 ExternalModule paths, ky 15 CALLS resolutions). Rows are
replayed in stage order, then walk order. Batch boundaries do not matter: a
batch size of 1 and of 1,000,000 gave identical graphs on eight reference
repositories.

## 10. The merge

The plan's target design merges per-file summaries "in sorted path order".
That is wrong for this code base: Python registers files in walk order, and
the order decides which of two same-stem files claims a module's bare name
(#1569). The merge folds summaries in walk order, and the plan is corrected
in the same pull request as this document.

The rules a Rust merge must reproduce, all language-agnostic except where
said:

- **Module names.** From the path; `__init__.py`, `__init__.pyi` and
  `mod.rs` take their directory's name; a notebook keeps its extension; a
  same-stem file of another language family on disk makes both keep their
  extensions (#2586); a `.d.ts` beside its implementation takes the bare
  name (#1720); otherwise the first file in walk order claims the bare name
  and later ones get `.ext` (#1569). Only files parsed by tree-sitter claim.
- **The function registry.** The first registration takes the plain name,
  later ones `@line`, then `@line_col`; across files an `_10000·k` slot is
  added. The label is last-wins, except TypeScript's type-and-value twins.
  `simple_name_lookup` is a union, so order does not matter.
- **Inheritance.** Last assignment wins, except a TypeScript class and
  interface of one name, whose parents append.
- **Imports.** Last-wins per module and local name; the Scala and Rust
  cross-file passes are per language.
- **Type locations.** First claim per module, line and column.

Phase 2 builds the summary type, the fold and the module-name plan, compared
with the `module` lines of Python's `index.txt`. The other sections fill as
languages move.

## 11. Grammars

- **One version on both engines.** A grammar compiled into the crate has
  exactly the version `uv.lock` holds for its Python package, or the same
  git commit. They move together in one pull request, and Dependabot groups
  each grammar's two sides, as it does the runtime.
- **Compiled in when their language moves.** Phase 2 compiles Go's, which
  phase 3 uses and which gives the scheduler a real grammar to test with.
  All sixteen together compile in about 31 s on 4 cores and add about 31 MB
  installed (about 3.2 MB compressed); one at a time keeps the growth tied to
  use.
- **Checked, not trusted.** `test_grammar_pins.py` checks that the versions
  or commits agree, and compares a structural fingerprint of each compiled
  grammar as py-tree-sitter and the crate see it: ABI, node-kind and field
  counts, parse-state count, and a hash of the kind and field names. A
  matching version is not enough: SQL 0.3.11's Python package was generated
  at ABI 15 and its crate at ABI 14.
- **Exact pins** in `treesitter-full` for each language compiled into the
  crate, so a PyPI install cannot float away from it.
- **Names that mislead.** SQL's crate is `tree-sitter-sequel`; Markdown's is
  `tree-sitter-md`. The crates named `tree-sitter-sql` and
  `tree-sitter-markdown` are unrelated grammars.
- **C and C++** come from the patched forks at their pinned commits, as git
  dependencies: both carry a `Cargo.toml` and a `build.rs`, and the crate is
  not published to crates.io, so its git dependencies are allowed. Their
  `parser.c` files (3.7 MB and 17 MB) are not vendored.
- **Dart** keeps today's grammar. crates.io's `tree-sitter-dart` 0.3.0 is a
  rewrite with another root node and 25 of the node names cgr's Dart code
  uses missing. When Dart moves (phase 4), the crate compiles today's
  grammar from source; where that source is hosted is decided then.

## 12. CI and fuzzing

No new job:

- **Rust Graph Core** runs the engine's `cargo test`: about a minute more.
- **Wheels**: about 0.5 min more to build on Linux, 1 min on macOS and 1 to
  2 min on Windows; each grows with the grammars compiled in.
- **The `both` cells' frame check** costs one walk per index, milliseconds.
- **Fuzzing**: new targets beside the five existing ones, for the pattern
  matcher, the ignore-file reader, and the hash-cache and folder-time
  readers. They share the 600 s budget; if coverage stalls, the Rust fuzzing
  splits into two jobs.
- **Toolchains**: the newest minimum among the new dependencies is Rust
  1.90, under `RUST_TOOLCHAIN` (1.97), so nothing moves.

## 13. The steps

Each is one pull request. Each keeps the whole of CI green, and none
removes, skips or marks a test as expected to fail.

0. **Today's bugs, in Python, first.** Five pull requests: #3721 with #3722
   (the walk keeps regular files with UTF-8 names), #3723 with #3724
   (reading the ignore files), #3725 (the fingerprint), #3726 (`reingest`'s
   cache) and #3727 (the reused updater's re-parse plan). Each with a test
   that fails first.
1. **tree-sitter 0.27.** The pair moved by the `AGENTS.md` procedure, the
   bridge's size check, cJSON's graph re-recorded (section 2).
2. **This document**, and the plan's corrections: the engine inside
   `cgr-graph-rs`, the merge in walk order.
3. **The engine's skeleton.**
   - Adds: `src/engine/` with the pool and the order rules,
     `cgr_graph_rs.Engine`, `codebase_rag/engine_frame.py` with the flag
     off, the fuzz target scaffold.
   - Tests: the same output on 1, 2 and every thread; the flag off changes
     nothing.
4. **The ignore rules.**
   - Adds: reading both files and the matcher.
   - Tests: the differential corpus against `pathspec`; every existing
     ignore test run against the Rust matcher too; the fuzz target.
5. **Discovery and the file plan.**
   - Adds: the walk, its order, the hashes on every core; the `both`
     cells' frame check for the file plan; the flag on with the strict
     shadow in the `rust` cells; the frame's plan in
     `scripts/handoff.py check`.
   - Tests: the order corners (punctuation against `/`, case, non-ASCII
     names), links, special files, names that are not UTF-8, unreadable
     folders.
6. **Change detection, the file half.**
   - Adds: the state folder, the cache and folder-time reader and writer,
     the in-sync check, new, changed and deleted files.
   - Tests: a cache written by Python read by Rust and the reverse, byte
     for byte; mtimes preserved; the in-sync check's cases; the `both`
     comparison on every incremental test.
7. **The structure pass and File nodes.**
   - Adds: the rows and `structural_elements` from Rust, written at today's
     point.
   - Tests: the structure tests on both; the reference graphs unchanged.
8. **The merge frame.**
   - Adds: the summary type, the walk-order fold, the module-name plan.
   - Tests: the plan equals Python's `module` lines on the fixtures and the
     pinned repositories.
9. **Go's grammar.**
   - Adds: `src/grammars/` with Go, its exact pin, `test_grammar_pins.py`,
     the Dependabot grouping.
   - Tests: the pin and fingerprint checks; Go parses to the same trees on
     both runtimes over the Go fixtures.
10. **The switch.**
    - Adds: the flag on, the production shadow check.
    - Worked when: the reference graphs are unchanged, every cell passes,
      and the pull request states the times on the pinned repositories and
      on django.

### Exit criteria

Phase 2 is done when all of these hold:

- The seven bugs are fixed in Python, each with a test.
- The tree-sitter pair is py-tree-sitter 0.27.0 and the crate 0.27.2, and
  the bridge is on.
- The frame finds the files, hashes them, tells what changed on disk, builds
  the structure and the File rows for every index, with the shadow check in
  place.
- The `both` cells' frame check is clean on every test that indexes, the
  `rust` cells pass with the strict shadow, and the pinned repositories'
  file plans are identical.
- The output is the same on 1, 2 and every thread.
- Go's grammar is compiled in, its pins agree and its fingerprint matches.
- No test was removed, skipped or marked as expected to fail, and no job is
  slower beyond its usual spread.

## 14. Risks and open questions

- **Pattern semantics.** A port of `pathspec` can part from it on rare
  patterns. *Mitigation:* the differential corpus, the fuzz target, an exact
  `pathspec` pin, and the production shadow, which falls back.
- **Windows.** Case-insensitive structure order, `\\?\` prefixes and
  junctions. *Mitigation:* the `rust` cells on Windows run the strict
  shadow; nothing switches on until they pass.
- **State compatibility.** A user's cache must survive the upgrade.
  *Mitigation:* the cross-reading tests. A one-time re-parse after the
  upgrade is acceptable: the fingerprint change in #3725 causes one, and
  most releases already do, because the fingerprint hashes cgr's own
  sources.
- **The shadow's cost** is today's walk and hashing, kept for one phase.
  *Recommendation:* accept it; it ends in phase 5.
- **Free-threaded Python.** py-tree-sitter turns the interpreter lock back
  on. *Recommendation:* no action; the engine runs its own threads with the
  lock released.
- **Dart's grammar.** Its hosting is decided in phase 4.
- **The copied behaviours** in section 5 stay until after the rewrite, as
  the plan says for every interface change.
