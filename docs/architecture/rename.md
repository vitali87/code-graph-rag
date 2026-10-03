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

Sites come from the graph, never from text search (the source is read only
to [cross-check](#cross-check-against-the-source) the plan):

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

A class named by an `INHERITS`, `ACCEPTS` or `RETURNS` edge that carries no
rewrite site refuses unconditionally: the graph knows the reference exists
but records no position to rewrite, so renaming would leave that edge
pointing at the old name. `--allow-heuristic` does NOT bypass this, because
the problem is a missing location rather than an uncertain one. A class that
is only instantiated or called renames normally.

The rename also refuses when the new name is not a valid identifier, when
the qualified name has no definition in the graph, or when the definition's
name token cannot be found at the recorded position (a stale graph).

### Cross-check against the source

The plan is only as complete as the index, and the index misses sites in
known ways (a Rust re-export, a Java static import or method reference).
Applying only the sites the graph knows would leave the others under the old
name and break the build while reporting success (issue #2564). So before
anything is written, the project's sources in the definition's language
family are read (the indexer's own walk, under `.cgrignore` and
`.gitignore`) for identifier tokens spelling the old name:

- for a function, every bare use, called or not (`callback = helper`,
  `map(helper, xs)`, `return helper`), and a qualified one only through its
  module (`util.helper` after `from pkg import util`, `util::helper`, or
  `u.helper` after `import pkg.util as u`; not `util.helper` after
  `from vendor import util`): `d.get(key)` and `subprocess.run(...)` are other
  objects' methods, whatever the function is called. In Python, JavaScript,
  TypeScript and Rust a bare name reaches another file's function only
  through an import, so outside the function's own file a bare use counts
  only where an import of it reaches it (`from pkg.util import sorted`, or
  `from pkg.util import *`): a `sorted(xs)` that imports no project `sorted`
  is the builtin. After a star import from another module it may still be
  the function, and is held to the plan as uncertain, as is a bare use of a
  top-level function of a classic JavaScript script (a file whose code has
  no import or export and no `require()`, `module.exports` or `exports`
  of Node's own; a comment, a string, or a use that a local or parameter of
  that name declared around it shadows does not count, and a `const` holds
  only in its block), which every script on the
  page shares. In Go, Java, C# and the
  like a same-package call needs no import, and every bare use counts;
- for a method, a use through its class (`Greeter.greet`, `Greeter::greet`),
  through its own object in the body of its class or of one whose header
  names it (`self.name`, `this.name()`, `Self::name`, `super().name`), or
  through a variable every binding of which declares or builds the class
  (`parse: &mut Parse`, `Greeter g`, `let parse = Parse::new(frame)?`).
  These are certain. The class's name counts as the class only where it
  reaches the class's own module: qualified through that module
  (`cache.Cache()`, `pc.Cache()` after `import pkg.cache as pc`,
  `crate::parse::Parse`), or bare in its own file, in its package, or after
  an import that names its module. A module is told by its path, not its
  last name: `pkg.cache`, `vendor.cache` and `pkg2.cache` are three modules.
  A relative import (`from .cache import x`, `'./cache.js'`) and a Rust path
  from `crate`, `self` or `super` are resolved from the importing file; a
  Python import from a source root spells the module from its top-level
  package down (`pkg.cache` once `pkg/__init__.py` or `pkg/__init__.pyi`
  exists). Where two
  source roots hold the spelled module (`pkg/cache.py` and
  `src/pkg/cache.py`), Python's import path decides which one loads, and
  the source does not say: a call through it is uncertain, held to the plan
  and never rewritten. When another
  symbol of the project shares the name, `other.Cache()`,
  `from pkg.other import Cache` and `class Sub(other.Cache)` are that one;
  an import through a package above the class (`use crate::Parse`,
  `from pkg import Cache`) counts only when no other symbol shares it. A
  Java file is named after its class, so there the import's package tells
  the two apart (`import a.Greeter`). A bare name means what the scope
  around it binds: a parameter `Cache` hides the imported class in its own
  function only. A call through any other object, or a bare call in a
  language with an implicit `this` (Java, C#, C++, Scala, Dart) outside the
  class and without a static import of the method, counts only in a file
  that may hold an object of the class: one that names it, or imports from
  its module, where a factory may build one (`cache = make_cache()` after
  `from pkg.cache import make_cache`). It is uncertain: `d.get(key)` may be
  a dict's. A read without a call counts only in Python, JavaScript and
  TypeScript, where a method is an attribute; in Rust, Java or C++
  `self.name` is the field of the name;
- for anything else (a class, an interface, a type), every occurrence.

A Python `.pyi` stub of a defining file (`widget.pyi` beside `widget.py`,
`__init__.pyi` beside `__init__.py`, `pkg.pyi` beside the package `pkg/`)
states that module's interface. The indexer skips such a stub (#2445), so
the graph has no site in it, and it is read as the defining file itself: its
declaration of the symbol (`class Widget:`, `def spin` inside it) counts,
and its own module-level `class Widget` does not hide its uses
(`peer: Widget`, `-> Widget`) the way another module's would. The rename
refuses over them, and `--allow-heuristic` rewrites them with the stub's
`__all__`. A `->` reaches a member only in C, C++, PHP and C#; elsewhere it
is a return type or a lambda's arrow, so `-> Widget` in another module's
stub is that module's own `Widget`.

Comments and strings are prose and never count, and neither does a token
that binds the name instead of using it (a parameter, an assignment or loop
target, a definition), a bare use such a binding shadows in its function,
a keyword argument's name (`f(helper=1)`), or a key that labels a property
(`{ helper: 1 }` in JavaScript; a Python dict key, `{MyError: on_error}`, is
evaluated and counts, as does JavaScript shorthand, `{ helper }`). An
occurrence counts as planned when a site of the plan covers it, or the
import statement of one (its own span, not its line:
`from pkg.util import helper; helper(1)` still holds the call), or when the
graph gives it to another symbol of the same name: that symbol's
definition, sites and import statements, and every bare use where an import
binds the name to it (the whole file at module level, only the function
around an import written inside one). Files whose sites the graph gives to a project
whose name extends this one are left out, as the plan leaves them.
Whatever is left is `unplanned`: the rename refuses and lists each one, the
way it refuses a guessed site. `--allow-heuristic` (`allow_heuristic: true`)
rewrites the certain ones as guessed sites, and the report lists them in
`unplanned`. An uncertain one is listed with resolution `receiver_unknown`
and refuses the rename even under `--allow-heuristic`: rewriting it could
rename another type's method, which no postcondition would notice, so it is
left to be checked by hand.

## Postcondition contract

An applied rename is measured through the [structural
delta](structural-delta.md) and held to its
[contract](postcondition-contract.md): the symbol set and call-site count
must be unchanged apart from the renamed hierarchy, no caller may be left
dangling, no site resolved by guesswork may have been rewritten without
`--allow-heuristic`, and no duplicate group or import cycle may appear.
The contract is measured only when the caller supplies `reingest` (the CLI and the MCP tool always do; a programmatic caller that omits it gets an applied rename with no verdict). A failing contract undoes this rename's own transaction (refusing if a later edit was recorded on top of it), re-ingests the restored files
and reports the reasons in `message`; `verdict.affected_tests` lists the
tests to run after a rename that passed.

A failed contract always reports `applied: false`, exits the CLI with code 1,
and skips `after_apply`. The separate `undone` field is `true` when the
rename was reversed, `false` when rollback was refused or restoration could
not be confirmed, and `null` when no contract rollback was needed. With
`undone: false`, files may still contain the rename or later edits; inspect
the working tree and the failure message before proceeding. A completed
rollback whose re-ingest failed reports `undone: true` and
`graph_incomplete: true`, so the graph needs rebuilding even though the
files were restored.

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
  "undone": null,
  "transaction_id": "...",
  "files": ["pkg/__init__.py", "pkg/app.py", "pkg/util.py"],
  "sites": [{"kind": "call", "path": "pkg/app.py", "line": 4, "col": 11, "owner": "myproj.pkg.app.run", "resolution": "exact"}],
  "ambiguous": [],
  "unplanned": [],
  "unlocatable": [],
  "doc_mentions": ["README.md:12"],
  "hierarchy": ["myproj.pkg.util.helper"],
  "diff": "...",
  "message": "3 file(s) written"
}
```

`sites` and `ambiguous` are the located sites, `unplanned` the occurrences
the graph had no site for (rewritten only under `--allow-heuristic`, and
also listed in `sites`), `hierarchy` the definitions renamed together, and
`diff` the unified diff of what was (or, on `--dry-run`, would be) written.
