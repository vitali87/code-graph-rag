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
re-ingest, with three fixed Cypher queries scoped to the project:

| Read                    | What                                                                    |
|-------------------------|-------------------------------------------------------------------------|
| definitions             | The symbols defined in the touched files, with their declared positional parameters and whole-skeleton fingerprints. |
| sites                   | Every `CALLS` / `REFERENCES` / `INSTANTIATES` edge into or out of the touched files, with the per-site location and argument shape from [edge-site properties](graph-schema.md#edge-site-properties). Callees defined elsewhere are fetched by name so their signatures are known. |
| module imports          | The project's `Module -IMPORTS-> Module` graph.                         |

The two snapshots are diffed client-side; two further project-wide linear
reads (the duplicate fingerprints and the dead-code call graph) serve the
duplicate and test lookups. The reads and the re-ingest run under the same
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
| `signature_changes`  | Symbols whose positional parameters changed, with every call site and a verdict each. |
| `arity_findings`     | Call sites in the edited files that pass more positional arguments than the callee declares (`too_many`), the only verdict that needs no knowledge of defaults. |
| `new_duplicates`     | New or changed functions whose fingerprint (`exact`) or branch set (`similar`, Jaccard at the duplicates threshold) matches an existing function; `original` is the older one. The duplicate detector's minimum size applies. |
| `new_import_cycles`  | Strongly connected components of the module import graph that contain an edited module and did not exist before the edit. |
| `tests_reaching`     | Test functions from which any symbol of the edited files is reachable through the call graph, with the shortest distance and the symbol it is reached through. |

### Arity verdicts

Verdicts use the receiver arithmetic of `crash_correlation.diagnose_arity`:
a method's `self` counts for CPython but is not caller-supplied. Only
Python definitions carry `positional_params`, so sites of other languages
read `unknown`. The stored parameter list ends at `*args`; the definition
header is read back so a variadic callee is never reported as receiving
too many arguments. `possibly_missing` means fewer arguments than
parameters: the graph does not record defaults, so this is a hint, not a
finding, and does not trip `--fail-on-found`.

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
