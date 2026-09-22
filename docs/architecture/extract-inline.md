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

The span is a run of whole statements of the body of a function or method
(a class or module is refused), by 1-based inclusive line numbers. A span
is refused when it cannot be expressed as one call with the same meaning:

- it cuts through a statement, or holds none;
- it leaves the function early: a `return` or `yield` anywhere, a `break`
  or `continue` whose loop or `switch` lies outside the span;
- it awaits (`await`, `async for`, `async with`, `for await`), since the
  helper is synchronous;
- in JS/TS, it uses `arguments`, or uses `this` when the helper would be a
  standalone function.

Scope analysis is tree-sitter based and runs in statement order:

- **inputs**: names the span reads before binding them that the enclosing
  function binds (its parameters, or assignments before the span);
- **outputs**: names the span binds that the statements after it read.

Attribute and property names are not reads (`obj.total` reads `obj`), a
keyword argument's name is not a read, the target of a plain assignment is
not a read (an augmented one is), and nested functions are descended (a
closure still reads). Python binds through assignments, `for` and `as`
targets, imports and nested definitions; JS/TS through declarators,
destructuring and plain assignments.

The new function is placed right after the enclosing definition (after its
decorators or `export`) at the same indentation, the span dedented into it,
and returns the outputs. The call site depends on how many there are:

| Outputs | Python | JS/TS |
| --- | --- | --- |
| none | `new_name(x, y)` | `new_name(x, y);` |
| one | `a = new_name(x, y)` | `const a = new_name(x, y);` if the span declared `a`, else `a = new_name(x, y);` |
| several | `a, b = new_name(x, y)` | `const { a, b } = new_name(x, y);` if the span declared them all; otherwise the new ones are declared first (`let b;`) and the call assigns `({ a, b } = new_name(x, y));` |

The helper returns `a` for one output and `a, b` (Python) or `{ a, b }`
(JS/TS) for several. A name the span assigns without declaring or
receiving it is declared inside a JS/TS helper, which strict code needs.

A method's span becomes a method. In Python `self` is carried as the
receiver and the call reads `self.new_name(...)`. In a JS/TS class the
helper is a method called as `this.new_name(...)`, or a `static` one called
through the class name. A method outside a named class is refused. In
TypeScript a parameter's annotation travels with it; other inputs stay
unannotated. Generated lines use the file's own newline, so a CRLF file
stays CRLF.

## Inline

Only a single-return body inlines (a docstring may precede the `return`).
Async functions, generators, variadic or destructured parameters, and a
JS/TS return that uses `this`, `super` or `arguments` are refused, since
the bare expression would not keep their contract.

At each call site the graph records (located by its recorded start and
end, so `helper(2).upper()` rewrites `helper(2)`), arguments bind to
parameters positionally and by keyword, a method call binds `self` to the
receiver expression, and the substituted expression is parenthesised where
precedence could change. Substitution is by token position in the returned
expression, so a parameter named `a` never touches `obj.a`.

A site keeps its call, unchanged, when rewriting it would change what runs:

- an argument that is not a name, literal or plain attribute chain would
  be evaluated other than exactly once, or out of order, or conditionally;
- an omitted argument's default is not a literal (Python evaluates it once,
  at definition);
- the call passes a splat or spread (`*xs`, `**kw`, `...xs`);
- the returned expression names a free variable the caller's scope could
  resolve differently.

Inlining is therefore best-effort per site: the sites that can be rewritten
are, the rest are left as calls.

A callee with a caller resolved by guesswork (`heuristic`, `overload`) or
only by a trace (`dynamic`) is refused with the sites named: those sites
cannot be rewritten with confidence. The definition is removed only when
every site was rewritten and nothing else still needs it: no reference to
it as a value (a callback), and no import of it still used, re-exported or
listed in `__all__`. It goes together with the import entries that bound
its name and are no longer used (`from pkg.util import wrapper, other`
keeps `other`; a statement binding only the name goes).

## Atomicity and contract

Both run as one [edit transaction](edit-transactions.md) of
[patcher](patchers.md) span edits and are held to the
[postcondition contract](postcondition-contract.md): extract expects the
new symbol added and nothing else to move; inline expects the callee
removed (callers of a removed symbol are the plan, not a dangling
reference); both expect no new duplicate group and no new import cycle.
The contract is checked when the caller supplies a re-ingest callback,
as the CLI and MCP tools do; a direct API call without one gets the
transaction and parse checks only. A failing contract undoes this edit's
own transaction and re-ingests the restored files; if the undo is refused
(a newer edit sits on top, or the files changed by hand) the report says
the edit was not rolled back rather than undoing someone else's edit.
