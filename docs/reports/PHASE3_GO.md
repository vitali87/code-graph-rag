# Rust Rewrite, Phase 3: Go End to End

[RUST_REWRITE_PLAN.md](RUST_REWRITE_PLAN.md) names phase 3 in one line:
"Go end to end, with its type checker: the pilot. Its default moves to Rust
when every gate holds; its internals tests are rewritten; its Python code is
deleted. The porting rate measured here sizes the rest." This document
decides how the Rust engine indexes Go, how it uses the Go type checker,
where the line between Go and the later phases runs, how the default moves
and Go's Python code goes, lists the pull requests in order, and says what
the phase measures for phase 4.

It was written against `main` at 0.7.134 (November 2026), the release that
turned the frame on for every user (#3757), the last step of phase 2
([PHASE2_FRAME.md](PHASE2_FRAME.md)).

## Where phase 3 starts

- **The frame is on.** Since #3757 every index finds, hashes and checks its
  files, builds the Package, Folder and File rows and plans the module names
  in Rust, with Python's answers computed beside them and used on any
  difference or failure. Python's copy of those parts goes one published
  release later (#3766), not in phase 5, provided no report of a frame
  difference is open and #3740 (Windows junctions) is fixed. The graph half
  of change detection, the route of each file, the module-name plan and the
  Project node stay on their own schedules (PHASE2_FRAME.md, section 3).
- **The crate.** `cgr-graph-rs` 0.1.17: 23,513 lines of Rust, of which the
  engine (`src/engine/`) is 9,158 and the grammars (`src/grammars/`) 434.
  Go's grammar is compiled in (`tree-sitter-go = "=0.25.0"`, the release
  `uv.lock` holds for the Python package), next to the patched C and C++
  forks. The merge's file summary (`src/engine/merge.rs`) already has its
  definitions, parents, imports and type-location sections, typed
  `Unfilled` until a language fills them. The tree walks the bridge runs on
  py-tree-sitter's trees (`anchor_hash`, `ast_fingerprint`,
  `interface_fingerprint` in `src/trees/`) take a `tree_sitter::Node`, so
  the Go engine calls them on its own trees.
- **The selection.** `rust_engine_languages()` still returns nothing, and
  `INDEX_ENGINE_DEFAULTS` puts Go on Python. The `both` cells compare the
  frame on every index and would compare two whole indexes of each test's
  repository the moment a language is listed; the `rust` cells already run
  the frame with the strict check on Windows and macOS.
- **Go on Python today**, measured on this commit:

  | Code | Lines | Functions | Role |
  |---|---:|---:|---|
  | `parsers/go/` (seven modules) | 3,869 | 215 | Build constraints (`builds.py`, 618), method sets (`interfaces.py`, 699), the interface link (`interface_ingest.py`, 1,066), type inference (`type_inference.py`, 630), syntax helpers (`utils.py`, 613), module paths (`module_paths.py`, 187), the link's shared state (`link_state.py`, 31) |
  | `parsers/go_frontend/frontend.py` and `frontends/go.py` | 834 | 33 | Builds, runs and reads the Go type checker |
  | `parsers/go_frontend/gotypes/main.go` | 562 | 19 | The type checker itself, in Go (`go/packages`, `go/types`) |
  | Go's functions in the shared processors | 2,515 | 117 | Calls (`call_processor.py` 905, `call_resolver.py` 440), the updater's Go steps (`graph_updater.py` 320), type inference (`type_inference.py` 278), definitions (`function_ingest.py` 172, `definition_processor.py`, `class_ingest/`), the standard-library reader (`stdlib_extractor.py` 148), imports (`import_processor.py` 50), fields and parameters (70), helpers (`utils.py` 63, `type_facts.py`, `positional_params.py`) |
  | Go's functions in the I/O group's passes | 1,099 | 54 | RPC exposure (`rpc_exposure.py`, 598), call-registered routes (`endpoint_routes.py`, 331), I/O access (`io_access/processor.py`, 161), flows (9) |
  | Go's functions in the edit tools | 191 | 11 | `editing/rename.py` (160), `editing/import_paths.py`, `editing/imports.py` |
  | Go's constants (`constants/ast_go.py` and others) | 185+ | | Node kinds, field names, predeclared types, the built-in interfaces' methods |

  "Go's functions in the shared processors" counts the functions named for
  Go. Go files also run the shared code every language runs: function
  ingest, the generic call resolution, imports, docstrings, fingerprints.
  How much of it a Go index reaches is measured in section 7.
- **Go's tests.** 182 test files hold Go source, a `go.mod` or
  `SupportedLanguage.GO`; they define 3,702 test functions, many of them
  about other languages too. 594 of them write Go source in their own body,
  and 36 files are named for Go (400 tests).
  About 150 test functions in 31 files reach Go's own Python state (the
  `go_*` tables of the definition processor, `_join_go_implements`,
  `GoBuilds`, the frontend's `_parse_payload` and build steps); about 210
  read the shared registries (`function_registry`, `simple_name_lookup`,
  ...) after indexing Go; the rest assert on the graph or on ingestor calls.
- **The pinned Go repository** is `google/uuid` alone: 21 Go files, one
  module, a 868-line graph and 32 recorded answers. It declares no
  interface, so nothing pinned covers implicit interfaces, overrides or
  embeds, and its only build variant is a `_js.go` file name. The reference
  graphs, the recorded answers and `scripts/handoff.py` all index with
  `GO_FRONTEND=treesitter` (`constants/reference_graphs.py`), and the
  reference-graphs job installs no Go, so the type checker's path, which is
  what users with Go installed get by default (`GO_FRONTEND=auto`), has no
  pinned check. The unit-test cells and the Go Frontend and Toolchain Tests
  jobs install Go 1.27.2 or newer.
- **The evaluations.** `evals/go_l1.py` (structure against `go/ast`),
  `evals/go_retrieval.py` (cross-file call localisation against `go/ast`)
  and `evals/go_build_variants.py` (a differential oracle against `go/types`
  run once per build) exist and are type-checked in CI, but not run.
- **Time** (section 8): a fresh index of `golang/tools` (1,975 Go files)
  takes 87 s, 80% of it in the call pass, and a sync after a one-line edit
  97 s.
- **A bug being fixed.** #3744: with Go installed, a named pipe inside a
  local `replace` target outside the repository, or one reached through a
  linked folder, still blocks the type checker until its 15-minute timeout.
  Its fix reads each anchor's `replace` directives with `go mod edit -json`
  and adds the special files under those targets to the overlay. The Rust
  side copies the fixed behaviour.

## The decisions

| Question | Decision | Instead of |
|---|---|---|
| Where Go ends in phase 3 | Everything that writes Go's definitions, imports, types, interfaces, overrides and calls, in every capture group but `io` (section 1) | Porting the I/O group's frameworks for one language, which is phase 5's work |
| The Go type checker | The Go helper stays, unchanged; Rust builds, runs and reads it with today's protocol, cache, timeouts and overlay (section 2) | Python driving it, or a rewrite of `go/types` |
| When it runs | Beside the extraction, one module anchor at a time (section 2) | Before the definitions pass, as today |
| The index | Rust's merge holds Go's index and hands it back into Python's registries after stage 6 (section 3) | Python's registries as the source |
| The writer | Rust builds the rows; today's ingestor writes them, at the stage points Python wrote them (section 3) | Rust writing to the store |
| Incremental runs | Rust re-reads every walked Go file on every run; nothing of Go is read back from the graph (section 4) | Porting the rehydration of Go's tables from the graph |
| Module names | Rust's plan names every module from the step Go moves (section 4) | Python's naming until phase 4 |
| The I/O group meanwhile | Python's I/O, flow, RPC and route passes keep running on Go files until phase 5, reading the Go trees with py-tree-sitter and Go's facts from Rust (section 5) | Keeping Go's Python analysis alive for them |
| A production shadow | None. One release with Go on Rust by default and `go=python` as the way back, then the deletion (section 6) | Both engines in production, as the frame had |
| Internals tests | Pinned to Python with the `index_engine` marker, then rewritten as graph-level tests run on both engines or as Rust tests (section 6) | Kept as they are, or deleted |
| A second pinned Go repository | `spf13/afero`, recorded on Python first (section 6) | uuid alone, which has no interface |

## 1. Where Go ends in phase 3

The plan gives phase 5 "cross-language linking (endpoints, routes, RPC,
imports, trace edges), data flow and I/O" for every language. Go has code
in each of those, and in the edit tools, which are phase 7's. So "Go end to
end" means Go's language analysis: what the default capture groups
(`structure`, `calls`, `types`, `imports`) and the definition extras
(`parameters`, `fields`, `constants`, `enum_variants`) hold for a Go file.
The `io` group (`READS_FROM`, `WRITES_TO`, `FLOWS_TO`, `EXPOSES`,
`RESOLVES_TO`, `Resource` nodes and the call-registered routes) is off by
default; its passes are shared frameworks of about 13,600 lines with a
table per language, and they move together in phase 5.

| Python today | Phase 3 | Until |
|---|---|---|
| `parsers/go/` | Ported, then deleted | |
| `parsers/go_frontend/frontend.py`, `frontends/go.py` | Ported, then deleted | |
| `parsers/go_frontend/gotypes/` (the helper's Go source) | Kept, unchanged, as package data; Rust builds it from there | Phase 9 embeds it in the program |
| Go's functions in the shared processors and the updater | Ported, then deleted | |
| The shared code Go files run (function ingest, call resolution, imports, docstrings) | Ported for Go; Python's stays for the other languages | Each language's move in phase 4 |
| Go's constants | Moved to Rust, checked against the compiled grammar | |
| Go's tables in the I/O group's passes | Kept; they read Go's facts from Rust (section 5) | Phase 5 |
| The two Go type helpers `rpc_exposure.py` calls (`callee_segments`, `infer_value_type`) and `type_identifier_text` | Moved into `rpc_exposure.py` | Phase 5 |
| What `editing/rename.py` calls: `GoBuilds.apart`, `read_package_clause`, `package_key`, `declaration_scope` | The first two called through the extension, from the Rust port; the two string helpers moved into `rename.py` | Phase 7 |
| `go.mod` as a dependency manifest (the `manifest` route) | Unchanged | Phase 5, with the other manifests |

So every module under `parsers/go/` goes, and no Go analysis is left in
Python once phase 3 ends.

## 2. The Go type checker

`go/types` exists only in Go, so the helper stays a Go program and the Rust
engine drives it exactly as `frontend.py` does today. Nothing a user sees
changes, except that the helper's time now overlaps the extraction.

### What stays as it is

- **The helper.** `gotypes/main.go`, `go.mod` and `go.sum`, unchanged, in
  `codebase_rag/parsers/go_frontend/gotypes/`. Python hands the engine its
  path; the Go Frontend job keeps building it with `go build ./...`.
- **The protocol.** `gotypes <module root>`, with `CGR_IGNORE_DIRS` (the
  ignored directory names minus the tracked-source rescue's, sorted and
  comma-joined), `CGR_GO_OVERLAY` when there is an overlay, `GOTOOLCHAIN=local`
  and `CGO_ENABLED=0` over the caller's environment. The answer is the last
  non-blank line of standard output, one JSON object: `calls`, `externals`,
  `implements`, `checked`, `compared`. A non-zero exit, no output, a line
  that is not JSON, JSON that is not an object, or an entry with a missing
  key or a value `int()` refuses makes the run a failure, never empty facts
  (#1462). The payload is read with the crate's own copy of `json.loads`
  (`src/engine/state/json.rs`), so it accepts and refuses exactly what
  Python does, and a position is converted as `int()` converts it.
- **The build.** In `$CGR_HOME/go_gotypes-<the helper's go directive>/`:
  sources copied to `src/`, `go build -o out/gotypes .` under the host's
  own `GOOS` and `GOARCH` (a `GOOS` set for the analysis must not build a
  binary this host cannot run), `GOTOOLCHAIN=local`, `CGO_ENABLED=0`. A
  binary newer than the newest of the three sources is reused. `go env
  GOVERSION` is checked against the floor before reuse and before a build.
  A failed build writes `.build-failed` (`{"go_version", "failed_at"}`) and
  is not retried with the same Go for `GO_FRONTEND_BUILD_FAILURE_TTL_S` (an
  hour) or until the sources change; a timed-out build writes nothing. The
  build runs under the same `mkdir` lock, `.build-lock`, 600 tries 0.5 s
  apart (`parsers/build_lock.py`), so a Python cgr and a Rust one sharing a
  `CGR_HOME` never build at once.
- **Timeouts.** 30 s for `go env GOVERSION`, 300 s for the build, 900 s for
  each helper run, and #3744's limit for `go mod edit -json`. A timeout is a
  failure of that step: a build degrades the whole frontend to tree-sitter,
  a run degrades its anchor only.
- **The anchors.** One run per module root (each `go.mod` the module-path
  discovery keeps, by its own `rglob` walk, not the indexer's), in sorted
  order, each prefixed with its path from the repository root. A failed
  anchor is recorded as degraded and its module falls back to the
  heuristics; the others keep their facts. An external call site whose
  callee's module is one of the repository's own, loaded from that module's
  directory here, is dropped, so it resolves as first-party (#2809).
- **The overlay.** Every pipe, socket and device under the repository, in
  every folder including those cgr ignores, and every link to one, by
  absolute path; with #3744, also those under each local `replace` target
  outside the repository or reached through a link, spelled as Go joins
  them, the listing bounded and never following links. One overlay file,
  `{"Replace": {path: ""}}` in a temporary directory, serves every anchor
  of the run, and none is written when there is nothing to hide.
- **Without Go.** `GO_FRONTEND=treesitter` runs nothing. `auto` with no `go`
  on `PATH` logs at info and indexes with tree-sitter alone; an explicit
  `gotypes` with no `go` logs a warning and does the same. No `go.mod`
  outside the ignored folders runs nothing, silently. The parser fingerprint
  keeps recording the resolved mode, so installing Go re-parses.
- **What the facts are for.** Call sites keyed by (file, line, byte column
  of the name token, name) give the exact first-party callee; external
  sites suppress the name-trie fallback; implements pairs join both ends by
  declaring position; `analysed`, `checked` and `compared` decide where the
  name-and-arity match may still guess (section 3, stage 6).

### What changes

- **Process model.** Today the helper runs before the definitions pass and
  the index waits for it. The engine starts it on a thread of its own before
  the Go files are parsed, and joins its facts at the merge: the target
  design's "external analysers run beside step 1". It still starts only
  once the in-sync check has found work, as today. The anchors still run
  one at a time: each `go/packages` load uses every core
  for its own type checking, and loading several modules with their
  dependencies at once can take gigabytes. A child process is spawned with
  piped output read on two threads, so a full pipe never stalls it, and is
  killed at its deadline.
- **Logging.** The engine returns each event (the build failing, the floor,
  an anchor degraded, a payload refused, the counts) as a log key and its
  fields; Python logs them with today's templates in `logs.py`, at today's
  levels. The tests that read those messages keep reading them.

## 3. The Rust Go engine, stage by stage

The engine runs Go's part of stages 2 to 8 of `GraphUpdater.run`
(PHASE1_SEAMS.md, section 3); the other languages run through Python's
passes as today. Once `go` resolves to `rust`, every file `_route` gives
the tree-sitter tier and Go goes to the engine instead of
`_process_single_file`; `_route` stays the one place that decides.

| Stage | Python today | The Rust engine | Hands to the next |
|---|---|---|---|
| 2. Type checker | `_run_go_frontend` | The helper, started on its own thread (section 2) | Facts, joined at stage 5 |
| 3. Change detection, Go's part | `_go_package_siblings` | Same-folder rule (section 4) | The Go files to re-parse |
| 4. Parse and extract | `definition_processor.process_file`, `function_ingest`, `class_ingest`, `import_processor._parse_go_imports`, `GoBuilds.constraint` | Every walked Go file, on every core, into a summary | One `FileSummary` per file |
| 4. Merge | Registration as pass 2 walks | The walk-order fold (`merge.rs`) | Go's project index |
| 5. Join | `_join_go_implements` | Proven pairs joined by position | Proven pairs, IMPLEMENTS rows |
| 6. Deferred definitions | `resolve_deferred_go_methods`, `link_go_interfaces` | Receiver methods placed, the interface link | The complete index; Method, INHERITS, IMPLEMENTS, OVERRIDES rows |
| 6. Hand-back | (registries filled in place) | Go's index written into Python's registries | What Python's later passes and `index.txt` read |
| 7. Calls | `process_calls_in_file` for Go | Every re-parsed Go file, on every core, against the read-only index | CALLS, INSTANTIATES, REFERENCES rows; deferred IMPORTS; each caller's facts for section 5 |
| 8. After calls | IMPORTS flush, unresolved references | The same, for Go | Rows, Module properties |

### Stage 4: parsing and extraction

- **Parsing.** One `tree_sitter::Parser` per worker (`map_init`, PHASE2
  section 8), the compiled Go grammar, each file read once. A tree is
  dropped once its file's summary is made and parsed again in stage 7:
  parsing every Go file of `golang/tools` takes 1.1 s on one core, 1.3% of
  its index, and holding 2,000 trees costs more memory than parsing them
  twice costs time.
- **What a summary holds**, mapped to the Python that writes it now:
  - the package clause (`go_package` on the Module node, the
    `go_package_names` table);
  - each function, method and type with every property the graph holds for
    it today: name, positions, `name_start_*`, docstring, `is_exported`,
    `modifiers`, `decorators` (empty for Go), `param_types`,
    `positional_params`, `return_type`, `first_return_type`,
    `chain_return_type`, `field_types`, `anchor_hash`, `ast_fingerprint`,
    `ast_fingerprint_nodes` and `ast_branch_fingerprints`, from the existing
    Rust walks;
  - struct types as Class, interfaces as Interface with `embeds`, other
    defined types and aliases as Type, as `class_ingest/node_type.py` and
    `_ingest_go_interface_methods` label them;
  - each interface's method specs, as Method nodes and as signatures
    (`GoMethodSpec`: parameter and result types, package, file);
  - receiver methods, with their receiver type's name: a receiver type may
    be declared in another file of the package, so the method is placed at
    stage 6, as `_defer_go_receiver_method` defers it;
  - imports (`_parse_go_import_spec`): local name, import path, and the
    module path it maps to through `resolve_go_import_path`, longest module
    prefix first, preferring a directory that holds the package (#941);
  - the file's build constraint (`builds.py`): its name's `_GOOS`,
    `_GOARCH` or `_GOOS_GOARCH` suffix and its `//go:build` line, or its
    `// +build` lines, read from the first 64 KiB as `go/build` reads them;
  - parameters, fields and constants when their groups are on
    (`parameter_nodes.py`, `field_nodes.py`);
  - type locations keyed by (file, line, column), which the helper's facts
    join against.
- **Constants.** Go's node kinds and field names become Rust constants. A
  test asks the compiled grammar for each (`Language::id_for_node_kind`,
  `field_id_for_name`), so a name the grammar lacks fails the build's tests
  instead of matching nothing.

### Stage 4: the merge

The fold takes summaries in walk order, as phase 2 built it, and fills the
sections it left `Unfilled`, with the rules PHASE2_FRAME.md section 10
lists: the first registration of a qualified name takes it plain, later
ones `@line`, then `@line_col`, across files with an `_10000·k` slot;
simple names are a union; imports are last-wins per module and local name;
a type location's first claim holds. Two orders must be kept exactly:

- Pass-2 registrations of every file come first, in walk order, then the
  deferred receiver methods, in the order their files were walked: that is
  when Python registers them, and the registration order decides which
  duplicate gets the plain name.
- Go's own tables (package names, method specs, interface declarations,
  return types) are keyed by qualified name and filled in the same order,
  so a later declaration replaces an earlier one where Python's dicts do.

### Stages 5 and 6: the type checker's facts and the interface link

- **Proven pairs** (`_join_go_implements`): each pair's two declaring
  positions are looked up in the type locations; a miss on either end drops
  the pair. They replace the previous run's proven pairs.
- **Receiver methods** (`resolve_deferred_go_methods`): the container is
  the receiver type declared in the same package, as `package_key` and
  `declaration_scope` spell it (a package is its directory and its clause,
  and a `_test.go` file's declarations are seen from those files), chosen
  among build variants with `GoBuilds.variants`; the label is the
  container's, Class when it is unknown.
- **The link** (`link_go_interfaces`), step for step: the predeclared names
  each package declares; each interface's embeds resolved, one meaning per
  build where a name is declared per build; the method sets; the variants a
  type's other builds declare; full method sets per set of builds, capped at
  64 alternatives and 4,096 constraint nodes; then, outside the modules the
  helper analysed and the files it checked, the implicit implementations by
  method name, parameter and result counts and predeclared types; the
  OVERRIDES of each implementer's methods; the links between declarations
  one interface's method set joins (a union-find, so each set is joined as
  a tree); the builds each pair holds in; and `implementers_unknown` on
  every interface, so a sync clears a mark that no longer holds.
- **Build constraints** (`builds.py`): the expression parser with its
  limits (depth 100, 1,000 tokens), `all_of`, `any_of` and `negate` with
  their simplifications, and `decide`, which enumerates every known GOOS and
  GOARCH and at most 10 free tags, and answers unknown past that. Its
  answers are memoised per set of constraints, as `GoBuilds` memoises them.
  The port is held to `builds.py` by a differential test over a generated
  corpus of constraints and file names, while `builds.py` exists, and after
  that by the cases it collected, as Rust tests.

The merged index is then written into Python's registries
(`function_registry`, `simple_name_lookup`, `module_qn_to_file_path`,
`import_mapping`, `class_inheritance`, `function_locations`,
`interface_implementers`, `go_package_names`, the Go return types). Python's
remaining passes read them: the I/O group's (section 5), the overrides
pass, the endpoints, `known_module_paths`, `index.txt`, and the tests that
read a registry. The registries are a copy; Rust's
index is the one stage 7 resolves against. Writing 16,615 functions into
them costs milliseconds.

### Stage 7: calls

Each re-parsed Go file is parsed again on a worker and walked as
`process_calls_in_file` walks it, against the read-only index, each worker
with its own caches (the plan's target design, step 4):

- **Type inference** (`go/type_inference.py`, `parsers/type_inference.py`'s
  Go functions): parameter and receiver types, `var` and `:=` bindings,
  range clauses, call results, composite literals, span bindings for
  shadowed and redeclared names, return types across packages and files.
- **Resolution** (`call_processor.py`'s and `call_resolver.py`'s Go
  functions, and the shared path they fall back to): the helper's exact
  call site first, or its external site, which suppresses the fallback; then
  same-package functions with build variants, imported packages, value and
  pointer receivers, embedded promotion, dispatch through an interface to
  its implementers in the builds that hold each pair, the build-variant
  siblings of a callee, function values as REFERENCES, composite literals as
  INSTANTIATES.
- **Edge properties.** `line`, `col`, `end_line`, `end_col`, `arg_count`,
  `kwarg_names`, `spread_args` and `resolution`, as the site recorder writes
  them (#1522).
- **After calls.** IMPORTS edges are emitted once resolution has run, as
  `flush_deferred_import_edges` does; each Go Module's
  `unresolved_references` is written as `_write_unresolved_references`
  writes it.

### The rows

Rust builds node and relationship rows; the ingestor writes them through
the `FilteringIngestor` and the capture selection, as in phase 2. Python
replays Go's rows at the stage where it wrote them: a file's definition rows
when stage 4's walk-order loop reaches that file, the join's after stage 4,
the link's at stage 6, the calls' at stage 7. The last write of a repeated
row still wins in the same order, and tests that read ingestor calls see
them where they saw them. The stream need not repeat Python's repeated or
partial emissions (PHASE1_SEAMS.md, section 2): the store is what is
compared. Where a test counts calls, it is rewritten to read the graph
(section 6).

Going through the ingestor, the rows reach the embedded store, Memgraph,
Neo4j and the protobuf index alike.

A file whose extraction or resolution fails, by an error or by a panic the
engine catches per file, is reported as Python reports a failing file now
(the sync summary's failed files, #3173), and the run goes on: the plan's
invariant that one file does not sink a run. Every walk keeps a stack of
its own, as the Python walks were made to, so deep nesting cannot overflow
a worker's stack.

If the profile of a Rust index shows the row objects matter (PHASE2_FRAME.md
section 9 measured about 10 µs a row in Python against 23 µs in the store),
the engine hands encoded batches straight to the crate's own writer when the
store is the embedded one. That is a later step, not a gate.

## 4. Change detection and incremental runs

PHASE2_FRAME.md section 3 left the graph half of change detection in
Python, with "each language's dependents" moving with it.

- **Go's same-folder rule** moves to the engine: a Go file that is re-parsed
  or deleted re-parses every walked Go file beside it, since a method's
  receiver type may live in a sibling (#2483). The engine returns them as
  `reparse` lines with reason `dependent`, in the file plan, where
  `_go_package_siblings` adds them now.
- **The shared rules stay in Python** until phase 5 moves incremental sync:
  the callers holding edges into re-parsed files, found from the graph, the
  subtree deletion, the capture and restore of inbound edges, the sync time
  and the unfinished-run markers. Go's one rule inside them, that IMPLEMENTS
  and OVERRIDES edges out of Go files are recomputed every run and never
  restored (`_relinked_go_edge`), becomes a table entry.
- **No read-back.** On an incremental run Python reads Go's tables back
  from the graph for the files it does not re-parse
  (`_rehydrate_go_return_types`, `_rehydrate_go_method_sets`,
  `_rehydrate_go_package_clause`, `_rehydrate_go_type_locations`,
  `_prune_go_return_types`, 162 lines). The engine instead parses and
  extracts every walked Go file on every run and emits rows only for the
  re-parsed ones. Go's index on a sync is then the fresh index's, by
  construction: the plan's invariant 8 holds for it without a rehydration
  to keep right. It costs one parse and extraction of the Go files, about a
  second for `golang/tools` on 4 cores (section 8). If re-reading measures
  far slower than that on a large repository, the engine keeps each file's
  summary beside the hash cache, keyed by its md5, and published with it at
  stage 11.
- **The watcher.** `reingest` keeps its shared machinery in Python; for Go
  files it calls the same engine entry with the changed set, where today it
  calls `_reingest_go_package_files`, `_run_go_frontend` and
  `_join_go_implements`.
- **Module names.** Phase 2 computes the module-name plan in Rust on every
  index and compares it with Python's map. From the step Go moves, Rust's
  plan is the one used, for every language: Go's modules are named by
  Rust's merge, and a claim crosses languages (a same-stem file of another
  family makes both keep their extensions, #2586), so one plan for every
  file leaves no two answers to agree. Python's naming stays beside it as
  the frame's other answers do, and goes with them.

## 5. What the I/O group reads from Go until phase 5

With the `io` group on, `process_calls_in_file` runs three walks for each
caller beside its call resolution: I/O access, RPC exposure and flows, and
`_emit_route_call_endpoints` reads each module's tree after the calls. For
Go they read the tree, Go's registries and three things Go's Python analysis
computes: each caller's local variable types, its positional parameter
slots, and the callee of a call site. Once Go's analysis is Rust's:

- **Trees.** Python parses the Go files these passes need with
  py-tree-sitter, using the Go grammar compiled into the crate, as the
  parser loader already does for C and C++, so both read the same trees.
  Only with the `io` group on, so a default index parses each Go file once,
  in Rust.
- **Callers.** The engine lists each Go caller with its qualified name, its
  container, its byte span and its facts (local variable types, parameter
  slots, the resolved callee of each call site, keyed as the helper keys
  them). Python finds the node by its span
  (`descendant_for_byte_range`) and calls the three walks with them.
  `FlowProcessor` asks `resolve_function_call` of the resolver; for a Go
  call it is answered from that table.
- **Registries.** The hand-back (section 3) gives them the rest.

This is the one place phase 3 adds Python, about 150 lines by estimate,
and phase 5 removes it. Each language that moves in phase 4 reuses it.

## 6. Parity, the switch, and the deletion

### The comparisons

- **The `both` cells** (Ubuntu, Python 3.13) index each test's repository
  twice more, all-Python and with the test's selection, and compare the file
  plan, the project index and the stored graph, once Go is in
  `rust_engine_languages()`. The cells install Go, so a fixture with a
  `go.mod` runs the type checker on both sides. Phase 3 adds:
  - **Go's index sections**, `index.txt` format version 2: `go_package`
    (module, clause), `go_method` (qualified name, signature, package,
    file), `go_interface` (embeds as written), `go_implements` (pair,
    `proven` or `guessed`, builds), `go_return_type`, `go_build` (file, the
    constraint in a canonical text). Python writes them from its tables,
    Rust from its index, so a difference names the stage at which they part.
  - **An incremental comparison**, as PHASE1_SEAMS.md section 6 promised for
    the first language whose change detection moves: on a test that syncs
    after an edit, both engines index, apply the edit and sync, and the
    stored graphs are compared.
- **The `rust` cells** (Windows and macOS, 3.13) run Go on Rust with every
  check strict, so Windows paths, drive letters in the overlay and the
  helper's `.exe` are exercised.
- **The pinned repositories.** `scripts/reference_graphs.py check
  --index-engine rust` already runs in the reference-graphs job; with Go
  listed it indexes uuid's Go on Rust and must give the recorded graph and
  answers. A new `scripts/handoff.py compare --engines python,go=rust`
  indexes each pinned repository holding a listed language under both
  selections, with `GO_FRONTEND=gotypes`, and compares the three files as
  `both` does; the job installs Go for it. Nothing is recorded for the
  type checker's path: both sides run live, so a new Go release that
  changes what `go/types` answers changes both alike.
- **A second pinned repository**, `spf13/afero` at its v1.15.0 release
  (`399bb34ad9fd8a252ad1d8bfaef96279b66dc774`): 59 Go files in three
  modules (two with local `replace` directives back to the root), 17
  interfaces, embeds of `io` interfaces, many implementations of one
  interface, four files with `//go:build` lines. It covers what uuid does
  not. It is recorded on Python, graph and answers, before any Go code
  moves (#3782).
- **A nightly job**, not required for merging, runs `both` over the test
  files that hold Go, with Go forced on Rust, from the first step that
  emits a Go graph (step 7), and posts the number of differing tests and
  lines; it may only fall. Gating CI holds no list of accepted
  differences (PHASE1_SEAMS.md, section 6).

### Joining `rust_engine_languages()`

`rust_engine_languages()` reads the list the crate publishes
(`cgr_graph_rs.Engine.languages()`). Go is added to it in one pull request,
once the nightly job reports no difference. From then the `rust` and `both`
cells run Go on Rust. Every test that reads Go's Python state is marked
`@pytest.mark.index_engine("go=python")` in that same pull request: it runs
on Python in every cell, and the list of marked tests is the work left.

If either Ubuntu 3.13 cell runs past 35 minutes once Go is compared, those
two cells become three shards, as PHASE1_SEAMS.md decided.

### The internals tests, rewritten

| Tests | Rewritten as | Against |
|---|---|---|
| The pure functions of `builds.py`, `interfaces.py`, `module_paths.py` and the frontend's payload reader (`test_go_build_constraints.py`, much of `test_go_implicit_interfaces.py`, `test_go_module_path_imports.py`, `test_go_frontend.py`) | Rust `#[test]`s, case for case | The ported functions; each case's input and expected answer copied from the Python test |
| Tests that read the definition processor's `go_*` tables, `go_type_locations` or the link's state | Graph-level tests: index the snippet through `GraphUpdater` and assert the IMPLEMENTS, OVERRIDES, CALLS, Method or `implementers_unknown` the graph holds | Both engines, in the `both` cells, so each also becomes a parity test |
| Tests of the helper's build and run (`test_go_frontend_toolchain_floor.py`, `test_go_frontend_degradation_visible.py`, `test_gotypes_test_packages.py`, the overlay tests in `test_symlink_policy.py`) | Black-box tests of the unchanged helper binary, and tests of the driver through the engine's entry, reading the logged events and the graph | The Rust driver, with a fake `go` on `PATH` where today they patch `subprocess.run` |
| Tests that count ingestor calls where Rust's stream differs | The same assertion on the stored graph | Both engines |
| Tests that read the shared registries after indexing Go | Unchanged | The hand-back fills the registries |

Each removed test gets its entry in `removed_tests.toml` naming its
replacements. No entry uses `obsolete`: every behaviour those tests
checked still exists, in Rust.

### The gates for moving the default

Go's entry in `INDEX_ENGINE_DEFAULTS` turns to `rust` when all of these
hold:

1. The `both` cells are clean on every test that indexes Go, fresh and
   incremental, and the `rust` cells pass.
2. No test is marked `go=python`.
3. The reference graphs and recorded answers of uuid and afero are
   unchanged with `--index-engine rust`.
4. `scripts/handoff.py compare` finds no difference on the pinned Go
   repositories with the type checker on.
5. `evals/go_l1.py`, `evals/go_retrieval.py` and
   `evals/go_build_variants.py --cases 400 --seed 7` score at least as well
   on Rust as on Python, run in the Toolchain Tests job, numbers in the pull
   request.
6. The engine's output is identical on 1, 2 and every thread, at another
   path and under another hash seed.
7. No pinned repository indexes slower on Rust, fresh or after an edit, and
   `golang/tools` meets the target in section 8.
8. #3766 has landed, so the first release that writes users' Go graphs
   from Rust has one answer under every stage it uses: a Go difference
   reported then cannot be the frame's.

### No production shadow

The frame had one because it was cheap: Python's walk and hashes cost
milliseconds beside it. For Go there is no cheap shadow. Running Python's
Go analysis beside Rust's doubles exactly the part of the index phase 3
makes fast (97% of a fresh `golang/tools` index), and comparing two
analyses means two graphs, so a difference found in production could only
be reported, not fallen back from without a second full index. What a
shadow would catch, the gates already run on every pull request: every
test that indexes Go, on both engines, fresh and incremental, on three
operating systems, two pinned repositories, the type checker on and off,
and the three evaluations against `go/types` and `go/ast`. So no release
runs both. Instead, the release that moves the default keeps Go's
Python engine one release, with `CGR_INDEX_ENGINE=go=python` as the way
back, announced under breaking changes; the next step deletes it.

### The deletion

Once the default-move release is published and no report of a Go
difference is open (#3766's rule), one pull request deletes Go's Python
code (section 1) and the `go=python` markers that are left. The
`tree-sitter-go` package stays, with its exact pin, until phase 5 removes
the sixteen grammar packages together: tests use it as the grammar package
they load, and since step 9 indexing takes Go's grammar from the crate.
After the deletion the build can no longer run Go on Python: `go=python`
warns once and runs Go on Rust, as `go=rust` falls back to Python on a
build without it (PHASE1_SEAMS.md, section 1), so a `.env` that worked
keeps working.

## 7. What the port reaches

Section 1 counts the code named for Go. What a Go index runs is wider. Four
Go repositories (uuid, gin, afero and `protocolbuffers/protobuf-go`, 672 Go
files between them) were indexed under coverage, with and without the type
checker and with every capture group on, and so was a repository holding
only a README and a text file, which runs the shell, the frame, the
structure pass and the document tier. Lines of `codebase_rag` that ran for
the Go repositories and not for that one:

| Area | Lines |
|---|---:|
| Go's own modules (`parsers/go/`, `go_frontend/`, `frontends/go.py`) | 1,560 |
| The shared processors (the rest of `parsers/`, the I/O group apart) | 7,354 |
| The I/O group's passes (`io_access/`, `flow_access/`, RPC, routes) | 1,763 |
| `graph_updater.py` | 223 |
| Everything else (registries, path helpers, the parser loader) | 631 |
| Total | 11,535 |

The largest shares of the shared processors are `call_processor.py`
(1,783), `call_resolver.py` (1,335), `utils.py` (500), `function_ingest.py`
(376), `import_processor.py` (322), `interface_fingerprint.py` (281),
`class_ingest/` (431), `type_inference.py` (184), `definition_processor.py`
(182) and `type_facts.py` (173), and even `parsers/py/` (581): scope and
expression helpers the other languages' passes share.

So for every line of Go's own code a Go index runs, it runs about five of
shared code. The port is sized by both: Go's own modules are ported whole
(the tests reach paths these repositories do not), and of the shared
processors only the lines Go reaches are ported, for Go. These figures are
a floor: the Go tests reach further than four repositories do.

## 8. Performance

Measured on a 4-core x86_64 Linux container, Python 3.12, this commit:
`GraphUpdater.run` on the embedded store, no embeddings, the default
capture groups, the Go module and build caches warm (a user's first run
also builds the helper and fetches modules). Fresh indexes are the median
of three; the sync is one edit appended to one file, then a sync with
nothing changed.

| | uuid | gin | golang/tools |
|---|---:|---:|---:|
| Go files, bytes | 21, 72 KB | 99, 705 KB | 1,975, 12.3 MB |
| Fresh, tree-sitter only | 0.42 s | 4.78 s | 87.4 s |
| Fresh, with the type checker | 0.99 s | 6.19 s | 81.8 s |
| of which the helper | 0.58 s | 1.99 s | 4.5 s |
| Pass 2, definitions | 0.16 s | 0.92 s | 11.5 s |
| Stage 6, deferred definitions and the link | 0.01 s | 0.20 s | 3.5 s |
| Pass 3, calls | 0.21 s | 3.54 s | 70.1 s |
| Sync after one edit, tree-sitter only | 0.55 s | 5.61 s | 97.3 s |
| Sync after one edit, with the type checker | not run | not run | 92.3 s |
| Sync with nothing changed | 0.07 s | 0.08 s | 0.20 s |
| Parsing every Go file, one core | 0.007 s | 0.078 s | 1.12 s |

`golang/tools` is commit `9b1bd87ef4909f3875059da88ea872e3469d49e7` (five
modules, 16,615 functions and methods); gin is
`0f09c3a9b4626d9fe9979cf4d0521d6ae32ab646`. On `golang/tools` the helper
analysed all five modules and returned 30,449 exact call sites, 34,734
external ones and 531 proven pairs; with them pass 3 takes 59 s instead of
70 s, because a proven site skips the heuristics.

What the numbers say:

- **Go's Python analysis is the index.** Passes 2 and 3 and stage 6 are 97%
  of a fresh `golang/tools` index. Parsing is 1.3% of it.
- **A one-line edit costs more than a fresh index.** Editing
  `go/packages/packages.go` re-parses the package's files and every file
  holding an edge into them, reads Go's tables back from the graph, and
  resolves their calls again: 97 s, against 87 s from scratch. On gin, 5.6 s
  against 4.8 s. Of a second, instrumented run of that sync (99 s), 63 s
  were the call pass, 8.9 s the deletion of the re-parsed files' subtrees,
  4.6 s their definitions and 0.7 s reading registries back from the
  graph.
- **The type checker is cheap beside it.** 4.5 s on `golang/tools`, 6% of
  the index; on uuid it is most of the time, 0.58 s of 0.99 s.
- **The `io` group's walks stay Python's.** With every capture group on,
  gin indexes in 7.14 s; its I/O, flow, RPC and route walks take 1.76 s of
  it, and they stay on Python until phase 5 (section 5), so with `io` on
  they bound what phase 3 gains. (The same run on `golang/tools` was
  stopped for lack of memory in this container, which other work shared;
  step 9 measures it.)

The gates and the target:

- **Gate** (section 6): no pinned repository and no run type slower on
  Rust.
- **Target, fresh:** `golang/tools` in under 15 s on this container, with
  or without the type checker (the helper's 4.5 s runs beside the
  extraction), about 6 times faster. Parsing on four cores is about 0.3 s.
  The index writes 175,872 rows (37,934 node and 137,938 relationship
  calls), and the writer stays Python's ingestor in phase 3 at about 30 µs
  a row, 10 of them in Python (PHASE2_FRAME.md, section 9): about 5 s, the
  largest part of what is left besides the analysis in Rust.
- **Target, sync:** under 20 s after one edit of `golang/tools`, against
  97 s, about 5 times faster. Re-reading every Go file costs about a second
  on four cores; what bounds it is the shared incremental machinery that
  stays in Python until phase 5, of which deleting the re-parsed files'
  subtrees alone took 8.9 s of that sync.
- **Small repositories** keep their fixed costs: start-up, the frame and,
  with Go installed, the helper's load of the standard library's types.

## 9. CI and fuzzing

No new gating job:

- **Rust Graph Core** runs the Go engine's `cargo test`, the differential
  build-constraint corpus among them.
- **Wheels** carry no new grammar (Go's is compiled in since phase 2); the
  engine's Go code adds compile time only.
- **The unit cells.** The Ubuntu 3.13 `both` cells took 22.8 and 25.0
  minutes on the last green run of `main`; comparing Go adds two indexes of
  each test repository that holds Go files. Past 35 minutes they get a third
  shard. The Windows 3.13 shard 2/2 took 38.4 minutes against a 45-minute
  cap; the `rust` cells should get faster once Go's analysis runs in Rust.
- **Reference Graphs** installs Go for `scripts/handoff.py compare` with
  the type checker on (about a minute for uuid and afero).
- **A nightly preview job**, from step 7 to step 10, runs `both` with Go
  forced on Rust over the test files that hold Go and the pinned
  repositories, and posts the count of differing tests and lines. It is not
  required for merging; step 10 removes it, when the gating cells take over.
- **Toolchain Tests** gains the three Go evaluations from step 7, when the
  engine first writes Go's whole graph, each comparing Rust with Python:
  `evals/go_l1.py` (structure against `go/ast`), `evals/go_retrieval.py`
  (cross-file call localisation against `go/ast`) and
  `evals/go_build_variants.py` with a small case count (the only oracle for
  build variants and interfaces against `go/types` per build). Go is
  already installed there. Gate 5 runs them at full size when the default
  moves; from step 7 every pull request runs them, so no step between can
  lose what they measure.
- **Fuzzing.** Three cargo-fuzz targets join the seven in
  `rust/cgr_graph_rs/fuzz/fuzz_targets/` and the Rust job's budget:
  `go_extract` (any bytes as a Go file: no panic, the same summary on one
  thread and on four), `go_build_constraint` (any file head: no panic,
  bounded time, and a constraint read back from its canonical text is the
  same constraint), and `gotypes_payload` (any bytes as the helper's
  output: facts or a refusal, never a panic or partial facts). Each gets a
  row in `CONTRIBUTING.md`'s table and a seed corpus from the Go tests'
  snippets.
- **Toolchains.** The engine's Go code needs no new crate: processes, pipes
  and timeouts from the standard library, JSON from the crate's own reader.
  `RUST_TOOLCHAIN` (1.97) does not move.

## 10. The steps

Each is one pull request, unless it names several. Each keeps the whole of
CI green, and none removes, skips or marks a test as expected to fail,
except the rewritten internals tests of step 11, each with its entry in
`removed_tests.toml`. Until step 10 nothing a user or a gating cell runs
indexes Go on Rust: the engine runs only when a preview set,
`index_engine.PREVIEW`, names Go. Like `engine_frame.ENABLED`, it is a
module flag, not a setting: the plugin's `--engine-preview go` and the
scripts' `--preview go` set it.

Every step that changes `rust/cgr_graph_rs/` bumps the crate's version
(`rust/cgr_graph_rs/pyproject.toml` and `Cargo.toml`) and the
`cgr-graph-rs` floor in `pyproject.toml`, as CI requires: steps 3 to 10,
one patch release each, from 0.1.18.

0. **Python first.**
   - #3744's fix, under way.
   - `spf13/afero` pinned: its entry in `repos.json`, its graph and its
     recorded answers, recorded on Python. *Worked when:* the
     reference-graphs job passes three runs alike, with no other fixture
     changed.
   - Throughout the phase, a difference the comparison traces to a Python
     bug is fixed in Python first, in its own pull request, with a test that
     fails first, before the port copies the fixed behaviour.
1. **This document.**
2. **Go's index sections and the engine comparison.**
   - Adds: `index.txt` format version 2 with the `go_*` sections, written
     by Python from its tables; `scripts/handoff.py compare --engines A,B`;
     Go installed in the reference-graphs job for it; the `--engine-preview`
     option and `index_engine.PREVIEW`.
   - Tests: the new sections on fixtures (a package across two files, an
     interface with embeds, a build-variant pair); the same bytes at another
     path under another seed; `compare` finds Python equal to itself and
     reports a planted difference.
   - Worked when: the hand-off check passes on uuid and afero, and the
     reference graphs are unchanged.
3. **The Go engine's skeleton.**
   - Adds: `src/engine/go/` with parsing on the pool, Go's node kinds and
     field names checked against the compiled grammar, the build-constraint
     reader and `decide`, the `go_build_constraint` fuzz target, and the
     two calls `editing/rename.py` will make (whether files build apart, a
     file's package clause).
   - Tests: the differential corpus against `builds.py` (file names,
     `//go:build` and `// +build` heads, block comments, truncated heads,
     the depth, token and size limits); the same answers on 1, 2 and every
     thread.
4. **Definitions and the merge.**
   - Adds: extraction (section 3, stage 4), the fold's sections, the
     receiver methods placed at stage 6, Rust's `index.txt`.
   - Tests: a preview run over every test file that holds Go shows no
     `definition`, `simple_name`, `module`, `imports`, `inherits`,
     `go_package`, `go_method`, `go_return_type` or `go_build` difference;
     the same on uuid and afero through `compare`.
5. **The type checker's driver.**
   - Adds: the build, the runs, the overlay with #3744's targets, the
     payload reader, the events; the `gotypes_payload` fuzz target.
   - Tests: on every repository the Go frontend tests build, and on uuid
     and afero, Rust's driver and `frontend.py` give the same facts; the
     build-failure marker, the floor, a timeout and a crash give the same
     events as today's warnings; the Go Frontend job runs them.
   - Worked when: also the same facts on `golang/tools`, by hand, with the
     times in the pull request.
6. **Interfaces.**
   - Adds: the proven-pair join, method sets, the link, overrides, the
     embedded-method links, `implementers_unknown`, and their rows.
   - Tests: the preview run shows no `go_interface` or `go_implements`
     difference; the link's cases from `test_go_implicit_interfaces.py` as
     Rust tests (the deep chains and diamonds included, for time).
7. **Calls.**
   - Adds: type inference, resolution, the call rows, IMPORTS and
     unresolved references, the rows replayed at their stage points: the
     engine writes Go's whole graph. The nightly preview job; the
     `go_extract` fuzz target; the three Go evaluations in Toolchain Tests.
   - Tests: uuid's and afero's graphs and answers on the preview equal the
     recorded ones; the nightly count is posted in the pull request.
8. **Incremental runs and the watcher.**
   - Adds: the same-folder rule in the engine, no read-back, the `reingest`
     entry, the relink rule as a table, and `both`'s incremental comparison.
   - Tests: every test that syncs after an edit, on the preview, compared;
     a sync of `golang/tools` after one edit timed against Python's.
9. **The I/O group's view.**
   - Adds: py-tree-sitter takes Go's grammar from the crate; the callers'
     facts; `resolve_function_call` answered from them for Go; the two RPC
     helpers moved into `rpc_exposure.py`.
   - Tests: the Go tests of the `io` group (`test_go_rpc_exposure.py`,
     `test_route_call_endpoints.py`, `test_io_handle_edges.py`,
     `test_callable_flow_tracing_go.py`) on the preview, compared.
10. **Go joins the Rust engine's languages.**
    - Adds: `Engine.languages()` naming Go and `rust_engine_languages()`
      reading it; the `rust` and `both` cells index Go on Rust, with Rust's
      module-name plan; every test that reads Go's Python state marked
      `go=python`; the nightly job removed.
    - Worked when: every cell passes; the pull request lists the marked
      tests and the cells' times, and adds the third shard if a cell passed
      35 minutes.
11. **The internals tests, rewritten**, in four pull requests: the pure
    functions as Rust tests; the frontend's tests through the driver; the
    tests of Go's tables as graph-level tests; the call-count tests on the
    graph. Each removes its markers and adds its `removed_tests.toml`
    entries. *Worked when:* no test is marked `go=python`.
12. **The default moves.**
    - Adds: Go's `INDEX_ENGINE_DEFAULTS` entry turned to `rust`, Rust's
      module-name plan in production, the breaking-change label and
      `changes/breaking/<n>.md` (what changed, `CGR_INDEX_ENGINE=go=python`
      for one release, a re-parse on the first sync, as the fingerprint
      records the engine).
    - Worked when: the gates of section 6 hold, each with its numbers in
      the pull request.
13. **The deletion.**
    - Adds: section 1's deletions; `editing/rename.py` reads build
      constraints and package clauses from the extension; `go=python` warns
      once and runs Go on Rust; `PHASE1_SEAMS.md` and this document updated
      where they described the way back.
    - Worked when: `scripts/test_removals.py check` passes, no file is left
      under `parsers/go/`, the reference graphs and answers are unchanged,
      and no test is marked `go=python`.

### Exit criteria

Phase 3 is done when all of these hold:

- Go indexes on Rust by default, fresh and incremental, with the type
  checker driven from Rust, on every platform the wheels cover.
- The gates of section 6 held when the default moved.
- No Go analysis is left in Python; what the I/O group and the edit tools
  still read of Go comes from the engine.
- Every removed test has its entry in `removed_tests.toml`.
- The porting rate (section 11) is recorded, and phase 4's order and sizes
  are set from it.

## 11. The porting rate

Phase 3 records, per step, in a table appended to this document by the
step's pull request:

- **Size:** the Python lines the step ports, by this document's tables, and
  the Rust lines it adds, code and tests apart.
- **Time:** from the step's first commit to its merge, and the review
  rounds it took to reach 5/5.
- **Differences found:** each difference the preview, `both` or `compare`
  found, filed as an issue and classed as a Python bug (fixed in Python
  first), a port bug, or Python behaviour that rests on an accident (an
  iteration order, a reused updater's state). The Python bugs are the
  measure of what the shadow would have caught in production.
- **Cost:** the `both` cells' times and the index times of section 8.

Phase 2 is the baseline: its eight implementation pull requests (#3735 to
#3757) merged within 53 hours and added 9,986 lines of Rust, tests included.

Phase 4 is sized from Go's rate. By one count for every language (its
own packages under `parsers/`, and the functions named for it in shared
code; shared code a language reaches without naming it is not counted, and
a class with a capitalised name is missed):

| Language | Own modules | Named in shared code | Total |
|---|---:|---:|---:|
| Go | 4,624 | 3,233 | 7,857 |
| Python | 9,474 | 5,219 | 14,693 |
| JavaScript and TypeScript | 7,174 | 6,781 | 13,955 |
| C and C++ | 5,254 | 3,905 | 9,159 |
| Rust | 2,161 | 5,953 | 8,114 |
| C# | 4,200 | 2,738 | 6,938 |
| Java | 5,757 | 951 | 6,708 |
| Dart | 1,535 | 2,058 | 3,593 |
| PHP | 1,560 | 1,065 | 2,625 |
| Scala | 608 | 827 | 1,435 |
| Lua | 920 | 495 | 1,415 |
| Julia | 269 | 604 | 873 |
| SQL | 0 | 292 | 292 |

Go is about a tenth of the language-specific code (7,857 of 77,657 lines).
Each later language reuses what Go ported of the shared path (function
ingest, the generic resolution, imports, the rows, the I/O group's view),
so its time is its own lines at Go's rate plus what of the shared path it
reaches that Go did not, measured for it as section 7 measured Go's.
Phase 4's order is set when phase 3 ends: the plan's order, unless the
measured rate shows a language whose shared path Go did not reach
(Python's scopes and decorators, JS and TS's module systems) is better
moved earlier or alone.

## 12. Risks and open questions

- **The shared path is larger than Go's own code.** Most of what a Go index
  runs is shared code (section 7), with branches for other languages
  interleaved. *Recommendation:* port only what Go reaches, case by case
  from the Go tests, and let the preview's difference count say when it is
  done; do not port a shared function's other languages ahead of them.
- **The order of the rows.** Tests that read ingestor calls see Go's rows
  where Python wrote them, but not always as many. *Recommendation:* replay
  at Python's stage points (section 3) and rewrite the few tests that count
  calls to read the graph; never copy Python's repeated emissions to
  satisfy a count.
- **A Go release changes what `go/types` says.** Both engines run the same
  helper, so they change alike, and nothing of it is recorded.
  *Recommendation:* keep the type checker's path out of the recorded
  graphs; compare it live, as section 6 does.
- **Memory on a large repository.** `go/packages` with types and syntax for
  a big module takes gigabytes. *Recommendation:* one anchor at a time, as
  today, beside the extraction.
- **One release of a way back.** After the deletion, a Go regression has no
  `go=python` to fall back to. *Recommendation:* accept it; the gates are
  the defence, a fix lands in Rust, and a release follows every merge.
- **Re-reading every Go file on a sync.** About a second on `golang/tools`;
  more on a far larger repository or a slow disk. *Recommendation:* measure
  it in step 8; add the summary cache of section 4 only if a sync is slower
  than Python's.
- **The I/O group's view is temporary code**, which phase 5 removes.
  *Recommendation:* keep it; the alternatives are porting the I/O
  frameworks for one language or keeping Go's Python analysis alive, both
  larger.
- **Rust's module-name plan for every language.** It is compared on every
  index today and has agreed; on an incremental run Python's map is seeded
  from the graph, Rust's is the fresh answer. *Recommendation:* turn it on
  with Go (step 10 in CI, step 12 for users); a difference the incremental
  comparison finds is an incremental-against-fresh bug, fixed in Python
  first.
- **Windows.** The helper's `.exe`, drive letters in the overlay, `\\?\`
  prefixes. *Recommendation:* the `rust` cells on Windows run the type
  checker on every Go test with a `go.mod`; nothing moves until they pass.

Open questions, each with a recommendation:

1. **Is afero the right second repository?** It adds interfaces, embeds,
   build constraints and three modules for 59 files. *Recommendation:*
   yes; add `gin-gonic/gin` (99 files) only if the reference-graphs job has
   time to spare, for size.
2. **Should the default move wait for #3766?** *Recommendation:* yes (gate
   8): a release that changes both the frame's fallback and Go's engine at
   once makes a report hard to place, and #3766 is due first anyway.
3. **Run the type checker's anchors in parallel?** *Recommendation:* no,
   until a profile shows the helper, not the extraction, is what users wait
   for; then at most two at once.
4. **Keep the summaries between runs?** *Recommendation:* not in phase 3
   (section 4), unless step 8's numbers ask for it.
5. **Hand rows straight to the crate's writer?** The Python side of the
   writer is about 1.8 s of `golang/tools`' 175,872 rows. *Recommendation:*
   only if a Rust index of `golang/tools` misses the target of section 8
   and that is where the time goes (PHASE2_FRAME.md, section 9).
