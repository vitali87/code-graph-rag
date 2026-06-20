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
