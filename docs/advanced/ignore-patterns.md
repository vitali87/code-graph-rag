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
mtimes, the exclusion stamp, the parser fingerprint and pending-work markers),
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
own lock for as long as it exists. Delete it once no older cgr runs against
the checkout.

![cgr stats on pallets/flask, then a .cgrignore excluding tests and examples, a cgr start --update-graph sync, and cgr stats showing the smaller graph](../assets/demos/ignore-patterns.gif)

*Recorded on pallets/flask.*

## Default Exclusions

Code-Graph-RAG automatically excludes common non-source directories such as `.git`, `node_modules`, `__pycache__`, `dist`, `build`, and similar.

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
