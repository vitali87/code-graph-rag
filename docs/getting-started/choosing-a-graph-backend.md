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
```

The schema-DDL HTTP client always speaks plain `http`, with no TLS option,
and sends Basic credentials on every request. That is correct for the
container this project ships, which binds to loopback only — but
`ARCADEDB_HOST` is not validated, so pointing it at a non-loopback host
sends the Basic credentials over plaintext HTTP on the network. Only point
`ARCADEDB_HOST` at a loopback-bound ArcadeDB instance.

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
