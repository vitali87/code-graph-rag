# cgr evaluation harness

Scores the knowledge graph that `code-graph-rag` (cgr) builds against ground truth, with no Memgraph required (an in-memory capturing ingestor drives `GraphUpdater(...).run(force=True)`).

## L1 — structure (containment)

Scores cgr's definition nodes and `DEFINES`/`DEFINES_METHOD` edges against a scope-aware Python `ast` oracle.

```bash
uv run python -m evals.cli --target codebase_rag
```

Writes `evals/results/scores.csv` and `evals/results/diff.json`. Node identity join is `(kind, file, start_line)`.

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
