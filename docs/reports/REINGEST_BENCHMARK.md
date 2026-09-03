---
description: "Measured latency of a one-file scoped re-ingest (GraphUpdater.reingest) against the whole-tree update path."
---

# Scoped re-ingest latency

Issue #1524 asks for a graph that reflects an agent's edit in hundreds of
milliseconds. `GraphUpdater.reingest(paths)` re-parses only the named files
and the files that depend on them (one level, found through the graph's own
`CALLS`/`REFERENCES`/`INSTANTIATES`/`IMPORTS`/`INHERITS`/`IMPLEMENTS`/`RETURNS`/`ACCEPTS` edges), resolves
calls within that set only, and restores every other inbound edge verbatim.
The watcher (`realtime_updater.py`) and the MCP `reingest` tool both run
through it.

## Harness

`benchmarks/bench_reingest.py` indexes a corpus once, then repeatedly toggles
a trailing comment on one file (the bytes and hash change, the AST does not)
and re-ingests it, reporting p50 and p95 over the iterations. For comparison
it then bumps the file's mtime and runs the whole-tree incremental path
(`GraphUpdater.run()` behind the hash cache, what `update_repository` does)
for the same edit. Both run against the in-memory `_StatefulIngestor`, so the
numbers cover parsing, scoped call resolution and graph construction and
exclude Memgraph round-trips by construction. Logging is at INFO, as in the
MCP server.

```bash
uv run python -m benchmarks.bench_reingest . --file codebase_rag/services/graph_diff.py
uv run python -m benchmarks.bench_reingest . --file codebase_rag/parsers/utils.py --iterations 10
```

## Results

Corpus: this repository (1,409 indexed files, 661,870 lines including tests
and docs; ~414k lines of Python/TypeScript/Go/Rust/Java source). Apple
Silicon laptop, cgr 0.0.804 + #1524, 2026-08-30.

| Edited file | Dependents re-parsed | reingest p50 | reingest p95 | whole-tree update p50 |
|---|---|---|---|---|
| `codebase_rag/services/graph_diff.py` (typical module) | 2 | 194 ms | 445 ms | 2,934 ms |
| `codebase_rag/parsers/utils.py` (hub imported by 54 files) | 54 | 3,492 ms | 3,733 ms | 6,992 ms |

The typical edit lands well inside the 1 s p95 budget. The hub case is
bounded by the dependents: each of the 54 importing files is re-parsed and
has its calls re-resolved, at roughly 60 ms per file, because a change to a
provider can rebind calls in every file that imports it (a new override
shadowing an inherited method is the canonical case, issue #1229). That is
the same rule the batch incremental path applies; it trades latency for the
guarantee that the graph after `reingest` equals a clean index, which
`codebase_rag/tests/test_reingest.py` checks over a set of single and
composite edits.

## Structural delta overhead

The structural delta (issue #1525) wraps the same re-ingest in two reads
of the touched files' subgraph and the diff, and reports its own cost as
`delta_ms`. Same harness, `--iterations 10`, editing the hub
`codebase_rag/graph_query.py` (48 dependents re-parsed):

| Edited file | reingest p50 | reingest p95 | delta overhead p50 | delta overhead p95 |
|---|---|---|---|---|
| `codebase_rag/graph_query.py` (hub, 48 dependents) | 2,446 ms | 2,527 ms | 142 ms | 677 ms |

The p95 is the first iteration, before the process has warmed the
symbol-resolution and path caches; every later iteration sits within a few
milliseconds of the p50. The overhead is dominated by the backward
test-reach walk (one indexed query per hop, bounded to 12 hops), which is
why the delta walks from the edited symbols instead of building the
project's reverse call graph: that alone cost about 900 ms here.

## Where the time goes

Profiled on the typical case (`cProfile`, so absolute numbers are inflated):

- scoped call pass over the file and its two dependents: ~40%
- definitions pass (tree-sitter parse, function and class ingest): ~15%
- the two graph queries that find dependents and capture inbound edges: ~25%
  in the in-memory store; indexed lookups in Memgraph
- state removal, Rust/C# import bookkeeping, override pass, flush: the rest

Two costs that were not scoped before #1524 and are now:

- `FunctionRegistryTrie._invalidate_ending_with_cache` scanned every cached
  dotted suffix on every registry insert and delete; re-registering a hub
  file's dependents made that 3.7 s alone. The dotted keys are now bucketed
  by their last segment.
- The watcher deleted every `CALLS` edge in the graph and re-ran the call
  pass over every parsed file on each change. It now delegates to
  `reingest`.

## What would make the hub case faster

Re-resolving only the call sites in dependents that resolved into (or could
now resolve into) the changed file, instead of re-parsing whole dependents,
would cut the hub case to the cost of the changed file plus a fraction of
each dependent. That needs the per-site edge properties from #1522 to find
those sites cheaply and is left for a follow-up.

## What a scoped re-ingest does not rebuild

Two passes are repository-wide by construction and are left to
`update_repository`: the code-quality finding analysis (`HAS_SMELL`,
`HAS_VULNERABILITY`, `IMPLEMENTS_PATTERN`) and the URL-to-endpoint link pass
(`RESOLVES_TO`), which drops every network link and rebuilds it from all live
resources. A re-parsed file's finding edges are detached with its old subtree
and come back on the next full update; endpoint links for a changed route are
refreshed the same way. Scoping those two passes is tracked separately.
