---
description: "How cgr assembles a graph-ranked, token-budgeted context slice around a symbol, location or task: what goes in, in what order, and why each piece is there."
---

# Context Slice

`context(target, budget_tokens)` hands an agent the few things that matter
about a symbol instead of the files around it (issue #1536).
`semantic_search` plus `get_code_snippet` is grep with embeddings; the
graph can rank by structural distance and stop at a budget.

```bash
cgr context myproj.pkg.util.helper
cgr context pkg/util.py:12 --budget 2000
cgr context "sum the scaled items"
```

The MCP tool `context` takes `target`, `budget_tokens` (default 4000) and
the optional `project`.

## Target

`target` is a qualified name, a bare name (`helper`, `Store.get`), a
`path:line` location, or free text. Names and locations resolve through
the deterministic `resolve` query (exact, then dotted-suffix, then
same-name matches; definitions before modules). Free text falls back to
embedding similarity when the semantic extra is installed: the best match
becomes the target and every candidate's own similarity is kept as a
tie-breaker. A target the graph cannot place yields an empty slice with
`resolved: null`.

## What goes in

| Piece                 | Distance | Source                                                   |
|-----------------------|----------|----------------------------------------------------------|
| target                | 0        | The definition's full source.                            |
| direct callers        | 1        | The call line of each `CALLS` site into the target (from the per-site location, issue #1522), not the whole caller. |
| direct callees        | 1        | Each callee's signature: the header up to its opener.    |
| types                 | 1        | The header of each type the target `RETURNS` or `ACCEPTS` (issue #1527). |
| reaching tests        | depth+1  | The full source of each test from which the target is reachable, with the distance and the symbol it reaches it through. |
| documentation         | 2        | Sections of documents whose links point at the target's file, those mentioning the target's name (all of them) or, failing that, the document's first section. |

## Ranking and budget

Candidates are ordered by graph distance, then trace hotness
(`dynamic_call_count` on the caller's edge, from `cgr trace ingest`), then
embedding similarity for a free-text task, then name. Tokens are counted
with the same encoder the query tools use, and pieces are taken in order
while they fit: the total never exceeds `budget_tokens`. A piece is
charged for what it prints, its source and the fields beside it (name,
file, span, reason), so a slice of one-line call sites cannot print many
times its budget in metadata (issue #3243). The target is special: when it
alone exceeds the budget it is trimmed to the most leading lines that fit
and the slice is marked `truncated`; a budget too small for even its first
line names it in `omitted` instead. `omitted_count` counts every
candidate that did not fit, and `omitted` names the best-ranked ten of
them with the reason each would have been included; beside the pieces,
the printed slice adds only those fields.

A central symbol has thousands of callers and tests, nearly all of which a
budget leaves out. Their sources are read only for a candidate whose
fields still fit, each file the slice quotes is decoded once, and a test's
span comes from the reach index already built, never from a graph query
per test.

## Output

```json
{
  "target": "myproj.pkg.util.helper",
  "resolved": "myproj.pkg.util.helper",
  "budget_tokens": 4000,
  "used_tokens": 398,
  "pieces": [
    {"qualified_name": "myproj.pkg.util.helper", "file": "pkg/util.py", "span": [8, 13],
     "why_included": "target", "source": "def helper(items: list[int]) -> Total:\n    ...", "tokens": 104},
    {"qualified_name": "myproj.pkg.app.run", "file": "pkg/app.py", "span": [5, 5],
     "why_included": "direct caller: the call line", "source": "return helper([1, 2]).value", "tokens": 52}
  ],
  "omitted": [],
  "omitted_count": 0,
  "truncated": false
}
```
