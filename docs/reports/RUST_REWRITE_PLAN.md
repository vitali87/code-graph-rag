# Rust Rewrite: Plan

code-graph-rag moves to one Rust program that gives users the same
functionality and the same interface as today, and needs no Python to run.
This document records why, what "the same interface" means, the state the
work starts from, how the test suite stays green at every step, and the
order of the work.

It supersedes [INDEXER_RUST_PLAN.md](INDEXER_RUST_PLAN.md), which moved the
indexer's per-node tree walks to Rust one at a time and kept call
resolution, the shared registries and the passes' control flow in Python.
Its four landed stages stay; what it kept in Python moves too.

All measurements below were taken on a 4-core x86_64 Linux container with
Python 3.12, on `main` between 0.6.81 and 0.7.1 (November 2026).

## The decision

- **Target:** one Rust program. Every command, option, exit code, MCP tool,
  setting, graph label and file format users rely on today keeps working the
  same way.
- **Delivery does not change:** the program ships inside the same PyPI
  package, so `uv tool install "code-graph-rag[...]"` and `pipx install`
  keep working (ruff and uv ship as Rust programs the same way), plus the
  release binaries and the Docker image.
- **Python's remaining roles:** the test harness during the migration, and
  the small Python SDK (`from cgr import load_graph`, ...), rebuilt as a thin
  package over the Rust core so its imports keep working. Nobody needs
  Python to run `cgr`.

## Why

### Where indexing time goes

Five public repositories indexed from scratch with the real pipeline
(`GraphUpdater.run(force=True)`, in-memory ingestor, the CLI's INFO log
level), sampled with `py-spy --native --idle` at 100 Hz:

| Repository | Files | Time | Parsing | Tree queries | Reading nodes from Python | Rust tree walks | Python analysis |
|---|---|---|---|---|---|---|---|
| gin (Go) | 130 | 7 s | 3.5% | 1.0% | 7.0% | 3.4% | 74% |
| gson (Java) | 317 | 46 s | 0.8% | 0.8% | 16.1% | 0.8% | 80% |
| ripgrep (Rust) | 225 | 8 s | 6.1% | 2.2% | 8.1% | 1.8% | 74% |
| zod (TypeScript) | 734 | 23 s | 3.8% | 15.1% | 10.1% | 4.9% | 62% |
| django (Python) | 6,805 | 172 s | 3.8% | 4.1% | 5.9% | 3.3% | 82% |

The rest is startup and file I/O. Roughly half of the tree-sitter time is
the C engine, which is the same code in Rust; the other half is Python
objects built for nodes, 3% to 9% of a run. Parsing is never the cost. The
cost is the analysis written in Python, and a Rust parser alone changes
nothing about it.

### The same work in Rust and in Python

Identical grammar versions, the `tree-sitter` crate 0.26.8 against
py-tree-sitter 0.26.0, best of three runs, seconds:

| | django, 2,818 `.py` | zod, 478 `.ts` | gson, 268 `.java` | gin, 99 `.go` |
|---|---|---|---|---|
| parse, Python | 1.68 | 0.35 | 0.13 | 0.10 |
| parse, Rust | 1.75 | 0.49 | 0.19 | 0.10 |
| parse and visit every node, Python (cursor) | 2.79 | 0.77 | 0.30 | 0.17 |
| parse and visit every node, Python (`children`, as the indexer walks) | 3.97 | 1.05 | 0.34 | 0.29 |
| parse and visit every node, Rust | 2.17 | 0.57 | 0.17 | 0.13 |
| parse and query, Python | 2.08 | 0.59 | 0.18 | 0.13 |
| parse and query, Rust | 2.82 | 0.71 | 0.21 | 0.14 |
| parse on 4 Python threads | 2.16 | 0.48 | 0.20 | 0.10 |
| parse and visit on 4 Python processes | 0.96 | 0.24 | 0.10 | 0.10 |
| parse and visit on 4 Rust threads | 0.64 | 0.18 | 0.05 | 0.04 |

- Parsing and queries cost the same: the engine is the same C code.
- Visiting nodes is several times cheaper in Rust.
- **Python cannot use more than one core for this in one process.**
  py-tree-sitter holds the GIL for the whole of `Parser.parse` and every
  query (`tree_sitter/binding/parser.c` in 0.26.0 and 0.26.1 releases it only
  around `print_dot_graphs`), and on a free-threaded interpreter it turns the
  GIL back on (`Py_MOD_GIL_USED` in `module.c`). Four threads are slower than
  one. Four processes gain up to 3x, but a parsed tree cannot leave the
  process that parsed it, and the call pass reads shared registries. Rust
  threads gained 3.4x on 4 cores with every tree and registry shared.

### What the moves so far show

- The indexer plan's four Rust stages and its first Python fix gained about
  1.9x compounded on one core (INDEXER_RUST_PLAN.md). Turning the bridge off
  slows zod from 20.3 s to 27.5 s.
- The largest single cost found in the profiles is not tree-sitter: 28% of
  django's run is `_import_target_is_a_namespace` copying a module's whole
  registry subtree to test it for emptiness (#2919; the fix is #3024). With an
  early exit django indexes in 122 s instead of 172 s, the same graph byte for
  byte. Fixes like this land before the port, so the port is measured against
  an honest baseline.

### Grammars

Live registry data for 77 languages:

- The 16 grammar packages cgr installs publish the same version to PyPI and
  crates.io on the same day, except Dart (PyPI 0.1.0 is a single-maintainer
  repackaging of a fork; crates.io has 0.3.0).
- 17 languages exist only as crates: Vue, Angular, Elm, Erlang, R, Protocol
  Buffers, Gleam, Clojure, LaTeX, D, Nim, Prisma, Racket, Scheme, Apex,
  COBOL, Thrift. None exists only on PyPI.
- Crates are not all interchangeable: 64 of 75 depend only on
  `tree-sitter-language` and load into any engine; 10 pin a whole engine
  (`tree-sitter-md` needs `^0.27`, `tree-sitter-angular` `~0.25`, others
  `0.20` to `0.26`). Cargo allows one engine per build, so those are built
  from their grammar sources instead.
- A grammar compiled from its crate and handed to py-tree-sitter parsed all
  883 files of Django's `django/` package to identical trees, at the same speed (1.94 s
  against 1.98 s). Rust grammars do not have to wait for the rewrite.

### What Python costs users today

On a clean install of `code-graph-rag[treesitter-full,ast-grep]`:

- 91 packages, 228 MB, and Python 3.12 or newer.
- `cgr --version`: 0.55 s warm, 1.7 s cold, nearly all of it imports.
- `cgr mcp-server` answers `initialize` after 1.4 s warm, 3.5 s cold, once
  per agent session.
- Issues filed by outside users: about 13 of 119 are install and dependency
  failures (#93, #96, #136, #146, #147, #148, #191, #279, #307, #475, ...), and
  #149 and #1786 are Python recursion limits that stopped an index.

## Starting conditions

### Code

- 228,000 lines of Python outside the tests: the indexer about 130,000
  (`parsers/` 113,000, `graph_updater.py` 11,000), the edit tools 12,700,
  the agent tools 8,300, the embedded graph's Python side 6,700, tracing
  5,500, the MCP server 4,700, the CLI about 5,000, the analyses (dead code,
  duplicates, taint, structural delta, context) about 8,000, and 15,400 of
  constants.
- `cgr-graph-rs` 0.1.8, about 19,000 lines of Rust: the embedded graph
  (SQLite store, Cypher hot paths, graph algorithms), TOML and XML readers,
  and the tree bridge. It is already required: there is no pure-Python
  fallback.
- External analysers: the Go type checker (a separate program, on by
  default), libclang for C/C++ (Python bindings, on when installed), Roslyn
  for C# and javac for Java (separate programs, opt-in), Jedi for Python (a
  Python library, opt-in).

### Tests

- 1,461 test files, about 17,900 test functions, 27,099 test cases. A full
  local run (`pytest -n 4 -m "not integration"`) took 12 minutes: 26,750
  passed, 5 failed, 344 skipped. Four failures are toolchains missing on that
  machine (Go 1.27, the .NET SDK). The fifth is a real bug that CI never sees,
  #3628: its test needs `tsc`, which no CI job installs.
- By what they exercise (passed cases): only the graph the indexer produces,
  8,215; the graph and Python internals, 5,299; indexer internals only, 2,976;
  indexer constants and types, 2,316; not the indexer, 7,944.
- About 880 test files index through `GraphUpdater`, 478 of them building it
  directly. 105 files reach into `updater.factory`, 64 call
  private methods, 18 read the function registry, 11 use fake tree nodes, 72
  test incremental reingest.
- CI: unit tests on Ubuntu, Windows and macOS with Python 3.12 and 3.13 in
  two shards each, a base-install run, Memgraph integration tests, the Rust
  core's `cargo test`, the Go frontend, wheel and binary smoke tests, and the
  ruff, ty, bandit, Sonar, CodeQL and Greptile gates.
- The per-language accuracy evaluations in `evals/` are type-checked in CI
  but not run.

### Open work

- 326 open issues (221 bugs), 94 open pull requests (mostly analysis fixes),
  8 open epics. Two epics add indexer features: the database schema tier
  (#1610) and the deployment tier (#1612).
- The graph is not yet the same for the same input: query captures are read
  in tree-sitter's unstable order (#2915; the fix, #2970, also removes a
  file-order dependence measured on zod), Go build-constrained duplicates
  (#3148), C++ out-of-line definitions (#3137), TS shadowed locals (#3332).
  Incremental and fresh indexes differ (#2403, #2911).

## The interface the Rust program must keep

| Surface | Today |
|---|---|
| Command line | 51 commands and about 150 options, with their exit codes and output formats |
| MCP server | 37 tools with 86 parameters, over stdio and HTTP |
| Settings | 108 settings read from the environment and `.env`; `.cgrignore`; workspaces |
| Graph | 26 node labels and 36 relationship types, on the embedded store, Memgraph and Neo4j |
| Files | JSON export, the protobuf index (`index`, `verify-index`, `diff-index`), the embedded graph file |
| Chat agent | `cgr start` and `cgr optimize`, with 7 providers: Ollama, Anthropic, OpenAI, Google, Azure, a LiteLLM proxy, MiniMax |
| Python SDK | `load_graph`, `MemgraphIngestor`, `CypherGenerator`, `embed_code`, `settings` |
| Install | the PyPI package and its extras, `uv tool`, `pipx`, release binaries, the Docker image |

What is tied to a Python library today, and what replaces it (crates.io
downloads, November 2026):

| Today | In Rust | Downloads |
|---|---|---|
| MCP Python SDK | `rmcp`, the official Rust SDK | 51M |
| typer | `clap` | 1.35B |
| prompt_toolkit | `reedline` | 4.3M |
| pydantic-ai and LiteLLM | `genai` or `rig` | 0.6M, 4.5M |
| torch (UniXcoder embeddings) | `ort` (ONNX Runtime) and `tokenizers` | 27M, 48M |
| libclang bindings | `clang-sys` | 366M |
| Neo4j and Memgraph drivers | `neo4rs` (Memgraph speaks Bolt) | 1.4M |
| qdrant-client | `qdrant-client` | 6.3M |
| pymilvus | `milvus-sdk-rust` (official, young) | 26K |
| ast-grep-py | `ast-grep-core` | 3.1M |
| `cgr language add-grammar` | `tree-sitter-loader` | 1.1M |
| watchdog, ignore rules | `notify`, `ignore` | 200M+ |
| Jedi | none: runs as an optional helper when a Python interpreter is present, as the Go, Java and .NET helpers run when their toolchains are | |

## Target design: every core

1. Every file, on any free core: read it, parse it once, extract a compact
   summary (definitions, imports, classes, call sites with their context),
   drop the tree. Parsing is cheap enough to parse again where a later stage
   needs a tree.
2. Merge the summaries into the project-wide indexes (function registry,
   inheritance, imports, type locations) in sorted path order.
3. External analysers run beside step 1, as separate processes, bounded by
   the core count; their facts join at step 2.
4. Resolve calls file by file on every core, against read-only indexes, each
   worker with its own caches.
5. One writer streams the results to the graph backend while resolution
   runs.
6. The same input gives the same graph for any file order, core count and
   seed. A test enforces it, on both engines.

## Keeping the tests green at every step

1. Every pull request passes the whole existing CI unchanged. No test is
   skipped, marked expected to fail, or deleted to get green.
2. `GraphUpdater` stays the front door while the engine moves. Behind it a
   per-language switch picks the Python or the Rust engine, so the 880 test
   files that index through it keep working, and a language not yet moved
   runs on Python.
3. Three CI modes from the first Rust commit: `python`, `rust` (every
   language moved so far on Rust) and `both` (both engines run on each test's
   repository and the test fails on any difference in the graph handed to the
   ingestor). `both` turns about 13,500 graph-level tests into parity tests;
   the `both_*_walks` fixtures already do this for the four Rust stages.
4. No test disappears silently. Before a language's Python code is deleted,
   each internals test is rewritten as a graph-level test or a Rust test, and
   a checked-in map names the replacement of every removed test. CI refuses a
   removal without one.
5. An interface snapshot is checked in: commands and options, exit codes, MCP
   tools and their schemas, settings, graph labels and properties, file
   formats. CI fails when the code and the snapshot differ; a change to it
   goes through the breaking-change process.
6. Black-box tests run whatever `cgr` is installed, over the command line and
   MCP, and compare with recorded answers on pinned repositories. They pass on
   the Python implementation first and keep passing as each surface moves.
   Tests that drive the CLI or MCP in-process become black-box tests before
   that surface moves.
7. Reference graphs of pinned public repositories, one or more per language:
   small ones on every pull request, django nightly. They change only on
   purpose, with the difference shown in the pull request.
8. Invariant tests: any file order, core count and seed give the same graph;
   an incremental sync equals a fresh index; a file that fails does not sink
   the run; deep nesting (#3173) and very large files (#2453) index.
9. A CI job with every optional toolchain installed (Go 1.27, .NET, Node with
   `tsc`, Java, Lua, libclang), so tests that skip on the default runners run
   somewhere.
10. Before a language's default moves to Rust, its accuracy evaluation in
    `evals/` scores at least as well as on Python.
11. Timing gates: small repositories on every pull request, django nightly.
    No regressions.
12. Rust's own: unit tests, the existing ClusterFuzzLite fuzzing extended to
    every language, wheels for every platform on each change.
13. The chat agent is tested against a scripted model, so its behaviour is
    repeatable without a live provider.

## Phases

Each phase ends when its exit criteria hold; each step inside it is its own
pull request.

0. **Groundwork, in Python.** Land #2970 and #3024. Fix or record the other
   nondeterminism (#3148, #3137, #3332) and the incremental-against-fresh
   differences (#2403, #2911), so reference graphs are stable. Add the
   all-toolchains CI job and fix #3628. Record the interface snapshot and the
   reference outputs. Decide, per language, what happens to its open pull
   requests before it moves. *Exit:* the same graph on every run, order and
   seed for the pinned repositories; snapshot and reference checks in CI.
1. **Seams.** The per-language engine switch, the `rust` and `both` CI
   modes (identical to `python` while nothing has moved), the data each stage
   hands to the next written out by the Python engine too.
2. **The Rust engine's frame**, a crate beside `cgr-graph-rs`: discovery
   with the ignore rules, change detection, the all-cores scheduler,
   tree-sitter 0.27 and the grammars, the merge, the writer behind the same
   ingestor interface. It never touches py-tree-sitter's trees, so it takes
   the current engine without waiting for the Python binding.
3. **Go end to end**, with its type checker: the pilot. Its default moves to
   Rust when every gate holds; its internals tests are rewritten; its Python
   code is deleted. The porting rate measured here sizes the rest.
4. **The other languages**, one release each: JS/TS, Python, Java, C#, C/C++
   (libclang through `clang-sys`), PHP, Scala, Lua, Dart, Julia, SQL.
   Families move together (JS with TS, C with C++).
5. **The rest of indexing**: cross-language linking (endpoints, routes, RPC,
   imports, trace edges), data flow and I/O, the Markdown tier, the ast-grep
   tier, incremental sync and the watcher. Then py-tree-sitter, the 16 grammar
   packages, the tree bridge and the pin procedure in `AGENTS.md` go.
6. **The graph engine**: the rest of the Cypher engine in Rust, and the
   Memgraph and Neo4j writers.
7. **Analyses, edit tools and trace ingestion**, behind the same Python API
   while Python is still the shell.
8. **The shell**: the command line, the MCP server, the chat agent and its
   providers, embeddings through ONNX Runtime (vectors compared with today's
   so existing vector stores stay valid; #3472).
9. **Packaging**: the Rust program in the PyPI package, the binaries, the
   Docker image, and the Python SDK as a thin package over the Rust core.

New indexer features (#1610, #1612) are built in Rust once phase 2 lands.

## Risks

- **Behaviour drift.** The Python analysis encodes years of fixes. The
  reference graphs, the `both` mode and the evaluations are the defence,
  which is why phase 0 makes the graph deterministic first.
- **Two engines at once.** Each language spends as little time as possible
  on both; while it moves, its fixes land in Rust, guarded by graph-level
  tests that run on both engines until its Python code is gone.
- **Embedding parity.** If ONNX vectors differ beyond tolerance, existing
  vector stores need a re-embed; measure before switching.
- **Milvus.** Its official Rust SDK is young; its gRPC API is the fallback.
- **The chat agent** has to behave the same across seven providers; the
  scripted-model tests pin the loop, a live smoke test per provider pins the
  clients.

## Corrections to INDEXER_RUST_PLAN.md

- Its step 5 says py-tree-sitter releases the GIL while parsing. It does not
  in the pinned 0.26.0 or in 0.26.1, and four threads parse no faster than
  one. Parallel parsing needs processes there, or Rust.
- Its rule that resolution stays in Python because it shares registries
  across files is what this plan reverses: in Rust the registries are built
  once, then read by every core.
