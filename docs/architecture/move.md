---
description: "How cgr moves a definition to another module: cut with its imports, paste, rewrite every importer, keep the old path working on request, and refuse an import cycle before touching a file."
---

# Move

`move` is edit-algebra operation 3 (issue #1534). Moving a symbol out of a
shared dumping ground into the one package that uses it shrinks affected
sets, and by hand it is tedious: the definition, its own imports, every
importer, re-exports. The graph knows the definition's span, the imports its
module binds and every importer's statement
([edge-site properties](graph-schema.md#edge-site-properties)), so the
whole move is one transaction.

```bash
cgr move myproj.pkg.util.helper pkg.core --dry-run
cgr move myproj.pkg.util.helper pkg/core.py --keep-alias
```

The MCP tool `move` takes `qualified_name`, `target_module` (a dotted
module name or a repo-relative path; created when missing), and the
optional `keep_alias`, `dry_run` and `project`.

## What happens

1. **Cut.** The definition's whole lines, plus its decorators (or `export`
   wrapper), and the comment lines immediately above it, leave the old
   module together with the blank lines that separated it from what
   follows. Methods are refused: move the class.
2. **Paste with what it needs.** Of the old module's import statements
   (the graph's `IMPORTS` sites of that module), those binding a name the
   moved text uses are copied to the target, narrowed to that name and
   respelled when the specifier is relative (JS/TS). Names the moved text
   takes from the old module itself are imported from it.
3. **Importers.** Every importer of the symbol is retargeted through the
   [import rewriter](patchers.md#import-rewriting-for-rename-and-move):
   `from pkg.util import helper, other` becomes two statements, one per
   module. Python uses through a module import (`pkg.util.helper(...)`)
   follow too, gaining `import pkg.core`. Importer statements the rewriter
   cannot retarget are listed as `unchanged_importers`.
4. **The old module.** When it still uses the name it imports it back from
   the target. With `keep_alias` it always does, marked as a re-export
   (`# noqa: F401`; `export { helper } from './core'` in JS/TS), so the old
   import path keeps working.

## Cycle refusal

Before any file is touched, the module import graph from the graph store
is simulated with the move applied: the target gains the copied imports
and, when the moved text needs the old module, an edge back to it; the old
module gains an edge to the target when it still uses the name or keeps an
alias; every importer of the old module gains the target. A strongly
connected component containing the old or new module that did not exist
before is a new cycle, and the move is refused naming it.

## Atomicity and contract

Edits are [patcher](patchers.md) span edits staged in one
[edit transaction](edit-transactions.md), together with the target file when
it is new; a file that no longer parses rolls everything back. Applied moves
are held to the [postcondition contract](postcondition-contract.md) for a
move: the symbol reported moved to its new home (the structural delta pairs
a removed and an added definition across files by name and fingerprint),
importers updated with no caller left dangling, call-site count unchanged,
no new import cycle, no new duplicate. A failing contract undoes the
transaction and re-ingests the restored files.
