---
description: "The GraphIngestor/GraphDialect seam that lets the code graph run on Memgraph or ArcadeDB, and the genuine engine differences it has to paper over."
---

# Graph Backend Abstraction

The code graph can be stored in **Memgraph** (the default) or **ArcadeDB**,
selected with `GRAPH_BACKEND`. Both engines run the same conformance suite
on every change, but Memgraph is the older, better-trodden path — see
[Choosing a graph backend](../getting-started/choosing-a-graph-backend.md)
for the operational tradeoffs. This page documents the seam itself, for
whoever adds a third backend or has to debug the second one.

## The seam

Everything that touches the graph goes through two abstractions in
`codebase_rag/services/graph/`:

- **`GraphIngestor`** (`protocol.py`) — the full storage surface: node/
  relationship ingestion (`IngestorProtocol`, pre-existing), the read/write
  query surface (`QueryProtocol`, pre-existing), plus lifecycle and admin
  operations this project added — `ensure_constraints`, `flush_nodes`,
  `flush_relationships`, `clean_database`, `list_projects`,
  `list_project_roots`, `delete_project`, `export_graph_to_dict`, and
  sync/async context-manager entry and exit. `MemgraphIngestor` and
  `ArcadeDBIngestor` both implement it in full; nothing in the parser or
  RAG-agent layers imports either concrete class.
- **`GraphDialect`** (`dialect.py`) — everything that genuinely differs
  between engines, deliberately kept small. Seven members (the class
  docstring undercounts it as six):

  | Member | Purpose |
  |---|---|
  | `name` | The `GraphBackend` enum value, for display and dispatch. |
  | `ensure_schema(ingestor)` | Create whatever the engine needs before `MERGE` is efficient: unique constraints and indexes on Memgraph; vertex/edge types, property declarations, and unique indexes on ArcadeDB. |
  | `apply_query_limit(query, mb)` | Bound a read query's resource use, or return it unchanged when the engine has no equivalent. |
  | `procedure_catalog` | The prompt fragment listing callable graph-algorithm procedures. |
  | `allowed_proc_prefixes` | Procedure namespaces the read-only guard permits. |
  | `is_benign_error(exc)` | True when the error means "already done" and should not be logged. |
  | `is_retryable(exc)` | True for transient write conflicts worth retrying. |

  `MemgraphDialect` and `ArcadeDBDialect` implement it; `is_retryable` is a
  hard `False` on Memgraph's dialect, so `retry_on_transient` (`retry.py`)
  is a pure pass-through there and only ever loops on ArcadeDB.

`services/graph/factory.py` resolves both from `settings.GRAPH_BACKEND`:
`get_ingestor()` and `get_dialect()`. Every entry point — `cgr start
--update-graph`, `cgr stats`, `cgr dead-code`, `cgr export`, `cgr
delete-project`, the interactive agent, `cgr doctor`'s health check —
reaches the graph through one of these two functions, never by
constructing `MemgraphIngestor` directly. The one exception is
`codebase_rag/main.py::connect_memgraph`, kept under its historical name
because tests patch it by name; it is a one-line wrapper around
`get_ingestor()`, not a hardcoded Memgraph connection.

`cgr.MemgraphIngestor` still imports and is still in `cgr.__all__`: it is
public API, and the shared Cypher in `cypher_queries.py` runs unchanged on
both backends, so nothing about adding ArcadeDB required removing or
renaming it.

## Genuine engine differences

These are the differences the conformance suite exists to pin down, found
while building and hardening the ArcadeDB path. Everything else — schema,
query surface, dead-code analysis, the RAG agent — is identical between
engines by construction.

### 1. Relationship `MERGE` has no backing index

Memgraph's `MERGE` on a relationship pattern can see an in-flight write
from earlier in the *same* `UNWIND`-batched statement, because its engine
backs the check with the same index it uses for lookups. ArcadeDB's
`MERGE` has no such index for relationships (only vertex types get one),
so it cannot see an earlier row's write from that same batch — two rows
with an otherwise-identical merge pattern created two edges on ArcadeDB
where Memgraph converged on one. `ArcadeDBIngestor` dedups client-side
before flushing (`_dedupe_rows_sharing_a_merge_pattern`), collapsing rows
that share the same endpoints and merge-key properties and overlaying
their properties the way Memgraph's own `SET`-after-`MERGE` would, so the
two engines converge on the same graph regardless of engine-level `MERGE`
semantics.

Dedup alone was not sufficient at real-repository scale, though: an
isolated probe against a live server showed that an `UNWIND` batch where
**two or more distinct rows** (not duplicates — genuinely different
edges) target the same vertex can deadlock ArcadeDB's Cypher engine
outright (`Neo.TransientError.Transaction.DeadlockDetected`), and
indexing this repository's own ~1,100-file tree showed the failure mode
is not always a raised exception either — a handful of edges onto a
shared hot target (a test helper called from dozens of files) vanished
from a batched flush with no error and no attempted/created mismatch in
the ingestor's own bookkeeping. Retrying or bisecting an already-sent
batch cannot rule that out; it only reduces how often it is hit.
`ArcadeDBIngestor._chunk_endpoint_disjoint` partitions every relationship
flush into chunks whose rows share no endpoint before anything is sent,
removing the collision outright rather than reacting to it. This costs
nothing for the common case (every row has a unique target, so the whole
group stays in one batch); only a genuinely hot vertex degrades to one
`MERGE` per row for the rows that share it.

### 2. Server bootstrap: root password and role

The `arcadedata/arcadedb` image's `ARCADEDB_ROOT_PASSWORD` environment
variable is a no-op — decompiling the server shows it only reads
`-Darcadedb.server.rootPassword` as a JVM system property. Without it the
server falls back to an interactive `askForRootPassword` prompt that hangs
forever with no TTY attached. The Bolt listener is also a plugin, off by
default, and needs enabling in the same `JAVA_OPTS`. Separately,
`defaultDatabases`' per-database credential entry
(`user:password:role`) needs an explicit `:admin` role suffix: without it
root gets a roleless per-database account and every schema DDL statement
403s with "User ... is not allowed to update schema", even though root is
the server's global superuser. `codebase_rag/docker-compose.yaml`'s
`arcadedb` service sets all three in one `JAVA_OPTS` block.

### 3. No `QUERY MEMORY LIMIT`; timeout rides on a `Query` object

ArcadeDB cannot parse Memgraph's `QUERY MEMORY LIMIT` clause, so
`ArcadeDBDialect.apply_query_limit` returns the query unchanged.
`ARCADEDB_TX_TIMEOUT_S` is the wall-clock substitute, and it has to be
threaded through as a `neo4j.Query(cypher, timeout=...)` object rather
than a `timeout=` keyword on `session.run()` — the bare-kwarg form is
silently ignored by the driver for this server.

## Where the tests live

`test_graph_backend_conformance.py` and `test_query_corpus.py` run the
same assertions against both backends via `pytest.mark.parametrize` (no
backend conditionals in either file); `test_graph_factory.py` covers
`get_ingestor`/`get_dialect` dispatch and the ArcadeDB credential guard in
isolation. `make test-integration-memgraph` and
`make test-integration-arcadedb` run one backend's slice of the full
integration suite.
