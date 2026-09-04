---
description: "How cgr renames a definition through the graph: every call, reference, import and override site is rewritten in one transaction, and guessed sites refuse the rename."
---

# Rename Operation

`rename` is the first graph-native edit operation (issue #1532). Given a
qualified name and a new identifier it rewrites the definition and every
site the graph knows about, atomically, or explains why it will not.

```bash
cgr rename myproj.pkg.util.helper assist --dry-run   # plan and diff only
cgr rename myproj.pkg.util.helper assist             # apply
```

The MCP tool of the same name takes `qualified_name`, `new_name`, and the
optional `allow_heuristic`, `dry_run` and `project` fields, and returns the
same report as JSON.

## What gets rewritten

Sites come from the graph, never from text search:

| Site kind    | Source                                                          |
|--------------|-----------------------------------------------------------------|
| `definition` | The `name` field of the definition node, and of every override in both directions (`OVERRIDES` edges), so a method rename keeps the hierarchy consistent. |
| `call`       | `CALLS` edges into the definition, using the per-site `line`/`col` recorded at ingest (see [graph schema](graph-schema.md#edge-site-properties)). |
| `reference`  | `REFERENCES` and `INSTANTIATES` edges, the same way.            |
| `import`     | `IMPORTS` edges whose `imported_name` is the symbol; the statement is retargeted by the [import rewriter](patchers.md#import-rewriting-for-rename-and-move), and an alias (`import helper as h`) is kept, so aliased call sites need no edit. |

Python modules that export the symbol through `__all__` (the defining module
and any package `__init__` importing it) have the entry renamed too. Markdown
files mentioning the old name are listed in `doc_mentions` for a human to
review; prose is never rewritten.

Only the rightmost identifier inside each site span is touched: `a.b.helper(x)`
becomes `a.b.assist(x)` and the receiver is untouched. Every edit is a
span-preserving [patcher](patchers.md) edit, so formatting elsewhere in the
file is not disturbed.

## Refusal

The graph tags each call edge with how it was resolved (issue #1526). A
rename that would rewrite a `heuristic`, `overload` or `dynamic` site
refuses by default and reports those sites, because a guessed site is as
likely to belong to a different symbol with the same name. Dynamic
(trace-only) edges without a location are listed as `unlocatable` and also
refuse. Pass `--allow-heuristic` (`allow_heuristic: true`) to rewrite through
them anyway.

The rename also refuses when the new name is not a valid identifier, when
the qualified name has no definition in the graph, or when the definition's
name token cannot be found at the recorded position (a stale graph).

## Atomicity

All edits are staged in one [edit transaction](edit-transactions.md). Every
staged file is re-parsed with its language's Tree-sitter grammar before
anything is written; a file that no longer parses rolls the whole rename
back with `RENAME_PARSE_FAILED`. Applied renames are recorded in the edit
history, so `cgr edits undo` reverses them.

## Report

```json
{
  "qualified_name": "myproj.pkg.util.helper",
  "old_name": "helper",
  "new_name": "assist",
  "applied": true,
  "transaction_id": "...",
  "files": ["pkg/__init__.py", "pkg/app.py", "pkg/util.py"],
  "sites": [{"kind": "call", "path": "pkg/app.py", "line": 4, "col": 11, "owner": "myproj.pkg.app.run", "resolution": "exact"}],
  "ambiguous": [],
  "unlocatable": [],
  "doc_mentions": ["README.md:12"],
  "hierarchy": ["myproj.pkg.util.helper"],
  "diff": "...",
  "message": "3 file(s) written"
}
```

`sites` and `ambiguous` are the located sites, `hierarchy` the definitions
renamed together, and `diff` the unified diff of what was (or, on
`--dry-run`, would be) written.
