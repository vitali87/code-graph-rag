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
in the repository root, alongside the hash cache. An index built before that
file existed has no recorded set, so the first run after upgrading re-runs once
to establish it and logs why.

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
