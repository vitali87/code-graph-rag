# Rust Rewrite, Phase 1: Seams

[RUST_REWRITE_PLAN.md](RUST_REWRITE_PLAN.md) names phase 1 in one line:
"The per-language engine switch, the `rust` and `both` CI modes (identical
to `python` while nothing has moved), the data each stage hands to the next
written out by the Python engine too." This document decides how each of
those works, which pull requests build them and in what order, and when the
phase is done.

Nothing moves to Rust in phase 1. Every step keeps today's graph, byte for
byte: the reference graphs do not change at all during the phase.

It was written against `main` at 0.7.98 (November 2026).

## Where phase 1 starts

- **The front door.** `GraphUpdater` (`codebase_rag/graph_updater.py`)
  drives every index: the CLI, the MCP server, the watcher and about 880
  test files. Its stages are listed in section 3.
- **The ingestor interface.** Everything the indexer writes goes through
  three methods, `ensure_node_batch`, `ensure_relationship_batch` and
  `flush_all` (`IngestorProtocol` in `codebase_rag/services/__init__.py`).
  Incremental runs also read and delete through `fetch_all` and
  `execute_write` (`QueryProtocol`). One writer class, `MemgraphIngestor`,
  serves all three stores; `GRAPH_BACKEND` picks its dialect, and the
  embedded SQLite store is the default. `GraphUpdater` wraps it in a
  `FilteringIngestor`, which drops what the capture selection leaves out.
- **The Rust code today.** `cgr-graph-rs` holds the embedded graph, the
  Cypher parser, the TOML and XML readers and the tree bridge. The bridge
  (`codebase_rag/parsers/native_trees.py`) runs the indexer plan's four
  landed stages on py-tree-sitter's own trees. They are called from
  `anchor_hash.py` and `ast_fingerprint.py` (stage 1), `py/ast_analyzer.py`
  (stage 2, scopes), `interface_fingerprint.py` (stage 3), and
  `utils.py` and `py/variable_analyzer.py` (stage 4, called parameters and
  local aliases). One module flag, `native_trees.ENABLED`, turns the bridge
  off.
- **The `both_*` fixtures.** There are four, in
  `codebase_rag/tests/conftest.py`: `both_scope_walks`,
  `both_interface_walks`, `both_alias_walks` and `both_callable_scans`. Each
  runs a test twice, once on the Rust walk and once on its Python twin, and
  each run must pass the test's own assertions. They do not compare the two
  runs' outputs with each other; the bridge's start-up probe does that, on
  one fixed snippet. Stage 1 has no fixture. So the plan's "already do this"
  is half true: they are the model for running a test on both paths, not for
  comparing the paths. The `both` mode below compares.
- **Settings.** `AppConfig` in `codebase_rag/config.py` declares all 108.
  `scripts/public_surface.py` builds the snapshot's `settings` section from
  `AppConfig.model_fields`, and `test_public_surface.py` fails when the code
  and `docs/reference/public-surface.json` differ.
- **CI.** The unit tests run in 12 cells: Ubuntu, Windows and macOS, Python
  3.12 and 3.13, two shards each, each capped at 45 minutes. The suite was
  split into shards because whole runs hit that cap. Extra capacity from our
  own servers (#3649) was closed without merging, so CI runs on GitHub's
  hosted runners only. Phase 1 adds no job.

## What phase 1 owes the plan's rules

| Rule in "Keeping the tests green" | What phase 1 builds |
|---|---|
| 2. `GraphUpdater` stays the front door; a per-language switch behind it | The `CGR_INDEX_ENGINE` setting and where `GraphUpdater` reads it (section 1) |
| 3. Three CI modes | `--engine-mode python\|rust\|both` and the jobs that run each (section 2) |
| 4. No test disappears silently | The replacement map and the check that enforces it (section 4) |
| 6. Black-box tests run whatever `cgr` is installed | The `rust` and `both` modes set the environment variable too, so a `cgr` started by a test runs on the selected engine |
| 7. Reference graphs | Unchanged through the phase; the scripts learn to pass an engine selection for phase 3 |
| 8. Invariant tests | The hand-off files are checked for the same output at another path under another hash seed |

## 1. The engine switch

### The setting

The setting is `CGR_INDEX_ENGINE` (the field is `INDEX_ENGINE`, as
`SKIP_EMBEDDINGS` is read from `CGR_SKIP_EMBEDDINGS`). It is not
`CGR_ENGINE`: the comments on `GRAPH_BACKEND` and the plan's phase 6
already call the graph store "the graph engine".

Its value is a list of tokens, split on commas, semicolons or spaces, as
`CGR_CAPTURE` is (the same `split_spec`). A token is one of:

- an engine on its own, which sets the default for every language: `auto`,
  `python` or `rust`;
- `LANGUAGE=ENGINE`, which sets one language. A language is named as
  `SupportedLanguage` names it: `python`, `javascript`, `typescript`, `tsx`,
  `rust`, `go`, `scala`, `java`, `c`, `cpp`, `php`, `lua`, `c_sharp`,
  `dart`, `sql`, `julia`.

The default is `auto`.

| Value | Meaning |
|---|---|
| `auto` (default) | Each language on the engine this release runs it on by default. In phase 1, all Python |
| `python` | Every language on Python |
| `rust` | Every language this build can run on Rust, on Rust; the rest on Python |
| `go=rust` | Go on Rust, every other language `auto` |
| `python,go=rust` | Go on Rust, every other language on Python |
| `rust,c=python,cpp=python` | Everything this build can run on Rust, except C and C++ |

Rules:

- `auto` resolves through a table in `constants/`, `INDEX_ENGINE_DEFAULTS`,
  one entry per language. When Go's default moves in phase 3, that table
  changes and the setting's default stays `auto`. `go=python` is then the
  way back. `go=auto` is allowed and means the release's choice.
- Values are read in any case and stored lower-case.
- At start-up, like any invalid setting, cgr refuses an unknown language,
  an unknown engine, a language named twice, two bare engines, and a token
  with nothing after `=`.
- Families move together, as the plan says: `javascript`, `typescript` and
  `tsx`, and `c` and `cpp`. They resolve each other's imports and includes,
  so one family split across two engines would resolve across the split. A
  value that would split a family is refused, with a message that names
  every member of that family. The engine crate lists whole families only,
  and `INDEX_ENGINE_DEFAULTS` gives a family one entry, so neither can
  split one either. Loosening this later is an additive change; tightening
  it would break setups.

### A language not yet moved

The engine a language actually runs on is the one asked for, if this build
can run that language on it, and Python otherwise. Which languages the build
can run on Rust comes from one function, `rust_engine_languages()` in a new
`codebase_rag/index_engine.py`. In phase 1 it returns an empty set. From
phase 2 it reads the list the engine crate publishes.

When Rust is asked for and the build cannot run that language on it:

- asked through the default (`rust`, or `auto` once a default is Rust): the
  language runs on Python with a debug line only. This is what lets CI run
  the whole suite in `rust` mode.
- asked by name (`go=rust`): the language runs on Python and cgr warns once
  per process, naming the language.

It never stops cgr from starting. A `.env` that works on one build must not
break another.

### Where GraphUpdater reads it

- `GraphUpdater.__init__` takes `index_engine: IndexEngineSelection | None =
  None`. `None` reads the setting through `default_index_engine()`, as
  `capture` reads `CGR_CAPTURE` through `default_capture()`.
- The constructor resolves it once, into `self.engines` (every language and
  the engine it runs on) and `self.rust_languages` (the languages on Rust,
  empty in phase 1). The watcher, the MCP server and the tests keep one
  updater across many runs, and a run must never straddle two selections.
  `MemgraphIngestor` resolves its dialect once for the same reason.
- Phase 1 also gives the updater its one routing point:
  `_route(filepath) -> FileRoute`, the tier and language a file is indexed
  by. Today that decision is the `if`/`elif` chain in
  `_process_single_file` plus `_tree_sitter_language`; both use `_route`
  afterwards, with no change in behaviour. Phase 2 splits the files to
  re-parse by route: files whose language is on Rust go to the Rust engine,
  the rest through today's passes. Phase 1 does not add that split. With no
  Rust engine to hand files to, it would be a branch no run can take.
- The tree bridge stays under `native_trees.ENABLED`. Its walks are part of
  the Python engine: `CGR_INDEX_ENGINE=python` does not turn them off.
- There is no command-line option in phase 1. The setting reaches the CLI,
  the MCP server and the watcher alike.

### The public surface

One entry is added to the snapshot's `settings` section, by running
`uv run python scripts/public_surface.py`:

```json
{"name": "INDEX_ENGINE", "env": ["CGR_INDEX_ENGINE"], "env_only": false, "type": "str", "constraints": {}, "default": "auto"}
```

It adds and removes nothing else, so it is not a breaking change. It is not
environment-only: choosing an engine opens nothing, so a project's `.env`
may set it. The parsed selection lives in `index_engine.py`, not as a
property on `settings`, which would also add a member to the snapshot's
Python SDK section. The setting gets a row in
`docs/getting-started/configuration.md` and a commented line in
`.env.example`.

### How tests choose it

- **A whole run of the suite:** `--engine-mode` (section 2).
- **One updater:** `GraphUpdater(..., index_engine=parse_index_engine("go=rust"))`.
- **One test, in its own process and in any `cgr` it starts:** the marker
  `@pytest.mark.index_engine("go=python")`. It sets the setting and the
  environment variable for that test, over whatever the mode set. It exists
  for internals tests that must stay on Python until they are rewritten
  (phase 3 onward). The test still runs in every mode; nothing is skipped.
  The pinned tests of a language are the list of work its internals rewrite
  has left, and that language's phase ends when none are left.
- A test that sets the variable or patches the setting itself wins over the
  mode, because its own fixtures run after the mode's.

## 2. The three CI modes

### Choosing a mode

A pytest option, `--engine-mode`, takes `python`, `rust` or `both`. The
default is `python`. It is registered by a plugin,
`codebase_rag/tests/engine_modes.py`, loaded from `addopts` in
`pyproject.toml` with `-p`, next to `suite_shards`. A bare `pytest`, as CI
runs it, loads the tests' `conftest.py` too late to register an option, the
reason `suite_shards` is a plugin too. An option rather than an environment
variable: a variable would also reach the `.env` merge and every child
process, and the option shows in the job's command line. The plugin prints
the mode in the session header.

### `python`

Today's suite, unchanged.

### `rust`

An autouse fixture sets `settings.INDEX_ENGINE` to `rust` and
`CGR_INDEX_ENGINE=rust` in the environment, both through `monkeypatch`.
Updaters built in the test and any `cgr` the test starts then run every
language the build can run on Rust on Rust, and the rest on Python. In
phase 1 that is none, so this mode runs exactly what `python` runs.

### `both`

The test runs as in `rust` mode. In addition, each time the test's
`GraphUpdater.run` returns on a repository (not a single-file target), the
plugin indexes the same repository twice more and compares:

- once with every language on Python, once with the test's own selection;
- each a fresh, full index (`force=True`, its own scratch state directory,
  no embeddings) into its own scratch embedded graph;
- each built by a new `GraphUpdater.replica()` from the test updater's own
  inputs: repository, parsers, queries, exclusions, project name and
  capture;
- each writing its hand-off files (section 3).

The comparison runs after the test's own run, so a test that skips for a
missing grammar or fails in its run pays nothing extra. The two scratch runs
call `run` without the test's wrappers, so the pass-failure gate and the
grammar skip see the test's own run only; a failure swallowed in a scratch
run is reported with the comparison.

It is skipped when the two selections put every language that has a file
in the repository on the same engine. When the test's selection has no
language on Rust at all, that is known without reading the repository. In
phase 1 that is always the case, so `both` costs what `python` costs. Once
some language is on Rust, the plugin walks the repository's eligible files
(`walk_eligible_files`, no parsing) to see whether it holds that language.
Within one test it compares each distinct state of the repository once.

Not compared in phase 1: `reingest`, the watcher, single-file targets, and
an incremental run as an increment. An incremental run is compared as the
fresh index of the files it saw. Whether an incremental sync equals a fresh
index is the plan's invariant 8; the tests that check it run in every cell,
the `rust` cells included.

### What `both` compares

1. **The graph.** Each scratch graph is exported (`export_graph_to_dict`,
   what `cgr start -o` writes) and turned into lines by the reference
   graphs' own `canonical()`: one node or relationship per line, a node
   named by its labels and unique key, the checkout written as `<root>`,
   `last_synced_at` dropped, sorted, duplicates kept. The two line lists
   are compared as multisets with the reference graphs' `difference()`.
   "The graph handed to the ingestor" is compared as the embedded store
   keeps it. Two streams that store the same graph are the same graph,
   whatever the order and repetition of their batches. Comparing the raw
   calls would force Rust to copy Python's repeated and partial emissions,
   and an in-memory copy of the store's rules drifts from the real store,
   as the one in `evals/cgr_graph.py` did (#1938). One store is enough:
   the same writer class sends the same batch queries to all three, and
   `test_embedded_graph_memgraph_parity.py` holds the embedded engine's
   Cypher to the answers Memgraph gave.
2. **The hand-offs.** The file plan and the project index (section 3), to
   name the first stage at which the two engines part.

### How a difference is reported

The test fails at teardown, as `_fail_on_swallowed_pass_errors` does, so
its own assertions still report. The message names the repository and the
languages on Rust, then each artefact in pipeline order, marking the first
that differs, with up to 20 differing lines each, `-` for Python and `+`
for Rust:

```
Python and Rust engines differ on <tmp>/repo (go on Rust)
  files: same
  index: 3 lines only on Python, 1 only on Rust  <- first difference
    - definition	uuid.uuid.Parse	{"label":"Function"}
    ...
  graph: 12 lines only on Python, 4 only on Rust
    - E	CALLS	Function	"uuid.uuid.Must"	Function	"uuid.uuid.Parse"	{...}
    ...
```

When `CGR_PARITY_DIFF_DIR` is set, the plugin also writes both sides' files
and the full differences there, under the test's id. Only the plugin reads
it; it is not a cgr setting. CI sets it and uploads the directory when a
cell fails, from phase 3, when there is something to fail.

### Which jobs run which mode

| Job | Phase 1 mode |
|---|---|
| Unit tests, Python 3.12 (Ubuntu, Windows, macOS, two shards each) | `python`; macOS keeps coverage |
| Unit tests, Ubuntu, Python 3.13, two shards | `both` |
| Unit tests, Windows and macOS, Python 3.13, two shards each | `rust` |
| Base install, toolchain tests, Go frontend, integration | `python` |
| Reference graphs | `python`, plus the hand-off check (section 3) |

`rust` and `both` cost nothing while nothing has moved, so they replace
`python` in existing cells instead of adding jobs. The 3.12 cells keep the
Python engine under test on every operating system, and while nothing has
moved the 3.13 cells test it too. `both` goes on Ubuntu, where its extra
runs are cheapest. `rust` goes on Windows and macOS, because the Rust engine
must work there too and no other job would run it there.

Decided now for phase 3, when Go is the first language the build can run on
Rust:

- The Ubuntu 3.13 cells then pay two extra indexes for each test whose
  repository holds Go files, and nothing for the rest. If either cell runs
  past 35 minutes, those two cells become three shards. No other job
  changes.
- The reference-graphs job also checks the repositories of the languages on
  Rust (`uuid` for Go) with those languages on Rust.

## 3. The hand-off data

### The stages GraphUpdater runs today

`GraphUpdater.run`, in order:

| # | Stage | Code | Hands to the next |
|---|---|---|---|
| 0 | Prologue | `run` up to `_is_already_in_sync`: per-run resets, the Project node, `_drop_cache_if_graph_lost`, `_reparse_all_if_parser_changed`, `_register_generated_sources` | The effective unignore set, or an early return when the checkout is in sync |
| 1 | Structure (pass 1) | `structure_processor.identify_structure()` | Package and Folder nodes; `structural_elements`, each directory's container |
| 2 | Frontends before definitions | `_run_cpp_frontend` (libclang mode), `_run_emitting_frontends(BEFORE_DEFINITIONS)`, `_run_csharp_frontend`, `_run_go_frontend` | The files libclang covered; Roslyn's base-kind answers; the Go and C# frontends' resolved call sites |
| 3 | Discovery and change detection | `_process_files` up to the parse: `_hash_baseline`, `_collect_eligible_files`, `_scan_eligible_file`, the dependents expansion, `_delete_stale_subtrees` | The eligible files in walk order; the files to re-parse, with their bytes; the deleted files; the inbound edges to restore |
| 4 | Definitions (pass 2) | `_pre_parse_changed_files`, then `_process_single_file` per file: `definition_processor.process_file`, manifests, notebooks, the ast-grep and document tiers, File nodes | Definition nodes; the registries (`function_registry`, `simple_name_lookup`, `module_qn_to_file_path`, `import_mapping`, `class_inheritance`, and dozens of per-language tables); `_parsed_files`; the parsed trees |
| 5 | Joins after definitions | On incremental runs the rehydrations; `_join_csharp_partials`, `_join_go_implements`, `_run_python_frontend`, `_run_java_frontend`, `_prune_stale_seeded_module_qns` | The same registries, with the frontends' facts joined |
| 6 | Deferred definitions | `_resolve_deferred_definitions` | Complete registries; `known_module_paths`; inheritance, containment and type edges |
| 7 | Calls (pass 3) | `_process_function_calls` | CALLS and flow edges; deferred IMPORTS edges; what each call resolved to |
| 8 | After calls | The IMPORTS flush, LINQ calls, unresolved references, overrides, endpoints, findings | Edges |
| 9 | Flush and clean-up | `flush_all`, trace edges carried over, endpoint resources, the orphan prune, glosses | The stored graph |
| 10 | Embeddings | `_generate_semantic_embeddings` | Vectors |
| 11 | Commit | `_commit_run_state` | Hash cache, directory times, parser fingerprint, sync time |

Stages 1 to 9 write through the filtering ingestor. Most hand-offs between
them are shared objects the stages keep changing, not values one stage
returns to the next. That is why only three points are written out.

### The hand-off points the Rust engine replaces first

The plan's target design is: each file read into a summary, the summaries
merged into project-wide indexes, calls resolved against those indexes, one
writer. Three points of today's pipeline line up with it:

| Point | Where in today's run | Replaced in |
|---|---|---|
| A. The file plan | End of stage 3 | Phase 2: discovery with the ignore rules, change detection |
| B. The project index | End of stage 6, before pass 3 | Phase 2 builds the merge; phase 3 fills it with Go's extraction |
| C. The graph | Everything stages 1 to 9 hand the ingestor | Phase 2: the writer |

C is compared by `both` (section 2). A and B are written out.

Not written out:

- **Per-file summaries.** The Python engine has none. Pass 2 emits and
  registers as it walks the tree, and pass 3 resolves calls from the tree
  again. Making one in Python would mean rewriting pass 2 to compare it
  with code that does not exist yet. B holds the same facts after the
  merge.
- **The per-language tables.** Their shape is Python's. A language adds its
  own section to B when it moves: Go adds its method sets, interface pairs
  and type-checker call sites in phase 3, once the Rust design shows what it
  keeps.

### The serialised form

A new module, `codebase_rag/handoff.py`, writes it. `GraphUpdater` takes
`handoff_dir: Path | None = None`. `None`, the default, writes nothing and
costs nothing. It is a parameter, not a setting: it is an internal parity
tool that the test plugin and the scripts pass, not part of the interface.

| File | Written | Holds |
|---|---|---|
| `<handoff_dir>/files.txt` | At the end of stage 3 | The file plan, A |
| `<handoff_dir>/index.txt` | When stage 6 returns | The project index, B |
| `<handoff_dir>/graph.txt` | By the `both` plugin, from the export | The graph, C, as the reference graphs write it |

Each line is `kind<TAB>key<TAB>value`, like a reference-graph line. The
value is JSON written the reference graphs' way: sorted keys, no ASCII
escaping, no spaces. UTF-8, `\n` line ends. The first line is
`# cgr handoff <file> 1`, the format version. Each engine writes the
version it implements; a comparison of two versions fails and names both.
Phase 3 moved `index.txt` to version 2, which adds Go's own sections
([PHASE3_GO.md](PHASE3_GO.md), section 6) and keeps every section below
byte for byte.

`files.txt`:

| Kind | Key | Value | Order |
|---|---|---|---|
| `file` | Path from the repository root, `/`-separated | `{"language": "go" or null, "route": ..., "md5": ...}` | Walk order |
| `reparse` | Path | `{"reason": "new" \| "changed" \| "dependent"}` | Sorted; incremental runs only |
| `deleted` | Path | `{}` | Sorted; incremental runs only |

- `route` is what `_route` decides: `tree_sitter`, `cpp_frontend` (libclang
  emitted the file), `manifest`, `notebook`, `ast_grep`, `document` or
  `none`.
- `md5` is the hash this run records for the file in the hash cache.
- The `file` lines keep walk order, a directory's files by name and then its
  subdirectories'. The order is data: pass 2 registers files in it, and it
  decides which of two same-stem files claims a module's bare name (#1569).

`index.txt`, every line sorted:

| Kind | Key | Value | Read from |
|---|---|---|---|
| `module` | Module qualified name | `{"language": ..., "path": ...}` | `module_qn_to_file_path` |
| `definition` | Qualified name | `{"label": ...}` | `function_registry` |
| `simple_name` | Simple name | Sorted list of qualified names | `simple_name_lookup` |
| `imports` | Module qualified name | `{local name: target}` | `import_mapping` |
| `inherits` | Class qualified name | Parents, in declaration order | `class_inheritance` |

### Keeping it deterministic

The rules the reference graphs follow:

- No absolute paths, no times, no row ids. Paths are relative to the
  repository root, with `/`.
- Every set is written sorted. A list keeps its order only where the order
  means something (the files, a class's parents).
- Nothing depends on the checkout's path, `PYTHONHASHSEED` or the operating
  system's line ends.

Two checks hold it:

- A unit test indexes one small fixture repository per reference language
  twice, the second time at another path under another `PYTHONHASHSEED`, in
  a child process, and requires byte-identical hand-off files.
- `scripts/handoff.py check --runs 2` does the same for the pinned
  repositories in `fixtures/reference_graphs/repos.json`, in the
  reference-graphs job. Each run indexes its copy in a child Python process
  of its own, started with its own `PYTHONHASHSEED`, as the reference-graph
  harness does: the seed is fixed when an interpreter starts, so runs in one
  process could not vary it. The child calls `GraphUpdater` directly; the
  hand-off is not reachable through `cgr`, on purpose.

The hand-off files are not checked in. Both engines exist side by side
until a language's Python code is deleted, so they are compared live. After
that, the reference graphs hold the language.

### The comparison that makes it a parity check

In `both` mode, Python's files are the expected side and Rust's the actual
side:

- `files.txt` is compared line by line in order, because the order is data;
- `index.txt` and `graph.txt` are compared as multisets of lines, with
  `difference()`.

Any difference fails. Reading the report in pipeline order tells where to
look: a file plan that differs means discovery or change detection parted;
an equal file plan with a different index means extraction or the merge; an
equal index with a different graph means resolution or the writer.

In phase 1 both sides are Python. The check is then the Python engine
against itself across paths and hash seeds, and the plugin's own tests plant
a difference to show it is caught.

## 4. Tests that touch internals

The plan's rule: before a language's Python code is deleted, each internals
test is rewritten as a graph-level test or a Rust test, and a checked-in map
names the replacement of every removed test; CI refuses a removal without
one. Phase 1 adds the map, empty, and the check. Nothing is removed.

### The map

`codebase_rag/tests/removed_tests.toml`. TOML because it takes comments,
Python reads it with the standard library, and the `check-toml` pre-commit
hook already validates it. An entry, with made-up names:

```toml
[[removed]]
test = "codebase_rag/tests/test_go_receivers.py::TestReceivers::test_pointer_receiver_registered"
replaced_by = ["codebase_rag/tests/test_go_methods_graph.py::test_pointer_receiver_method_defined"]
reason = "Read the Python registry; the graph-level test checks the same DEFINES_METHOD edge on both engines."
pull_request = 4321
```

- `test` is the removed test's id without parameters: the file, the class
  if any, the function. A Rust test is `rust/<crate>/<file>.rs::<mod>::<fn>`.
- `replaced_by` lists one or more tests that exist after the change: a
  graph-level Python test or a Rust `#[test]`. If a replacement is itself
  removed later, it gets an entry of its own, and the check follows the
  chain to a test that exists.
- `obsolete` takes the place of `replaced_by` only when the code the test
  checked is deleted in the same pull request, as the tree bridge is in
  phase 5. It names the deleted file, and the check confirms the file is
  gone. The `reason` is reviewed, so even this is not silent.

### The check

`scripts/test_removals.py check --base <sha>`, standard library only, runs
in the lint job on every pull request. It fetches the base commit (depth
1) and lists the tests of both trees without importing anything:

- Python: an `ast` walk of the files pytest collects, by the `testpaths`,
  `python_files`, `python_classes` and `python_functions` in
  `pyproject.toml`;
- Rust: the `#[test]` functions under `rust/`.

It fails when:

- a test on the base is not on the head and has no entry;
- an entry's replacement does not exist on the head, after following
  chains;
- an entry names a test that still exists on the head;
- an `obsolete` file still exists.

Each failure says which line to add. The base is the event's base commit:
`actions/checkout` checks out the pull request merged into its base, so the
difference is exactly what the pull request removes, even when `main` has
moved on. Listing statically takes seconds and needs no dependencies, where
collecting all 27,000 cases means installing and importing the whole suite
twice. It sees test functions: a dropped parametrize case is left to
review. A renamed or moved test counts as removed, and its entry
names the new id.

## 5. The steps

Each is one pull request. Each keeps the whole of CI green, and none
removes, skips or marks a test as expected to fail.

1. **Share the reference graphs' line format.**
   - Adds: `codebase_rag/canonical_graph.py`, holding `canonical()`, the
     JSON writer, the `<root>` rewrite, `difference()` and the file
     reader and writer, moved from `scripts/reference_graphs.py`, which
     imports them. The hand-off writer and the `both` plugin need them,
     and production code cannot import from `scripts/`.
   - Tests: the existing reference-graph tests; one more pins `canonical()`
     on a small export.
   - Worked when: the reference-graphs job passes with no fixture changed.
2. **The setting.**
   - Adds: `codebase_rag/index_engine.py` (the engine names, the selection
     record, `parse_index_engine`, the resolution, `rust_engine_languages()`
     returning nothing), `INDEX_ENGINE_DEFAULTS`, the `AppConfig` field and
     its start-up validation, the snapshot entry, the configuration row and
     the `.env.example` line.
   - Tests: every value form; each refusal; the family rule; resolution
     (`auto`, `python` and `rust` all give Python everywhere; `go=rust`
     gives Python and one warning); the snapshot entry.
   - Worked when: `test_public_surface.py` passes with one added entry;
     `CGR_INDEX_ENGINE=go=fortran cgr --version` is refused the way other
     invalid settings are; the reference graphs are unchanged.
3. **GraphUpdater reads it.**
   - Adds: the `index_engine` parameter, `engines`, `rust_languages`,
     `_route()` taken out of `_process_single_file`, and `replica()`.
   - Tests: on a fixture per language, every selection gives the same
     ingestor calls; `_route` agrees, for every file, with the tier that
     indexed it; `replica()` copies every input; the parameter wins over
     the setting.
   - Worked when: the whole of CI passes and the reference graphs are
     unchanged.
4. **The modes.**
   - Adds: the `engine_modes` plugin (`--engine-mode`, the `rust` fixture,
     the `both` comparison with its skip and report, the `index_engine`
     marker, the session header), the marker in `pyproject.toml`, and in
     `ci.yml` the 3.13 cells' modes from section 2.
   - Tests, through `pytester`: the option and the header; `rust` sets the
     setting and the variable, and a child `cgr` sees it; the marker wins
     over the mode; `both` skips when nothing has moved; with
     `rust_engine_languages()` patched to Go, `both` compares and finds
     Python equal to itself; a planted difference (one edge dropped, one
     property changed) is reported as exactly those lines.
   - Worked when: each 3.13 cell's header shows its mode, the cells pass,
     and their times stay within their usual spread.
5. **The Python engine writes its hand-offs.**
   - Adds: `codebase_rag/handoff.py`, the `handoff_dir` parameter, and the
     two files at their points.
   - Tests: the contents on fixtures (a Go fixture's files in walk order
     with their routes and hashes; its modules, definitions, imports and
     parents); an incremental run's `reparse` and `deleted` lines; the
     same bytes at another path under another seed; nothing written
     without `handoff_dir`; the same graph with and without it.
   - Worked when: the tests pass and the reference graphs are unchanged.
6. **Compare the hand-offs.**
   - Adds: `both` gives each scratch run a hand-off directory and reports
     the three files in order; `scripts/handoff.py record|check --runs N`
     for the pinned repositories, each run in a child process under its
     own `PYTHONHASHSEED`; `--index-engine SPEC` on it and on
     `scripts/reference_graphs.py`, which writes the value into the
     environment it controls (today it strips every cgr setting from the
     index's environment, so a selection in the shell never arrives); the
     hand-off step in the reference-graphs job.
   - Tests: the scripts' own (the selection reaches the index's
     environment; two runs are compared; a difference fails).
   - Worked when: the job passes, and the pull request states how much time
     the step added. Over three minutes, the step runs nightly instead.
7. **The replacement map.**
   - Adds: `codebase_rag/tests/removed_tests.toml` (empty),
     `scripts/test_removals.py`, its step in the lint job, and a paragraph
     each in `CONTRIBUTING.md` and `AGENTS.md`.
   - Tests: small Git repositories made in the test cover each rule above:
     a removal without an entry, with one, a missing replacement, a chain,
     a stale entry, `obsolete`, a rename, a removed Rust test.
   - Worked when: the lint job passes here, and the pull request shows it
     failing on a scratch branch that deletes one test.

### Exit criteria

Phase 1 is done when all of these hold:

- `CGR_INDEX_ENGINE` is in the snapshot and the docs, is validated at
  start-up, and every value indexes the pinned repositories to today's
  reference graphs.
- `GraphUpdater` resolves it once per updater, and `_route` is the only
  place that decides how a file is indexed.
- CI runs `python` in the 3.12 cells, `both` in the Ubuntu 3.13 cells and
  `rust` in the Windows and macOS 3.13 cells, with the same number of jobs
  as before and no cell slower beyond its usual spread.
- `both` is shown to catch a planted difference, and to cost nothing when
  nothing has moved.
- The Python engine writes `files.txt` and `index.txt` on request,
  byte-identical at another path under another hash seed, on the fixtures
  and on the pinned repositories.
- `removed_tests.toml` exists, empty, and every pull request is checked
  against it; a planted removal fails the check.
- No test was removed, skipped or marked as expected to fail during the
  phase.

## 6. Risks and open questions

Questions the code could not settle, each with a recommendation.

- **Walk order against sorted path order.** The plan's target design merges
  summaries "in sorted path order". Python registers files in walk order: a
  directory's files by name, then its subdirectories. The two differ
  (`a/z.go` comes before `a/b/c.go` in walk order), and the order decides
  which same-stem file claims a module's bare name. *Recommendation:* the
  Rust merge uses walk order, the file plan records it, and the plan's
  wording is corrected in phase 2's first pull request. Python's order is
  left as it is.
- **Incremental parity.** `both` compares fresh indexes only.
  *Recommendation:* enough until a language's change detection runs on
  Rust, which is Go in phase 3. Then `both` adds an incremental comparison:
  index, apply the test's edit, sync on each engine, compare the stored
  graphs.
- **Porting a language while `both` gates.** Go's Rust engine will be
  incomplete for many pull requests. *Recommendation:* no list of accepted
  differences in gating CI, which would be an expected failure by another
  name. A language joins `rust_engine_languages()` only once `both` is clean
  on every test that indexes its files. Until then a nightly job, not
  required for merging, runs `both` with that language forced on Rust and
  reports the number of differing lines, which may only fall.
- **The mode leaking into tests that read the environment.** `rust` and
  `both` set `CGR_INDEX_ENGINE` in the environment so that child processes
  see it. A test that compares a dump of the settings, a child's whole
  environment, or `model_fields_set` can see it too. *Recommendation:* fix
  each such test in step 4 to pin its own environment; keep the variable,
  since it is what puts black-box tests on Rust.
- **Family refusal.** Refusing a split family may be stricter than the code
  needs. *Recommendation:* keep it; loosening later is additive.
- **Moving a default.** When Go's `auto` turns to Rust, the snapshot does
  not change, but what writes Go's graph does. *Recommendation:* announce it
  under breaking changes anyway (`CONTRIBUTING.md` counts a new default as
  one), with `CGR_INDEX_ENGINE=go=python` as the way back.
- **No command-line option.** *Recommendation:* add none until a user asks.
  The setting covers the CLI, the MCP server and the watcher; an option
  would cover the CLI alone.
- **The bridge switch.** `native_trees.ENABLED` stays a separate module
  flag, and the `both_*` fixtures stay as they are. *Recommendation:* leave
  both until phase 5 removes the bridge with py-tree-sitter.
- **The index may lack what Go needs.** The five sections in `index.txt`
  are the ones every language fills. *Recommendation:* add Go's own section
  in phase 3, as format version 2, rather than guess it now.
- **CI time when Go moves.** The Ubuntu 3.13 cells pay two extra indexes per
  test with Go files. *Recommendation:* measure in phase 3, and give those
  cells a third shard past 35 minutes. No new job.
- **Renames under the removal check.** Every renamed test needs an entry.
  *Recommendation:* accept it. It is one entry, and the map then also
  records where tests went.
