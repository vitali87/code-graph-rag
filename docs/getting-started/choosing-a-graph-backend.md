---
description: "Store the code graph in Memgraph (the default) or ArcadeDB, and what changes when you switch."
---

# Choosing a Graph Backend

The knowledge graph can be stored in **Memgraph** or **ArcadeDB**. Memgraph
is the default and the better-tested path — it has been the only backend
for most of this project's history, and it is what the Quick Start and
`cgr daemon up` (which injects `--profile memgraph` unless `GRAPH_BACKEND`
says otherwise) assume. ArcadeDB is a genuine second backend: the same
conformance suite runs against both
engines on every change, and `cgr start --update-graph`/`cgr stats`/`cgr
dead-code` and every other graph command work identically regardless of
which one is active.
Reach for it if you already run ArcadeDB, want a JVM-embeddable engine, or
want a second engine's disk/query characteristics; stay on Memgraph if
you have no reason to switch.

## The `GRAPH_BACKEND` setting

```bash
GRAPH_BACKEND=memgraph   # default; no other setting needed
GRAPH_BACKEND=arcadedb   # switches every graph read/write to ArcadeDB
```

Unset, empty, or absent, this defaults to `memgraph` — a `.env` with only
`MEMGRAPH_*` keys (or no graph settings at all) works exactly as before.

## Installing the driver

ArcadeDB is spoken over its Bolt listener with the `neo4j` Python driver,
which is an optional dependency:

```bash
pip install 'code-graph-rag[arcadedb]'
# or, from a source checkout:
uv sync --extra arcadedb
```

`neo4j` is pinned `>=5.28,<6` — the 6.x line postdates ArcadeDB's published
Bolt certification matrix, so this repo has not verified it against that
major version. Without the extra installed, `GRAPH_BACKEND=arcadedb`
fails fast with a clear "install code-graph-rag[arcadedb]" error rather
than an opaque import failure.

## Mandatory credentials

Unlike Memgraph (which runs without authentication by default in this
project's compose stack), ArcadeDB's Bolt listener rejects unauthenticated
connections outright. Both of these are required when
`GRAPH_BACKEND=arcadedb`:

```bash
ARCADEDB_USERNAME=root
ARCADEDB_PASSWORD=              # set to something; empty fails the guard
```

The rest have working defaults:

```bash
ARCADEDB_HOST=localhost
ARCADEDB_BOLT_PORT=7687
ARCADEDB_HTTP_PORT=2480         # schema DDL only; MERGE traffic goes over Bolt
ARCADEDB_DATABASE=codegraph
ARCADEDB_TX_TIMEOUT_S=600       # wall-clock write-transaction budget
ARCADEDB_HTTP_SCHEME=http       # http or https; see below
ARCADEDB_BOLT_SCHEME=bolt       # bolt, bolt+s, or bolt+ssc; see below
```

## Two transports, two independent guards

ArcadeDB is spoken over **two** connections, and both carry the same
`ARCADEDB_USERNAME`/`ARCADEDB_PASSWORD`:

- **HTTP** (`ArcadeHttpClient`), used only for schema DDL (`CREATE VERTEX
  TYPE`, `CREATE INDEX`, ...). Sends Basic auth on every request.
- **Bolt** (`ArcadeDBIngestor`, via the `neo4j` driver), used for every
  Cypher read and write — this is where all graph data and every generated
  query actually travel.

Both refuse to start in plaintext against a non-loopback host, and the two
guards are independent — hardening one does nothing for the other:

- If `ARCADEDB_HTTP_SCHEME=http` (the default, matching the container this
  project ships, which binds to loopback only) and `ARCADEDB_HOST` is not a
  loopback address (`localhost`, `127.0.0.1`, `::1`), `ArcadeHttpClient`
  refuses to start rather than send Basic auth over the network in the
  clear.
- If `ARCADEDB_BOLT_SCHEME=bolt` (the default) and `ARCADEDB_HOST` is not a
  loopback address, `ArcadeDBIngestor` refuses to start rather than send
  Bolt credentials — and all graph data — over the network in the clear.

**A remote ArcadeDB therefore needs both settings changed, not just one.**
Setting only `ARCADEDB_HTTP_SCHEME=https` satisfies the HTTP guard but
leaves Bolt, which carries everything else, wide open:

```bash
ARCADEDB_HTTP_SCHEME=https      # HTTP client: TLS
ARCADEDB_BOLT_SCHEME=bolt+s     # Bolt driver: TLS, verified certificate
# or, for a self-signed certificate:
ARCADEDB_BOLT_SCHEME=bolt+ssc   # Bolt driver: TLS, self-signed certificate
```

`bolt+s` and `bolt+ssc` are the `neo4j` driver's own URI schemes, not
something specific to this project — `bolt+s` verifies the server's
certificate against a trusted CA, `bolt+ssc` accepts a self-signed one.
Either requires the ArcadeDB server itself to have TLS enabled on its Bolt
listener; setting `ARCADEDB_BOLT_SCHEME` alone does not turn on server-side
TLS, it only tells the client which protocol to speak.

Alternatively, reach a remote ArcadeDB through a loopback-bound tunnel
(e.g. SSH port forwarding) and leave both settings at their plaintext
defaults — from the client's point of view `ARCADEDB_HOST` is then
`localhost`, which both guards trust.

**What this project has and has not verified:** the loopback/non-loopback
guard logic itself is covered by unit tests for both transports. Neither
`ARCADEDB_HTTP_SCHEME=https` nor `ARCADEDB_BOLT_SCHEME=bolt+s`/`bolt+ssc`
has been exercised end-to-end against a real TLS-enabled ArcadeDB server —
this repository has no such container in its test infrastructure. Treat
the TLS schemes as correctly *shaped* (the neo4j driver and ArcadeDB both
document them), not as a path this project has proven works.

## Running the container

`codebase_rag/docker-compose.yaml` defines both engines behind mutually
exclusive Compose profiles, `memgraph` and `arcadedb`:

```bash
ARCADEDB_PASSWORD='your-password' \
  docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb up -d
```

`cgr daemon up` (and `cgr start --update-graph`, which starts the stack
automatically) already does the equivalent of this for you, reading
`GRAPH_BACKEND` and passing the matching `--profile` — driving the compose
file directly only matters if you want the container running without
going through `cgr`.

## The port-7687 collision

Memgraph and ArcadeDB both default to Bolt port 7687, and Compose has no
"default-active" profile — a service carrying *any* profile is excluded
unless that profile is explicitly activated, so a bare `docker compose up`
against this file starts only the backend-agnostic `qdrant` service.
**Never bring both profiles up at once**: they collide on the same
published port, and whichever container loses the bind silently serves
nothing on 7687 while the other looks fine. Switching backends means
tearing one profile down before bringing the other up:

```bash
docker compose -f codebase_rag/docker-compose.yaml --profile arcadedb down
docker compose -f codebase_rag/docker-compose.yaml --profile memgraph up -d
```

## Under the hood

See [Graph Backend Abstraction](../architecture/graph-backends.md) for how
`GraphIngestor` and `GraphDialect` route every graph operation through the
selected backend, and the specific engine differences the ArcadeDB path
has to account for.
