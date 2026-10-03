---
description: "What every write tool reports back: the structural delta of the edit, computed from the graph before and after the scoped re-ingest, and the cgr check gate built on it."
---

# Structural Delta

An agent that edits code through cgr gets immediate feedback on what the
edit did to the structure of the program (issue #1525). After
`surgical_replace_code`, `write_file` and `structural_replace` (with
`dry_run=false`) the touched files are re-ingested through the
[scoped re-ingest](../guide/mcp-server.md) and a JSON delta is appended to
the tool result. `cgr check --base <ref>` computes the same delta for a
whole working tree, for CI and pre-commit.

## How it is computed

`codebase_rag.structural_delta` is the in-memory twin of
`services/graph_diff.py`, which diffs exported indexes offline. It reads
the touched files' subgraph twice, immediately before and after the
re-ingest, with four fixed Cypher queries scoped to the project:

| Read                    | What                                                                    |
|-------------------------|-------------------------------------------------------------------------|
| definitions             | The symbols defined in the touched files, with their declared positional parameters and whole-skeleton fingerprints. |
| sites                   | Every `CALLS` / `REFERENCES` / `INSTANTIATES` edge into or out of the touched files, with the per-site location and argument shape from [edge-site properties](graph-schema.md#edge-site-properties). Callees defined elsewhere are fetched by name so their signatures are known. |
| module imports          | The project's `Module -IMPORTS-> Module` graph.                         |

The two snapshots are diffed client-side; one further project-wide linear
read (the duplicate fingerprints) serves the duplicate lookup, and the tests
reaching a changed symbol are found by walking its callers one hop at a time
(`CYPHER_DELTA_CALLERS_OF`). The reads and the re-ingest run under the same
lock as every other MCP graph access, so the delta always describes one
generation of the graph. Measured overhead on top of the re-ingest is
reported per call as `delta_ms`; the benchmark (`benchmarks/bench_reingest.py`)
records it next to the re-ingest itself.

## What it reports

```json
{
  "paths": ["pkg/util.py"],
  "reparsed": ["pkg/util.py"],
  "affected": ["pkg/app.py"],
  "removed_files": [],
  "symbols": {
    "added": [],
    "removed": [],
    "renamed": [{"old": "proj.pkg.util.helper", "new": "proj.pkg.util.assist", "path": "pkg/util.py"}],
    "changed": []
  },
  "dangling_callers": [
    {"caller": "proj.pkg.app.run", "path": "pkg/app.py", "line": 5, "col": 11,
     "target": "proj.pkg.util.helper", "renamed_to": "proj.pkg.util.assist"}
  ],
  "signature_changes": [],
  "arity_findings": [],
  "new_duplicates": [],
  "new_import_cycles": [],
  "tests_reaching": [
    {"qualified_name": "proj.tests.test_app.test_run", "path": "tests/test_app.py",
     "depth": 2, "through": "proj.pkg.app.run"}
  ],
  "reingest_ms": 41.2,
  "delta_ms": 3.8
}
```

| Field                | Meaning                                                                                           |
|----------------------|---------------------------------------------------------------------------------------------------|
| `symbols.renamed`    | A symbol that disappeared while one with the same whole-skeleton fingerprint appeared in the same file. Paired one-to-one. |
| `symbols.changed`    | A symbol whose skeleton fingerprint or declared positional parameters moved. A change to a literal alone does not register here. |
| `dangling_callers`   | Call sites of a removed or renamed symbol that still name it: every caller in a file that was not part of the edit, and callers in edited files that did not re-bind to the new name. The `line`/`col` are the site's recorded position. |
| `signature_changes`  | Symbols whose positional parameters changed, with every call site and a verdict each, and `remote_callers`: call sites in any project that reach an endpoint the symbol exposes, through a network resource or directly for an RPC or dispatch resource (issue #1603). |
| `arity_findings`     | Call sites in the edited files the callee's language rejects: more positional arguments than the callee declares (`too_many`), the only verdict that needs no knowledge of defaults, and, where the signature declares which parameters are optional, fewer than it requires (`too_few`, see [signatures outside Python](#signatures-outside-python)). |
| `new_duplicates`     | New or changed functions whose fingerprint (`exact`) or branch set (`similar`, Jaccard at the duplicates threshold) matches an existing function; `original` is the older one. The duplicate detector's minimum size applies. |
| `new_import_cycles`  | Strongly connected components of the module import graph that contain an edited module and did not exist before the edit. |
| `tests_reaching`     | Test functions from which any symbol of the edited files is reachable through the call graph, with the shortest distance and the symbol it is reached through. |

### Arity verdicts

Verdicts use the receiver arithmetic of `crash_correlation.diagnose_arity`:
a method's `self` counts for CPython but is not caller-supplied. That is
Python's rule; the languages whose signatures declare optionality follow
[their own](#signatures-outside-python). The stored Python list ends at `*args`; the definition
header is read back so a variadic callee is never reported as receiving
too many arguments. A `**opts` unpacking at the site supplies keywords
only and adds no positional, so `send(req, **opts)` passes one positional
to `def send(request, **kwargs)`. A `*rest` unpacking adds an unknown
number: the positionals written beside it are a floor, so `too_many` still
holds when they alone exceed the parameters (`one(a, b, *rest)` against
`def one(a)`), and the site reads `unknown` otherwise. `possibly_missing`
means fewer arguments than parameters: the graph does not record
defaults, so this is a hint, not a finding, and does not trip
`--fail-on-found`.

### Signatures outside Python

TypeScript, JavaScript, Go, Rust, PHP, Java and C# definitions store
`positional_params` too (issue #2517): every parameter a call fills, marked
with the optionality the signature declares. `pad?` may be left out (a
TypeScript `?`, any default value), `...rest` takes any number of trailing
arguments (rest, variadic, C# `params`), and `self` (Rust) or `this s` (a C#
extension method) is a receiver that a method call leaves implicit and a
path call (`S::m(s, 1)`, `Util.Ext(s, 1)`) passes. The site's
`call_qualifier` says which form it is written in, and the receiver counts
among the arguments only where the call passes it. A C# name counts as the
extension's class only when it binds no local, parameter, field or property
at the call, so `Util.Ext(1, 2)` on a string named `Util` stays an instance
call. A site whose form cannot be read (a name that is neither, such as an
inherited member) is judged both ways and keeps a verdict only when the two
agree. A TypeScript `this:` parameter, Java's `C this` and Go's receiver
field are never passed and are not listed. Each marker is a receiver only in
its own language: anywhere else a parameter named `self` is an ordinary one,
counted like any other. A change to the list is a signature change, and
each site is judged by the number of arguments it passes:

| Verdict            | When |
|--------------------|------|
| `ok`               | The count fits: every required parameter at least, every parameter at most, any number past a rest parameter, the receiver counted where the call passes it. A surplus is `ok` too where JavaScript is either end of the call or in PHP, both of which drop it at run time. |
| `too_few`          | Fewer arguments than the required parameters, where the language rejects the call: TypeScript, Go, Rust, PHP (`ArgumentCountError`), Java and C#. A finding: it trips `--fail-on-found` as `too_many` does. |
| `too_many`         | More arguments than the parameters, where the language rejects the call: TypeScript, Go, Rust, Java and C#. A finding. |
| `possibly_missing` | Fewer arguments than the required parameters where JavaScript is either end of the call: it passes `undefined`, and nothing type-checks a JavaScript caller. A hint. |
| `unknown`          | The site passes a number of values its arguments do not show (`f(...args)`, `f(...$args)`, `f(xs...)`, Go's `f(pair())` passing every result of `pair`, a tagged template; the edge's `spread_args`), or the edge was bound by name alone (`resolution` `heuristic` or `overload`) and may lead to a same-named function the call never runs. |

Java and C# put a method's parameter types in its qualified name, so a
parameter added, removed or retyped renames the node, and its callers are
reported under `dangling_callers` with `renamed_to`. A change that keeps
the types (a parameter renamed, a C# default added or dropped) is a
signature change as above.

Not read, so their edits never reach `signature_changes`: C and C++ (a
default sits on the header declaration, which the definition need not
repeat), Scala and Dart (named and curried parameter lists), Lua (any count
is accepted) and bodiless TypeScript signatures (an overload, an interface
or abstract member: a call matches one of possibly several). A definition
indexed before these lists existed has none on the base side and is not
compared; the first sync after upgrading re-parses every file, since the
parser changed, and records the lists and every site's `spread_args`.

## `cgr check`

```bash
cgr check --base origin/main --fail-on-found
```

The graph is assumed to reflect `--base` (index there, then edit). Files
that differ between the base and the working tree, untracked files
included, are re-ingested and the delta printed as JSON. With
`--fail-on-found` the command exits 1 when the delta reports dangling
callers, `too_many` arity findings, new duplicates or new import cycles.
A project that is not indexed is refused: a scoped re-ingest completes a
graph, it cannot stand in for the first index.
