---
description: "How Code-Graph-RAG indexes repositories containing Git submodules, and how to exclude or scope them."
---

# Git Submodules

Code-Graph-RAG has no special handling for Git submodules. A submodule is an
ordinary directory on disk, so its files are walked and indexed exactly like
the rest of the repository.

That is usually not what you want, and this page describes what actually
happens so you can decide.

## Submodule files become part of the parent project

Indexing a repository whose `sub/` directory is a submodule produces module
nodes for the submodule's files, qualified under the **parent** project:

```text
MODULE nodes: testproj.parentmod
              testproj.sub.childmod
```

There is no separate project, and nothing marks those nodes as belonging to
another repository. Calls, imports and inheritance across the boundary
resolve as if the submodule's code were first-party.

The consequence to be aware of: dead-code and duplicate reports cover
submodule code too, and a `1.0` precision figure measured over such a
repository was measured over vendored code as well as your own.

## A submodule's own `.gitignore` is **not** consulted

Only the **repository root's** `.gitignore` and `.cgrignore` are read. A
`.gitignore` inside the submodule has no effect:

```text
sub/.gitignore contains "artifacts/"
walk sees: parentmod.py, sub/artifacts/gen.py, sub/childmod.py
```

`sub/artifacts/gen.py` is indexed despite the submodule excluding it. Build
output and generated code inside a submodule therefore reach the graph
unless the parent excludes them.

Note this is separate from the built-in exclusions. Names like `build`,
`dist` and `node_modules` are skipped everywhere, including inside
submodules, because they are default exclusions rather than anything read
from a `.gitignore`. It is the submodule-specific rules that are lost.

## Excluding a submodule

Put the rule in the **parent** repository's `.cgrignore`. Root-level rules do
reach into submodule directories:

```gitignore
# .cgrignore in the parent repository root
sub/
```

To keep the submodule but drop its generated output, name the path from the
parent root:

```gitignore
sub/artifacts/
```

Both work because the walk treats the submodule as an ordinary subdirectory,
which is the same reason its own ignore file is skipped.

## Indexing a submodule as its own project

If you want the submodule analysed separately, index it as its own
repository, pointing at the submodule directory. It is a working tree with
its own history, so the usual commands apply, and its root `.gitignore` is
then honoured because it is the root.

For querying both together, see [Multi-Project](../guide/multi-project.md).

## Summary

| Question | Answer |
|---|---|
| Are submodule files indexed? | Yes, as part of the parent project |
| Are they marked as external? | No |
| Is the submodule's `.gitignore` read? | No, only the repository root's |
| Do default exclusions apply inside it? | Yes |
| Do parent-root `.cgrignore` rules apply inside it? | Yes |
| Is `.gitmodules` parsed? | No |
