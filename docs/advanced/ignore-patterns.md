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
- Lines starting with `!` un-ignore matching paths that a **default** exclusion would skip (explicit excludes always win; the name-ending rule under [Default Exclusions](#default-exclusions) cannot be un-ignored)
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
in the repository root, alongside the hash cache. An index built before that
file existed has no recorded set, so the first run after upgrading re-runs once
to establish it and logs why.

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

```
!docs/js/jquery.min.js
```

A directory-level `!` is not enough, which keeps the default intact for the
common case: a repository that ships generated API documentation gets none of
its vendored JavaScript unless it names each file it actually wants.

## Scope

Only the **repository root's** `.cgrignore` and `.gitignore` are read. Ignore files in subdirectories are not, and that includes a Git submodule's own `.gitignore` -- its files are indexed as part of the parent project unless the parent excludes them. See [Git Submodules](git-submodules.md).
