# Indexer Rust Port: Measured Plan

The embedded graph already runs its hot paths in Rust (`rust/cgr_graph_rs`,
shipped as the `cgr-graph-rs` wheels). This plan covers the next question:
should the indexing pipeline that fills the graph move to Rust too, and in
what order? It answers from measurements of the current pipeline, not from
estimates, and splits the work into separate PRs. Each PR is measured again
before the next one starts.

`docs/reports/REWRITE_RECOMMENDATIONS.md` found a Rust rewrite unjustified,
because tree-sitter work was 3.1% of CPU time in its profile. That profile
predates the current pipeline and the embedded graph. On today's code the
analysis passes are 90% of a large repository's run, so the question needs
answering again.

## How it was measured

Each repository was indexed from scratch:

```
cgr start --update-graph --clean --yes [--no-embeddings]
```

The runs used the embedded graph, the native extension on, Python 3.12 and
x86_64 Linux. py-spy sampled each one at 200 Hz. Sampling leaves the
interpreter untouched, whereas cProfile inflates pure-Python code against C
two- to three-fold. `benchmarks/index_profile.py` turns a recording into the
tables below, and its docstring has the exact commands.

The corpus has three repositories:

| Repository | Source files | Python lines |
|---|---|---|
| click | 17 | 12 k |
| rich | 100 | 39 k |
| code-graph-rag (its own tree) | 1,728 | 707 k |

Some runs shared the machine with a PyInstaller build, so absolute times are
noisy. Shares of samples are not, and the plan sizes everything by share.

## Where the time goes

### With embeddings (the default)

| Stage | click | rich |
|---|---|---|
| Embedding model inference (torch) | 42.0% | 34.1% |
| Calls pass | 11.6% | 25.3% |
| Definitions pass | 10.0% | 13.3% |
| Source extraction for embeddings | 8.8% | 6.6% |
| Imports and startup | 17.4% | 11.0% |
| Graph store (Rust ingest and SQLite) | 1.6% | 2.0% |
| Other | 8.7% | 7.6% |

### Without embeddings

| Stage | click | rich | code-graph-rag |
|---|---|---|---|
| Calls pass | 29.6% | 38.9% | 52.9% |
| Definitions pass | 17.9% | 23.9% | 37.3% |
| Imports and startup | 38.7% | 29.2% | 1.8% |
| Graph store (Rust ingest and SQLite) | 4.4% | 2.1% | 1.4% |
| Other | 9.4% | 5.9% | 6.6% |

Indexing code-graph-rag's own tree took 288 s without embeddings.

### Hottest functions, indexing code-graph-rag

These are self-time shares, attributed to the innermost code-graph-rag frame:

| Share | Function | What it is |
|---|---|---|
| 15.1% | `parsers/utils.py:sorted_captures` | tree-sitter query execution, mostly on behalf of the lookup below |
| 5.0% | `parsers/cpp/preproc_recovery.py:parse_with_preproc_recovery` | C/C++ preprocessor recovery |
| 4.1% | `parsers/anchor_hash.py:anchor_hash` | subtree hashing |
| 3.9% | `parsers/stdlib_extractor.py:_python_module_has_entity` | stdlib membership checks |
| 3.3% | `parsers/utils.py:_scan_invoked_parameters` | tree walk |
| 3.2% | `parsers/py/ast_analyzer.py:_binding_targets` | Python scope analysis |
| 2.6% | `parsers/py/ast_analyzer.py:_declared_non_local` | Python scope analysis |
| 2.4% | `graph_updater.py:_hash_file_with_bytes` | file hashing |
| 2.3% | `parsers/py/ast_analyzer.py:_nonlocal_names` | Python scope analysis |
| about 4.3% | `parsers/ast_fingerprint.py` (three functions) | subtree fingerprints |
| about 3.0% | `parsers/interface_fingerprint.py` (four functions) | interface fingerprints |

Of the `sorted_captures` time, 9.4 points come from
`_find_python_method_in_ast`. It reruns the class query over the whole file,
and the function query over each matching class body, every time a call's
return type needs a method node.

## What Rust can and cannot speed up

- **Embedding inference** is 34% to 42% of a default run. It is already
  native code in torch, so Rust gains nothing there. Batching, a smaller or
  quantised model, or ONNX Runtime are the levers, and they are out of scope
  here.
- **Imports and startup** are 29% to 39% of a small repository's run without
  embeddings. Most of that is `vector_store.py` importing `qdrant_client` and
  `pymilvus` at module load, even when the backend is SQLite. On click that is
  14.1% and 7.5% of the run; on rich, 10.0% and 9.6%. `main.py` also imports
  `pydantic_ai` (3.8% to 8.0%). This is fixed in Python by importing them where
  they are used.
- **The graph store** is already Rust and SQLite, at 1.4% to 4.4%. Nothing is
  left to gain there.
- **The analysis passes** (calls and definitions) are what remains. They are
  90% of a large repository's run, and they are pure-Python tree walks: every
  `node.children`, `node.type` and `node.text` crosses into C and allocates a
  Python object. This is the work Rust does well. Parsing itself is cheap:
  `Parser.parse` is 0.2% to 0.3% of the run.

## The plan

Every step is its own PR. Each one re-records the three repositories with
`benchmarks/index_profile.py` and puts the before-and-after table in its
description. Python fixes go first: they are cheaper, and they change the
shares the Rust steps are sized by.

### 1. Python: lazy vector-store and agent imports

Import `qdrant_client`, `pymilvus` and `pydantic_ai` in the functions that
use them, not at module load. The type-only names already sit behind
`TYPE_CHECKING`. The module-level bindings that the tests patch need a small
accessor instead.

Expected gain: 17% to 22% off a small repository's run without embeddings
(about 1.5 s per run on click), and nothing measurable on a large one.

### 2. Python: index definitions once per parsed file

Replace the repeated queries in `_find_python_method_in_ast`,
`_find_function_ast_node` and `_find_class_node` with one map per tree,
built on first use:

```
class name → {method name → node}
function name → node
```

Cache it beside the parsed file. Also memoise
`_python_module_has_entity` per (module, entity).

Expected gain: about 13 points of the code-graph-rag run (9.4 + 2.5 + 0.9
from the query lookups, plus up to 3.9 from the stdlib checks, minus the cost
of building the map).

Parity: the indexer's existing test suite, plus a graph dump of the three
repositories before and after, compared node for node and edge for edge.

### 3. Rust: the parity harness and the first stage, fingerprints and anchor hashes

These go in the same crate, behind the same switch-per-path design as the
graph engine (`EG_RUST_*`): each Rust path has a Python twin, and the twin is
the fallback.

**How Rust gets the tree.** Rust parses the source itself with the
`tree-sitter` crate, using the grammar versions the Python wheels pin
(`tree-sitter` 0.26.0, `tree-sitter-python` 0.25.0 today). py-tree-sitter
exposes no stable pointer to its `TSTree`, so the tree cannot be shared, and
parsing again costs less than 0.3% of the run. Python passes the source bytes
and the byte ranges of the nodes it wants. Rust returns plain tuples (hashes,
names, spans), so no Python objects are allocated per node.

**Stage 1** covers `anchor_hash`, `ast_fingerprint` and
`interface_fingerprint` for Python sources. Each is a pure function of a
subtree, about 11 points of the code-graph-rag run, and has no dependency on
registry state. That makes it the cheapest place to prove the harness.

**The harness** is a differential test. It runs both implementations over the
repository's own fixtures and the three corpus repositories and requires
byte-identical output. It also adds a fuzz target, under `fuzz/` beside the
existing ClusterFuzzLite ones, that feeds the same source to both.

**Gate.** Identical output on the corpus, and at least 1.1x end to end on
the code-graph-rag run, or the stage does not ship.

### 4. Rust stage 2: Python scope analysis

Port the binding and scope walks in `parsers/py/ast_analyzer.py`:
`_binding_targets`, `_declared_non_local`, `_nonlocal_names`,
`_inner_scope_only_names` and `_bindings_in`. Also port the local-alias
collection in `variable_analyzer.py`. Together these are about 15% to 18% of
the code-graph-rag run. The output is name sets per scope, keyed by node
span. Same harness, same gate.

### 5. Rust stage 3: call-site extraction

Port the walk that finds call nodes and their parts:
`_scan_invoked_parameters`, the call collection in `call_processor.py` and
`_collect_all_call_nodes`. These are about 12% of the run. Resolution
(`call_resolver.py` and the function registry) stays in Python, because it
reads and writes registry state that every language shares. Rust hands back
call sites as tuples, and Python resolves them as it does today.

### 6. Other languages, by measured share

Each further grammar adds roughly 0.5 MB to 5 MB of compiled parser to every wheel. A language gets a Rust
path only when a corpus where it dominates shows its passes above 10% of the
run. The first candidate is C/C++ preprocessor recovery (5.0% of the
code-graph-rag run).

## What the steps add up to

Take the code-graph-rag run without embeddings (288 s) and assume a ported
walk runs 10x faster than its Python twin. Every step has to confirm that
factor.

| After | Remaining share | Estimated time |
|---|---|---|
| Today | 100% | 288 s |
| Steps 1 and 2 (Python) | about 87% | about 250 s |
| Step 3 (fingerprints) | about 77% | about 220 s |
| Step 4 (scope analysis) | about 62% | about 180 s |
| Step 5 (call sites) | about 51% | about 145 s |

That is about 2x end to end on a large repository. Most of what remains is
call resolution and registry work in Python. That is the next thing to
measure once these steps land.

Step 1 matters most for small repositories. On a default run with
embeddings, model inference bounds everything, and no amount of Rust in the
passes moves it.

## Risks

- **Grammar skew.** The Rust grammar crates and the Python grammar wheels
  must be the same versions, or the two trees differ. Bump them together in
  one PR. The differential harness fails on any skew.
- **Two implementations.** Every ported walk keeps its Python twin as the
  fallback for platforms without wheels, as the graph engine does. A change
  to one needs the same change to the other, and the harness enforces it.
- **Wheel size and build time.** The grammars are compiled C. Measure wheel
  size in each PR and keep the languages to the measured ones (step 6).
- **The boundary.** A port that returns per-node Python objects saves
  nothing. Each stage returns one batch per file.
