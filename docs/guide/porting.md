---
description: "Check a port between languages: pair the functions of an old and a new implementation and compare their calls and arity in the graph."
---

# Porting Between Languages

Every language lands in the same graph schema: a Python function and a Rust
function are both callables with `CALLS` edges and declared positional
parameters. Two implementations of the same code, in two languages, therefore
produce graphs that can be compared. `cgr port check` pairs the functions and
methods of an old implementation with those of a new one and judges each pair
from the graph, with no LLM in the path: did the port keep the calls the
original made, and does it take the same parameters?

It answers for a port in progress (which functions are still missing), for a
finished one (which twins drifted apart), and for twin implementations kept
side by side, such as a Python reference with a Rust fast path. The wider plan
(side effects, data flows, endpoints, behaviour from traces, and an agent loop
that ports one unit at a time) is tracked in
[epic #3413](https://github.com/vitali87/code-graph-rag/issues/3413).

## Checking a port

Index the repository holding both implementations, then name each one by its
path relative to the repository root:

```bash
cgr start --repo-path . --update-graph
cgr port check src/legacy src/rewrite
```

When the new implementation lives in another repository, index both and name
the second project with `--new-project` (each scope is then relative to its
own project's root):

```bash
cgr port check src src --project legacy__1a2b3c4d --new-project rewrite__5e6f7a8b
```

Take a Python module and its Rust port:

```python
# py/geometry.py
def area(width, height):
    return scale(width) * height


def perimeter(width, height):
    return 2 * (width + height)


class Shape:
    def describe(self, prefix):
        return prefix + label()
```

```rust
// rs/geometry.rs
pub fn area(width: i64, height: i64) -> i64 { scale(width) * height }

pub fn perimeter(width: i64) -> i64 { 4 * width }

impl Shape {
    pub fn describe(&self, prefix: &str) -> String { format!("{}!", prefix) }
}
```

`cgr port check py rs` prints a JSON report. Its summary:

```json
{
  "ambiguous": 0,
  "differs": 2,
  "match": 3,
  "new_only": 1,
  "old_only": 1,
  "paired_by": {"container": 0, "explicit": 0, "module": 5, "name": 0},
  "pairs": 5,
  "unknown": 0
}
```

`area` matches. `perimeter` differs on arity (two parameters against one).
`Shape.describe` differs on its calls: the Python version calls `label` and
the Rust one does not.

```json
{
  "arity": {"new": 1, "old": 1, "open": false, "verdict": "MATCH"},
  "calls": {
    "extra": [],
    "missing": [
      {
        "new": "geo__e4d7ab2f.rs.geometry.label",
        "old": "geo__e4d7ab2f.py.geometry.label",
        "resolution": "exact"
      }
    ],
    "unpaired_new": [],
    "unpaired_old": [],
    "verdict": "DIFFERS"
  },
  "new": {
    "path": "rs/geometry.rs",
    "qualified_name": "geo__e4d7ab2f.rs.geometry.Shape.describe",
    "start_line": 24
  },
  "old": {
    "path": "py/geometry.py",
    "qualified_name": "geo__e4d7ab2f.py.geometry.Shape.describe",
    "start_line": 22
  },
  "paired_by": "module",
  "verdict": "DIFFERS"
}
```

## How definitions are paired

Names are compared after folding case convention and surrounding
underscores, so `_read_number`, `readNumber` and `ReadNumber` agree. Each
function or method of the old scope is then paired, strongest evidence
first:

| `paired_by` | Meaning |
|-------------|---------|
| `explicit` | A `--pair OLD=NEW` you gave. Each side is a qualified name or a dotted suffix that names exactly one definition of its scope. |
| `module` | The same name at the same module path relative to each scope, and in the same class for a method: `legacy/a/utils.py` pairs with `rewrite/a/utils.rs`, not with `rewrite/b/utils.rs`. A package entry file such as `__init__.py`, `mod.rs`, `lib.rs` or `index.ts` stands for its directory. |
| `container` | The same name in the same container when the two layouts differ: the enclosing class for a method, the module file's stem for a function, so `lexer.py` still pairs with `cypher/lexer.rs`. |
| `name` | The same name, the only one of its name left on each side, in different containers. Worth a look: a generic name such as `get` can pair two unrelated functions. |

A name that still matches several definitions on both sides is never guessed:
it is listed under `ambiguous` with its candidates, for `--pair` to settle.
Definitions with no counterpart are listed under `old_only` and `new_only`.
Test functions are left out, classified as `cgr graph tests-reaching`
classifies them; `--include-tests` pairs them too.

## What each pair is judged on

Every dimension answers `MATCH`, `DIFFERS` or `UNKNOWN`, and the pair's verdict
is the worst of them. `UNKNOWN` is what the graph cannot settle, and is never
reported as `MATCH`.

**Calls.** The first-party calls each side makes to callables inside its own
scope, mapped through the pairs: `missing` lists calls the old side makes and
the new side does not, `extra` the reverse. A difference that rests only on
`heuristic` or `overload` edges is `UNKNOWN`, since the other side may have
bound the same call exactly. A callee with no twin on the other side is listed
under `unpaired_old` or `unpaired_new` but is not a difference by itself: a
helper the original keeps and the port inlines shows up there. Calls to the
standard library or to code outside the scopes are not compared.

**Arity.** The fixed positional parameters, with receivers left out (Python
`self` and `cls`, Rust `self`, a C# extension method's `this` parameter).
`open` is true when a side declares parameters past them: a rest parameter,
or a Python `*args`, keyword-only or `**kwargs` one. The count alone cannot
compare those, so the verdict is `UNKNOWN`. Positional parameters are recorded
for Python, JavaScript, TypeScript, Go, Rust, PHP, Java and C#; a pair with a
side in another language is `UNKNOWN` on arity.

## In CI

`--fail-on-found` exits with status 1 when any pair `DIFFERS`. Pairs that are
`UNKNOWN`, and definitions that are unpaired or ambiguous, do not fail the
check, so a port in progress can be gated on the pairs it already has:

```bash
cgr port check src/legacy src/rewrite --fail-on-found
```

| Status | Meaning |
|--------|---------|
| `0` | The JSON report. |
| `1` | With `--fail-on-found`, a pair `DIFFERS`. |
| `2` | The two scopes overlap in one project, or a `--pair` is malformed or pairs a definition twice. |
| `3` | A project is not indexed. |
| `4` | A scope holds no function or method, or a `--pair` names none or several. |
