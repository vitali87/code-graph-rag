---
description: "Configure .cgrignore to exclude directories from Code-Graph-RAG analysis."
---

# Ignore Patterns

You can specify additional directories to exclude from analysis by creating a `.cgrignore` file in your repository root.

## Format

```
# Comments start with #
vendor
.custom_cache
my_build_output
```

## Rules

- One directory name per line
- Lines starting with `#` are comments
- Blank lines are ignored
- Patterns are exact directory name matches (not globs)
- Patterns from `.cgrignore` are merged with `--exclude` flags and auto-detected directories

## Default Exclusions

Code-Graph-RAG automatically excludes common non-source directories such as `.git`, `node_modules`, `__pycache__`, `dist`, `build`, and similar.
