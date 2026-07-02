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
- Lines starting with `!` un-ignore matching paths that a **default** exclusion would skip (explicit excludes always win)
- Lines starting with `#` are comments; blank lines are ignored
- Patterns from `.cgrignore` are merged with `--exclude` flags (which use the same syntax) and auto-detected directories

## Default Exclusions

Code-Graph-RAG automatically excludes common non-source directories such as `.git`, `node_modules`, `__pycache__`, `dist`, `build`, and similar.
