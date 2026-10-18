---
description: "Configure .cgrignore to exclude files and directories from Code-Graph-RAG analysis using gitignore-style patterns."
---

# Ignore Patterns

You can specify additional files and directories to exclude from analysis by creating a `.cgrignore` file in your repository root. Patterns follow `.gitignore` conventions.

## Format

```
# Comments start with #
vendor
*.gen.ts
docs/*.md
/generated
fixtures/**
!bin/keep.py
```

## Rules

- Patterns follow [gitignore](https://git-scm.com/docs/gitignore) syntax: `*` matches within a path segment, `**` crosses segments, `?` matches a single character
- A bare name (`vendor`) matches a file or directory with that name at any depth
- A pattern containing a slash (`docs/*.md`, `/generated`) is anchored to the repository root
- A trailing slash (`build/`) matches directories only
- Lines starting with `!` un-ignore matching paths that a **default** exclusion would skip (explicit excludes always win; the name-ending rule under [Default Exclusions](#default-exclusions) is un-ignorable only for `.min.js` and `.min.css`, and only by a `!` line naming the file)
- Lines starting with `#` are comments; blank lines are ignored
- Patterns from `.cgrignore` are merged with `--exclude` flags (which use the same syntax) and auto-detected directories

## Changing the Exclusion Set

The exclusion set is part of what an index is built against, so changing it is
treated as a change to the repository. Passing a different set of `--exclude`
flags, editing the patterns in `.cgrignore` or `.gitignore` (including `!`
negations), or changing the unignore choices made in interactive setup all
re-run the sync even when no file on disk has changed: newly excluded files
have their `Module`, `Class` and `Method` nodes removed from the graph, and
newly included ones are indexed.

The set each index was built under is recorded in `.cgr-exclusion-state.json`
alongside the hash cache, in the checkout's state directory (see
[cgr's own state](#cgrs-own-state)). An index built before that file existed
has no recorded set, so the first run after upgrading re-runs once to
establish it and logs why.

## cgr's own state

A sync keeps what it needs to stay incremental (the hash cache, directory
mtimes, the exclusion stamp, the parser fingerprint and pending-work markers)
and its [sync lock](../guide/realtime-updates.md#one-sync-of-a-checkout-at-a-time),
and `cgr rename` / `cgr edits` keep their undo history and lock, in one
directory per checkout under `CGR_HOME` (default `~/.cgr`):

```
~/.cgr/state/<directory-name>__<path-hash>/
```

The name is the checkout's default project name, derived from its absolute
path, so two clones of one repository never share state, and a checkout
indexed under several `--project-name`s shares one. Nothing is written into
the repository, so `git status` stays clean after a sync and a read-only
checkout can still be synced incrementally. Point `CGR_HOME` at another
directory to keep the state elsewhere, for example on a CI cache volume.

Versions before this change wrote these files, all named `.cgr-*`, into the
repository root. The next sync moves any that are still there into the state
directory, keeping the hash cache and its timestamp, so it stays incremental
and reports "already in sync" when nothing changed. A file the state
directory already holds is never replaced by the older copy in the tree. If
they were committed, `git status` then shows them as deleted; commit the
deletion, and remove any `.cgr-*` lines added to `.gitignore` for them if you
like. `cgr check` never counts these files as edits; a source file that only
starts with `.cgr-` is checked like any other.

The one exception is the edit lock, `.cgr-edit-lock`: an older cgr still
running (an MCP server started before the upgrade, say) keeps taking it at
the repository root, so it is left there, and edits take it as well as their
own lock for as long as it exists. If it is there but cannot be opened,
edits stop with a message naming it rather than run beside that cgr. Delete
it once no older cgr runs against the checkout. Such an older cgr also goes
on recording its edits in a new `.cgr-edit-history.json` at the root; the
next sync or edit adds those entries to the history in the state directory
rather than dropping them, so `cgr edits undo` still reverses them. An entry
already taken from the tree, or that has left the history since (undone, or
aged out of the last 50), is never added again. While the entries cannot be
added (the state directory refuses the write, say), edits of the checkout
stop with a message saying why, so that undo keeps their order.

![cgr stats on pallets/flask, then a .cgrignore excluding tests and examples, a cgr start --update-graph sync, and cgr stats showing the smaller graph](../assets/demos/ignore-patterns.gif)

*Recorded on pallets/flask.*

## Default Exclusions

Code-Graph-RAG automatically excludes common non-source directories. A name
matches a directory at any depth. Cargo's `src/bin/` is always indexed.

Some of these names hold first-party, committed source as often as build
output: Dart's `bin/main.dart` entry point, npm and gem executables, a Go
package called `out`, a JavaScript `env` module. The files git tracks under a
directory with one of those names are indexed, and the run log names each
directory they are in. Everything else under the same name is still skipped:
an untracked `bin/generated.js` beside a tracked `bin/main.dart`, a
`.gitignore`d build folder, an untracked `tools/bin/`, and a tracked file that
also sits under another excluded directory (`vendor/bin/lib.js` is still
vendored code). A tracked path whose name holds pattern characters (`*`, `?`,
`[`, `]`, `!`, `\`) is not rescued either. An explicit exclude (`.cgrignore`
or `--exclude`) still wins.

The compiler frontends (go/types, javac, Roslyn) see the rescued files too, so
their calls keep the compiler's binding. javac is handed the rescued files
themselves, so an untracked source under another directory of the same name
never enters its compilation. The real-time watcher re-reads these rules when
`.cgrignore`, `.gitignore` or the git index changes, so a `git mv`, `git add`
or `git rm` under one of these names, or an edited ignore file, applies without
a restart.

<!-- SECTION:default_exclusions -->
| Directory name | Excluded |
|---|---|
| `.cache` | always |
| `.claude` | always |
| `.cxx` | always |
| `.dart_tool` | always |
| `.eclipse` | always |
| `.eggs` | always |
| `.env` | always |
| `.git` | always |
| `.gradle` | always |
| `.hg` | always |
| `.idea` | always |
| `.ipynb_checkpoints` | always |
| `.maven` | always |
| `.mypy_cache` | always |
| `.nox` | always |
| `.npm` | always |
| `.nyc_output` | always |
| `.pnpm-store` | always |
| `.pytest_cache` | always |
| `.qdrant_code_embeddings` | always |
| `.ruff_cache` | always |
| `.svn` | always |
| `.tmp` | always |
| `.tox` | always |
| `.venv` | always |
| `.vs` | always |
| `.vscode` | always |
| `.yarn` | always |
| `__pycache__` | always |
| `bin` | except the files git tracks in it |
| `bower_components` | always |
| `build` | always |
| `coverage` | except the files git tracks in it |
| `dist` | always |
| `env` | except the files git tracks in it |
| `htmlcov` | always |
| `node_modules` | always |
| `obj` | except the files git tracks in it |
| `out` | except the files git tracks in it |
| `Pods` | always |
| `site-packages` | always |
| `target` | except the files git tracks in it |
| `temp` | except the files git tracks in it |
| `tmp` | except the files git tracks in it |
| `vendor` | always |
| `venv` | always |
<!-- /SECTION:default_exclusions -->

Individual files are also skipped by how their **name ends**, covering build
output and editor leftovers (`.pyc`, `.pyo`, `.o`, `.a`, `.so`, `.dll`,
`.class`, `.tmp`, `~`) as well as minified bundles (`.min.js`, `.min.css`).
Minified bundles matter more than their count suggests: a project that commits
generated API documentation (jazzy, YARD, JSDoc, Sphinx) ships a vendored
jQuery or Lunr with it, and those files can contribute more functions than the
project's own source, under names the minifier chose (`v`, `y`, `ce`).

Only the exact ending is matched, so `app.min.js` is skipped while `admin.js`
and `min.js` are indexed normally. A non-minified vendored file (say
`docs/js/typeahead.jquery.js`) is not covered by this rule; exclude it with a
`.cgrignore` pattern naming the file, or `docs/**/js/**` for a whole vendored
directory. Prefer either to a blanket `docs/**`, which also drops the
first-party Markdown under `docs/` that the document tier indexes on purpose.

Unlike the directory exclusions above, un-ignoring the **directory** a
generated file sits in does not bring it back: `!build/` does not resurrect
`build/out.pyc` or `build/js/jquery.min.js`.

How to override it depends on which kind of file it is:

| Ending | Overridable? |
|---|---|
| `.pyc`, `.pyo`, `.o`, `.a`, `.so`, `.dll`, `.class`, `.tmp`, `~` | No. Compiled output and editor droppings are not source in any configuration. |
| `.min.js`, `.min.css` | Yes, with a `!` line naming the **file exactly**. |

So a bundle you maintain, or a third-party one you want to ask questions about,
can be indexed deliberately:

```text
!docs/js/jquery.min.js
```

A directory-level `!` is not enough, which keeps the default intact for the
common case: a repository that ships generated API documentation gets none of
its vendored JavaScript unless it names each file it actually wants.

The rescuing line must name the file literally: a `!` pattern containing `*`,
`?` or `[` does not rescue a bundle, so `!docs/js/*.min.js` and `!*.min.js`
have no effect and each file needs its own line. Everywhere else in this file
`!` patterns take globs as usual; this one rule is the exception, because a
glob is how you write "this whole subtree", which is exactly the
directory-level intent the default is protecting.

## Scope

Only the **repository root's** `.cgrignore` and `.gitignore` are read. Ignore files in subdirectories are not, and that includes a Git submodule's own `.gitignore` -- its files are indexed as part of the parent project unless the parent excludes them. See [Git Submodules](git-submodules.md).

## Symbolic Links

Indexing never follows a symbolic link, whether it names a file or a
directory and wherever it points:

| Link in the repository | What indexing does |
|---|---|
| `pkg/linked.py -> ../../outside/private.py` (target outside the repository) | Skipped. The target is never read, so nothing from outside `--repo-path` reaches the shared graph or a `cgr export`. |
| `pkg/linkdir -> ../../outside/lib` (directory outside the repository) | Skipped, and nothing under it is walked. |
| `pkg/alias.py -> core.py` (target inside the repository) | Skipped. `pkg/core.py` is indexed once, under its own path, so there is no second module duplicating its definitions. |
| `app/vendored -> ../pkg` (directory inside the repository) | Skipped. `pkg/` is indexed under its own path, and `app/vendored` gets no `Package` or `Folder` node. |

A dangling link is skipped the same way. The same rule applies to the
incremental sync, the real-time watcher, the interactive setup's list of
directories to keep, contract discovery, and the structural search and replace
tools. A link that an ignore rule already excludes is excluded as before. When
an indexed file is replaced by a link, the watcher removes the file's nodes and
does not index the link.

Each skipped link is logged at DEBUG with its target (run with
`LOGURU_LEVEL=DEBUG` to see them), and every sync logs one INFO line counting
them:

```text
Skipping symlink pkg/linked.py: its target /home/me/outside/private.py is outside the repository and is never read
Skipping symlink pkg/alias.py: its target pkg/core.py is in the repository and is indexed only under its own path
Skipped 4 symlink(s): links are not followed, ...
```

The repository root itself may be a link, or sit under one: only the entries
inside it are judged. A file named explicitly (a single-file sync, or the MCP
`reingest` tool) is resolved instead: naming `pkg/alias.py` re-indexes
`pkg/core.py`, and a link out of the repository is refused.

A graph built before this rule loses the links' modules, and the `Package`
nodes of linked directories, on its next sync. A link to a plain directory was
merged into its target's `Folder` node, so that `Folder` keeps one extra
containing folder until the project is rebuilt (`cgr delete-project`, then
sync again). The `Package` of a link out of the repository whose target sits
at the link's own relative path under another root (`pkg -> ../other/pkg`)
stays as well: it is shaped like the node another checkout indexed under the
same project name holds for its own directory, and the sync cannot tell the
two apart.

Shared sources that a repository reaches through a link are not indexed. Index
them as a project of their own (see [Multi-Project](../guide/multi-project.md)),
or replace the link with a copy or a [Git submodule](git-submodules.md).
