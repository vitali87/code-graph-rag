---
description: "Find functions and methods that are unreachable from any entry point using the code knowledge graph."
---

# Dead Code Detection

`cgr dead-code` reports functions and methods that are **unreachable** from any
entry point in the knowledge graph. It walks the `CALLS` and `REFERENCES` edges
outward from a set of roots (exported and public symbols, tests, decorated
handlers such as routes/tasks/CLI commands, and dunder/lifecycle methods) and
lists everything the walk never reaches.

The results are **candidates for review, not a guaranteed delete list**. Code
reached only through dynamic dispatch, reflection, string-keyed lookups, or an
external framework that the static graph cannot see may still be reported. Read
each candidate before removing it.

## Prerequisites

Index the repository first, so the graph exists in Memgraph:

```bash
cgr daemon up
cgr start --repo-path /path/to/your/repo --update-graph --clean
```

## Basic Usage

```bash
cgr dead-code
```

If a single project is indexed it is used automatically. When several are
indexed, name one:

```bash
cgr dead-code --project-name my-project
```

## Declaring Entry Points

A function invoked only by a framework or an external caller has no visible
call site, so it looks unreachable. Mark such roots so the code they reach is
not reported:

```bash
# Treat any symbol whose qualified name ends with these as reachable roots
cgr dead-code -e main -e cli.run -e handlers.webhook

# Treat symbols carrying a decorator as roots (extends the built-in set:
# route, task, fixture, command, ...)
cgr dead-code --decorator-root celery_app.task --decorator-root my_registry.register
```

## Excluding Generated Code

Generated or vendored code (API clients, protobuf stubs) is full of callbacks a
library invokes and reports noisily. Exclude it by file-path glob:

```bash
cgr dead-code --exclude '*client/core*' --exclude '*.gen.*'
```

## Options

| Option | Description |
|--------|-------------|
| `--project-name`, `-n` | Project to scan. Defaults to the sole indexed project. |
| `--entry-point`, `-e` | Treat symbols whose qualified name ends with this value as reachable roots. Repeatable. |
| `--decorator-root` | Treat symbols carrying this decorator as roots. Extends the built-in set. Repeatable. |
| `--exclude` | Glob matched against a symbol's file path to exclude from the report. Repeatable. |
| `--include-tests` / `--no-include-tests` | Treat test code as reachable roots so the production code it exercises is not reported. On by default. |
| `--classes` / `--no-classes` | Also report unreachable classes. Off by default. |
| `--format` | Output format: `table` (default) or `json`. |
| `--output`, `-o` | Write the report to this file instead of stdout. |
| `--fail-on-found` | Exit with code 1 when any candidate is found (useful in CI). |

## Use in CI

Fail a build when new unreachable code appears, writing a JSON report for the
job artifacts:

```bash
cgr dead-code --format json --output dead-code.json --fail-on-found \
  --exclude '*_generated*'
```

## How It Works

1. **Roots**: exported/public symbols, tests (unless `--no-include-tests`),
   decorated handlers, dunder/lifecycle methods, plus any `--entry-point` and
   `--decorator-root` you add.
2. **Reachability**: a breadth-first walk over `CALLS` and `REFERENCES` edges
   from every root.
3. **Report**: functions and methods (and, with `--classes`, classes) the walk
   never reaches, minus anything matching an `--exclude` glob.

First-class functions matter for accuracy: a callback stored in an object, an
inline arrow handed to `useMutation`/`.forEach`/`new Promise`, or a function
passed as an argument is recorded as a `REFERENCES` edge so it stays reachable
rather than being reported as dead.
