---
description: "How cgr changes a function's parameter list and rewrites every call site the graph knows per an explicit mapping, listing what it could not map."
---

# Change Signature

`change_signature` is edit-algebra operation 2 (issue #1533). The
definition changes and its callers in other packages do not: that is the
most-cited cross-package breakage. The graph knows every call site with its
argument shape ([edge-site properties](graph-schema.md#edge-site-properties)),
how each edge was resolved (`resolution`), and the declared parameter
types (`param_types`), so every site is rewritten per an explicit mapping
or listed as unmapped.

```bash
cgr change-signature myproj.pkg.util.helper -p a@0 -p n:int=1 -p b@1 --dry-run
cgr change-signature myproj.pkg.util.helper -p a@0 -p n:int=1 -p b@1
```

The MCP tool `change_signature` takes `qualified_name`, `new_params` (a
list of the same specs), and the optional `allow_heuristic`, `dry_run` and
`project`, and returns the report as JSON.

## The mapping

`new_params` is the new parameter list in order. Each spec is
`name[:annotation][=default][@source]`:

| Spec            | Meaning                                                                                       |
|-----------------|-----------------------------------------------------------------------------------------------|
| `a@0`           | Keeps old positional parameter 0 (receiver excluded) as `a`; the old text (annotation, default) is carried over. |
| `key@old`       | Keeps the old parameter named `old`, renamed to `key`.                                        |
| `n:int=1`       | A new parameter; every site that passes nothing for it gains the literal `1`, and the definition gets `n: int = 1`. |
| `b:str='x'@1`   | Keeps old parameter 1 with a new annotation and default.                                       |
| `extra`         | A new parameter with no source: unmapped. Sites that pass no value for it are left untouched and listed. |

Old parameters that no spec names are dropped; the values sites passed for
them are dropped with them.

## What is rewritten

- **Definitions**: the parameter list of the definition and of every method
  in its override hierarchy (`OVERRIDES` edges, both directions). In Python
  a required parameter placed after one with a default is refused.
- **Call sites**: each `CALLS` site of every hierarchy member. Positional
  values follow the new order; a value the site passed by keyword keeps its
  keyword; once a site relies on an old default that is no longer last,
  every later value is spelled by name (Python only; a language without
  keyword arguments leaves such a site unmapped). A keyword-only site that
  keeps every name is left exactly as written.
- **Unmapped**: sites the mapping cannot complete (no value for a required
  parameter, a spread or splat argument, an unknown keyword, a language
  without keyword arguments where they would be needed), sites without a
  recorded location, and sites resolved by guesswork (`heuristic`,
  `overload`, `dynamic`) unless `--allow-heuristic`. Each carries the
  reason.

## Type checks

A default literal is checked against the declared type of the parameter
it fills, from the new annotation or the graph's `param_types` of the old
parameter it maps from. For the builtin names (`int`, `float`, `str`,
`bool`, `list`, `dict`, `set`, `tuple`, unions of them, `None`) a literal
of the wrong kind is refused before anything is written; an annotation the
graph cannot interpret (a class, a generic) is not checked.

## Atomicity and contract

All edits are span-preserving [patcher](patchers.md) edits staged in one
[edit transaction](edit-transactions.md); a file that no longer parses rolls
the whole change back. Applied changes are recorded for `cgr edits undo`
and held to the [postcondition contract](postcondition-contract.md): every
call site of the changed signature must be mapped (a rewritten site is
mapped by construction) or listed as unmapped, no `too_many` finding may
remain unlisted, no site resolved by guesswork may have been rewritten
without leave, and no duplicate group or import cycle may appear. A failing
contract undoes the transaction and re-ingests the restored files; the
verdict, with the tests reaching the changed symbols, rides on the report.
