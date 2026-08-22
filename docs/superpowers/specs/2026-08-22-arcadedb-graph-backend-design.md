# ArcadeDB as a pluggable graph backend

**Date:** 2026-08-22
**Status:** Approved design, pending implementation plan

## Summary

Extract the graph-storage layer behind a `GraphIngestor` protocol and a small
`GraphDialect`, then add ArcadeDB as a second implementation alongside Memgraph.
Memgraph stays the default. Both backends are maintained and tested against the
same conformance suite, so neither can drift.

## Why this is feasible

Every database call already funnels through one class, `MemgraphIngestor` in
`codebase_rag/services/graph_service.py`. The parsers and `graph_updater.py`
(95KB, one Cypher-shaped string in it) only ever call `ensure_node_batch()` and
`ensure_relationship_batch()`. Eight non-test modules import the ingestor.

The eval harness independently confirms the seam is in the right place:
`evals/cgr_graph.py` defines `_CapturingIngestor`, an in-memory stand-in that
duck-types those same two methods. This design formalizes an abstraction the
codebase already invented informally.

## Verified behaviour

Probed against a live ArcadeDB 26.8.1 server, not inferred from documentation.
Probe types were created and dropped.

### Ports unchanged

- The node-ingest query, verbatim:
  `UNWIND $batch AS row MERGE (n:L {qualified_name: row.id}) SET n += row.props RETURN count(n) AS created`
  Works, auto-creates the vertex type, idempotent on re-run.
- The relationship-ingest query shape (`MATCH a, MATCH b, MERGE (a)-[r:T]->(b), SET r += row.props, RETURN count(r)`).
- `delete_project`'s exact shape: `OPTIONAL MATCH` over variable-length paths with a
  multi-variable `DETACH DELETE p, container, defined`. Removed all three vertices
  correctly. This was the riskiest existing query.
- `id(n)` returns an **integer** (an encoded RID), not a `#12:0` string.
- `labels(n)`, `labels(n)[0]`, `properties(n)`, `type(r)`, `keys(n)`,
  label disjunction `(n:Class|Method)`, `[:A|B*1..3]`, `[:T*]`, `OPTIONAL MATCH`,
  `WHERE NOT (n)--()`, `STARTS WITH`, `UNWIND labels(n)`.

### Breaks

| Construct | Result on ArcadeDB |
|---|---|
| `CREATE CONSTRAINT ON (n:L) ASSERT n.p IS UNIQUE` | parse error |
| `DROP CONSTRAINT ON (n:L) ASSERT ...` | parse error |
| `CREATE INDEX ON :L(p)` | parse error |
| `SHOW CONSTRAINT INFO` | Memgraph-only |
| `QUERY MEMORY LIMIT n MB` suffix | parse error |
| `CALL pagerank.get()` and the rest of MAGE | unknown procedure |

Indexing additionally requires a declared schema property first: `CREATE INDEX`
failed with *"the property does not exist"* until `CREATE PROPERTY L.p STRING`
was run. Both are SQL, and the Bolt listener accepts Cypher only.

### ArcadeDB procedure surface

`algo.pageRank` (yields `node, score`), `algo.scc`, `algo.wcc`, `algo.louvain`
resolve and return results. `algo.betweennessCentrality` does **not**, despite
betweenness appearing in ArcadeDB's published algorithm list. The Cypher `CALL`
surface is undocumented — the docs show only the Java `GraphAlgorithms` API — so
the catalog must be enumerated by probing a running server.

`algo.*` yields RIDs as strings (`"#46:0"`), not node objects. MAGE's `YIELD node`
hands back a node whose properties can be read directly; ArcadeDB's does not.

## Architecture

`MemgraphIngestor` is public API — `cgr/__init__.py` lists it in `__all__` — so
`services/graph_service.py` must survive as a re-export shim. That is a
constraint, not a convenience.

```
codebase_rag/services/graph/
  protocol.py     GraphWriter(Protocol), GraphIngestor(GraphWriter, Protocol)
  dialect.py      GraphDialect(Protocol)
  memgraph.py     MemgraphIngestor + MemgraphDialect   (moved, near-unchanged)
  arcadedb.py     ArcadeDBIngestor + ArcadeDBDialect   (new)
  arcade_http.py  ArcadeHttpClient                     (SQL over HTTP, DDL only)
  factory.py      get_ingestor() / get_dialect()

codebase_rag/services/graph_service.py   -> shim, re-exports MemgraphIngestor
codebase_rag/cypher_queries.py           -> unchanged, shared by both backends
```

### GraphWriter and GraphIngestor

The protocol splits in two. `GraphWriter` is the narrow sink — exactly
`ensure_node_batch` and `ensure_relationship_batch`. `GraphIngestor` extends it
with connection and query concerns.

The split exists because `evals/cgr_graph.py`'s `_CapturingIngestor` already
implements precisely those two methods and nothing else. With the split it can
declare `GraphWriter` and be type-checked against it, instead of duck-typing a
surface it only partially satisfies. `graph_updater.py` and the parsers likewise
only need `GraphWriter`, which narrows what they can reach for.

`GraphIngestor` is not a new surface. It is the existing one, extracted verbatim: `__enter__`,
`__exit__`, `__aenter__`, `__aexit__`, `ensure_constraints`, `ensure_node_batch`,
`ensure_relationship_batch`, `flush_nodes`, `flush_relationships`, `flush_all`,
`fetch_all`, `execute_write`, `clean_database`, `list_projects`,
`list_project_roots`, `delete_project`, `export_graph_to_dict`.

Nothing above the seam changes. `graph_updater.py`, the parsers, `main.py`,
`mcp/`, and `realtime_updater.py` keep calling exactly what they call today.

### GraphDialect

Carries only genuine divergence — six members:

| Member | Memgraph | ArcadeDB |
|---|---|---|
| `ensure_schema(labels, keys, rel_types)` | `CREATE CONSTRAINT` + `CREATE INDEX` over Bolt | `CREATE VERTEX TYPE` / `PROPERTY` / `INDEX ... UNIQUE` + `CREATE EDGE TYPE` over HTTP |
| `apply_query_limit(q)` | appends `QUERY MEMORY LIMIT n MB` | identity |
| `procedure_catalog` | the MAGE block in `prompts.py` section 2b | the `algo.*` block |
| `allowed_proc_prefixes` | MAGE prefixes | `algo.` |
| `is_benign_error(exc)` | current substring match | ArcadeDB's wording |
| `is_retryable(exc)` | always `False` | concurrent-modification / transient |

Everything else stays shared. `cypher_queries.py` holds 41 query constants and
only the handful above diverge, so a Cypher fix lands once.

### Schema bootstrap

ArcadeDB's `ensure_schema` generates, all `IF NOT EXISTS`:

- 20 vertex types, from `NodeLabel`
- 20 property declarations + 20 unique indexes, from `_NODE_LABEL_UNIQUE_KEYS`
- 25 edge types, from `RelationshipType`

No hand-maintained second schema list. The existing guard in
`constants/graph.py` that rejects any `NodeLabel` missing a unique key keeps the
generated schema in sync for free.

## Transport

Bolt for data, HTTP for DDL.

`neo4j.Driver` (`bolt://host:7687`) carries all `MERGE`/`MATCH` traffic — it is
the certified path, streams, and binds parameters properly. `ArcadeHttpClient`
(`http://host:2480`) is touched only inside `ensure_schema()`: POST SQL to
`/api/v1/command/{db}`, raise on non-2xx. It has no role in the hot path and no
reason to grow.

Bolt-only is not viable: without SQL there is no way to create an index, and
every `MERGE` degrades to a full type scan, making ingestion quadratic.

## Ingestor internals

**Parallel flush simplifies.** `_flush_node_group_with_own_conn` currently calls
`_create_connection()` per label group and closes it after. `neo4j.Driver` is
thread-safe and pools internally, so that becomes `with driver.session() as s:`
per group — same parallelism, no hand-rolled connection lifecycle. The
`_conn_lock` guarding a single shared connection is unnecessary for ArcadeDB;
only buffer mutation needs guarding.

**Result mapping normalizes at the boundary.** `_cursor_to_results` reads
`cursor.description[].name` plus `fetchall()`; the neo4j path is `result.keys()`
plus `dict(record)`. Both produce `list[ResultRow]`, so `dead_code.py`,
`graph_audit.py`, `graph_loader.py`, and the MCP tools are untouched.

**No embedding migration.** `id(n)` returns an int on ArcadeDB, and
`vector_store.py` keys its Qdrant/Milvus payloads on that int. It keeps working
unchanged. This was the one thing that could have forced a change outside the
graph package.

**Auth inverts.** ArcadeDB's Bolt listener rejects the `none` scheme. The current
both-or-neither check (`AUTH_INCOMPLETE`) stays for Memgraph but becomes
*required* for ArcadeDB — a missing credential is a construction-time config
error, not a later connection failure.

**Retry is the one new mechanism.** ArcadeDB is MVCC/optimistic. Parallel `MERGE`
of many `CALLS` edges converging on one hot `Function` vertex raises
concurrent-modification errors that Memgraph's engine does not produce. Today
`_flush_*` logs and re-raises the first error. A shared `retry_on_transient()`
wrapper around the batch execute, driven by `dialect.is_retryable(exc)`, with
bounded attempts and jittered backoff, re-raising on exhaustion so failures still
surface. Memgraph returning `False` makes the loop inert on the default path —
zero behaviour change unless ArcadeDB is selected.

**Two smaller notes.** `clean_database`'s `MATCH (n) DETACH DELETE n` works but
leaves ArcadeDB type definitions behind; harmless, since `ensure_schema` is
idempotent and reuses them. `_migrate_legacy_path_keys()` (the issue #897
constraint migration) is Memgraph-only by construction and is a documented no-op
on the ArcadeDB dialect.

## LLM and query layer

Three call sites read from the dialect: `prompts.py` section 2b
(`procedure_catalog`), `services/llm.py`'s `_validate_cypher_read_only`
(`allowed_proc_prefixes`, replacing the module-level
`CYPHER_ALLOWED_PROCEDURE_PREFIXES` import), and `fetch_all`
(`apply_query_limit`).

**The catalog is written from probe results, not documentation.** The
implementation includes an explicit discovery step: probe a running ArcadeDB
server for real procedure names and signatures, and write
`ArcadeDBDialect.procedure_catalog` from what answers. Guessing from the docs
produces a prompt that instructs the model to call procedures that do not exist.
The fragment must also state that `algo.*` yields RID strings, or the model will
write `node.qualified_name` and get nothing.

**The memory guard needs a substitute.** `QUERY MEMORY LIMIT` exists to contain
runaway LLM-generated queries; ArcadeDB has no equivalent. Dropping it silently
trades a hard bound for nothing. `ArcadeDBDialect.apply_query_limit` stays
identity, and the ArcadeDB read path passes a **transaction timeout** on the
driver call. That bounds wall clock rather than memory — a weaker guarantee, but
a real one.

**Cosmetics without a dialect member.** `build_graph_schema_and_rules()` opens
with "a Memgraph knowledge graph" and `_format_active_projects_block` says "This
Memgraph database". Both are neutralized to "knowledge graph", correct for either
backend, rather than adding a `display_name` member.

## Configuration and packaging

Backend selection follows the `VectorStoreBackend` precedent: a `GraphBackend`
StrEnum (`memgraph` | `arcadedb`) in `constants/providers.py`, driven by a
`GRAPH_BACKEND` setting defaulting to `memgraph`.

Settings are additive. The six `MEMGRAPH_*` settings stay as they are — renaming
them would break every existing `.env` for no benefit. Alongside them:
`ARCADEDB_HOST`, `ARCADEDB_BOLT_PORT` (7687), `ARCADEDB_HTTP_PORT` (2480),
`ARCADEDB_USERNAME`, `ARCADEDB_PASSWORD`, `ARCADEDB_DATABASE`.

`ARCADEDB_DATABASE` has no Memgraph counterpart — Memgraph is single-database,
ArcadeDB is multi. It is required when the backend is selected. The factory reads
only the set matching the chosen backend.

**One deliberate divergence from the precedent.** `_get_vector_store()` returns
`None` and warns when its dependency is missing, because semantic search
degrades gracefully. A missing graph backend is not degradable. `get_ingestor()`
**raises** with an actionable message (`pip install code-graph-rag[arcadedb]`)
rather than returning `None`. Same `has_*()` detection helper
(`has_neo4j_driver()` in `utils/dependencies.py`), opposite failure policy.

New extra: `arcadedb = ["neo4j>=5.28"]`. `pymgclient` stays a hard dependency
since Memgraph remains the default.

### Compose

Both engines default to port 7687, so they cannot both run unprofiled.
`docker-compose.yaml` gains an `arcadedb` service under a compose profile, with
Memgraph under a `memgraph` profile active by default — existing
`docker compose up` behaviour is unchanged.

```yaml
arcadedb:
  image: arcadedata/arcadedb
  profiles: ["arcadedb"]
  environment:
    ARCADEDB_SERVER_ROOT_PASSWORD: ${ARCADEDB_PASSWORD}
    JAVA_OPTS: >-
      -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin
  ports: ["7687:7687", "2480:2480"]
```

### Stack and health

`SERVICE_MEMGRAPH`, `wait_for_memgraph`, `StackStatus.memgraph_reachable`,
`StackStatus.memgraph_endpoint`, `MSG_STACK_HEALTHY`'s `{memgraph}` placeholder,
and `HealthChecker.check_memgraph_connection` all hardcode the engine. These are
internal names, so they are neutralized (`graph_reachable`, `graph_endpoint`,
`check_graph_connection`) and dispatch on `GRAPH_BACKEND`.

The ArcadeDB health probe checks **both** ports. A working Bolt listener with a
dead HTTP endpoint would pass startup and then fail at `ensure_constraints()`.

## Testing

### Wall-clock mitigation

The integration suite is pinned to a single `xdist_group("memgraph-integration")`
because each test wipes the database. Making that group **per-backend**
(`graph-integration-memgraph`, `graph-integration-arcadedb`) lets the two
backends run concurrently on separate xdist workers. Each backend still runs its
36 tests serially, but the two backends overlap, so wall clock stays near 1x
instead of 2x whenever CI has two workers.

### Conformance suite

`codebase_rag/tests/integration/test_graph_backend_conformance.py`, parametrized
over both backends, pins the contract the protocol only implies:

- MERGE idempotency — the same node twice yields one node
- unique-key enforcement per `NODE_UNIQUE_CONSTRAINTS`
- relationship properties round-trip
- `FLOWS_TO` parallel-edge preservation via `MERGE_KEY_PROPS_BY_REL`'s
  `(via, kind)` — issue #722's regression, and the subtlest thing a new backend
  could quietly break
- parallel flush against a hot target vertex, which exercises the retry path
- `delete_project` plus orphan and unanchored-resource pruning
- `export_graph_to_dict`, and `id(n)` returning an int

### Query corpus gate

Nothing today verifies that generated Cypher actually runs. The eval harness runs
against `_CapturingIngestor` and never touches a database, so this blind spot
exists on Memgraph too.

The gate is a fixed list of roughly 30 to 40 representative queries — the
`CYPHER_EXAMPLE_*` constants, the prompt's few-shots, and the shared strings in
`cypher_queries.py` and `constants/graph.py` — executed against both backends,
asserting each parses and returns the expected column shape. Queries run against
a small seeded fixture graph built through the ingestor, so column shapes are
assertable rather than trivially empty. Deterministic, no LLM in CI, and it
catches dialect divergence at the layer where it bites.

Genuine natural-language-to-Cypher quality measurement stays out of PR CI
(nondeterministic, costs tokens) and becomes an opt-in script.

### Existing test churn

`test_graph_service.py` patches `codebase_rag.services.graph_service.mgclient` in
three places; `test_memgraph_batching.py` and `test_health_checker.py` are
similar. `patch()` resolves against the module where the name is looked up, so
the re-export shim does not save them — they repoint to `services.graph.memgraph`.

## Rollout

Six phases, each independently mergeable.

| # | Phase | ArcadeDB present |
|---|---|---|
| 1 | Extract `GraphIngestor` and `GraphDialect`; Memgraph implements both | no |
| 2 | Conformance suite and corpus gate, Memgraph only | no |
| 3 | `ArcadeDBDialect`, `ArcadeHttpClient`, schema bootstrap | dialect only |
| 4 | `ArcadeDBIngestor`; conformance green on both | yes |
| 5 | Probe server, write real `algo.*` catalog, wire prompts and security | yes |
| 6 | Parametrize the 36 integration tests; compose profiles; docs | yes |

Phases 1 and 2 contain no ArcadeDB code at all — pure refactor plus new tests. If
the full suite stays green through phase 2, the extraction is faithful, and phase
3 onward builds against a contract that already exists.

## Acceptance criteria

- Conformance suite, corpus gate, and all 36 integration tests green on both
  backends.
- One real repository indexed end to end on ArcadeDB and spot-checked.
- With `GRAPH_BACKEND` unset, behaviour identical to today.

## Out of scope

- Vector-store consolidation onto ArcadeDB's native vector index. `vector_store.py`
  is an independent subsystem keyed only by `node_id`; folding it in would double
  this design. Worth revisiting afterward — it would collapse three services into
  one.
- Flipping the default to ArcadeDB.
- Retiring the Memgraph path.
- Full behavioural parity for `algo.*` algorithms the prompt rarely reaches for.
