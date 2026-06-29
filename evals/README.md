# cgr evaluation harness

Scores the knowledge graph that `code-graph-rag` (cgr) builds against ground truth, with no Memgraph required (an in-memory capturing ingestor drives `GraphUpdater(...).run(force=True)`).

## L1 — structure (containment)

Scores cgr's definition nodes and `DEFINES`/`DEFINES_METHOD` edges against a scope-aware Python `ast` oracle.

```bash
uv run python -m evals.cli --target codebase_rag
```

Writes `evals/results/scores.csv` and `evals/results/diff.json`. Node identity join is `(kind, file, start_line)`.

## L2 — module-call attribution (ast oracle)

Scores whether cgr attributes the right calls to the *module* (caller side). A
call runs at module-load time -- and so belongs to the module -- iff it is a
top-level statement, a decorator, or a default-argument expression, i.e. it is
NOT inside a function body. The L3 execution trace cannot measure this: it
records the innermost *function* frame as the caller and drops `<module>`
frames, so module-level attribution is its structural blind spot. An `ast`
oracle fills it.

```bash
uv run python -m evals.module_calls --target codebase_rag
```

How it works:

- **Oracle** (`module_calls.oracle_module_calls`): walks each file's AST modelling
  import-time execution. A call counts when it runs at module load: top-level
  statements, list/set/dict comprehensions (eager), decorators, argument
  defaults, and -- only when the file does not `from __future__ import
  annotations` -- argument/return annotations. It does NOT count function/method
  bodies, lambda bodies, or generator expressions (deferred until called or
  consumed). Class bodies stay at module scope. It collects the simple name of
  every such call whose callee is first-party (a name defined in the target),
  excluding dunders.
- **cgr side** (`module_calls.cgr_module_calls`): every `CALLS` edge whose caller
  is a `Module` node, keyed by `(module_file, callee_simple_name)`; a constructor
  call resolved to a `Class.__init__` *method* is credited to `Class` (a bare
  first-party function named `__init__` is left as a filtered dunder).
- **Score**: precision/recall over `(module_file, callee_simple_name)` edges.

The exact-attribution guarantee is covered by `test_eval_module_calls.py`
(precision == recall == 1.0 on a controlled fixture: a top-level call, a
default-argument call, a `__main__` call, and a nested call that must NOT be
module-attributed).

On the whole `codebase_rag` target the metric is a lower bound that surfaces two
real, separate cgr gaps (not attribution errors):

- **Recall** is bounded by constructor calls to first-party classes with no
  explicit `__init__` (NamedTuple/dataclass/pydantic) -- cgr has no method node
  to point the call at, so no edge is emitted. Closing this needs constructor
  calls to target the class node (tracked with the dead-code Class work).
- **Precision** is bounded by the trie suffix-match fallback occasionally
  resolving a module-level call to an unrelated first-party name.

## L3 — CALLS recall (execution-traced)

Measures whether cgr's static `CALLS` graph contains the call edges that actually fire at runtime.

```bash
uv run python -m evals.l3
```

How it works:

- **Static side** (`cgr_graph.extract_cgr_calls`): builds cgr's graph over the target package (default `codebase_rag`) and collects every `CALLS` edge.
- **Traced side** (`calls_trace.trace_calls`): runs cgr indexing a small fixture (`evals/results/l3_workspace/fixture/`, written by `_write_fixture`) under `sys.settrace`, recording every `(caller, callee)` where both are first-party functions in the target. This is a dynamic trace of *cgr's own code* executing — the fixture's only job is to drive cgr through diverse code paths.
- **Recall** = `|traced ∩ static| / |traced|`. `missed = traced − static` is written to `evals/results/calls_diff.json`. Two scopes are reported: *all calls* and *explicit* (excluding dunder callees).

Because the ground truth is an execution trace, recall is a sound lower bound: it can only credit cgr for call sites the fixture actually exercises. Enriching the fixture (more Python constructs, more languages) widens coverage and is the intended way to harden the metric.

### Decorator-wrapper normalization

When a function is wrapped by a `functools.wraps` decorator (e.g. cgr's `@recursion_guard`), calling it dispatches at runtime through the decorator's generic inner `wrapper`, so a naive trace records two edges:

```
caller            -> recursion_guard.decorator.wrapper      # the generic wrapper frame
recursion_guard.decorator.wrapper -> the_real_method        # wrapper calling func(...)
```

cgr's static graph instead "sees through" the decorator and records the single logical edge `caller -> the_real_method`, which is what a reader of the graph wants — the recycled `wrapper` is plumbing, not a meaningful call-graph node.

To keep the trace and the static graph in agreement, `calls_trace._frame_qn` attributes a `wrapper` frame to the function it wraps (recovered from the wrapper's closed-over callable, following any `__wrapped__` chain). This turns `caller -> wrapper` into `caller -> the_real_method` and collapses `wrapper -> the_real_method` into a self-edge (which the tracer already drops). The decision is **normalize in the eval**, not model wrappers in cgr, so cgr's graph stays free of generic wrapper nodes.

Covered by `codebase_rag/tests/test_l3_decorator_normalization.py`.

## Retrieval — graph vs grep (file-level call localization)

Answers the question raised in issue #424: does graph-augmented retrieval find
the code that calls a symbol better than plain grep? This is the retrieval layer
decoupled from any LLM, which is the measurement the GitLab GKG evaluation
([work item #224](https://gitlab.com/gitlab-org/rust/knowledge-graph/-/work_items/224))
flagged as out of scope. (That work item, contrary to a widely repeated claim,
contains no "8% over grep" figure; its headline was an agentic SWE-bench-Lite
pass rate of roughly 6 to 7 of 23 issues. This benchmark measures retrieval
quality directly instead.)

```bash
uv run python -m evals.retrieval --target codebase_rag
```

The task: for every first-party symbol `S`, find the files that call `S`. The
comparison unit is a file-level call edge `(caller_file, callee_simple_name)`,
which mirrors the GKG "did it open the right file" localization signal. Three
conditions are scored against one Python `ast` oracle over the same file and
first-party symbol universe:

- **graph** (`retrieval.cgr_call_edges`): every cgr `CALLS`/`INSTANTIATES` edge,
  reduced to its caller node's file and the callee's simple name (a constructor
  resolved to `Class.__init__` is credited to `Class`, as in L2).
- **grep_name** (`retrieval.grep_call_edges`, `GrepMode.NAME`): ripgrep for the
  bare symbol token `\b(name)\b`, the first thing a user reaches for.
- **grep_call** (`GrepMode.CALL`): ripgrep for the symbol followed by a paren
  `\b(name)\s*\(`, a call-tuned pattern.
- **Oracle** (`retrieval.oracle_call_edges`): every `ast.Call` whose callee
  simple name is first-party and non-dunder, attributed to its file.

Requires `rg` (ripgrep) on `PATH`; `evals.retrieval` exits cleanly if it is
missing. Writes `evals/results/retrieval_scores.csv` and
`evals/results/retrieval_diff.json`. The thesis and grep's two failure modes (a
bare reference or import counts as a hit, and a definition site `def S(` is
indistinguishable from a call) are pinned by
`codebase_rag/tests/test_retrieval_eval.py`.

Both grep conditions reach recall 1.0 by construction: the oracle is itself
name-based, so any called name is present textually and grep cannot miss it. The
entire story is therefore precision, which is exactly where the resolved graph
wins. Graph recall below 1.0 reflects the few call edges cgr does not resolve;
graph false positives are call edges cgr emits that the pure-`ast` notion of a
call does not see (worth a look, but a small fraction).

## Incremental update — incremental vs clean re-index

Answers a correctness question the other layers cannot: after cgr re-indexes only
the files that changed, does the resulting graph still equal a clean full
re-index of the same tree? Incremental indexing is where a knowledge graph
silently rots, so the clean re-index is the oracle and any divergence is a real
bug.

```bash
uv run python -m evals.incremental --target codebase_rag --sample 25
```

The probe is a semantically neutral edit: a trailing comment is appended to one
file, changing its hash (so cgr treats it as modified) without changing its AST
(so a clean re-index of the edited tree is identical to the original). For each
sampled file the harness indexes a fresh copy, applies the neutral edit, runs an
incremental update, then compares the mutated graph node for node and edge for
edge against a clean forced re-index of the identical on-disk state.

The comparison runs against a faithful in-memory store (`cgr_graph._StatefulIngestor`)
that implements the exact delete and fetch Cypher the incremental updater issues
(`DETACH DELETE` of a changed file's `Module` subtree, file and folder deletes,
orphan-external pruning, and the prune path queries), so deletions take real
effect rather than being mocked away. The store's semantics are pinned by
`codebase_rag/tests/test_incremental_eval.py`; the same suite also pins the
runner's requirement to purge any pre-existing hash cache copied from the source
tree, without which the baseline index would skip every file.

What it surfaces: editing a file `DETACH`-deletes its `Module` subtree, including
the `CALLS` edges incident on its functions. Outbound calls from the changed file
are rebuilt, but inbound `CALLS` from unchanged callers are deleted and never
rebuilt, because those callers are not reprocessed. This is
[issue #532](https://github.com/vitali87/code-graph-rag/issues/532), and the eval
shows it is broader than the issue records: a fresh incremental run rebuilds the
function registry from changed files only, so even the changed file's own
outbound calls to symbols defined in unchanged files are dropped (the callee is
unknown at resolution time). Writes `evals/results/incremental_scores.csv` and
`evals/results/incremental_diff.json`.

## Import resolution — internal vs external classification

The structural L1 above grades internal `IMPORTS` edges by their resolved target
file. It does not check how cgr classifies the *other* imports: stdlib and
third-party. This eval does, against an `ast` plus filesystem oracle, to surface
internal/external misclassification (the shape of
[issue #498](https://github.com/vitali87/code-graph-rag/issues/498)).

```bash
uv run python -m evals.import_resolution --target codebase_rag
```

The comparison unit is `(importing_file, top_level_package, is_external)`. Both
sides reduce an import to its top-level package name the same way (`import
numpy.linalg` and `from numpy import x` both reduce to `numpy`), and both decide
internal versus external by whether that top level is the project package, so the
oracle is independent of cgr's own resolver. cgr models an external import as a
`Module` node flagged `is_external=True` linked by `IMPORTS` (it does not emit
`ExternalPackage`/`DEPENDS_ON_EXTERNAL` for code-level imports), so the eval reads
the flag off each `IMPORTS` target. `from __future__ import ...` is a compiler
directive rather than a dependency and is excluded on both sides (a calibration
the tests pin). Writes `evals/results/imports_scores.csv` and
`evals/results/imports_diff.json`; the oracle and the misclassification signal
are pinned by `codebase_rag/tests/test_import_resolution_eval.py`.

## L1 (Go) — structure against a native `go/ast` oracle

The Python L1 above grades cgr against a Python `ast` oracle. To grade other languages with *independent* ground truth, each language is checked against its own standard-library parser rather than against cgr's own tree-sitter output. The first such oracle is Go.

```bash
uv run python -m evals.go_l1 --target /path/to/go/repo --project-name myrepo
```

How it works:

- **Oracle** (`evals/oracles/go_ast.go`): a small Go program that walks the target with the standard library's `go/parser` + `go/ast` and emits one JSON record per declaration (function-local type declarations included, via `ast.Inspect`, since cgr captures those too). The `kind` field already uses cgr's `NodeLabel` vocabulary (`Function`, `Method`, `Class`, `Interface`, `Type`), so records join cgr's nodes directly on `(kind, file, start_line)`. Mapping: `func` → `Function`, `func` with a receiver → `Method`, `type … struct` → `Class`, `type … interface` → `Interface`, any other `type …` (defined types and aliases) → `Type`. Requires the `go` toolchain on `PATH`; `evals.go_l1` exits cleanly if it is missing.
- **cgr side** (`cgr_graph.extract_cgr_go_nodes`): builds cgr's graph over the target and keeps the Go (`.go`) definition nodes.
- **Fair file set**: `run_go_oracle` drops oracle records under any directory in cgr's `IGNORE_PATTERNS` (e.g. `bin`, `vendor`, `build`), so the oracle grades exactly the files cgr indexes — single source of truth, no drift.
- **Score**: per-kind precision/recall/F1 via `score.score_node_kinds`, written to `evals/results/go_scores.csv` and `evals/results/go_diff.json`.

Validated on `apache/thrift` (1604 cgr Go nodes vs 1604 oracle nodes — exact):

| label | tp | fp | fn | precision | recall |
|---|---|---|---|---|---|
| Function | 535 | 0 | 0 | 1.0000 | 1.0000 |
| Method | 907 | 0 | 0 | 1.0000 | 1.0000 |
| Class | 106 | 0 | 0 | 1.0000 | 1.0000 |
| Interface | 30 | 0 | 0 | 1.0000 | 1.0000 |
| Type | 26 | 0 | 0 | 1.0000 | 1.0000 |

Both gaps the oracle originally exposed are fixed: Go `type` declarations (struct/interface/defined-type) are captured (see `codebase_rag/tests/test_go_type_declarations.py`), and Go receiver methods are now `Method` nodes qualified by their receiver type with a `DEFINES_METHOD` edge from it (see `codebase_rag/tests/test_go_receiver_methods.py`), rather than being mislabelled `Function`.

## L1 (Rust) — structure against a native `syn` oracle

The second native oracle is Rust, checked against `syn` (the de-facto standard Rust parser).

```bash
uv run python -m evals.rust_l1 --target /path/to/rust/repo --project-name myrepo
```

- **Oracle** (`evals/oracles/rs_oracle/`): a small Rust program that parses every `.rs` file with `syn` and emits one JSON record per declaration, in cgr's `NodeLabel` vocabulary. A `syn::visit::Visit` walk recurses into function bodies (function-local defs), `impl`/`trait` associated types, and closures (which cgr models as anonymous `Function` nodes), so the comparison is apples-to-apples. Mapping: `struct` → `Class`, `enum` → `Enum`, `union` → `Union`, `trait` → `Interface` (+ its methods → `Method`), `type` (incl. associated types) → `Type`, `fn`/closure → `Function`, `impl` method → `Method`. Requires the `cargo` toolchain (`proc-macro2`'s `span-locations` feature gives real line numbers); `evals.rust_l1` exits cleanly if it is missing.
- **cgr side** (`cgr_graph.extract_cgr_rust_nodes`), **score** (`score.score_node_kinds`), output to `rs_scores.csv` / `rs_diff.json`.

Validated on `apache/thrift`'s `lib/rs` (758 cgr Rust nodes vs 758 oracle nodes — exact, all kinds 1.0). The oracle surfaced one cgr gap, now fixed: methods in an `impl Trait for <primitive>` block (e.g. `impl From<Foo> for u8`) were dropped because the `primitive_type` impl target was unhandled (see `codebase_rag/tests/test_rust_impl_primitive_target.py`).

## L1 (TypeScript) — structure against the TypeScript compiler API

The third native oracle is TypeScript, checked against the TypeScript compiler API.

```bash
uv run python -m evals.ts_l1 --target /path/to/ts/repo --project-name myrepo
```

- **Oracle** (`evals/oracles/ts_oracle/`): a Node script that parses every `.ts`/`.tsx` file (`.d.ts` excluded) with the TypeScript compiler API and emits one JSON record per declaration, in cgr's `NodeLabel` vocabulary. Mapping, matching how cgr models TypeScript: `class` → `Class`, `interface` → `Interface`, `enum` → `Enum`, `type` → `Type`, `namespace`/`module` → `Class` (a class-like container), `function` → `Function` (or `Method` inside a namespace/class), arrow functions and function expressions → `Function` (cgr captures every one, like a Rust closure), `method`/`constructor` → `Method`. Requires `node`/`npm` (the `typescript` dependency is installed on first run; `package-lock.json` is committed and `node_modules/` is gitignored). `evals.ts_l1` exits cleanly if node is missing.
- **cgr side** (`cgr_graph.extract_cgr_ts_nodes`), **score** (`score.score_node_kinds`), output to `ts_scores.csv` / `ts_diff.json`.

Validated on `apache/thrift`'s TypeScript (`lib/nodets`, `lib/ts`): 136 cgr nodes vs 136 oracle nodes — exact, all kinds 1.0. No cgr gap found.

## L1 (JavaScript) — structure against the TypeScript compiler API

The same compiler-API oracle parses JavaScript too (the TypeScript compiler accepts JS), so JavaScript reuses `evals/oracles/ts_oracle/` over `.js`/`.jsx`.

```bash
uv run python -m evals.js_l1 --target /path/to/js/repo --project-name myrepo
```

Same mapping as TypeScript, with two JS-specific points matching cgr: object-literal shorthand methods are modelled as standalone `Function`s (not `Method`s), and every arrow function / function expression is a `Function`. Output to `js_scores.csv` / `js_diff.json`.

Validated on `apache/thrift`'s JavaScript (`lib/js`, `lib/nodejs`): 1087 cgr nodes vs 1087 oracle nodes — exact, all kinds 1.0. No cgr gap found.

## L1 (Java) — structure against the JDK Compiler Tree API

The sixth native oracle is Java, checked against the JDK's own parser (`com.sun.source` / `javax.tools`).

```bash
uv run python -m evals.java_l1 --target /path/to/java/repo --project-name myrepo
```

- **Oracle** (`evals/oracles/java_oracle/Oracle.java`): parses every `.java` file with the JDK Compiler Tree API (`task.parse()` only parses, so missing dependencies are fine) and emits one JSON record per declaration. Mapping, matching how cgr models Java: `class` → `Class`, `interface` → `Interface` (+ its method signatures → `Method`), annotation type (`@interface`) → `Class`, `enum` → `Enum`, method/constructor → `Method`. A method declared inside an **anonymous class** (e.g. `new Runnable() { public void run() {...} }`) is modelled as a standalone `Function` — the same way cgr treats it (and JS object-literal methods); the oracle replicates cgr's rule (a member is a `Method` only when its nearest enclosing named class precedes any enclosing method/lambda body). Requires `javac`/`java`; the oracle is compiled on first run (the `.class` is gitignored, the source committed). `evals.java_l1` exits cleanly if the JDK is missing.
- **cgr side** (`cgr_graph.extract_cgr_java_nodes`), **score** (`score.score_node_kinds`), output to `java_scores.csv` / `java_diff.json`.

Validated on `apache/thrift`'s `lib/java`: 2861 cgr nodes vs 2861 oracle nodes — exact, all kinds 1.0 (including the 103 anonymous-class methods graded as `Function`). No cgr gap found.

## L1 (Lua) — structure against a `luaparse` oracle

The seventh native oracle is Lua, checked against `luaparse`.

```bash
uv run python -m evals.lua_l1 --target /path/to/lua/repo --project-name myrepo
```

- **Oracle** (`evals/oracles/lua_oracle/`): a Node script that parses every `.lua` file with `luaparse` (`luaVersion: "5.3"`, so bitwise operators / integer division parse) and emits a `Function` record per function declaration/expression. Lua has no classes, so cgr models every function — global, `local`, table (`t.f`), method (`t:m`), and anonymous function expressions — as a `Function`. Requires `node`/`npm` (the `luaparse` dependency installs on first run; `package-lock.json` committed, `node_modules/` gitignored).
- **cgr side** (`cgr_graph.extract_cgr_lua_nodes`), **score** (`score.score_node_kinds`), output to `lua_scores.csv` / `lua_diff.json`.

Validated on `apache/thrift`'s Lua (`lib/lua`, `test/lua`): 376 cgr nodes vs 376 oracle nodes — exact, 1.0. No cgr gap found.

## L1 (PHP) — structure against a `php-parser` oracle

The eighth native oracle is PHP, checked against `php-parser` (a pure-JS PHP parser, so no `php` binary is needed).

```bash
uv run python -m evals.php_l1 --target /path/to/php/repo --project-name myrepo
```

- **Oracle** (`evals/oracles/php_oracle/`): a Node script that parses every `.php` file with `php-parser` and emits one record per declaration. Mapping, matching cgr: `class` → `Class`, `interface` → `Interface` (+ methods → `Method`), `trait` → `Class` (+ methods → `Method`), `enum` → `Enum`, `function` → `Function`, closure / arrow `fn` → `Function`. Methods of an **anonymous class** (`new class {...}`) are `Function`s (like Java/JS object-literal members), and a declaration's line is its first attribute (`#[Attr]`) line when present — both matching cgr's node span. Requires `node`/`npm` (the `php-parser` dependency installs on first run; `package-lock.json` committed, `node_modules/` gitignored).
- **cgr side** (`cgr_graph.extract_cgr_php_nodes`), **score** (`score.score_node_kinds`), output to `php_scores.csv` / `php_diff.json`.

Validated on `apache/thrift`'s PHP (`lib/php`): 1295 cgr nodes vs 1295 oracle nodes — exact, all kinds 1.0. No cgr gap found.

## Latest results (target: `codebase_rag`)

Committed snapshots live in `evals/results/` — `scores.csv` (L1), `diff.json` (L1 per-label missing/extra), `calls_diff.json` (L3 missed edges). Regenerate with the commands above.

### L1 — structure (`uv run python -m evals.cli`)

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| node | Module | 417 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| node | Class | 926 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| node | Function | 1955 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| node | Method | 3919 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | DEFINES | 2742 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | DEFINES_METHOD | 3919 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | INHERITS | 153 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | IMPORTS | 1274 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

Span (end_line) accuracy on matched defs: 6800/6800 exact.

### L3 — CALLS recall (`uv run python -m evals.l3`)

| scope | traced | captured | missed | recall |
|---|---|---|---|---|
| all calls | 634 | 634 | 0 | 1.0000 |
| explicit (no dunders) | 580 | 580 | 0 | 1.0000 |

The L3 fixture exercises rich Python plus all 11 supported languages; recall is a sound lower bound over the cgr code paths that fixture drives. These numbers are for the Python `codebase_rag` target — graded multi-language recall (JS/Rust/Go/Java/C/C++/Lua/PHP/Scala) is future work pending a SCIP-based oracle.

### Retrieval — graph vs grep (`uv run python -m evals.retrieval`)

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| retrieval | graph | 3217 | 587 | 37 | 0.8457 | 0.9886 | 0.9116 |
| retrieval | grep_name | 3254 | 10591 | 0 | 0.2350 | 1.0000 | 0.3806 |
| retrieval | grep_call | 3254 | 5638 | 0 | 0.3659 | 1.0000 | 0.5358 |

The resolved graph more than doubles the precision of even the call-tuned grep
(0.85 versus 0.37) at near-perfect recall, for an F1 of 0.91 versus 0.54: a gain
of roughly 0.38 absolute (about 70% relative) over the strongest grep baseline.
Bare-name grep, the common first attempt, scores far worse (F1 0.38). This is
the decoupled retrieval result behind the intuition that a structural graph
beats text search for code navigation.

### Incremental update — incremental vs clean re-index (`uv run python -m evals.incremental`)

Over a 25-file neutral-edit sample on `codebase_rag` (micro-averaged across
probes; clean re-index is the oracle, so `fn` is edges the incremental graph
dropped and `fp` is stale edges it kept):

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| edge | CALLS | 327832 | 4 | 3318 | 1.0000 | 0.9900 | 0.9950 |
| edge | IMPORTS | 82001 | 7 | 599 | 0.9999 | 0.9927 | 0.9963 |
| edge | INSTANTIATES | 25086 | 0 | 414 | 1.0000 | 0.9838 | 0.9918 |
| edge | DEFINES / DEFINES_METHOD / CONTAINS_* / INHERITS / OVERRIDES | (all) | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| node | all kinds | (all) | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

Only **3 of 25** edits reproduced a clean re-index exactly. The damage is
confined to the three cross-file *reference* edge types (CALLS, IMPORTS,
INSTANTIATES): editing a file deletes and rebuilds its own subtree, so node,
containment, DEFINES, INHERITS, and OVERRIDES edges (each single-parent and
local) stay perfect, but edges pointing *into* the changed file from unchanged
files are deleted and never rebuilt. The micro-averaged recall looks high because
each edit only perturbs the edges touching one file, but the per-edit effect is
large (e.g. editing `graph_updater.py` drops 1406 edges). This is
[issue #532](https://github.com/vitali87/code-graph-rag/issues/532), shown here
to extend beyond inbound `CALLS` to `IMPORTS` and `INSTANTIATES` as well.

### Import resolution — internal vs external (`uv run python -m evals.import_resolution`)

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| edge | imports-all | 1986 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | imports-internal | 462 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | imports-external | 1524 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

A clean negative result: on `codebase_rag`, cgr classifies every import correctly,
internal and external alike. This rules out #498-style misclassification on this
corpus and stands as a regression guard. (The first run reported 247 missing
externals; investigation showed they were all `from __future__ import ...`, an
oracle over-count now corrected rather than a cgr bug.)

### Next step: agentic resolved-rate (out of scope here)

The above isolates retrieval. The complementary end-to-end measurement is GKG's
own design: hold one agent and model fixed and vary only the tools (graph tools
versus grep), then report SWE-bench-style resolved rate over real issues. That
needs an LLM, a container harness, and many runs, so it is tracked separately
rather than run inside this deterministic harness.
