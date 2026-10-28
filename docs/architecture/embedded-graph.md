---
description: "The embedded SQLite graph that cgr uses by default, how it runs Cypher, and how it compares with Memgraph."
---

# Graph Backends

Code-Graph-RAG stores its knowledge graph in one of three engines, chosen with
`GRAPH_BACKEND`:

| Backend | `GRAPH_BACKEND` | Needs | Install |
|---|---|---|---|
| Embedded graph (default) | `sqlite` | nothing: a file under `~/.cgr` | included |
| Memgraph | `memgraph` | Docker, the bundled stack (`cgr daemon up`) | `code-graph-rag[memgraph]` |
| Neo4j 5 | `neo4j` | a Neo4j server you run | `code-graph-rag[neo4j]` |

All three take the same Cypher: the ingestor, the query tools and the
LLM-generated queries are unchanged between them. The engine-specific
statements (index and constraint DDL, memory limits, the procedure catalogue
in the prompt) live in `codebase_rag/graph_dialects.py` and
`codebase_rag/prompts.py`.

## Why the default is embedded

A first run used to need Docker, a running Memgraph container and, wherever no
`pymgclient` wheel fit the platform, `cmake` to build the driver. Each was a
place to fail before the first query (issue #386). The embedded graph needs
none of them: `cgr start` writes `~/.cgr/graph.sqlite` and queries it in
process. SQLite ships with Python, so there is nothing else to install.

## How it works

`codebase_rag/embedded_graph` is a Cypher engine over SQLite:

- **Storage.** One table of nodes and one of relationships, each with a JSON
  property map. A node's unique key (`NODE_UNIQUE_CONSTRAINTS`, e.g. a
  function's `qualified_name`) is lifted into its own indexed column, so every
  `MERGE` and every `{qualified_name: $qn}` lookup is an index seek.
  Relationships are indexed by `(src, type)` and `(dst, type)`.
- **Reads.** A read query compiles to one SQL statement of chained CTEs, one
  per clause: `MATCH` joins the node and relationship tables onto the bindings
  so far, `OPTIONAL MATCH` left-joins a match over its distinct inputs,
  `WITH`/`RETURN` project, group and sort. Variable-length patterns are
  recursive CTEs that keep each path's relationships, so no relationship
  repeats in a path, as Cypher requires. Compiled plans are cached by query
  text and parameter types. Every piece of SQL the engine emits is spelled
  in one module, `embedded_graph/sql.py`, and values reach SQLite only as
  bound parameters.
- **Writes.** `CREATE`, `MERGE` (with `ON CREATE`/`ON MATCH`), `SET`,
  `REMOVE`, `DELETE`/`DETACH DELETE` and `FOREACH` run clause by clause inside
  one transaction, so a failed statement changes nothing.
- **Procedures.** The graph algorithms the prompt offers run under their
  MAGE names: `pagerank.get`, `degree_centrality.get`,
  `weakly_connected_components.get`, `nxalg.strongly_connected_components`,
  `nxalg.simple_cycles`, `nxalg.find_cycle`, `nxalg.topological_sort`,
  `nxalg.shortest_path`, `betweenness_centrality.get`,
  `graph_util.ancestors` and `graph_util.descendants`.
- **Stable plans.** SQLite's planner chooses join orders from statistics.
  Measured while a sync is still writing, they describe a nearly empty graph,
  and plans built on them scan instead of seek. The engine instead pins the
  statistics measured on a real 38k-node cgr graph, so every connection plans
  for that shape (unique keys, a few relationships per node) at any size.
- **Generated queries stay read-only.** `EXPLAIN` reports Memgraph's operator
  names, so the plan check every backend uses refuses a write before it runs.
  The query then runs on a connection SQLite opened read-only, which refuses
  any write the check could have missed.

Semantics follow Memgraph where engines differ: nulls sort last ascending and
first descending, `1 IN [null]` is false, `collect()` drops nulls, integer
division truncates, `SET n += {k: null}` removes `k`, and an unaliased column
is named by its source text.

### What it does not support

- Path variables (`MATCH p = ...`), `nodes(p)`, `relationships(p)` and
  `shortestPath(...)`; `nxalg.shortest_path` covers the last. The prompt tells
  the model so.
- More than one label per node (cgr never writes one).
- `exists(n.prop)` is accepted, where Memgraph rejects it; `n.prop IS NOT NULL`
  is the portable spelling.

## Parity with Memgraph

Measured against Memgraph 3.3:

- **Semantics.** 53 probe queries covering the differences above (null
  ordering, three-valued logic, path uniqueness, `MERGE` behaviour, ...) return
  exactly what Memgraph returned. They run in the unit suite
  (`test_embedded_graph_memgraph_parity.py`).
- **Every read query cgr ships.** On a real graph of 38,183 nodes and 166,107
  relationships, loaded through cgr's own batch writes, all **154 of 154**
  read queries in the package return the same rows as Memgraph. Their total
  run time was **39.9 s embedded (33.0 s with the Rust extension below),
  221.5 s on Memgraph**. Loading that graph took 3.1 s for nodes and 6.9 s for
  relationships.

- **Writes, end to end.** Indexing the same repositories with
  `cgr start --update-graph` into Memgraph and into the embedded graph gave the
  same nodes and relationships: 748 nodes and 2,473 relationships for `click`
  (sync 3.7 s embedded, 4.1 s Memgraph), 1,397 and 7,187 for `rich` (8.5 s,
  8.8 s). The only differences were the sync timestamp, and the `resolution`
  label of 3 `CALLS` edges in `rich`, which also differs between two runs on
  the same backend.

## Scale

SQLite reads only the pages a query touches, so memory follows the working
set rather than the graph's size. Measurements taken while choosing the
design, on the same storage layout:

- The Kubernetes repository (2.9M lines) indexed into Memgraph in 27.6 minutes,
  bounded by Memgraph accepting about 1,000 relationships a second; the same
  graph loaded into SQLite in 21.9 s. Exporting one project's relationships
  took 3.0 s from SQLite, and Memgraph did not finish within its 600 s
  timeout.
- Replicating that graph 100 times (3.77M nodes, 16.6M relationships), SQLite
  served per-project queries at flat latency in 595 MB of memory; Memgraph
  held 3.98 GiB at 50 times.

One graph file serves every indexed project, as one Memgraph instance does.
Any number of processes can read it at once; writes take turns (a writer waits
up to 60 s for another to finish), so the ingestor flushes on one connection
instead of a pool of workers.

## The Rust extension

`rust/cgr_graph_rs`, published as `cgr-graph-rs`, is the engine's native
half, written with [PyO3](https://pyo3.rs) and built with
[maturin](https://www.maturin.rs), the way uv and ruff are shipped. It is a
dependency of code-graph-rag, installed as a prebuilt wheel, so no Rust
toolchain is needed, on:

| Platform | Wheel |
|---|---|
| Linux x86_64 and aarch64, glibc 2.17+ | `manylinux2014` |
| Linux x86_64 and aarch64, musl (Alpine) | `musllinux_1_2` |
| macOS 10.13+, Intel and Apple silicon | `universal2` |
| Windows x64 | `win_amd64` |

Each wheel is built against Python's stable ABI, so one per platform serves
every CPython from 3.12 on. Anywhere else (PyPy, 32-bit or other
architectures) code-graph-rag installs without it and the engine runs the
Python twin of every part, which stays the reference: each Rust path is
tested against it, and gives the same answers.

A checkout builds the crate it carries: `uv sync` compiles it with the Rust
toolchain on your `PATH` (uv takes it as a path source), so a change to the
Python and Rust halves is tested together. `.github/workflows/rust-wheels.yml`
builds the wheels. A release uploads any of the crate version's wheels PyPI
does not have yet, then publishes code-graph-rag only once PyPI has every one
of them (`scripts/check_cgr_graph_rs_on_pypi.py`). The crate has no sdist, so
a platform without its wheel could not install that code-graph-rag. `make
release`, which publishes from a workstation, cannot build every platform's
wheel, so it stops until PyPI has them all.

It runs PageRank, degree centrality, weakly and strongly connected
components, betweenness centrality, ancestors and descendants, and returns
the same records as the Python implementations, floats included (the suite
checks this on random graphs).

The other procedures, and any statement that writes before its `CALL`, stay
in Python: the extension reads the file on its own connection, which only sees
committed data. So that it reads the graph its statement reads, every
statement that writes bumps a counter in `cgr_meta` (`graph_version`), and the
extension answers only at the version the statement sees; when another process
has committed since, the engine runs the procedure itself. Procedures stop at
the query's timeout where the Python ones check it.

The extension also carries a port of the engine's Cypher lexer and parser,
which the engine uses when it is installed: it builds the very syntax tree the
Python parser builds, or raises the same `CypherSyntaxError` (offset and
message), about ten times faster. Parsed queries are cached either way, so
this speeds up the first run of each query, such as one a model just wrote.
The Python parser stays the reference: the port hands it the rare query whose
meaning rests on Python itself (a non-ASCII identifier, an integer beyond 64
bits, very deep nesting), and the suite compares the two on every query cgr
ships and thousands of mutations of them.

Two more hot paths use the extension when it is installed. Each is checked
against the Python path, which stays the reference and the fallback:

- **The ingestor's batches.** The node and relationship MERGE batches the
  indexer flushes are written in one transaction, row by row in the engine's
  order and through SQLite's own JSON functions, so the file comes out the
  same: every row, id and property text. The suite replays random batches
  both ways and compares the tables, and indexing this repository (37k
  nodes, 158k relationships), recorded and replayed through each writer,
  gave identical tables. Replaying that run's ingestor calls took 7.9 s
  instead of 21.0 s, and writing a 38k-node graph's batches 6.8 s instead of
  22.4 s. A batch with a value the extension cannot write exactly as the
  engine would, such as a non-finite float, an integer beyond 64 bits or a
  boolean key, is written by the engine.
- **Result decoding.** The JSON text SQLite returns for nodes,
  relationships, lists and maps is turned into Python values for the whole
  result set at once, with the values and types `json.loads` gives (it hands
  `json.loads` what it cannot decide, such as a lone surrogate). On that
  graph `MATCH (n) RETURN n` takes 0.60 s instead of 1.31 s, and returning
  every relationship 0.80 s instead of 2.24 s.

The extension can also walk variable-length relationships
(`-[:CALLS*1..3]->`), row for row and path for path as the engine's recursive
CTE does, but the engine does not use it: handing the rows back to SQLite
costs about what the walk saves, so on cgr's own graph its shipped walks ran
0.8 to 1.3 times as fast as SQLite's, and wider model-style walks 0.9 to 1.9
times.

Writing to the graph is now 1% to 4% of an index run; the rest is the
indexer's own analysis, embedding inference and start-up. Which of those Rust
could speed up, measured stage by stage, is in
[the indexer's Rust plan](../reports/INDEXER_RUST_PLAN.md).

The indexer uses the extension too. Each definition's anchor hash and
clone-detection fingerprint, which names each Python function binds, and each
file's interface fingerprint (what the files that depend on it can see) are
computed in Rust on the very tree py-tree-sitter parsed: the extension reads
the C node inside the Python `Node` and walks it with the same tree-sitter
runtime, so nothing is parsed twice and no grammar is compiled into the
wheel (`codebase_rag/parsers/native_trees.py` says how, and what guards it).
The Python functions stay the reference, as everywhere else in the
extension.

## Switching backends

The engines do not share data. To keep using a Memgraph graph you already
built, install the driver and select it:

```bash
uv tool install "code-graph-rag[treesitter-full,memgraph]"   # or pip install 'code-graph-rag[memgraph]'
export GRAPH_BACKEND=memgraph
cgr daemon up
```

To move to the embedded graph instead, leave `GRAPH_BACKEND` unset and
re-index each project with `cgr start --repo-path <repo> --update-graph`.

An install upgraded from a version where Memgraph was the default often still
sets `MEMGRAPH_HOST` (or another `MEMGRAPH_*` connection setting) without
`GRAPH_BACKEND`. cgr then opens the embedded graph, where nothing is indexed
yet, and logs a warning once per process saying so. Setting `GRAPH_BACKEND`
to either `memgraph` or `sqlite` settles the choice and silences it.
