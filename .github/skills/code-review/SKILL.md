---
name: code-review
description: Review pull requests to code-graph-rag, a multi-language code graph parser. Focuses on graph-edge correctness in language parsers, cross-language consistency, and test fixtures that actually exercise the fix.
---

# Reviewing code-graph-rag

This repository parses source in many languages with tree-sitter and ingests the
result into a code graph: nodes for modules, classes and functions, edges such as
`CALLS`, `INSTANTIATES`, `DEFINES` and `IMPORTS`. Most defects that reach `main`
are wrong or missing *edges* for a specific language construct, not crashes.
Weight the review accordingly.

## Highest-value checks

### Graph-edge correctness
- Does a new or changed construct produce every edge it should, and no edge it
  should not? A constructor call is the recurring example: it should record
  `INSTANTIATES` on the class *and* `CALLS` on the constructor.
- Check the qualified name the edge is attached to. Name-mangling bugs recur
  here: duplicate-suffixed classes, verbatim identifiers (`@class` in C#),
  named constructors, generics (`Box<int>()`) and marker-stripping passes have
  each produced an edge pointing at a name that no node has.
- Resolution must respect scope. A local variable shadowing a type means the
  receiver is not a construction; flag resolution that matches on bare name
  without checking what is actually in scope.

### Language coverage and consistency
- A fix in one language's parser usually applies to its siblings. If the change
  touches `dart/`, ask whether `csharp/`, `cpp/`, and the rest share the shape
  and need the same fix — or say explicitly why they do not.
- Grammars are optional at install time. Code and tests must not assume a
  grammar is present; tests for one language should skip cleanly without it.

### Tests
- Every behavioral fix needs a fixture reproducing the exact construct, and the
  fixture must be *valid source in that language* — an invalid fixture can pass
  for the wrong reason. Verify the assertion would fail without the fix.
- Prefer asserting on specific edges between specific qualified names over
  asserting on counts, which pass accidentally.

## Repository conventions
- Python 3.12, `ruff` (line length 88), and `uv` for dependencies. Any
  dependency change must update `uv.lock`; CI fails on drift via `uv lock --check`.
- Comments explain *why*, not what. Match the density of surrounding code.

## Calibration
Report correctness problems: a wrong edge, a missed construct, a fixture that
does not test the fix, a resolution that ignores scope. Do not report style that
`ruff` already enforces, and do not restate what the diff does. If the change is
correct, say so briefly rather than inventing findings.
