---
description: "How cgr extracts a span of statements into a new function and inlines a single-return function at its call sites, both under a transaction and the postcondition contract."
---

# Extract and Inline

`extract` and `inline` are edit-algebra operations 4 and 5 (issue #1535).
`cgr duplicates` finds clones; these two act on them, and together with
rename, change_signature and move they complete the minimal algebra.

```bash
cgr extract myproj.pkg.report.build 3 11 accumulate --dry-run
cgr inline myproj.pkg.util.wrapper
```

The MCP tools `extract` (`qualified_name`, `start_line`, `end_line`,
`new_name`) and `inline` (`qualified_name`) take the same optional
`dry_run` and `project`.

## Extract

The span is a run of whole statements of the function's body, by 1-based
inclusive line numbers. A span that cuts through a statement, holds none,
or leaves the function early (a `return` or `yield` anywhere, a `break` or
`continue` whose loop lies outside the span) is refused: such a span cannot
be expressed as one call.

Scope analysis is tree-sitter based and runs in statement order:

- **inputs**: names the span reads before binding them that the enclosing
  function binds (its parameters, or assignments before the span);
- **outputs**: names the span binds that the statements after it read.

Attribute and property names are not reads (`obj.total` reads `obj`), a
keyword argument's name is not a read, and nested functions are descended
(a closure still reads). Python binds through assignments (augmented
included), `for` and `as` targets and nested definitions; JS/TS through
declarators and plain assignments.

The new function is placed right after the enclosing definition (after its
decorators or `export`) at the same indentation, the span dedented into it,
with `return a, b` (Python) or `return { a, b }` (JS/TS) for the outputs.
The span becomes `a, b = new_name(x, y)`, or in JS/TS `const { a, b } =
new_name(x, y);` when the span declared the outputs and a plain assignment
when they existed before. A Python method's span becomes a method: `self`
is carried as the receiver and the call reads `self.new_name(...)`. In
TypeScript a parameter's annotation travels with it; other inputs stay
unannotated.

## Inline

Only a single-return body inlines (a docstring may precede the `return`).
Every call site the graph knows is rewritten: arguments bind to parameters
positionally and by keyword, missing ones take the definition's defaults,
a method call binds `self` to the receiver expression, and the substituted
expression is parenthesised where precedence could change (an argument
that is not an atom, a result that is not a call or a name). Substitution
is by token position in the returned expression, so a parameter named `a`
never touches `obj.a`.

A callee with a caller resolved by guesswork (`heuristic`, `overload`) or
only by a trace (`dynamic`) is refused with the sites named: those sites
cannot be rewritten with confidence. Once every site is rewritten the
definition is removed together with the import entries that bound its name
(`from pkg.util import wrapper, other` keeps `other`; a statement binding
only the name goes).

## Atomicity and contract

Both run as one [edit transaction](edit-transactions.md) of
[patcher](patchers.md) span edits and are held to the
[postcondition contract](postcondition-contract.md): extract expects the
new symbol added and nothing else to move; inline expects the callee
removed (callers of a removed symbol are the plan, not a dangling
reference); both expect no new duplicate group and no new import cycle.
A failing contract undoes the transaction and re-ingests the restored files.
