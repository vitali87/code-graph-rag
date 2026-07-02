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

What it surfaced and drove a fix for:
[issue #532](https://github.com/vitali87/code-graph-rag/issues/532). Editing a
file `DETACH`-deletes its `Module` subtree, including the reference edges incident
on its functions. The eval showed the loss was broader than the issue recorded:
**inbound** `CALLS`/`IMPORTS`/`INSTANTIATES` from unchanged callers were deleted
and never rebuilt (the callers are not reprocessed), and a fresh incremental run
also rebuilt the function registry from changed files only, so even the changed
file's **outbound** calls to symbols defined in unchanged files were dropped. The
fix, verified by this eval, has two parts:

- **Inbound** edges are captured before deletion and restored verbatim (rather
  than re-resolved, which would diverge: cgr resolution is context-sensitive).
- **Outbound** resolution rehydrates the function registry from the persisted
  graph so calls into unchanged files resolve again.

Residual divergence is confined to the changed file's own calls resolved through
type inference / protocol dispatch (e.g. `self.x.method()`), which need the full
cross-file type context that a single-file reprocess does not rebuild; this is
documented as a deeper follow-on, not a regression. Writes
`evals/results/incremental_scores.csv` and `evals/results/incremental_diff.json`.

Inbound capture is intentionally scoped to re-indexed files (changed, **not** new
or deleted), because a re-indexed file keeps its module qualified name, so the
restore target still exists after reprocessing. Moved or renamed files are not
captured by design: the old path is deleted and the new path is new, so an
unchanged caller's import of the old name no longer resolves, exactly as in a
clean re-index, and dropping that now-dangling edge is correct. Restoring edges
for a vanished module qn would instead fabricate a phantom module node, so the
scoping is the safe choice rather than a gap. A transparently re-exported rename
(old name still resolves) is the one narrow case left to a clean re-index.

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

## Inheritance — resolved INHERITS and OVERRIDES

The structural L1 grades `INHERITS` by the base's simple *name*. This eval grades
two deeper things against an `ast` oracle: that cgr resolves a base to the correct
first-party class qualified name (`INHERITS` target), and that method overrides
are attributed to the right base class (`OVERRIDES`).

```bash
uv run python -m evals.inheritance --target codebase_rag
```

The oracle resolves a base only when it is unambiguous: defined in the same module
or imported via `from <first-party> import <Base>`. Attribute bases (`pkg.Base`),
star-imported, and external bases are skipped and counted, never guessed. Two
deliberate scope limits keep the oracle honest rather than noisy:

- **INHERITS** is graded only for top-level classes (the universe the oracle
  enumerates); cgr edges whose subclass is a class nested inside a function are
  not graded against an oracle that never saw them.
- **OVERRIDES** is graded only for single-inheritance classes, where "which base
  does method `m` override" is unambiguous. Multi-base mixin classes are excluded
  on both sides, because the answer there is decided by the MRO, which this ast
  oracle does not model.

Writes `evals/results/inheritance_scores.csv` and
`evals/results/inheritance_diff.json`; pinned by
`codebase_rag/tests/test_inheritance_eval.py`.

## Instantiation — file-level INSTANTIATES

The retrieval eval folds class instantiation into its `CALLS` localization (a
constructor call resolves to `Class.__init__`, credited to the class). This eval
grades cgr's `INSTANTIATES` edges on their own, so a constructor-resolution
regression is visible separately from ordinary calls.

```bash
uv run python -m evals.instantiation --target codebase_rag
```

The unit is `(caller_file, class_simple_name)`. The oracle counts every `ast.Call`
whose callee simple name is a first-party class, **excluding bare-name calls whose
name is rebound in that file by a non-first-party import** (`from ext import
Config; Config()` names the external `Config`, not a same-named first-party
class). cgr contributes its `INSTANTIATES` edges reduced to the caller's file and
the class simple name. Writes `evals/results/instantiation_scores.csv` and
`evals/results/instantiation_diff.json`; pinned by
`codebase_rag/tests/test_instantiation_eval.py`.

Making the oracle import-aware surfaced a cgr precision bug: a constructor whose
name was explicitly imported from an external module (`from evals.types_defs
import GraphData`, with `evals` outside the indexed project) was resolved by the
simple-name trie fallback to a same-named first-party class
(`codebase_rag.types_defs.GraphData`), emitting a wrong `INSTANTIATES` edge. Fixed
in `call_resolver.py` (`_is_external_import` suppresses the trie fallback for a
bare name bound to a genuinely external import; first-party imports, prefixed or
bare, are unaffected). On `codebase_rag`: precision rose from 0.976 to 1.000
(9 false edges removed), recall 0.997. The one remaining miss is a class defined
in a test method and instantiated from inside a nested class's method (a closure
over an enclosing-function scope), a known resolution gap left documented rather
than scoped away.

## Dead code — reachability over the captured graph

cgr's `dead-code` command reports functions/methods unreachable from any entry
point. It runs as a Cypher reachability query against the database, which the
deterministic in-memory harness cannot execute, so this eval faithfully
re-implements that query's reachability over the captured graph and grades it on
controlled fixtures whose dead set is known by construction.

```bash
uv run python -m evals.dead_code --target codebase_rag   # informational report
```

Roots are project functions that are decorated with an entry-point decorator
(`@app.route` and friends), exported, named as an entry point, reached by a
`Module` via `CALLS` (a module-level call), or in a test file when tests are
included; everything reachable from a root via `CALLS` (plus `INSTANTIATES` /
`INHERITS` with classes) is live, and the rest is dead. The reachability is
unit-tested on hand-built graphs, so when it is run over a cgr-built graph from a
fixture with a known dead set, a mismatch indicts cgr's `CALLS` graph (a missing
edge would flag a live function as dead) rather than the scorer. The graded eval
is the fixture suite `codebase_rag/tests/test_dead_code_eval.py`; the CLI's
corpus mode is informational only, because a real repo has no independent dead-code
oracle (true reachability needs the very call graph under test). On `codebase_rag`
it currently reports 4450 unreachable functions/methods (tests excluded).

## Cross-project — resolution across top-level packages (monorepo)

Every other eval runs on a single top-level package (`codebase_rag`), so none
checks the case cgr is built for: a monorepo with several top-level packages where
one references another. This eval extracts cgr's `CALLS` and `IMPORTS` edges whose
endpoints live in *different* top-level packages and grades them on synthetic
multi-package fixtures whose cross-package edges are known by construction
(`codebase_rag/tests/test_cross_project_eval.py`). It confirms that
`pkg_b.use.run()` calling `pkg_a.core.shared()`, and `pkg_b`/`pkg_c` importing
`pkg_a` modules, resolve across the package boundary, while intra-package edges
are correctly excluded. cgr resolves all of these; the eval stands as a
regression guard for monorepo cross-package resolution.

## Static calls — function-level direct-call recall

Grades cgr's `CALLS` graph at function granularity against an `ast` oracle that
resolves only the calls a reader can resolve without type inference: a bare-name
call (`foo()`) whose target is a first-party function reached via a `from ...
import foo` or a same-module top-level def. Each becomes a `(caller_qn,
callee_qn)` edge. Method / attribute / dynamic calls need cgr's type inference and
are out of the oracle's scope, so only **recall** is graded (cgr resolving more
than static analysis can is expected, not a false positive). The oracle uses ast
import resolution, not cgr's function-registry trie, so it is independent.

```bash
uv run python -m evals.static_calls --target codebase_rag
```

Decorator applications (`@deco(...)`) are excluded (they are not calls the
decorated function makes), and the oracle attributes a call to its real enclosing
function qn including nested scopes (`Class.method.nested`).

**This eval caught a real cgr bug.** A call inside a function nested in a method
was emitted with a caller qn that dropped the method (`Class.nested` instead of
`Class.method.nested`, matching no node), and was also over-attributed to the
enclosing method. After the root-cause fix in `call_processor.py`
(`_class_member_qn_and_label` + `_calls_owned_by`), recall on `codebase_rag` is
4434/4434 = 1.0. Pinned by `codebase_rag/tests/test_static_calls_eval.py` and the
regression `codebase_rag/tests/test_nested_method_call_qn.py`.

## Multi-language retrieval (Go) — Go CALLS vs `go/ast`

The retrieval benchmark above is Python-only. This extends file-level call
localization to a second language: for each first-party Go symbol, which files
call it. cgr's Go `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against call sites extracted by Go's own `go/ast` (the same oracle
program as the Go L1 structure eval, extended to emit `CallExpr` callees), over
the same first-party name universe. The oracle uses Go's standard parser, fully
independent of cgr's tree-sitter Go frontend, so this measures cgr's cross-file
Go call resolution against ground truth.

```bash
uv run python -m evals.go_retrieval --target <go-sources>
```

Requires the `go` toolchain on `PATH`; the eval exits cleanly if it is missing.
The oracle counts a call by its callee simple name (a bare `foo()` or the selector
tail of `x.Method()` / `pkg.Func()`), keeping only callees that are declared
first-party functions/methods, exactly mirroring the Python retrieval oracle.
Pinned by `codebase_rag/tests/test_go_retrieval_eval.py`, where cgr's Go call
graph matches the `go/ast` oracle on the fixture. The same harness shape
generalizes to the other native-oracle languages (Rust, TypeScript, Java) by
teaching each oracle to emit call sites.

Running this on a real stdlib package (`encoding/json` via `GOROOT`) instead of
the fixture first surfaced two Go call-graph bugs, then drove their fix to a
clean result. (1) Receiver methods got a receiver-dropping caller qn that bound
to no node; fixed by `_go_receiver_method_caller`. (2) Go receiver dispatch
(`d.method()`, a method call on a receiver, parameter, or composite-literal
local) resolved to no callee at all, because Go calls were not typed and the Go
`selector_expression` callee node was never read by `_get_call_target_name`.
Fixed by adding Go to the typed-language set, a Go local-variable type inference
engine (`parsers/go/type_inference.py`) that maps receivers/parameters/`:=`
locals to their type, and reading the selector callee name. On `encoding/json`,
precision/recall went from 1.0/0.55 to 1.0/1.0 (110/110 call edges, zero false
positives).

## Multi-language retrieval (Rust) — Rust CALLS vs `syn`

The same harness applied to Rust: for each first-party Rust symbol, which files
call it. cgr's Rust `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against call sites extracted by `syn` (the de-facto Rust parser, the
same oracle as the Rust L1 structure eval, extended to emit `ExprCall` path
callees and `ExprMethodCall` method idents), over the same first-party name
universe. `syn` is independent of cgr's tree-sitter Rust frontend.

```bash
uv run python -m evals.rust_retrieval --target <rust-sources>
```

Requires the `cargo` toolchain on `PATH`; the eval exits cleanly if it is missing.
Pinned by `codebase_rag/tests/test_rust_retrieval_eval.py`, where cgr's Rust call
graph matches the `syn` oracle on the fixture.

Running it on a real stdlib module (`core::str`, via the `rust-src` component
under the rustup sysroot) surfaced two cgr bugs and drove the fix from
precision/recall 0.91/0.65 to 0.94/0.95. (1) A method in a generic impl block
(`impl<'a> Thing for Chars<'a>`) was attributed to a caller qn that carried the
impl's generics (`crate.lib.Chars<'a>.go`), but the method node is registered on
the bare type (`crate.lib.Chars.go`), so every such `CALLS` edge had a dangling
caller and was dropped; fixed by routing `_get_rust_impl_class_name` through the
same `rs_utils.extract_impl_target` the definition pass uses (recovered 44 of 58
missing edges). (2) A regression from the externally-imported-name fix:
`_is_external_import` mistook Rust relative imports (`use super::b::helper`, whose
recorded target is the `::`-separated `super::b::helper`) for external symbols and
suppressed the trie fallback, dropping the call; fixed by restricting that guard
to dotted absolute-path imports (Python/Java form), leaving `::`-path and relative
imports to the trie. Pinned by `codebase_rag/tests/test_rust_impl_method_call_qn.py`.
The remaining gap is field/generic method dispatch (`self.field.method()`,
`Pattern` trait calls) needing deeper Rust type inference, plus oracle
undercount inside macro bodies (`write!` expands to `.fmt()` calls `syn` does not
see), documented rather than scoped away.

## Multi-language retrieval (Java) — Java CALLS vs the JDK Compiler Tree API

The same harness applied to Java: for each first-party Java symbol, which files
call it. cgr's Java `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against method-invocation sites extracted by `javac` (the JDK Compiler
Tree API, the same oracle as the Java L1 structure eval, extended to emit the
trailing identifier of each `MethodInvocationTree`), over the same first-party
name universe. `javac` is independent of cgr's tree-sitter Java frontend.

```bash
uv run python -m evals.java_retrieval --target <java-sources>
```

Requires the `javac`/`java` toolchain on `PATH`; the eval exits cleanly if it is
missing. Pinned by `codebase_rag/tests/test_java_retrieval_eval.py`, where cgr's
Java call graph matches the `javac` oracle on the fixture.

Running it on a real stdlib package (`java.util`, 349 files from the JDK
`src.zip`) surfaced three cgr bugs and drove recall from 0 (every Java call
dropped) to 0.52 at precision 1.0 (zero false positives). (1) The definition pass
registers a Java method node with its parameter signature (`Class.name(args)` —
Java overloads), but the call pass built the enclosing-method caller qn without
it (`Class.name`), so every Java method's `CALLS` from-endpoint matched no node
and the edge would not attach in Memgraph; fixed by routing the caller qn through
the same signature build the definition pass uses
(`codebase_rag/tests/test_java_call_caller_qn.py`). (2) `find_package_start_index`
returns `None` for any project not under a recognized `src/main/java` layout, so
`_build_fqn_lookup_map` left the simple-name to module map empty and all
cross-file resolution (instance dispatch in sibling files) silently failed; fixed
by falling back to the segment after the project root for flat/non-standard
layouts. (3) A static call on a bare class-name receiver in a sibling file
(`T.make()`, same package, no import) never resolved because the receiver-type
lookup only checked the current module and explicit imports; fixed with a
same-package class-name fallback in `_resolve_java_object_type`. The remaining gap
is interface/abstract dispatch (the name-based oracle counts a call whenever the
callee name is any declared first-party method, but cgr emits an edge only when
the concrete receiver type is statically knowable) and deep receiver-type
inference (iterator/functional-interface/generic element types), documented rather
than scoped away.

## Multi-language retrieval (TypeScript) — TS CALLS vs the TypeScript compiler API

The same harness applied to TypeScript: for each first-party TS symbol, which
files call it. cgr's TS `CALLS` edges, reduced to `(caller_file,
callee_simple_name)`, are graded against call sites extracted by the TypeScript
compiler API (the same oracle as the TS L1 structure eval, extended to emit the
callee identifier of each `CallExpression` — bare identifier or property-access
trailing name), over the same first-party name universe. `tsc` is independent of
cgr's tree-sitter TS frontend. The oracle also now names arrow / function
expressions by their binding (`const foo = () => …` → `foo`), matching how cgr
names them, so they enter the declared-name universe.

```bash
uv run python -m evals.ts_retrieval --target <ts-sources>
```

Requires `node`/`npm` (the `typescript` dependency installs on first run); the
eval exits cleanly if node is missing. Pinned by
`codebase_rag/tests/test_ts_retrieval_eval.py`, where cgr's TS call graph matches
the `tsc` oracle on the fixture.

Running it on a real library (`zod`, 88 non-test files from `packages/zod/src/v4`)
surfaced two cgr bugs and drove recall from 0.45 to 0.75 at precision 1.0 (zero
false positives). (1) **Exported-function duplication:** the definition pass
already ingests an exported function / const-arrow at its natural qn, but the
ES6-export pass (`ingest_exported_function`) re-registered it, minting a spurious
`qn@line` duplicate node (`register_unique_qn` collision) onto which call
resolution then bound — so callers of the natural node were invisible and ~half of
all TS call edges carried a mangled `name@line` callee. In an ESM codebase where
most functions are exported this polluted the whole graph; fixed by skipping the
export-pass registration when the natural qn already exists
(`codebase_rag/tests/test_ts_export_no_duplicate.py`). (2) **Arrow-body calls
unresolved:** the call pass derived a caller name with `child_by_field_name("name")`,
which is empty for an arrow / function expression, so it skipped the whole
`const f = () => …` body and never emitted its calls — and modern TS is mostly
arrow functions. Fixed by recovering the binding name for the `variable_declarator`
form so the body's calls attribute to the same qn the definition pass registered
(`codebase_rag/tests/test_ts_arrow_caller_calls.py`). A follow-up fix also models
**class-field arrow members** (`class T { helper = () => … }`): the definition
pass dropped them (the arrow has no `name` field, so `ingest_method` returned
early) so no `T.helper` node existed and its body's calls were lost. The binding
name is now taken from the enclosing field definition in both the definition and
call passes, modelling it as `T.helper` like a normal method
(`codebase_rag/tests/test_ts_class_field_arrow.py`). The remaining gap is calls
inside anonymous (returned/inline) arrows, method dispatch on typed receivers, and
the name-based oracle's over-count of common method names (`error` on a receiver
cgr cannot type) — documented rather than scoped away.

## Multi-language retrieval (JavaScript) — JS CALLS vs the TypeScript compiler API

The same harness over `.js`/`.jsx`: the `tsc` oracle parses JavaScript
syntactically (independent of cgr's tree-sitter JS frontend), so it grades cgr's
JS `CALLS` resolution against ground truth for a language cgr handles through a
partly distinct path (CommonJS `require`/`module.exports`, prototype methods, no
type annotations).

```bash
uv run python -m evals.js_retrieval --target <js-sources>
```

Requires `node`/`npm`; pinned by `codebase_rag/tests/test_js_retrieval_eval.py`
(cgr matches the oracle on a clean ES-module fixture).

Running it on a real corpus (`webpack/lib`, 577 files, classic CommonJS) surfaced
one dominant cgr bug and drove recall from 0.61 to 0.83 at precision ~1.0.
**Calls inside an anonymous arrow passed as an argument were dropped:** a callback
like `compiler.hooks.compilation.tap(NAME, (compilation) => { getReplacements() })`
has no name and no binding, so the call pass skipped the arrow, and
`_calls_owned_by` had already excluded its calls from the enclosing method — so the
call attributed to nothing and vanished. Since webpack (and most modern JS) is
saturated with this `hooks.tap(name, (x) => …)` pattern, it was the leading recall
killer. Fixed by excluding only *attributable* nested functions (named, or arrows
bound to a name) from the enclosing scope, so an anonymous arrow's calls bubble up
to the nearest named function/method — matching where the oracle attributes them
(`codebase_rag/tests/test_js_anonymous_callback_calls.py`). The remaining gap is
method dispatch on receivers cgr cannot type and the name-based oracle's over-count
of calls through function parameters (`callback`/`fn`) and Node built-ins
(`join`/`resolve`) whose names collide with first-party declarations — documented
rather than scoped away.

## Multi-language retrieval (PHP) — PHP CALLS vs `php-parser`

The same harness applied to PHP: for each first-party PHP symbol, which files
call it. cgr's PHP `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against call sites extracted by `php-parser` (the same oracle as the
PHP L1 structure eval, extended to emit the callee of each `call` — a bare
function name, or the trailing member of a `$obj->m()` / `Class::m()` lookup —
and to carry real declaration names), over the same first-party name universe.
`php-parser` is a pure-JS PHP parser, independent of cgr's tree-sitter PHP frontend.

```bash
uv run python -m evals.php_retrieval --target <php-sources>
```

Requires `node`/`npm` (the `php-parser` dependency installs on first run; no PHP
runtime needed). Pinned by `codebase_rag/tests/test_php_retrieval_eval.py`, where
cgr's PHP call graph matches the `php-parser` oracle on the fixture.

Running it on a real library (`monolog`, 121 files from `src/Monolog`) surfaced
one cgr bug and drove recall from 0.97 to 0.99 at precision 1.0 (zero false
positives). **Plain function calls were never resolved:** a PHP
`function_call_expression` carries its callee as a `name` node under the
`function` field, a type `_get_call_target_name` did not handle, so no callee name
was extracted and the edge was dropped — only method (`$obj->m()`) and static
(`Class::m()`) calls, which expose a `name` field directly, resolved. cgr's strong
OOP-PHP score had masked the gap (monolog is almost entirely methods). Fixed by
adding the PHP `name` type to the callee match arm
(`codebase_rag/tests/test_php_function_call.py`). The remaining gap is the
name-based oracle's over-count: monolog declares first-party methods named
`substr`/`reset`/`fwrite` (e.g. a UTF-8-aware `Utils::substr`), so a bare call to
the PHP builtin of the same name is counted as first-party — cgr's simple-name
trie fallback links it to the same-named first-party symbol, which the file+name
metric credits. Documented rather than scoped away.

## Multi-language retrieval (Lua) — Lua CALLS vs `luaparse`

The same harness applied to Lua: for each first-party Lua function, which files
call it. cgr's Lua `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against call sites extracted by `luaparse` (the same oracle as the Lua
L1 structure eval, extended to emit the callee of each call — a bare name, or the
trailing member of a `t.f()` / `t:m()` lookup — and to carry real declaration
names, including function expressions named after their assignment target), over
the same first-party name universe. `luaparse` is a pure-JS Lua parser,
independent of cgr's tree-sitter Lua frontend.

```bash
uv run python -m evals.lua_retrieval --target <lua-sources>
```

Requires `node`/`npm` (the `luaparse` dependency installs on first run; no Lua
runtime needed). Pinned by `codebase_rag/tests/test_lua_retrieval_eval.py`, where
cgr's Lua call graph matches the `luaparse` oracle on the fixture.

Running it on a real library (`penlight`, 39 files from `lua/pl`) surfaced one cgr
bug and held precision at 1.0 (zero false positives), recall 0.93. **Calls inside
function expressions were never attributed:** a function bound to a variable or
table field (`M.runner = function() ... end`) has no name field, so the call pass
could not derive the caller qn and skipped the whole body — the same family as the
JS/TS arrow-caller gap. The definition pass already names these after their
assignment target (`lua_utils.extract_assigned_name`); the fix reuses that in the
call pass so the body's calls attribute to the right node
(`test_cgr_resolves_function_expression_body_calls`). The remaining recall gap is
colon-method dispatch and the name-based oracle's over-count: a colon method
`function List:append(i)` gets cgr qn `List:append` (simple name `List:append`, not
`append`), and a call `obj:append(x)` on a receiver whose type cgr cannot
statically infer (e.g. `res[klass]:append(val)`) cannot resolve, while the oracle
credits any call whose simple name is a declared first-party function. Documented
rather than scoped away.

## Multi-language retrieval (C) — C CALLS vs `libclang`

The same harness applied to C: for each first-party C function, which files call
it. cgr's C `CALLS` edges, reduced to `(caller_file, callee_simple_name)`, are
graded against call sites extracted by `libclang`, over the same first-party name
universe. cgr parses C with tree-sitter by default (`CPP_FRONTEND=libclang` is
off), so `libclang` — a full C front end — is an independent oracle. C has no
overloading, so a simple name is unambiguous.

```bash
uv run python -m evals.c_retrieval --target <c-sources>
```

Requires `libclang` (the `clang.cindex` Python bindings). No `compile_commands.json`
is needed: each `.c` file is parsed directly, with the SDK sysroot
(`xcrun --show-sdk-path`) and clang's builtin-header directory
(`clang -print-resource-dir`) added so system and compiler headers resolve. A
translation unit that still emits an error diagnostic (a missing build-generated
header such as a configured `config.h`) has a possibly-truncated AST, so the oracle
**abstains** on it: that file is left out of the covered set and the cgr side is
held to the same files (the count of graded files is logged, never silently
dropped). Pinned by `codebase_rag/tests/test_c_retrieval_eval.py`, where cgr's C
call graph matches the `libclang` oracle on a header-free fixture.

Running it on a real project (`jq`, 18 of 21 `src/*.c` files parsed cleanly; the
other three need build-generated headers) gives precision **0.98**, recall
**0.93**, F1 0.96. Every diff entry was classified against the source; the tail
splits into two causes.

**Most of it is the C preprocessor gap** (inherent — cgr's tree-sitter front end
does not evaluate `#if`/macros, while `libclang` does), confirmed in both
directions:

- All 11 false positives are calls cgr emits inside inactive conditional branches
  that `libclang` correctly compiles out: `jvp_strtod` / `tsd_dtoa_context_get` in
  `jv.c` under `#ifdef USE_DECNUM`, `jv_parser_new` in `jq_test.c` under
  `#ifdef HAVE_PTHREAD`, `jv_mem_calloc` in `jv_print.c` under `#ifdef WIN32`,
  `yysymbol_name` in `parser.c` under `#if YYDEBUG` (`YYDEBUG` is `0`). cgr makes
  **zero misresolutions** — each is correct code that is simply dead.
- Most false negatives are calls that exist only after macro expansion, which cgr
  cannot see: the `jv_object_foreach` macro (`jv.h`) expanding to `jv_object_iter*`
  calls, the `token()` / `check_done()` macros in `jv_parse.c`, flex's input macros,
  and bison's `api.prefix {jq_yy}` rename (`libclang` records `jq_yylex` after the
  `#define`; cgr resolves the same call under the textual name `yylex`).

**The rest is a genuine cgr limitation, not the preprocessor:** in the
bison-generated `parser.c`, tree-sitter produces 20 `ERROR` nodes, and the normal
`yyerror` definition (a plain `void yyerror(...)` at `parser.c:406`) falls inside
one. cgr therefore never registers `yyerror` as a function, so every call to it is
unresolved. cgr silently drops real definitions that land inside a tree-sitter
parse-error region on machine-generated code. The trigger was reduced to a
`tree-sitter-c` grammar bug (a block comment inside a `#define` body breaks the
parse and drops the following declaration, present through the latest 0.24.2);
tracked in issue #555 for an upstream report. It is rooted in the grammar, not in
cgr's resolution logic, so it is reported here, not hidden.

## Multi-language retrieval (C++) — C++ CALLS vs `libclang`

The same harness applied to C++: for each first-party C++ function or member
function, which files call it. cgr's C++ `CALLS` edges, reduced to
`(caller_file, callee_simple_name)`, are graded against call sites extracted by
`libclang`, over the same first-party name universe (free functions, function
templates, and member functions; constructors/destructors are excluded because
cgr models object creation as `INSTANTIATES`). cgr parses C++ with tree-sitter by
default (`CPP_FRONTEND=libclang` is off), so `libclang` is an independent oracle.
Overloads collapse under the `(file, simple-name)` metric, so they need no
disambiguation.

```bash
uv run python -m evals.cpp_retrieval --target <cpp-sources> --define LEVELDB_PLATFORM_POSIX=1
```

Requires `libclang`. C++ standard headers must be parsed by a `libclang` whose
clang version matches the active SDK's `libc++`; the bundled pip wheel's older
clang cannot, so the oracle prefers a system `libclang`
(`/Library/Developer/CommandLineTools/usr/lib/libclang.dylib` on macOS) and pins
it before the first parse. No `compile_commands.json` is needed: each source is
parsed directly with the SDK sysroot, the SDK's `libc++` headers (which must
precede clang's builtin resource headers), and every first-party header directory
added as an include path. A build normally supplies platform macros (e.g.
`LEVELDB_PLATFORM_POSIX`); pass them with `--define`. A translation unit that still
emits an error diagnostic **abstains** (left out of the covered set; the cgr side
is held to the same files, the graded count logged). To avoid crediting or
penalizing calls whose simple name merely collides with a first-party symbol, the
oracle grades a call only when `libclang` resolves its callee to a **first-party
declaration** (`child.referenced`), so a `std::string::size()` call is never
counted as a first-party `size` edge. Pinned by
`codebase_rag/tests/test_cpp_retrieval_eval.py`, where cgr's C++ call graph matches
the `libclang` oracle on a header-free namespaced fixture.

Running it on a real project (`leveldb`, 40 of 42 core sources parsed cleanly; the
other two are Windows-only or need gmock) gives precision **0.99**, recall
**0.83**, F1 **0.90** — recall up from **0.54** before the namespace fix below, and
precision lifted from 0.96 by the receiver type inference chain (next section).

**The dominant gap was a real cgr bug: the call pass dropped the namespace from
the caller qn.** The definition pass binds a C++ free function or class inside a
`namespace` to a namespaced qualified name (`module.ns.fn`, `module.ns.Class`),
but the call pass built the enclosing caller's qn without the namespace
(`module.fn`, `module.Class.method`). Every such `CALLS` edge's source therefore
pointed at a node that does not exist (904 of 1227 C++ call sources dangled on
`leveldb`, all of it in `namespace leveldb`), so the call never attached. The fix
routes both the free-function and class qns through the same
`cpp_utils.build_qualified_name` the definition pass uses, so caller and node qns
always agree (RED test `test_cpp_namespace_call_caller_qn.py`). Dangling sources
fell to 251 and recall rose 0.54 → 0.82.

**Three follow-on cgr fixes then landed on top of the namespace fix, building out
C++ receiver type inference:** (a) C++ joined the typed-language set with an engine
(`parsers/cpp/type_inference.py`) mapping parameters and local variables — including
reference declarators, multi-variable declarations, and drop-on-conflict lexical
scope handling — to their class types, so `obj->method()` resolves to the method on
the receiver's class instead of by bare name; (b) a member call whose receiver has a
known external type (a `std::string`) skips the name-only trie fallback instead of
mis-binding to a same-named first-party method (`call_resolver._receiver_type_is_external`);
(c) member **fields** are captured per class at ingestion (`build_field_type_map`,
stored in `class_field_types` keyed by class qn) and merged into the receiver map by
the caller's enclosing class, so `mutex_.Lock()` in an out-of-line method resolves to
the field's type even though the field is declared in a different file (the header).
Together these lifted precision **0.96 → 0.99** (false positives 30 → 10) on `leveldb`.

The remaining tail is documented, not scoped away:

- **Operator overloads** (`operator=` ×25, `operator[]`, `operator==`/`!=`, 23% of
  the misses): `libclang` records `a = b` and `a[i]` as calls to the overloaded
  operator methods, while cgr models them as `builtin.cpp.*` operator calls — a
  metric difference, not a misresolution.
- **tree-sitter-cpp parse corruption in complex files (the dominant residual FPs).**
  Traced to a `tree-sitter-cpp` grammar limitation, not a cgr resolution bug. A
  preprocessor conditional inside a construct — the reduced minimal trigger is a
  constructor member-initializer list interrupted by `#if`/`#endif`:

  ```cpp
  class A { public: A(int n) :
  #if !defined(NDEBUG)
      x_(n),
  #endif
      y_(n) {} int x_; int y_; };
  ```

  emits `ERROR` nodes (4 vs 0 without the `#if`). In real files this cascades:
  `util/env_posix.cc` (24 `ERROR` nodes) mis-nests every sibling class under the
  first (`Limiter.PosixEnv`), turns `struct ::flock` references into phantom classes,
  and degrades out-of-line methods like `DBImpl::BuildBatchGroup` into free functions
  that lose member-field inference — which is what produces the residual `begin`/`end`
  and `Schedule`/`SetReadOnly*` false positives. This exact trigger is already reported
  upstream as [tree-sitter-cpp #297](https://github.com/tree-sitter/tree-sitter-cpp/issues/297)
  ("ifdef not supported inside constructor member initializer list"); it is a sibling of
  the tree-sitter-c grammar bug tracked in #555. It is an **emergent cascade** — every
  isolated small reproduction (clean out-of-line methods, same-basename `.h`/`.cc`, an
  unrelated `#if` error in the same file) resolves correctly, so there is no minimally
  reproducible cgr-side fix; the fix is upstream (grammar) or the libclang frontend.
- **The libclang frontend trades this precision gain for recall.** Running the same
  `leveldb` benchmark with `CPP_FRONTEND=libclang` (a `compile_commands.json` from
  CMake) parses the corrupt files correctly — `env_posix` classes nest properly and the
  FP count drops to 1 (precision **0.9975**) — but recall falls to **0.46** (F1 0.63): the
  frontend emits far fewer `CALLS` edges than the tree-sitter resolver, a diffuse gap
  (implicit operators, compile-DB parse-context differences from the oracle's
  per-file parse, and header-inline caller-file attribution). So the default
  tree-sitter path remains the higher-F1 choice; the libclang frontend is the correct
  frontend for parse fidelity but needs its own call-edge recall work before it is a
  drop-in improvement.

## Multi-language retrieval (Scala) — Scala CALLS vs `scalameta`

The same harness applied to Scala: for each first-party Scala `def`, which files
call it. cgr's Scala `CALLS` edges, reduced to `(caller_file, callee_simple_name)`,
are graded against call sites extracted by `scalameta`, over the same first-party
name universe (every declared `def`). cgr parses Scala with tree-sitter, so
`scalameta` (the Scala compiler front-end's own parser, run via `scala-cli`) is an
independent oracle. The oracle grades only files it parses cleanly (covered set;
the cgr side is held to the same files), so a file using Scala 3 syntax that the
2.13 parser rejects penalizes neither side.

```bash
uv run python -m evals.scala_retrieval --target <scala-sources>
```

Requires `scala-cli` on `PATH` (it fetches `scalameta` on first run); the eval
exits cleanly if it is missing. Pinned by
`codebase_rag/tests/test_scala_retrieval_eval.py`, where cgr's Scala call graph
matches the `scalameta` oracle on the fixture.

**It surfaced a real cgr bug: infix-operator calls were dropped.** cgr's Scala spec
lists `infix_expression` as a call node type, so it is collected, but the
callee-name extractor (`call_processor._get_call_target_name`) had no case for it —
an `infix_expression` (`a ~> b`, `xs map f`) exposes its callee through an `operator`
field, not the `function` field the extractor keyed on, so every infix operator call
(pervasive in a combinator DSL) returned no name and never attached. The fix adds a
Scala-gated `operator`-field case. On `scala-parser-combinators` recall rose
**0.40 → 0.73**, F1 **0.57 → 0.84**, precision held at **1.0**; `scopt` shows the same
shape (precision **1.0**, recall **0.71**).

**Scope: application and infix call sites, not bare paren-less selects.** Scala's
uniform access makes a nullary method call (`obj.done`) and a plain field read
(`obj.done` where `done` is a `val`) syntactically identical — both parse to a bare
`field_expression`/`Term.Select` with no argument list. Resolving those by simple name
would turn a same-named field read into a spurious `CALLS` edge, so neither cgr's call
extractor nor the oracle names a bare select; both grade `Term.Apply`/`ApplyInfix`
(and cgr's `call_expression`/`infix_expression`) sites, which carry an explicit
application. Measured on the two corpora, resolving bare selects added **zero** edges
anyway (the enclosing applications already cover the real calls), so this costs no
recall. The residual is the diffuse receiver-type-inference tail every language eval
carries (implicit conversions such as `"" ~>`, deeply generic receivers), not a
systematic gap.

## Semantic search — query to function relevance

cgr's semantic search embeds each function's source and retrieves by cosine
similarity to a query embedding. This grades that relevance directly: for
controlled fixtures whose natural-language query maps unambiguously to one
function, does cgr's embedder rank that function in the top `k`?

It uses cgr's own embedder over function source extracted from the captured graph
(the same text cgr embeds), computes the ranking, and scores recall@k against
curated `query -> function` cases (e.g. "read and parse a json file" should
retrieve `load_json_file`, not `send_email` or `compute_sales_tax`). This tests
the embedding-and-ranking pipeline that decides relevance; the Qdrant ANN layer
only approximates the same cosine ranking, so it is out of the loop here.

Requires the `semantic` extra (embedding model); the eval is skipped when it is
absent. Pinned by `codebase_rag/tests/test_semantic_search_eval.py`, where cgr
reaches recall@3 = 1.0 on the fixture. The relevance set is curated and
deliberately clear-cut: this is a regression guard that the pipeline retrieves
obviously-relevant code, not a broad relevance benchmark (which would need a large
human-judged dataset).

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

Over a 25-file neutral-edit sample on `codebase_rag`, **after the #532 fix**
(micro-averaged across probes; clean re-index is the oracle, so `fn` is edges the
incremental graph dropped and `fp` is stale edges it kept):

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| edge | CALLS | 333010 | 63 | 740 | 0.9998 | 0.9978 | 0.9988 |
| edge | IMPORTS | 82995 | 7 | 5 | 0.9999 | 0.9999 | 0.9999 |
| edge | INSTANTIATES | 25525 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | DEFINES / DEFINES_METHOD / CONTAINS_* / INHERITS / OVERRIDES | (all) | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| node | all kinds | (all) | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

**10 of 25** edits now reproduce a clean re-index exactly (up from 3 before the
fix), `INSTANTIATES` is perfect, and `IMPORTS` is all but perfect. For reference,
before the fix the same sample showed CALLS `fp`/`fn` of 4/3318, IMPORTS 7/599,
INSTANTIATES 0/414, and only 3/25 clean-equivalent: the fix cut CALLS divergence
by roughly 75% and IMPORTS by 98%. The residual is the changed file's own calls
resolved through type inference / protocol dispatch (the `fp`/`fn` are mostly the
same call resolved to the protocol method incrementally versus the concrete
implementation in a clean pass), which needs full cross-file type context to
close (see the methodology note above). Tracked under
[issue #532](https://github.com/vitali87/code-graph-rag/issues/532).

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

### Inheritance — resolved INHERITS and OVERRIDES (`uv run python -m evals.inheritance`)

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| edge | inherits-resolved | 31 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |
| edge | overrides | 57 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

Another clean negative result within the graded scope (top-level classes;
single-inheritance for overrides): cgr resolves every base to the correct
first-party class and attributes every single-inheritance override to the right
base. The first run showed minor `fp`/`fn`; investigation traced all of them to
oracle scope (a class nested in a test method, and multi-base mixin classes whose
override attribution is MRO-decided), not cgr defects, so the scope was tightened
rather than the discrepancies reported.

### Instantiation — file-level INSTANTIATES (`uv run python -m evals.instantiation`)

| category | label | tp | fp | fn | precision | recall | f1 |
|---|---|---|---|---|---|---|---|
| edge | instantiates | 378 | 0 | 0 | 1.0000 | 1.0000 | 1.0000 |

cgr localizes every constructor call exactly on `codebase_rag`: the
`INSTANTIATES` set and the ast oracle's constructor-call set are identical.

### Next step: agentic resolved-rate (out of scope here)

The above isolates retrieval. The complementary end-to-end measurement is GKG's
own design: hold one agent and model fixed and vary only the tools (graph tools
versus grep), then report SWE-bench-style resolved rate over real issues. That
needs an LLM, a container harness, and many runs, so it is tracked separately
rather than run inside this deterministic harness.
