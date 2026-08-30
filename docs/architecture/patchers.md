---
description: "How cgr rewrites source without re-emitting it: byte-span patches against the original file, batched with order-independent offsets, re-parsed and format-checked."
---

# Span-Preserving Patchers

A rewrite that re-emits a file from its AST loses comments, whitespace and
style, so every rename becomes a noisy diff. `codebase_rag.editing.Patcher`
(issue #1529) never re-emits: it replaces exact byte spans of the original
file and leaves everything around them byte-for-byte intact.

## Interface

```python
from codebase_rag.editing import Patcher

patcher = Patcher(repo_root)
patcher.replace_span("pkg/a.py", (start_byte, end_byte), "new text")
patcher.replace_identifier_at("pkg/a.py", line=12, col=4, old="helper", new="assist")
results = patcher.apply()            # {rel_path: PatchResult}, nothing written
results = patcher.stage_into(tx)     # or hand the patched files to an EditTransaction
```

- Positions follow the graph's convention: 1-based lines, 0-based byte
  columns (`start_line` / `start_col` on nodes, `line` / `col` on edges).
- Every edit is expressed against the **original** bytes of the file. A
  batch to one file is applied in one pass from the end backwards, so the
  result does not depend on the order the edits were queued in and callers
  never track shifting offsets. Overlapping spans are refused.
- `replace_identifier_at` checks that the bytes at the position are exactly
  `old` and, when a tree-sitter grammar exists for the file, that the syntax
  tree has a whole identifier node spanning exactly that range. A stale
  location from the graph, a prefix of a longer name, or a string literal
  that happens to contain the name is refused rather than patched.
- `PatchResult` carries the patched bytes, the edit count, `parses` (the
  file re-parsed with its grammar; `None` when there is no grammar, the
  generic byte-span fallback), and for Go and Rust the formatter verdict
  (`gofmt -l`, `rustfmt --check`, run only when installed). Formatter
  drift is reported, never silently rewritten: a rename that changed the
  width of an aligned column is the caller's decision to reformat.
- `stage_into` stages every result that still parses into an
  `EditTransaction`; a file that no longer parses is left out of the batch
  and reported through its result message.
- `Patcher(repo_root, overlay={...})` reads base content from an overlay
  first, so a second batch can build on a transaction's staged content.

## Languages

Tree-sitter grammars cover Python, TypeScript/JavaScript, Go, Java, Rust,
C#, C/C++, PHP, Lua, Scala and Dart for the identifier check and the
post-patch parse; Go and Rust add the formatter check. Files with no grammar
take the generic byte-span path: bytes are verified, the tree is not.
