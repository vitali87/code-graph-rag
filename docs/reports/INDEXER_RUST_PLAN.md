# Indexer Rust Port: Measured Plan

The embedded graph already runs its hot paths in Rust (`rust/cgr_graph_rs`,
shipped as the `cgr-graph-rs` wheels). This plan covers the next question:
should the indexing pipeline that fills the graph move to Rust too, and in
what order? It answers from measurements of the current pipeline, not from
estimates, and splits the work into separate PRs. Each PR is measured again
before the next one starts.

Python stays the shell: the CLI, the MCP server, the agent, the passes'
control flow and every registry the passes share remain Python. What moves
is the per-node work of walking a syntax tree, one function at a time, each
behind a Python twin that stays the reference.

`docs/reports/REWRITE_RECOMMENDATIONS.md` found a Rust rewrite unjustified,
because tree-sitter work was 3.1% of CPU time in its profile. That profile
predates the current pipeline and the embedded graph. On today's code the
analysis passes are 94% of a large repository's run, so the question needs
answering again.

This is the second version of the plan. The first assumed Rust would parse
each file again with grammars compiled into the wheel, that parsing costs
0.2% to 0.3% of a run, and that a ported walk runs about 10 times faster.
Measuring the first port showed all three wrong; the sections below say
what replaced them.

## How it was measured

Each repository was indexed from scratch:

```
cgr start --update-graph --clean --yes --no-embeddings
```

The runs used the embedded graph, the native extension on, Python 3.12 and
a 4-core x86_64 Linux machine. py-spy sampled each one (200 Hz for rich, 100
Hz for code-graph-rag). Sampling leaves the interpreter untouched, whereas
cProfile inflates pure-Python code against C two- to three-fold.
`benchmarks/index_profile.py` turns a recording into the stage table below,
and its docstring has the exact commands.

| Repository | Python files | Sync time under the profiler |
|---|---|---|
| rich | 213 | 13 s |
| code-graph-rag (its own tree) | 1,846 | 231 s |

## Where the time goes

### By stage

| Stage | rich | code-graph-rag |
|---|---|---|
| Calls pass | 44.2% | 59.7% |
| Definitions pass | 25.0% | 34.4% |
| Imports and startup | 23.2% | 0.8% |
| Graph store (Rust ingest and SQLite) | 2.9% | 2.2% |
| Other | 4.6% | 2.9% |

A default run also embeds every definition; model inference was 34% to 42%
of such a run in the first version's profile, and is native code in torch.

### By function, inclusive

Each row counts every sample with that function anywhere on the stack, so
rows overlap where one calls another.

| Function | rich | code-graph-rag | What it is |
|---|---|---|---|
| `_find_python_method_in_ast`, `_find_function_ast_node`, `_find_class_node` | 11.0% | 15.0% | rerun a whole-file query per lookup |
| Python scope analysis (`_binding_targets`, `_declared_non_local`, `_nonlocal_names`, `_inner_scope_only_names`, `_bindings_in`) | 8.4% | 13.6% | name binding walks |
| `interface_fingerprint.py` | 9.6% | 11.9% | what a file's callers can see |
| the two rows above together | 16.8% | 21.9% | they share the scope walks |
| `call_resolver.py` | 8.4% | 7.8% | resolution against the registries |
| `anchor_hash.py` | 3.8% | 4.9% | definition content hash |
| `ast_fingerprint.py` | 1.9% | 4.5% | clone-detection skeleton |
| tree-sitter parsing (`parse_with_preproc_recovery`) | 0.7% | 4.9% | half of it re-parses evicted trees |
| `_scan_invoked_parameters` | 2.3% | 4.2% | call-site walk |
| `safe_decode_text` | 2.1% | 3.0% | node text decoding |
| `java/type_resolver.py:_find_registry_entries_under` | below 0.3% | 2.5% | registry scan |

Of the startup share on rich, importing `pydantic_ai` is 6.8% and
`prompt_toolkit` 1.7%; the indexer uses neither.

## Two corrections to the first version

**Parsing is not free.** It is 4.9% of the code-graph-rag run. Half of that
is `_load_ast_from_disk` parsing again a tree the bounded AST cache evicted;
the other half is the first parse. A Rust path that parsed every file again
would add most of that back.

**A ported walk does not run 10 times faster.** On the 33,418 function and
class definitions of code-graph-rag's own tree (5.8 million nodes walked):

| Per node | Cost |
|---|---|
| Python twin of `anchor_hash` | about 890 ns |
| Python walk alone, over `children` lists py-tree-sitter has cached | 109 ns |
| Rust tree-sitter cursor walk alone | 93 ns |
| one SHA-256 update (SHA-NI) | about 12 ns |
| one BLAKE2b-64 hash of a node (one compression) | about 147 ns |

The Python cost is the per-node work (attribute reads, string encodes, set
lookups, tuple allocations), not the traversal; the Rust cursor is no faster
at walking than Python's cached lists. A port therefore gains what that work
costs and no more. Hash-bound walks gain least: the fingerprint must compute
one BLAKE2b per kept node, as its graph format fixes, and a SIMD BLAKE2b
(`blake2b_simd`) is no faster on one-block messages. Size the next stages at
**5x to 8x per ported walk**, not 10x.

## How Rust gets the tree: it borrows Python's

The first version had Rust parse each file again with grammars compiled into
the wheel, because py-tree-sitter exposes no handle on its C tree. It does
not need one. A py-tree-sitter `Node` is a C struct with the C `TSNode`
inline (`tree_sitter/binding/types.h` in its source):

```c
typedef struct { PyObject_HEAD TSNode node; PyObject *children; PyObject *tree; } Node;
typedef struct { PyObject_HEAD TSTree *tree; PyObject *source; PyObject *language; } Tree;
```

`TreeBridge` (`rust/cgr_graph_rs/src/trees/python.rs`) copies the `TSNode`
out of the object it is handed and walks it with the `tree-sitter` crate's
runtime. That runtime has to be the same C code py-tree-sitter compiled in:
code-graph-rag pins py-tree-sitter 0.26.0, whose bundled `node.c`, `tree.c`,
`tree_cursor.c`, `subtree.h` and `language.c` are byte-identical to those of
the `tree-sitter` crate 0.26.8 the extension pins (only `query.c`,
`unicode.h` and `wasm_store.c` differ, none of which a walk calls).

What this buys over parsing again:

- **No second parse**, and so none of the 4.9% above.
- **No grammar in the wheel.** The runtime reads any language's tree,
  because the grammar it consults is the one Python loaded. All 15 grammars
  are covered at once; the stripped library grew by 109 KB (3.7%), where the
  first version budgeted 0.5 MB to 5 MB per grammar.
- **No grammar version skew.** There is one grammar per language, Python's.
- **Drop-in ports.** Each ported function keeps its Python signature and is
  called where it was, per node; nothing is regrouped into per-file batches.

What guards it, since the bridge reads memory py-tree-sitter owns:

- It refuses any py-tree-sitter but the release it was checked against, and
  any build whose `Node` and `Tree` sizes (`__basicsize__`) differ from what
  the structs above imply.
- On every call it checks that the object is exactly a `tree_sitter.Node`,
  that its `tree` field is exactly a `tree_sitter.Tree`, and that both name
  the same `TSTree`, all before reading through a pointer.
- Before first use, `parsers/native_trees.py` parses a probe source and
  compares every node as the two runtimes read it (symbol, type, named
  flag, byte range, points, child count, field name) and both hashes of
  every node with the Python twins. Any difference turns the bridge off for
  the process, and the Python twins run.
- A node it cannot read exactly (a test double, a tree parsed from a
  callback rather than `bytes`, decorators that are not a list of `str`)
  raises `NotBorrowed`, and that call runs the Python twin.

The tree is only read, never freed or edited, and the `Node` the caller
passes keeps the tree and its source alive for the whole call. This stage
never releases the GIL.

## Stage 1, landed: the definition hashes

`anchor_hash` and `compute_ast_fingerprint` now run through the bridge when
it is on. Their Python bodies are `python_anchor_hash` and
`python_ast_fingerprint`, the reference twins. The classification constants
(punctuation, identifier and literal types, block types, ...) are handed to
the bridge from `constants/` when it is built, so the twins cannot drift
apart on them; a constant the Rust side cannot apply, such as another
`AST_FP_DIGEST_SIZE`, turns the bridge off.

**Parity.** Every named node of 3,309 files in 14 languages (C, C++, C#,
Dart, Go, Java, JavaScript, Julia, Lua, PHP, Python, Rust, Scala,
TypeScript; about 3.0 million nodes) hashed both ways, with and without
decorators: no difference. Indexing rich and code-graph-rag with the bridge
on and off gave identical nodes, hashes included. Edges differed only in
the `resolution` label of a few `CALLS` edges, which differs as much between
two runs with the bridge off. `codebase_rag/tests/test_native_trees.py`
keeps the per-language and per-node comparison in the suite, and the Rust
crate's own tests pin the Python twins' values.

**Speed**, per definition on code-graph-rag's own tree, over four runs:

| | Python | Rust | Gain |
|---|---|---|---|
| `anchor_hash` | 148 to 169 us | 17 to 24 us | 7x to 9.5x |
| `compute_ast_fingerprint` | 214 to 237 us | 38 to 43 us | 5x to 6x |
| both | | | 6.3x in every run |

End to end, without embeddings (sync time, best of the runs):

| Repository | Bridge off | Bridge on | Gain |
|---|---|---|---|
| rich | 12.2 s | 11.0 s | 1.10x |
| code-graph-rag | 222.5 s | 206.0 s | 1.08x |

The first version gated this stage on 1.1x end to end on code-graph-rag.
Stage 1 alone measures 1.08x, short of that gate, because it holds the two
hashes (9.4% of the run) and not `interface_fingerprint` (11.9%), which
shares the scope walks of step 3 below and moves there. The saving matched
what the profile predicted (7.8% at 6x; 7.4% measured), which is the check
the later steps' estimates rest on.

## Stage 2, landed: the Python scope walks

For every Python function it reads, call resolution asks which names the
function binds itself, and type inference asks which scope each of its
assignments belongs to (issue #3473). Each question walked the function's
body again, and the bodies of the defs around it: call resolution asked
twice per function (`shadowed_import_names`, then `locally_bound_names`),
an enclosing def was walked again for every function inside it, and the
`nonlocal` check walked a nested binding's whole body once per binding.

Now one walk reads a scope's facts (its parameters, what its own body
binds, the import paths that can make a binding a re-import, its `global`
and `nonlocal` declarations), and `ast_analyzer.scope_facts` keeps them per
scope node. The scope functions read the facts instead of walking. The
bridge reads them in Rust when it is on, with `python_scope_facts` as the
twin; `_inner_scope_only_names`, the comprehension and lambda walk, moved
the same way. A cached scope keeps its whole tree alive, where the AST
cache's memory cap cannot see it, so the cache holds the scopes of the last
few trees read (`PY_SCOPE_CACHE_TREES`), not a number of scopes, and is
dropped with the per-pass caches. The node types and fields the walks read
are handed to the bridge from `constants/`, as stage 1's are.

**Parity.** Every def, class, lambda and module of 1,821 files (37,810
scopes: `codebase_rag/`, `evals/`, `scripts/`) and every one of the 537,090
nodes of rich and click, read both ways: no difference.
`codebase_rag/tests/test_native_scopes.py` keeps the comparison in the
suite, and the regression tests of #1907, #1922, #1928, #2124 and #2666 run
on both paths (the `both_scope_walks` fixture). Indexing code-graph-rag
twice with this stage gave the same graph as `main`'s second run, byte for
byte (215,163 nodes and edges); `main`'s first run had one `CALLS` edge
more, which its own second run did not have. Indexing rich seven times on
each wrote the same set of lines: none is missing from the other set, and
the few that vary from run to run vary on `main` too.

**Speed.** Over those 37,810 scopes the Python walks take 18.2 s and the
Rust ones 3.9 s (4.7x, with each answer converted to Python). End to end,
without embeddings, from the same environment:

| Repository | `main` | Stage 2 | Gain |
|---|---|---|---|
| rich, median of 5 alternating runs | 14.2 s | 11.8 s | 1.20x |
| code-graph-rag, 2 alternating runs | 277.5 s, 261.1 s | 224.4 s, 214.3 s | 1.22x to 1.24x |

Under the profiler the scope walks fell from 14.2% of the code-graph-rag
run to 6.6%, and with `interface_fingerprint` from 20.8% to 16.6%. The run
gained more than those shares alone predict (about 11%); `main`'s own two
runs differ by 16 s. The tree has grown since stage 1 was measured: `main`
now takes 261 s to 277 s, where stage 1's `main` took 222 s.

## Stage 3, landed: the interface fingerprints

A sync decides whether the files that depend on an edited file must be read
again from that file's interface fingerprint (issue #2531), a hash of what
they can see of it. `interface_fingerprint` walks every node of the file
and, for each function, checks whether anything outside reads its body: a
call of one of its own parameters, a returned name it binds, a name it
hands on to another call. A body nothing reads is replaced by a short
projection of it (the definitions inside, what it returns, the calls that
hand a name on). That was 12.9% of the code-graph-rag run on stage 2.

The bridge now computes it in Rust, with `python_interface_fingerprint` as
the twin. The per-language rules (`_RULES`: which node types are functions,
blocks, definitions, calls and returns) and every node type and field the
checks read are handed to the bridge from `constants/` (`bridge_rules`), as
stages 1 and 2 hand theirs; a length prefix or byte order the Rust side
does not write turns the bridge off. The checks ask which names a function
binds; the Rust walk answers from stage 2's scope facts in Rust instead of
calling back into Python. The startup probe also fingerprints its sample
file both ways, and while it runs the twin's scope lookups stay in Python,
so the probe compares two separate computations, not Rust with itself.

The walk never asks a node for its parent, because tree-sitter answers
that by walking down from the root; it keeps the enclosing definitions on
its own stack. Its node-type sets are hashed with FNV, which is about a
third cheaper per lookup than Rust's default hasher and safe here, because
their keys are the grammar's type names, never text from the file being
read.

**Parity.** 2,656 files in five languages fingerprinted both ways:
code-graph-rag's `codebase_rag/` (1,743 Python files) and its 7 Java
files, rich (213) and click (91) in Python, express (142, JavaScript), ky
(98 as TypeScript, and the same 98 as TSX) and gson (264, Java): no
difference. `codebase_rag/tests/test_native_interface.py` keeps the
comparison in the suite, over a sample in each language, every definition
in them and every file of the package, and the tests of
`test_interface_fingerprint.py` run on both paths (the
`both_interface_walks` fixture). Indexing code-graph-rag on `main` and on
this stage, alternating, gave the same graph pair by pair, byte for byte,
fingerprints included (215,163 and 215,164 nodes and edges: the two pairs
differ by one `CALLS` edge, on both sides). rich's five runs on each side
wrote the same set of lines, and click's six runs the same graph.

**Speed.** Over those 2,656 files the Python fingerprint takes 58.5 s and
the Rust one 6.8 s (8.6x). Most of what is left is tree-sitter's cursor:
93 ns a node in the table above, about 125 ns with every tree of a run in
memory. And the walk visits a function's body more than once: to check
what reads it, then to project it.

End to end, without embeddings, sync time. This container ran about 1.6
times slower than when stage 2 was measured: stage 2's own code took 214 s
to 224 s on code-graph-rag then and 335 s to 358 s now, on the same tree.
Compare within the table only.

| Repository | `main` | Stage 3 | Gain |
|---|---|---|---|
| rich, median of 5 alternating runs | 19.8 s | 17.0 s | 1.16x |
| click, median of 3 alternating runs | 12.1 s | 11.3 s | 1.07x |
| code-graph-rag, 2 alternating runs | 358.2 s, 335.0 s | 300.4 s, 312.3 s | 1.07x to 1.19x, 1.13x on the means |

Stage 2's profile predicted 1.13x for code-graph-rag (12.9% at 8.6x).
Under the profiler `interface_fingerprint` fell from 12.9% of the run to
1.9%, and the scope walks from 6.6% to 3.2%, since the fingerprint no
longer calls them from Python.

## The plan

Every step is its own PR, re-measured with `benchmarks/index_profile.py`
before the next starts. The Python fixes come first where they are larger:
they are cheaper, and they change the shares the Rust steps are sized by.

### 1. Python: index definitions once per tree (about 15%)

`_find_python_method_in_ast`, `_find_function_ast_node` and
`_find_class_node` rerun the class or function query over a whole file every
time a call's return type needs a definition node. Build one map per tree on
first use (class name to its methods by name, top-level function name to
node), kept beside the cached AST and dropped with it. This is the largest
single item in the profile and needs no Rust.

### 2. Python: lazy agent imports (up to 9% of a small repository's run)

Import `pydantic_ai` and `prompt_toolkit` where the agent uses them, so
`cgr start --update-graph` does not load them. The first version found
`qdrant_client` and `pymilvus` there too when the `semantic` extra is
installed.

### 3. Rust: interface fingerprints

Landed: the scope walks as stage 2, `interface_fingerprint` as stage 3.

### 4. Rust: call-site extraction (2% to 5%)

`_scan_invoked_parameters` and the call collection in `call_processor.py`.
`_scan_invoked_parameters` alone fell from 3.7% to 1.9% with stage 3, which
stopped calling it from the fingerprint.
Rust returns the call sites as nodes the caller already holds or as spans;
resolution (`call_resolver.py`, the function registry) stays in Python,
because it reads and writes registry state every language shares.

### 5. Parallel walks

Once the per-file walks are Rust, compute them for every changed file at
once in the pre-parse stage (`_pre_parse_changed_files`), with the GIL
released: the bridge only reads trees nothing else mutates, and holding the
Python nodes across the release keeps the trees alive. The serial passes
then look the results up. On N cores this divides what is left of the
ported share by about N. py-tree-sitter already releases the GIL while
parsing (`Parser.parse`), so the first parse can move onto a thread pool
before any of this.

### 6. The AST cache

Half the parse time re-parses trees the bounded AST cache evicted. Size its
bound from the run's file count, or order the call pass so that a file's
callers follow it, before porting any of it.

### 7. Python: the Java registry scan (9.5% after stage 3)

`_find_registry_entries_under` in `java/type_resolver.py` asks the function
registry's trie for the names under a prefix, and when it finds none, as
for a type declared outside the repository, it scans every registered name
instead. On code-graph-rag's run that scan is 9.5% (5.5% on stage 2's
profile), almost all of it in the scan itself, for the run's 7 Java files
(`_methods_named`, under instance-method resolution). Answering from the trie alone, or remembering the empty
answers, needs no Rust; whether the scan can ever find what the trie does
not is the thing to check first.

## What the steps add up to

The code-graph-rag run without embeddings, timed without the profiler. The
tree grew between stages 1 and 2, and the container slowed between stages 2
and 3, so each stage is measured against the `main` of its day. The steps
not taken yet are projected from stage 3's profile, from its measured mean,
at 6x per ported walk:

| After | Time |
|---|---|
| Stage 1's `main` | 222 s, measured |
| Stage 1 (landed) | 206 s, measured |
| Stage 2's `main` | 261 s to 277 s, measured |
| Stage 2 (landed) | 214 s to 224 s, measured |
| Stage 3's `main`, in a container about 1.6x slower | 335 s to 358 s, measured |
| Stage 3 (landed) | 300 s to 312 s, measured |
| Step 1 (Python lookups, 22.6% of stage 3's run) | about 235 s |
| Step 7 (Java registry scan, 9.5%) | about 210 s |
| Step 4 (call sites, 2% to 5%) | about 200 s |

That is about 1.7x end to end on one core against stage 3's `main`, before
step 5. Most of what remains is call resolution and the passes' own control
flow in Python, which share registries across files and languages; they are
the part that stays Python.

## Beyond the indexer

- **Embeddings** are a third or more of a default run and already native.
  Rust would not make torch faster. What a Rust embedder (UniXcoder through
  `candle` or ONNX Runtime) inside `cgr-graph-rs` could do is remove torch
  from the `semantic` extra, which is most of its install size and of its
  import time. That is worth measuring on its own, against ONNX Runtime from
  Python, before any port.
- **Queries** already run their hot paths in Rust (see
  [Graph Backends](../architecture/embedded-graph.md)).

## Risks

- **Layout coupling.** The bridge reads py-tree-sitter's objects, so a
  py-tree-sitter bump must come with a check of its `types.h` and of the C
  runtime against the `tree-sitter` crate, and a crate bump with it. Until
  then the bridge turns itself off and the Python twins run: slower, never
  different. `test_native_trees.py` fails when the extension carries the
  bridge but it is off, and when either pin differs from the pair the bridge
  was checked with, so a bump cannot lose the speed silently. `AGENTS.md`
  ("Bumping tree-sitter") has the procedure.
- **Two implementations.** Every ported walk keeps its Python twin for
  platforms without wheels. A change to one needs the same change to the
  other; the parity tests enforce it, and the probe re-checks it in every
  process.
- **The boundary.** A port that returns a Python object per node saves
  little. Each port returns one value per call: a hash, a set of names, a
  list of spans.
- **Unsafe code.** The bridge is the crate's first code to read Python
  objects' memory directly. It stays in one function (`with_node`), with
  every check made before a pointer is followed.
